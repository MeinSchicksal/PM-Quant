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

PID_FILE = config.RUNTIME_DIR / "pipeline.pid"


class PipelineDriver:
    def __init__(self):
        self.feed = MarketFeed()
        self.runner = PaperRunner()
        self.running = True
        self._write_pid()

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

    def run(self):
        print(f"=== PM-Quant Live Pipeline Started ({config.BATCH_ID}) ===")
        print(f"Log directory: {config.CURRENT_LEDGER_DIR}")
        print("Press Ctrl+C to terminate gracefully.\n")

        active_tokens = {}
        last_slug = None

        while self.running:
            try:
                loop_start = time.time()
                info = self.feed.get_current_slug_info(loop_start)
                slug = info["slug"]
                start_t = info["start_t"]
                end_t = info["end_t"]
                tau_s = info["tau_s"]

                # 1. Check Window Rollover / Expiration
                if last_slug and last_slug != slug:
                    print(f"\n[Pipeline] Window {last_slug} ended. Settling...")
                    # Fetch close price with safe fallback
                    close_spot = self.feed.fetch_spot_state(self.runner.open_price, loop_start)
                    close_px = close_spot.price if close_spot.price > 0 else getattr(self.runner, "last_spot_price", self.runner.open_price)
                    self.runner.close_window(close_px)
                    print(f"[Pipeline] Window {last_slug} settled! Recorded to summary.jsonl\n")
                    active_tokens = {}

                # 2. Initialize New Window
                if last_slug != slug:
                    print(f"[Pipeline] Starting new window: {slug}")
                    active_tokens = self.feed.resolve_tokens(slug)
                    open_px = self.feed.fetch_window_open_price(start_t)
                    if open_px <= 0:
                        # Fallback to current spot
                        spot_initial = self.feed.fetch_spot_state(0.0, loop_start)
                        open_px = spot_initial.price
                    
                    self.runner.start_window(slug, start_t, end_t, open_px)
                    last_slug = slug
                    tokens_label = f"UP={active_tokens.get('token_up', 'Pending')[:10]}..." if active_tokens.get('token_up') else "Pending"
                    print(f"[Pipeline] Tokens: {tokens_label} | Open Px: ${open_px:,.2f}")

                # 2b. Retry token resolution if initially pending
                if not (active_tokens.get("token_up") and active_tokens.get("token_down")):
                    active_tokens = self.feed.resolve_tokens(slug)

                # 3. Step Evaluation Tick
                if active_tokens.get("token_up") and active_tokens.get("token_down"):
                    spot = self.feed.fetch_spot_state(self.runner.open_price, loop_start)
                    b_up = self.feed.fetch_order_book(active_tokens["token_up"])
                    b_dn = self.feed.fetch_order_book(active_tokens["token_down"])

                    self.runner.step(loop_start, spot, b_up, b_dn)

                    # Formatted terminal status line
                    last_dec = self.runner.decisions[-1] if self.runner.decisions else {}
                    act = last_dec.get("action", "hold")
                    side = last_dec.get("side") or ""
                    px_str = f"@{last_dec.get('price'):.2f}" if last_dec.get("price") else ""
                    print(
                        f"\r[{time.strftime('%H:%M:%S')}] tau: {tau_s:4.0f}s | "
                        f"BTC: ${spot.price:,.2f} ({spot.return_bps:+5.1f} bps) | "
                        f"Pos: UP {self.runner.pos_up.shares:.0f} / DN {self.runner.pos_down.shares:.0f} | "
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

        self._cleanup_pid()
        print("[Pipeline] Shutdown complete.")


def handle_sig(sig, frame):
    driver.stop()


if __name__ == "__main__":
    driver = PipelineDriver()
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)
    driver.run()
