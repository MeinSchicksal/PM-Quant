"""PM-Quant Live Paper Trading Pipeline Driver.

Connects MarketFeed, PaperRunner, and local persistent journals into
an automated continuous 5-minute execution loop.
"""
import os
import sys
import time
import signal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from runtime.market_feed import MarketFeed
from runtime.runner import PaperRunner
from runtime.trade_stream import TradeStreamClient

PID_FILE = config.RUNTIME_DIR / "pipeline.pid"


def is_pid_running(pid: int) -> bool:
    """Check if process with given PID is actively running."""
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # 0x1000 = PROCESS_QUERY_LIMITED_INFORMATION
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            success = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            # 259 = STILL_ACTIVE
            return bool(success and exit_code.value == 259)
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


class PipelineDriver:
    def __init__(self):
        self._check_existing_pipeline()
        self.feed = MarketFeed()
        self.runner = PaperRunner()
        self.trade_stream = TradeStreamClient(record_raw=True)
        self.running = True
        self._write_pid()

    def _check_existing_pipeline(self):
        if PID_FILE.exists():
            try:
                old_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
                if is_pid_running(old_pid):
                    print(f"\n[Pipeline] FATAL: Another pipeline process (PID {old_pid}) is already running!")
                    print("To prevent duplicate orders and ledger corruption, please stop it first.\n")
                    sys.exit(1)
            except (ValueError, OSError):
                pass

    def _write_pid(self):
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    def _cleanup_pid(self):
        if PID_FILE.exists():
            try:
                PID_FILE.unlink()
            except Exception:
                pass

    def stop(self):
        self.running = False
        try:
            self.trade_stream.stop()
        except Exception:
            pass

    def run(self):
        print(f"=== PM-Quant Live Pipeline Started ({config.BATCH_ID}) ===")
        print(f"Log directory: {config.CURRENT_LEDGER_DIR}")
        print("Press Ctrl+C to terminate gracefully.\n")

        self.trade_stream.start()

        active_tokens = {}
        last_slug = None
        last_start_t = None
        pending_settlements = []
        last_reconcile_t = 0.0
        prewarmed_next = {}

        while self.running:
            try:
                loop_start = time.time()
                info = self.feed.get_current_slug_info(loop_start)
                slug = info["slug"]
                start_t = info["start_t"]
                end_t = info["end_t"]
                tau_s = info["tau_s"]

                # 0. Background Reconciler: Ensure delayed Chainlink TWAP replaces spot fallback
                if loop_start - last_reconcile_t >= 5.0 and pending_settlements:
                    last_reconcile_t = loop_start
                    for item in list(pending_settlements):
                        p_slug, p_start = item
                        official_poly = self.feed.fetch_window_settlement_price(p_start)
                        if official_poly:
                            off_open, off_close, off_winner = official_poly
                            self.runner.reconcile_record(p_slug, off_open, off_close, off_winner)
                            pending_settlements.remove(item)
                            print(f"[Pipeline] Official Chainlink TWAP confirmed for {p_slug}: ${off_open:,.2f} -> ${off_close:,.2f} ({off_winner})\n")

                # 1. Check Window Rollover / Expiration
                if last_slug and last_slug != slug:
                    print(f"\n[Pipeline] Window {last_slug} ended. Settling...")
                    # Try official Polymarket TWAP settlement prices first
                    official_poly = self.feed.fetch_window_settlement_price(last_start_t) if last_start_t else None
                    if official_poly:
                        official_open, official_close, poly_winner = official_poly
                        self.runner.open_price = official_open
                        close_px = official_close
                        official_winner = poly_winner
                    else:
                        close_spot = self.feed.fetch_spot_state(self.runner.open_price, loop_start)
                        close_px = close_spot.price if close_spot.price > 0 else getattr(self.runner, "last_spot_price", self.runner.open_price)
                        official = self.feed.verify_official_settlement(last_slug)
                        official_winner = official.get("winner") if official else None
                        if last_start_t:
                            pending_settlements.append((last_slug, last_start_t))

                    self.runner.close_window(close_px, official_winner)
                    res_tag = f"Official truth: {official_winner}" if official_winner else f"Close px: ${close_px:,.2f}"
                    print(f"[Pipeline] Window {last_slug} settled ({res_tag})! Recorded to summary.jsonl\n")
                    active_tokens = {}

                # 2. Initialize New Window (with Pre-warmed Token Fast Path)
                if last_slug != slug:
                    print(f"[Pipeline] Starting new window: {slug}")
                    if prewarmed_next.get("slug") == slug and prewarmed_next.get("tokens"):
                        active_tokens = prewarmed_next["tokens"]
                        prewarmed_next = {}
                    else:
                        active_tokens = self.feed.resolve_tokens(slug)

                    open_px = self.feed.fetch_window_open_price(start_t)
                    if open_px <= 0:
                        # Fallback to current spot
                        spot_initial = self.feed.fetch_spot_state(0.0, loop_start)
                        open_px = spot_initial.price
                    
                    poly_spot_init = self.feed.get_polymarket_btc_price(start_t)
                    self.runner.start_window(slug, start_t, end_t, open_px, poly_spot=poly_spot_init)
                    last_slug = slug
                    last_start_t = start_t
                    tokens_label = f"UP={active_tokens.get('token_up', 'Pending')[:10]}..." if active_tokens.get('token_up') else "Pending"
                    print(f"[Pipeline] Tokens: {tokens_label} | Open Px: ${open_px:,.2f}")

                    # Update WebSocket subscription targets
                    token_list = [active_tokens.get("token_up"), active_tokens.get("token_down")]
                    self.trade_stream.set_active_targets([slug], [t for t in token_list if t])

                # 2b. Retry token resolution if initially pending
                if not (active_tokens.get("token_up") and active_tokens.get("token_down")):
                    active_tokens = self.feed.resolve_tokens(slug)
                    if active_tokens.get("token_up"):
                        self.trade_stream.set_active_targets([slug], [active_tokens.get("token_up"), active_tokens.get("token_down")])

                # 2c. Cross-round Smooth Overlap: Pre-listen and pre-warm next 5m window at tau <= 30s
                if tau_s <= 30.0:
                    next_start_t = end_t
                    next_slug = f"btc-updown-5m-{int(next_start_t)}"
                    if prewarmed_next.get("slug") != next_slug:
                        next_tokens = self.feed.resolve_tokens(next_slug)
                        if next_tokens.get("token_up") and next_tokens.get("token_down"):
                            prewarmed_next = {
                                "slug": next_slug,
                                "tokens": next_tokens
                            }
                            # Register next slug & tokens to trade stream early
                            combined_slugs = [slug, next_slug]
                            combined_tokens = [
                                active_tokens.get("token_up"), active_tokens.get("token_down"),
                                next_tokens.get("token_up"), next_tokens.get("token_down")
                            ]
                            self.trade_stream.set_active_targets(combined_slugs, [t for t in combined_tokens if t])

                # 3. Step Evaluation Tick
                if active_tokens.get("token_up") and active_tokens.get("token_down"):
                    spot = self.feed.fetch_spot_state(self.runner.open_price, loop_start)
                    b_up = self.feed.fetch_order_book(active_tokens["token_up"], "Up")
                    b_dn = self.feed.fetch_order_book(active_tokens["token_down"], "Down")

                    # Drain real-time WebSocket trade prints for physical queue deduction
                    trades = self.trade_stream.drain_trades(slug=slug)

                    poly_spot = self.feed.get_polymarket_btc_price(start_t)

                    self.runner.step(
                        loop_start, spot, b_up, b_dn,
                        net_stats=self.feed.get_net_summary(),
                        trade_prints=trades,
                        poly_spot=poly_spot
                    )

                    # Formatted terminal status line
                    last_dec = self.runner.decisions[-1] if self.runner.decisions else {}
                    act = last_dec.get("action", "hold")
                    side = last_dec.get("side") or ""
                    px_str = f"@{last_dec.get('price'):.2f}" if last_dec.get("price") else ""
                    cb_info = self.runner.circuit_breaker.get_status(loop_start)
                    cb_tag = cb_info["status"]
                    if cb_info.get("cooling_rounds_left"):
                        cb_tag += f"({cb_info['cooling_rounds_left']})"

                    print(
                        f"\r[{time.strftime('%H:%M:%S')}] tau: {tau_s:3.0f}s | "
                        f"BTC: ${spot.price:,.2f} ({spot.return_bps:+5.1f}bps, vol:{spot.realized_vol_bps:.1f}) | "
                        f"Pos: UP {self.runner.pos_up.shares:.0f} / DN {self.runner.pos_down.shares:.0f} | "
                        f"CB: {cb_tag} | "
                        f"Act: {act.upper()} {side} {px_str} "
                        f"{'(' + last_dec.get('reason', '')[:25] + ')' if last_dec.get('reason') else ''}",
                        end="", flush=True
                    )
                else:
                    print(
                        f"\r[{time.strftime('%H:%M:%S')}] tau: {tau_s:4.0f}s | "
                        f"Waiting for Polymarket contract deployment for {slug}...",
                        end="", flush=True
                    )

                # Maintain 1-second cadence
                elapsed = time.time() - loop_start
                sleep_s = max(0.05, 1.0 - elapsed)
                time.sleep(sleep_s)

            except KeyboardInterrupt:
                print("\n\n[Pipeline] Termination signal received. Stopping...")
                self.running = False
                break
            except Exception as e:
                print(f"\n[Pipeline] Tick warning: {e}")
                time.sleep(1.0)

        try:
            self.trade_stream.stop()
        except Exception:
            pass
        self._cleanup_pid()
        print("[Pipeline] Shutdown complete.")


def handle_sig(sig, frame):
    driver.stop()


if __name__ == "__main__":
    driver = PipelineDriver()
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)
    driver.run()
