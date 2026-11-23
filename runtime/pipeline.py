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
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from runtime.alerts import notify as send_alert
from runtime.market_feed import MarketFeed
from runtime.retention import cleanup_old_data
from runtime.runner import PaperRunner
from runtime.trade_stream import TradeStreamClient
from runtime.ws_feeds import BinanceSpotWSClient, PolymarketBookWSClient

PID_FILE = config.RUNTIME_DIR / "pipeline.pid"
LOCK_FILE = config.RUNTIME_DIR / "pipeline.lock"


def is_pid_running(pid: int) -> bool:
    """Check if process with given PID is actively running (guards against Linux PID reuse)."""
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
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                # Owned by another user (e.g. root daemon after host reboot): not our pipeline
                return False
            # Guard against PID reuse on Linux by checking /proc/<pid>/cmdline when available
            cmdline_path = Path(f"/proc/{pid}/cmdline")
            if cmdline_path.exists():
                try:
                    raw_cmd = cmdline_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore").lower()
                    if raw_cmd and ("python" not in raw_cmd and "pm-quant" not in raw_cmd and "pipeline" not in raw_cmd and "main.py" not in raw_cmd):
                        return False
                except OSError:
                    pass
            return True
    except Exception:
        return False


class PipelineDriver:
    def __init__(self):
        self._lock_fh = None
        self._check_existing_pipeline()
        self.feed = MarketFeed()
        self.runner = PaperRunner()
        ws_proxy = (self.feed.session.proxies or {}).get("https") or (self.feed.session.proxies or {}).get("http")
        self._wake_event = threading.Event()
        self.trade_stream = TradeStreamClient(record_raw=True, proxy=ws_proxy, wake_event=self._wake_event)
        self.spot_ws = BinanceSpotWSClient(
            proxy=ws_proxy,
            wake_event=self._wake_event,
            provider=str(config.PARAMS.get("SPOT_WS_PROVIDER", "binance")),
        )
        self.book_ws = PolymarketBookWSClient(proxy=ws_proxy, wake_event=self._wake_event)
        self.feed.attach_ws_clients(spot_ws=self.spot_ws, book_ws=self.book_ws, trade_ws=self.trade_stream)
        # Split slow I/O background jobs (reconcile/disk cleanup) from latency-sensitive
        # jobs (open-price calibration & next-window prewarm) so slow tasks never starve rollover.
        self._bg_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pm_bg_slow")
        self._fast_bg_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pm_bg_fast")
        self._reconcile_future = None
        self._open_calib_future = None
        self._prewarm_future = None
        self._cleanup_future = None
        self._clock_future = None
        self._last_cleanup_mono = 0.0
        self._last_clock_mono = 0.0
        self._resources_closed = False
        self.running = True
        self._write_pid()

    def _check_existing_pipeline(self):
        # On POSIX (Linux/macOS), use a kernel-enforced non-blocking flock so OOM/SIGKILL
        # releases the lock automatically and reboot PID reuse can never wedge startup.
        if sys.platform != "win32":
            try:
                import fcntl
                LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
                fh = open(LOCK_FILE, "a+", encoding="utf-8")
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    fh.close()
                    old_pid = "?"
                    if PID_FILE.exists():
                        try:
                            old_pid = PID_FILE.read_text(encoding="utf-8").strip() or "?"
                        except OSError:
                            pass
                    print(f"\n[Pipeline] FATAL: Another pipeline process (PID {old_pid}) holds {LOCK_FILE.name}!")
                    print("To prevent duplicate orders and ledger corruption, please stop it first.\n")
                    sys.exit(1)
                self._lock_fh = fh
                return
            except ImportError:
                pass
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
        if getattr(self, "_lock_fh", None) is not None:
            try:
                self._lock_fh.close()
            except Exception:
                pass
            self._lock_fh = None

    def _shutdown_resources(self):
        if getattr(self, "_resources_closed", False):
            return
        self._resources_closed = True
        for client in (self.trade_stream, self.spot_ws, self.book_ws):
            try:
                client.stop()
            except Exception:
                pass
        try:
            self.feed.close()
        except Exception:
            pass
        for ex_attr in ("_bg_executor", "_fast_bg_executor"):
            ex = getattr(self, ex_attr, None)
            if ex is not None:
                try:
                    ex.shutdown(wait=False)
                except Exception:
                    pass

    def stop(self):
        """Async-signal-safe stop request: only sets flags and wakes the main loop."""
        self.running = False
        if getattr(self, "_wake_event", None) is not None:
            self._wake_event.set()

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
        is_first_window = True
        pending_settlements = []
        if self.runner.summary_file.exists():
            try:
                now_boot = time.time()
                # O(1) RAM tail read (128KB covers ~250+ recent windows even on a 1-year ledger)
                # and never drop unverified non-gamma windows just because downtime exceeded 6h.
                fsize = self.runner.summary_file.stat().st_size
                with open(self.runner.summary_file, "rb") as sf_in:
                    if fsize > 131072:
                        sf_in.seek(fsize - 131072)
                        sf_in.readline()
                    tail_lines = sf_in.read().decode("utf-8", errors="ignore").splitlines()[-200:]
                for raw_ln in tail_lines:
                    if not raw_ln.strip():
                        continue
                    try:
                        rec = json.loads(raw_ln)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if rec.get("winner_source") != "gamma" and rec.get("slug") and rec.get("start"):
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
        # Recover & provisionally settle any orphaned window from live.json if restart crossed a 5m boundary
        boot_slug = self.feed.get_current_slug_info(time.time())["slug"]
        orphan = self.runner.settle_orphaned_live(boot_slug)
        if orphan and not any(it.get("slug") == orphan["slug"] for it in pending_settlements):
            pending_settlements.append({
                "slug": orphan["slug"],
                "start_t": orphan["start_t"],
                "open_px": orphan["open_px"],
                "close_px": orphan["close_px"],
                "twap_prices": None,
                "attempts": 0,
                "added_at": time.time(),
            })
            send_alert(
                f"orphan_recover_{orphan['slug']}",
                "跨窗重启自动补结算",
                f"已从 live.json 补结算孤儿窗口 {orphan['slug']} 并加入官方对账队列。",
                level="warn",
                cooldown_s=60.0,
            )

        last_reconcile_mono = 0.0
        last_status_log_mono = 0.0
        last_logged_action_cnt = -1
        is_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
        prewarmed_next = {}
        send_alert("pipeline_boot", "交易管道已启动", f"版本 {config.BATCH_ID} 已上线运行。", level="info", cooldown_s=60.0)

        try:
          while self.running:
            try:
                loop_start = time.time()
                loop_mono = time.monotonic()
                info = self.feed.get_current_slug_info(loop_start)
                slug = info["slug"]
                start_t = info["start_t"]
                end_t = info["end_t"]
                tau_s = info["tau_s"]
                close_px = 0.0  # only valid for a round settled in this iteration

                # Monotonic window guard: never roll backward on NTP clock step-back
                if last_start_t is not None and start_t < last_start_t:
                    send_alert(
                        "clock_step_back",
                        "检测到系统时钟回拨",
                        f"本地窗口时间从 {int(last_start_t)} 回退至 {int(start_t)}，已拒绝倒退切窗以保护账本。",
                        level="error",
                        cooldown_s=300.0,
                    )
                    time.sleep(0.5)
                    continue

                # 0. Non-blocking Background Retention Cleanup (hourly disk hygiene; preserves summary.jsonl)
                if self._cleanup_future is not None and self._cleanup_future.done():
                    try:
                        cres = self._cleanup_future.result()
                        if cres and (cres.get("removed_files", 0) > 0 or cres.get("removed_dirs", 0) > 0):
                            mode_tag = " [磁盘压力自动瘦身]" if cres.get("pressure_triggered") else ""
                            print(
                                f"\n[Pipeline] Auto-cleanup{mode_tag} freed {cres['freed_mb']:.2f} MB "
                                f"({cres['removed_dirs']} dirs, {cres['removed_files']} files; summary.jsonl preserved)"
                            )
                        if cres and cres.get("disk_guard_paused"):
                            reason_msg = cres.get("disk_guard_reason") or "磁盘空间危急"
                            print(f"\n[Pipeline] WARNING: {reason_msg}")
                            send_alert("disk_guard_paused", "磁盘空间危急熔断", reason_msg, level="error", cooldown_s=1800.0)
                    except Exception:
                        pass
                    finally:
                        self._cleanup_future = None

                cleanup_interval = float(config.PARAMS.get("CLEANUP_INTERVAL_S", 3600.0))
                if self._cleanup_future is None and cleanup_interval > 0 and (
                    self._last_cleanup_mono <= 0.0 or (loop_mono - self._last_cleanup_mono) >= cleanup_interval
                ):
                    self._last_cleanup_mono = loop_mono
                    self._cleanup_future = self._bg_executor.submit(cleanup_old_data)

                # 0a. Periodic Exchange Clock Offset Monitor (every 5 minutes)
                if self._clock_future is not None and self._clock_future.done():
                    try:
                        offset_s = self._clock_future.result()
                        if offset_s is not None and abs(offset_s) > 1.5:
                            print(f"\n[Pipeline] WARNING: Host clock offset vs Polymarket CLOB is {offset_s:+.2f}s!")
                            send_alert(
                                "clock_drift",
                                "服务器时钟偏差告警",
                                f"宿主机与 Polymarket CLOB 时钟偏差 {offset_s:+.2f}s（阈值 1.5s），请检查 NTP/chrony。",
                                level="error",
                                cooldown_s=900.0,
                            )
                    except Exception:
                        pass
                    finally:
                        self._clock_future = None

                if self._clock_future is None and (self._last_clock_mono <= 0.0 or (loop_mono - self._last_clock_mono) >= 300.0):
                    self._last_clock_mono = loop_mono
                    self._clock_future = self._fast_bg_executor.submit(
                        lambda: self.feed.check_clock_offset(self.feed._get_worker_session())
                    )

                # 0b. Non-blocking Background Reconciler: Confirm official Gamma/UMA settlement & Chainlink TWAP
                if self._reconcile_future is not None and self._reconcile_future.done():
                    try:
                        rec_results = self._reconcile_future.result()
                        for rec_ev in rec_results:
                            r_slug = rec_ev["slug"]
                            for pit in pending_settlements:
                                if pit.get("slug") == r_slug:
                                    if rec_ev.get("polled"):
                                        pit["attempts"] = pit.get("attempts", 0) + 1
                                        pit["last_poll_t"] = rec_ev.get("poll_t", loop_start)
                                    if rec_ev.get("drop_done"):
                                        pit["done"] = True
                            try:
                                if rec_ev.get("twap_prices"):
                                    off_open, off_close, off_winner = rec_ev["twap_prices"]
                                    self.runner.reconcile_record(
                                        r_slug, off_open, off_close, off_winner,
                                        winner_source="twap"
                                    )
                                    print(f"\n[Pipeline] Official Chainlink TWAP confirmed for {r_slug}: ${off_open:,.2f} -> ${off_close:,.2f} ({off_winner})")
                                    for pit in pending_settlements:
                                        if pit.get("slug") == r_slug:
                                            pit["twap_prices"] = rec_ev["twap_prices"]
                                            pit["open_px"] = off_open
                                            pit["close_px"] = off_close
                                            if pit.get("gamma_confirmed"):
                                                pit["done"] = True
                                if rec_ev.get("gamma_winner"):
                                    off_open, off_close = rec_ev["gamma_prices"]
                                    self.runner.reconcile_record(
                                        r_slug, off_open, off_close, rec_ev["gamma_winner"],
                                        winner_source="gamma"
                                    )
                                    print(f"\n[Pipeline] Official Gamma/UMA truth confirmed for {r_slug}: {rec_ev['gamma_winner']}")
                                    for pit in pending_settlements:
                                        if pit.get("slug") == r_slug:
                                            pit["gamma_confirmed"] = True
                                            pit.setdefault("gamma_confirmed_at", loop_start)
                                            # Keep item alive until official TWAP prices also arrive (prevents locking fallback prices)
                                            if pit.get("twap_prices"):
                                                pit["done"] = True
                            except Exception as rec_exc:
                                print(f"\n[Pipeline] Reconcile write warning for {r_slug}: {rec_exc}")
                    except Exception as fut_exc:
                        print(f"\n[Pipeline] Reconcile worker warning: {fut_exc}")
                    finally:
                        self._reconcile_future = None
                        pending_settlements = [it for it in pending_settlements if not it.get("done")]

                pending_settlements = [it for it in pending_settlements if not it.get("done")]
                has_fresh_unconfirmed_twap = any(
                    (not it.get("twap_prices")) and ((loop_start - float(it.get("added_at", loop_start))) < 60.0)
                    for it in pending_settlements
                )
                reconcile_interval_s = 4.0 if has_fresh_unconfirmed_twap else 15.0
                if self._reconcile_future is None and (loop_mono - last_reconcile_mono >= reconcile_interval_s) and pending_settlements:
                    last_reconcile_mono = loop_mono
                    # Pass immutable shallow-dict copies so worker thread never mutates main-thread state
                    snapshot_items = [dict(it) for it in pending_settlements]

                    def _bg_reconcile(items):
                        out_events = []
                        sess = self.feed._get_worker_session()
                        now_w = time.time()
                        for item in items:
                            if item.get("done"):
                                continue
                            age_s = now_w - float(item.get("added_at", now_w))
                            # Slow down polling after 30 minutes to avoid hammering Gamma during outages
                            if age_s > 1800.0 and (now_w - float(item.get("last_poll_t", 0.0))) < 120.0:
                                continue
                            p_slug = item["slug"]
                            p_start = item["start_t"]
                            next_attempts = item.get("attempts", 0) + 1
                            ev = {"slug": p_slug, "polled": True, "poll_t": now_w}

                            # Query Gamma only if not already confirmed
                            official = None
                            if not item.get("gamma_confirmed"):
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
                                            o_px = float(b2.get("openPrice", 0) or 0)
                                            c_px = float(b2.get("closePrice", 0) or 0)
                                            if o_px > 0 and c_px > 0:
                                                official_poly = (o_px, c_px, "Up" if c_px >= o_px else "Down")
                                except Exception:
                                    pass
                                if official_poly:
                                    ev["twap_prices"] = official_poly

                            if official:
                                off_open = official_poly[0] if official_poly else item.get("open_px", 0.0)
                                off_close = official_poly[1] if official_poly else item.get("close_px", 0.0)
                                ev["gamma_winner"] = official["winner"]
                                ev["gamma_prices"] = (off_open, off_close)
                            elif item.get("gamma_confirmed") and not official_poly:
                                # Stop retrying TWAP if still unavailable 10 minutes after Gamma confirmation
                                if (now_w - float(item.get("gamma_confirmed_at", now_w))) > 600.0:
                                    ev["drop_done"] = True
                            elif age_s > 21600.0:
                                print(f"\n[Pipeline] Reconcile timeout for {p_slug} ({next_attempts} attempts, 6h). Dropping.")
                                ev["drop_done"] = True

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
                        est_close = self.runner.estimate_twap_close_price(loop_start)
                        if est_close > 0.0:
                            close_px = est_close
                        else:
                            close_spot = self.feed.fetch_spot_state(self.runner.open_price, loop_start)
                            close_px = close_spot.price if close_spot.price > 0 else self.runner.open_price

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
                        if len(pending_settlements) > 100:
                            pending_settlements = pending_settlements[-100:]

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
                        resume_from_live=is_first_window,
                    )
                    is_first_window = False
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

                # 2b. Retry token resolution if initially pending (throttled when API schema paused)
                schema_guard = self.feed.get_api_schema_guard()
                if schema_guard.get("paused"):
                    send_alert(
                        f"api_schema_{schema_guard.get('source', 'unknown')}",
                        "上游接口变更，策略已安全暂停",
                        f"原因: {schema_guard.get('reason', 'unknown')}\n已自动撤单并暂停开新仓，每 5 分钟低频复检恢复。",
                        level="error",
                        cooldown_s=1800.0,
                    )
                if not (active_tokens.get("token_up") and active_tokens.get("token_down")):
                    allow_retry = (
                        not schema_guard.get("paused")
                        or (loop_mono - getattr(self, "_last_schema_probe_mono", 0.0)) >= 300.0
                    )
                    if allow_retry:
                        if schema_guard.get("paused"):
                            self._last_schema_probe_mono = loop_mono
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
                    if loop_mono - getattr(self, "_last_open_check_mono", 0.0) >= 1.0:
                        self._last_open_check_mono = loop_mono
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
                        self._open_calib_future = self._fast_bg_executor.submit(_bg_calib)

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
                        self._prewarm_future = self._fast_bg_executor.submit(_bg_prewarm)

                # 3. Step Evaluation Tick
                if active_tokens.get("token_up") and active_tokens.get("token_down"):
                    # Clear wake flag right before sampling WS snapshots so any push
                    # arriving during this tick triggers an immediate next wakeup.
                    self._wake_event.clear()
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

                    # 盘口 WS 积压滞后防呆护栏：用独立逐笔成交流实时成交价交叉校验 WS 盘口，
                    # 一旦真实成交价已甩开 WS 盘口 >= $0.05，立刻作废积压缓存并拉取实时 HTTP 盘口。
                    book_diverged = False
                    for tok_key, bk_obj in (("token_up", b_up), ("token_down", b_dn)):
                        tok_id = active_tokens.get(tok_key)
                        if not tok_id or bk_obj.best_bid is None or bk_obj.best_ask is None:
                            continue
                        tp_live = self.trade_stream.get_latest_price(tok_id, max_age_s=3.0, now=loop_start)
                        if tp_live is not None and (tp_live > bk_obj.best_ask + 0.05 or tp_live < bk_obj.best_bid - 0.05):
                            self.book_ws.invalidate_on_trade_divergence(tok_id, tp_live, max_gap=0.05)
                            book_diverged = True
                    if book_diverged:
                        f_u = self.feed._executor.submit(self.feed.fetch_order_book, active_tokens["token_up"], "Up")
                        f_d = self.feed._executor.submit(self.feed.fetch_order_book, active_tokens["token_down"], "Down")
                        b_up = f_u.result()
                        b_dn = f_d.result()

                    def _delayed_book(_side_name: str):
                        # Taker IOC 延迟到站核验优先直连实时 HTTP REST 盘口，彻底杜绝读到代理/TCP 积压旧 WS 帧
                        def _one(tok_key: str, label: str):
                            tok = active_tokens.get(tok_key)
                            if not tok:
                                return None
                            rest_bk = self.feed.fetch_order_book(tok, label)
                            if rest_bk is not None and (rest_bk.best_bid is not None or rest_bk.best_ask is not None):
                                return rest_bk
                            return self.book_ws.get_order_book(tok, max_age_s=1.5, require_recent_msg=True)
                        f_du = self.feed._executor.submit(_one, "token_up", "Up")
                        f_dd = self.feed._executor.submit(_one, "token_down", "Down")
                        return (f_du.result(), f_dd.result())

                    net_summary = dict(self.feed.get_net_summary())
                    net_summary["ws_connected"] = bool(self.trade_stream.connected)
                    net_summary["trades_latency_ms"] = float(getattr(self.trade_stream, "rtt_ms", 0.0) or 0.0)
                    net_summary["ws_trades"] = int(self.trade_stream.trade_count)
                    net_summary["ws_reconnects"] = int(self.trade_stream.reconnect_count)
                    net_summary["spot_ws_connected"] = bool(self.spot_ws.connected)
                    net_summary["book_ws_connected"] = bool(self.book_ws.connected)
                    net_summary["book_ws_msgs"] = int(self.book_ws.msg_count)
                    net_summary["book_empty"] = bool(
                        (b_up.best_bid is None and b_up.best_ask is None)
                        or (b_dn.best_bid is None and b_dn.best_ask is None)
                    )
                    if self.trade_stream.last_error:
                        net_summary["ws_last_error"] = self.trade_stream.last_error
                    schema_guard = self.feed.get_api_schema_guard()
                    self.runner.step(
                        loop_start, spot, b_up, b_dn,
                        net_stats=net_summary,
                        trade_prints=trades,
                        poly_spot=poly_spot,
                        poly_spot_ts=getattr(self.feed, "last_poly_btc_ts", None),
                        delayed_book_fetcher=_delayed_book,
                        delayed_prints_fetcher=lambda: self.trade_stream.drain_trades(slug=slug),
                        api_schema_guard=schema_guard,
                        async_taker_delay=True,
                    )

                    # Formatted terminal status line (TTY: live overwrite; background/systemd: 60s clean heartbeat)
                    last_dec = self.runner.decisions[-1] if self.runner.decisions else {}
                    act = last_dec.get("action", "hold")
                    side = last_dec.get("side") or ""
                    px_str = f"@{last_dec.get('price'):.2f}" if last_dec.get("price") else ""
                    cb_info = self.runner.circuit_breaker.get_status(loop_start)
                    cb_tag = cb_info["status"]
                    if cb_info.get("cooling_rounds_left"):
                        cb_tag += f"({cb_info['cooling_rounds_left']})"

                    status_line = (
                        f"[{time.strftime('%H:%M:%S')}] tau: {tau_s:3.0f}s | "
                        f"BTC: ${spot.price:,.2f} ({spot.return_bps:+5.1f}bps, vol:{spot.realized_vol_bps:.1f}) | "
                        f"Pos: UP {self.runner.pos_up.shares:.0f} / DN {self.runner.pos_down.shares:.0f} | "
                        f"CB: {cb_tag} | "
                        f"Act: {act.upper()} {side} {px_str} "
                        f"{'(' + last_dec.get('reason', '')[:25] + ')' if last_dec.get('reason') else ''}"
                    )
                    cur_action_cnt = len(self.runner.actions)
                    if is_tty:
                        print(f"\r{status_line}", end="", flush=True)
                    elif (loop_mono - last_status_log_mono) >= 60.0 or cur_action_cnt != last_logged_action_cnt:
                        last_status_log_mono = loop_mono
                        last_logged_action_cnt = cur_action_cnt
                        print(status_line, flush=True)
                else:
                    self._wake_event.clear()
                    if is_tty:
                        print(
                            f"\r[{time.strftime('%H:%M:%S')}] tau: {tau_s:4.0f}s | "
                            f"Waiting for Polymarket contract deployment for {slug}...",
                            end="", flush=True
                        )
                    elif (loop_mono - last_status_log_mono) >= 60.0:
                        last_status_log_mono = loop_mono
                        print(f"[{time.strftime('%H:%M:%S')}] Waiting for contract deployment for {slug}...", flush=True)

                # Event-driven cadence: react within ~20ms on WS spot/book/trade push,
                # while keeping a 0.20s heartbeat ceiling (or 5s low-power sleep when API schema paused).
                if self.feed.get_api_schema_guard().get("paused"):
                    self._wake_event.wait(timeout=5.0)
                    continue
                min_interval_s = 0.02
                max_interval_s = 0.20
                if getattr(self.runner, "pending_takers", None):
                    next_due = min(float(pt["arrive_at"]) for pt in self.runner.pending_takers)
                    max_interval_s = min(max_interval_s, max(0.01, next_due - time.time()))
                elapsed = time.monotonic() - loop_mono
                if elapsed < min_interval_s:
                    time.sleep(min_interval_s - elapsed)
                remaining = max_interval_s - (time.monotonic() - loop_mono)
                if remaining > 0 and self.running:
                    self._wake_event.wait(timeout=remaining)

            except KeyboardInterrupt:
                print("\n\n[Pipeline] Termination signal received. Stopping...")
                self.running = False
                break
            except Exception as e:
                print(f"\n[Pipeline] Tick warning: {e}")
                time.sleep(1.0)
        finally:
            self._shutdown_resources()
            if last_slug:
                print(
                    f"\n[Pipeline] INFO: window {last_slug} was still open at shutdown; "
                    f"positions are preserved in live.json and will auto-resume or auto-settle on next startup."
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
