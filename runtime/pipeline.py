"""PM-Quant Live Paper Trading Pipeline Driver.

Connects MarketFeed, PaperRunner, and local persistent journals into
an automated continuous 5-minute execution loop.
"""
import os
import sys
import json
import time
import signal
import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from runtime.market_feed import MarketFeed
from runtime.runner import PaperRunner
from runtime.trade_stream import TradeStreamClient
from runtime.ws_feeds import BinanceSpotWSClient, PolymarketBookWSClient

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
        ws_proxy = (self.feed.session.proxies or {}).get("https") or (self.feed.session.proxies or {}).get("http")
        self.trade_stream = TradeStreamClient(record_raw=True, proxy=ws_proxy)
        self.spot_ws = BinanceSpotWSClient(proxy=ws_proxy)
        self.book_ws = PolymarketBookWSClient(proxy=ws_proxy)
        self.feed.attach_ws_clients(spot_ws=self.spot_ws, book_ws=self.book_ws)
        self._bg_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pm_bg")
        self._reconcile_future = None
        self._open_calib_future = None
        self._prewarm_future = None
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
        for client in (self.trade_stream, self.spot_ws, self.book_ws):
            try:
                client.stop()
            except Exception:
                pass
        try:
            self.feed.close()
        except Exception:
            pass
        if getattr(self, "_bg_executor", None) is not None:
            try:
                self._bg_executor.shutdown(wait=False)
            except Exception:
                pass

    def run(self):
        # Line-buffer stdout so `python main.py start > pipeline.log` tails in
        # real time instead of flushing in 8 KB blocks.
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass
        print(f"=== PM-Quant Live Pipeline Started ({config.BATCH_ID}) ===")
        print(f"Log directory: {config.CURRENT_LEDGER_DIR}")
        print("Press Ctrl+C to terminate gracefully.\n")

        self.trade_stream.start()
        self.spot_ws.start()
        self.book_ws.start()

        active_tokens = {}
        last_slug = None
        last_start_t = None
        pending_settlements = []
        if self.runner.summary_file.exists():
            try:
                now_boot = time.time()
                tail_lines = self.runner.summary_file.read_text(encoding="utf-8").splitlines()[-12:]
                for raw_ln in tail_lines:
                    if not raw_ln.strip():
                        continue
                    rec = json.loads(raw_ln)
                    if rec.get("winner_source") != "gamma" and rec.get("slug") and rec.get("start"):
                        if (now_boot - float(rec.get("end", 0.0))) <= 7200.0:
                            o_px = float(rec.get("open_px", 0.0) or 0.0)
                            c_px = float(rec.get("close_px", 0.0) or 0.0)
                            twap_tpl = (
                                (o_px, c_px, rec.get("winner", "Up"))
                                if (rec.get("winner_source") == "twap" and o_px > 0 and c_px > 0)
                                else None
                            )
                            pending_settlements.append({
                                "slug": rec["slug"],
                                "start_t": float(rec["start"]),
                                "open_px": o_px,
                                "close_px": c_px,
                                "twap_prices": twap_tpl,
                                "attempts": 0,
                                "added_at": now_boot,
                            })
            except Exception:
                pass
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
                close_px = 0.0  # only valid for a round settled in this iteration

                # 0. Non-blocking Background Reconciler: Confirm official Gamma/UMA settlement & Chainlink TWAP
                if self._reconcile_future is not None and self._reconcile_future.done():
                    try:
                        rec_results = self._reconcile_future.result()
                        for rec_ev in rec_results:
                            r_slug = rec_ev["slug"]
                            if rec_ev.get("twap_prices"):
                                off_open, off_close, off_winner = rec_ev["twap_prices"]
                                self.runner.reconcile_record(
                                    r_slug, off_open, off_close, off_winner,
                                    winner_source="twap"
                                )
                                print(f"\n[Pipeline] Official Chainlink TWAP confirmed for {r_slug}: ${off_open:,.2f} -> ${off_close:,.2f} ({off_winner})")
                            if rec_ev.get("gamma_winner"):
                                off_open, off_close = rec_ev["gamma_prices"]
                                self.runner.reconcile_record(
                                    r_slug, off_open, off_close, rec_ev["gamma_winner"],
                                    winner_source="gamma"
                                )
                                print(f"\n[Pipeline] Official Gamma/UMA truth confirmed for {r_slug}: {rec_ev['gamma_winner']}")
                    except Exception:
                        pass
                    finally:
                        self._reconcile_future = None
                        pending_settlements = [it for it in pending_settlements if not it.get("done")]

                pending_settlements = [it for it in pending_settlements if not it.get("done")]
                if self._reconcile_future is None and (loop_start - last_reconcile_t >= 15.0) and pending_settlements:
                    last_reconcile_t = loop_start
                    snapshot_items = list(pending_settlements)

                    def _bg_reconcile(items):
                        out_events = []
                        sess = self.feed._get_worker_session()
                        for item in items:
                            if item.get("done"):
                                continue
                            p_slug = item["slug"]
                            p_start = item["start_t"]
                            item["attempts"] = item.get("attempts", 0) + 1
                            ev = {"slug": p_slug}

                            # Query Gamma & TWAP using worker thread session
                            official = None
                            try:
                                r = sess.get(f"https://gamma-api.polymarket.com/events?slug={p_slug}", timeout=(1.5, 2.5))
                                if r.status_code == 200:
                                    d = r.json()
                                    if d and d[0].get("markets"):
                                        m = d[0]["markets"][0]
                                        if m.get("closed") is True or m.get("umaResolutionStatus") == "resolved":
                                            outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
                                            oprices = json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])
                                            payouts = [float(p) for p in oprices]
                                            if len(payouts) == 2 and sorted(payouts) == [0.0, 1.0]:
                                                official = {"slug": p_slug, "winner": outcomes[payouts.index(1.0)], "payouts": payouts, "condition_id": m.get("conditionId"), "verified": True, "resolved_at": time.time()}
                                                self.feed.record_raw_event("settlements", official)
                            except Exception:
                                pass

                            official_poly = item.get("twap_prices")
                            if not official_poly:
                                try:
                                    iso_s = datetime.datetime.fromtimestamp(p_start, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
                                    iso_e = datetime.datetime.fromtimestamp(p_start + 300, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
                                    r2 = sess.get("https://polymarket.com/api/crypto/crypto-price", params={"symbol": "BTC", "eventStartTime": iso_s, "variant": "fiveminute", "endDate": iso_e, "twapEnabled": "true", "twapLookbackSeconds": "60"}, timeout=(1.5, 2.5))
                                    if r2.status_code == 200:
                                        b2 = r2.json()
                                        if b2.get("completed") and b2.get("closePrice") is not None:
                                            o_px = float(b2.get("openPrice", 0))
                                            c_px = float(b2.get("closePrice", 0))
                                            official_poly = (o_px, c_px, "Up" if c_px >= o_px else "Down")
                                except Exception:
                                    pass
                                if official_poly:
                                    item["twap_prices"] = official_poly
                                    ev["twap_prices"] = official_poly

                            if official:
                                off_open = official_poly[0] if official_poly else item.get("open_px", 0.0)
                                off_close = official_poly[1] if official_poly else item.get("close_px", 0.0)
                                ev["gamma_winner"] = official["winner"]
                                ev["gamma_prices"] = (off_open, off_close)
                                item["done"] = True
                            elif item["attempts"] >= 120 or (time.time() - item.get("added_at", time.time())) > 1800:
                                print(f"\n[Pipeline] Reconcile timeout for {p_slug} ({item['attempts']} attempts). Dropping.")
                                item["done"] = True

                            if ev.get("twap_prices") or ev.get("gamma_winner"):
                                out_events.append(ev)
                        return out_events

                    self._reconcile_future = self._bg_executor.submit(_bg_reconcile, snapshot_items)

                # 1. Check Window Rollover / Expiration
                if last_slug and last_slug != slug:
                    print(f"\n[Pipeline] Window {last_slug} ended. Settling...")
                    # Gamma UMA takes 7-12 min on-chain; enqueue for background verification instead of blocking rollover
                    official = None
                    official_winner = None
                    winner_source = "spot_fallback"

                    official_poly = self.feed.fetch_window_settlement_price(last_start_t) if last_start_t else None
                    if official_poly:
                        official_open, official_close, poly_winner = official_poly
                        self.runner.open_price = official_open
                        close_px = official_close
                        official_winner = poly_winner
                        winner_source = "twap"
                    else:
                        close_spot = self.feed.fetch_spot_state(self.runner.open_price, loop_start)
                        close_px = (
                            getattr(self.runner, "last_spot_price", 0.0)
                            or (close_spot.price if close_spot.price > 0 else self.runner.open_price)
                        )

                    if last_start_t:
                        pending_settlements.append({
                            "slug": last_slug,
                            "start_t": last_start_t,
                            "open_px": self.runner.open_price,
                            "close_px": close_px,
                            "twap_prices": official_poly,
                            "attempts": 0,
                            "added_at": loop_start
                        })
                        if len(pending_settlements) > 20:
                            pending_settlements = pending_settlements[-20:]

                    self.runner.close_window(close_px, official_winner,
                                            winner_source=winner_source)
                    res_tag = f"Official truth: {official_winner}" if official_winner else f"Close px: ${close_px:,.2f}"
                    print(f"[Pipeline] Window {last_slug} settled ({res_tag})! Recorded to summary.jsonl\n")
                    active_tokens = {}

                # 2. Initialize New Window (with Pre-warmed Token Fast Path)
                did_rollover = False
                if last_slug != slug:
                    did_rollover = True
                    print(f"[Pipeline] Starting new window: {slug}")
                    if prewarmed_next.get("slug") == slug and prewarmed_next.get("tokens"):
                        active_tokens = prewarmed_next["tokens"]
                        prewarmed_next = {}
                    else:
                        active_tokens = self.feed.resolve_tokens(slug)

                    poly_open, binance_open = self.feed.fetch_window_open_price(start_t)
                    is_official_open = bool(getattr(self.feed, "last_open_is_official", False))
                    if poly_open <= 0:
                        is_official_open = False
                        if last_slug and close_px > 0:
                            poly_open = close_px
                        else:
                            spot_initial = self.feed.fetch_spot_state(0.0, loop_start)
                            poly_open = spot_initial.price
                    if binance_open <= 0:
                        binance_open = poly_open
                    
                    poly_spot_init = self.feed.get_polymarket_btc_price(loop_start) or poly_open
                    self.runner.start_window(
                        slug, start_t, end_t, poly_open,
                        binance_open_px=binance_open,
                        poly_spot=poly_spot_init,
                        poly_open_provisional=not is_official_open,
                    )
                    # Window rollover HTTP init must not count as a market-feed loop gap
                    self.runner._last_step_time = None
                    self._open_calib_future = None
                    last_slug = slug
                    last_start_t = start_t
                    tokens_label = f"UP={active_tokens.get('token_up', 'Pending')[:10]}..." if active_tokens.get('token_up') else "Pending"
                    print(f"[Pipeline] Tokens: {tokens_label} | Poly Strike S0: ${poly_open:,.2f} ({'official' if is_official_open else 'provisional'}) | Binance S0: ${binance_open:,.2f}")

                    # Update WebSocket subscription targets
                    token_list = [t for t in (active_tokens.get("token_up"), active_tokens.get("token_down")) if t]
                    self.trade_stream.set_active_targets([slug], token_list)
                    self.book_ws.set_tokens(token_list)

                # 2b. Retry token resolution if initially pending
                if not (active_tokens.get("token_up") and active_tokens.get("token_down")):
                    active_tokens = self.feed.resolve_tokens(slug)
                    if active_tokens.get("token_up"):
                        t_retry = [t for t in (active_tokens.get("token_up"), active_tokens.get("token_down")) if t]
                        self.trade_stream.set_active_targets([slug], t_retry)
                        self.book_ws.set_tokens(t_retry)

                if did_rollover or getattr(self.runner, "_last_step_time", None) is None:
                    loop_start = time.time()
                    tau_s = max(0.0, end_t - loop_start)

                # 2c. Non-blocking Dynamic In-Window Open Price Calibration (resolves 5-15s TWAP delay)
                if self._open_calib_future is not None and self._open_calib_future.done():
                    try:
                        calib_slug, official_open = self._open_calib_future.result()
                        if calib_slug == slug and official_open and official_open > 0:
                            self.runner.update_open_price(float(official_open))
                            print(f"\n[Pipeline] Dynamic TWAP open calibrated for {slug}: ${float(official_open):,.2f}")
                    except Exception:
                        pass
                    finally:
                        self._open_calib_future = None

                if getattr(self.runner, "poly_open_is_provisional", False) and self._open_calib_future is None:
                    if loop_start - getattr(self, "_last_open_check_t", 0.0) >= 3.0:
                        self._last_open_check_t = loop_start
                        def _bg_calib(target_slug=slug, target_start=start_t):
                            iso_start = datetime.datetime.fromtimestamp(target_start, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
                            iso_end = datetime.datetime.fromtimestamp(target_start + 300, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
                            sess = self.feed._get_worker_session()
                            try:
                                poly_resp = sess.get(
                                    "https://polymarket.com/api/crypto/crypto-price",
                                    params={
                                        "symbol": "BTC",
                                        "eventStartTime": iso_start,
                                        "variant": "fiveminute",
                                        "endDate": iso_end,
                                        "twapEnabled": "true",
                                        "twapLookbackSeconds": "60"
                                    },
                                    timeout=2.5
                                )
                                if poly_resp.status_code == 200:
                                    body = poly_resp.json()
                                    val = body.get("openPrice")
                                    if val and float(val) > 0:
                                        return target_slug, float(val)
                            except Exception:
                                pass
                            return target_slug, None
                        self._open_calib_future = self._bg_executor.submit(_bg_calib)

                # 2d. Non-blocking Cross-round Smooth Overlap: Pre-warm next 5m window at tau <= 30s
                if self._prewarm_future is not None and self._prewarm_future.done():
                    try:
                        pw_slug, pw_tokens = self._prewarm_future.result()
                        if pw_tokens and pw_tokens.get("token_up") and pw_tokens.get("token_down"):
                            prewarmed_next = {"slug": pw_slug, "tokens": pw_tokens}
                            combined_slugs = [slug, pw_slug]
                            combined_tokens = [
                                active_tokens.get("token_up"), active_tokens.get("token_down"),
                                pw_tokens.get("token_up"), pw_tokens.get("token_down")
                            ]
                            combined_clean = [t for t in combined_tokens if t]
                            self.trade_stream.set_active_targets(combined_slugs, combined_clean)
                            self.book_ws.set_tokens(combined_clean)
                    except Exception:
                        pass
                    finally:
                        self._prewarm_future = None

                if tau_s <= 30.0:
                    next_start_t = end_t
                    next_slug = f"btc-updown-5m-{int(next_start_t)}"
                    if prewarmed_next.get("slug") != next_slug and self._prewarm_future is None:
                        def _bg_prewarm(ns=next_slug):
                            return ns, self.feed.resolve_tokens(ns)
                        self._prewarm_future = self._bg_executor.submit(_bg_prewarm)

                # 3. Step Evaluation Tick
                if active_tokens.get("token_up") and active_tokens.get("token_down"):
                    poly_spot = self.feed.get_polymarket_btc_price(loop_start)
                    spot, b_up, b_dn = self.feed.fetch_tick_bundle(
                        self.runner.open_price,
                        self.runner.binance_open_price,
                        active_tokens["token_up"],
                        active_tokens["token_down"],
                        now=loop_start,
                        poly_spot=poly_spot,
                    )

                    # Drain real-time WebSocket trade prints for physical queue deduction
                    trades = self.trade_stream.drain_trades(slug=slug)

                    def _delayed_book(side_name: str):
                        tok = active_tokens.get("token_up" if side_name == "Up" else "token_down")
                        if not tok:
                            return None
                        ws_bk = self.book_ws.get_order_book(tok, max_age_s=1.5)
                        if ws_bk is not None:
                            return ws_bk
                        return self.feed.fetch_order_book(tok, side_name)

                    net_summary = dict(self.feed.get_net_summary())
                    net_summary["ws_connected"] = bool(self.trade_stream.connected)
                    net_summary["ws_trades"] = int(self.trade_stream.trade_count)
                    net_summary["ws_reconnects"] = int(self.trade_stream.reconnect_count)
                    net_summary["spot_ws_connected"] = bool(self.spot_ws.connected)
                    net_summary["book_ws_connected"] = bool(self.book_ws.connected)
                    net_summary["book_ws_msgs"] = int(self.book_ws.msg_count)
                    if self.trade_stream.last_error:
                        net_summary["ws_last_error"] = self.trade_stream.last_error
                    self.runner.step(
                        loop_start, spot, b_up, b_dn,
                        net_stats=net_summary,
                        trade_prints=trades,
                        poly_spot=poly_spot,
                        delayed_book_fetcher=_delayed_book
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

                # Maintain 0.5-second (2 Hz) high-frequency cadence
                elapsed = time.time() - loop_start
                sleep_s = max(0.05, 0.5 - elapsed)
                time.sleep(sleep_s)

            except KeyboardInterrupt:
                print("\n\n[Pipeline] Termination signal received. Stopping...")
                self.running = False
                break
            except Exception as e:
                print(f"\n[Pipeline] Tick warning: {e}")
                time.sleep(1.0)

        for client in (self.trade_stream, self.spot_ws, self.book_ws):
            try:
                client.stop()
            except Exception:
                pass
        try:
            self.feed.close()
        except Exception:
            pass
        if getattr(self, "_bg_executor", None) is not None:
            try:
                self._bg_executor.shutdown(wait=False)
            except Exception:
                pass
        if last_slug:
            print(
                f"\n[Pipeline] WARNING: window {last_slug} was still open at shutdown and is "
                f"NOT in the journal (positions are dropped, not settled)."
            )
        self._cleanup_pid()
        print("[Pipeline] Shutdown complete.")


def handle_sig(sig, frame):
    driver.stop()


if __name__ == "__main__":
    driver = PipelineDriver()
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)
    driver.run()
