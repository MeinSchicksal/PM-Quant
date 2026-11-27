"""PM-Quant Market Data Feed, Recorder, and Settlement Verifier.

Handles:
1. Binance BTC spot streaming and 5m open price resolution.
2. Polymarket 5m event discovery and CLOB book & last_trade_price parsing.
3. 7x24 immutable raw market stream recording into data/1_market/.
4. Gamma API official settlement truth verification.
"""
import os
import sys
import math
import time
import json
import queue
import datetime
import requests
import threading
from concurrent.futures import ThreadPoolExecutor
from requests.adapters import HTTPAdapter
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.types import OrderBook, SpotState
from core.estimator import calculate_dual_horizon_vol, calculate_adaptive_spread_params
from runtime.alerts import set_alert_proxy
from runtime.retention import is_raw_recording_allowed


class MarketFeed:
    def __init__(self, proxy: str = None):
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=12, pool_maxsize=12)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

        # Configure proxy fallback (default to local Clash port on Windows only; direct on Linux/macOS)
        default_proxy = "http://127.0.0.1:7897" if sys.platform == "win32" else ""
        self.proxy = proxy if proxy is not None else os.environ.get("PM_PROXY", default_proxy)
        self._proxy_gen = 0
        self._consecutive_spot_fails = 0
        self._consecutive_poly_fails = 0
        self._last_reprobe_t = 0.0
        self._reprobe_lock = threading.Lock()
        self.clock_offset_s = 0.0
        self._last_clock_check_t = 0.0
        self._worker_sessions = []
        self._worker_sess_lock = threading.Lock()
        self._poly_sess_lock = threading.Lock()
        self._schema_lock = threading.Lock()
        self._schema_counts = {}
        self._schema_guard = {
            "paused": False,
            "source": "",
            "reason": "",
            "detected_at": 0.0,
        }
        self._test_connectivity()

        self.slug_cache = {}
        # 30-minute rolling spot buffer (~18000 ticks at up to 10 Hz event-driven cadence)
        self.spot_history = deque(maxlen=18000)
        self._last_vol_calc_t = 0.0
        self._cached_vol_bps = float(config.PARAMS.get("MIN_MODEL_VOL_BPS", 14.0))
        self._last_spot_rec_t = 0.0
        self._last_spot_rec_px = 0.0
        self._last_binance_px = 0.0
        self._last_binance_t = 0.0
        self._last_spot_source = "binance_vision"
        self._last_spot_currency = ""
        self._spot_continuity_t = 0.0
        self._thread_local = threading.local()
        self._net_lock = threading.Lock()
        self._poly_lock = threading.Lock()
        self._record_lock = threading.Lock()
        self._raw_write_queue: "queue.Queue[Optional[tuple]]" = queue.Queue(maxsize=4096)
        self._raw_writer_thread = threading.Thread(
            target=self._raw_disk_writer_loop, daemon=True, name="MarketFeedDiskWriter"
        )
        self._raw_writer_thread.start()
        self._fail_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pm_feed")
        self.last_open_is_official = False

        # Polymarket official real-time Chainlink price cache & single persistent poller thread
        self._last_poly_btc = None
        self._last_poly_btc_t = 0.0
        self._last_poly_btc_req_t = 0.0
        self._last_poly_srv_ts = 0.0
        self._last_poly_poll_t = 0.0
        self._poly_btc_poller_active = False
        self._poly_poller_stop = threading.Event()
        self._poly_poller_wake = threading.Event()
        self._poly_poller_thread = None
        self._poly_poll_req_t = 0.0
        self._poly_session = None
        self._spot_ws = None
        self._book_ws = None
        self._trade_ws = None
        self._chainlink_ws_active = False
        self._last_cl_ws_msg_count = 0
        self._last_cl_ws_bytes = 0

        # Real-time network speed, latency, and traffic accounting
        self.net_stats = {
            "poly_latency_ms": 0.0,
            "binance_latency_ms": 0.0,
            "chainlink_latency_ms": 0.0,
            "downstream_kbps": 0.0,
            "total_bytes": 0,
            "bytes_window": 0,
            "last_calc_t": time.time(),
            "msg_rate": 0.0,
            "msgs_window": 0,
        }

    def _record_net(self, source: str, lat_ms: float, byte_count: int):
        now = time.time()
        lock = getattr(self, "_net_lock", None)
        if lock is not None:
            lock.acquire()
        try:
            if source == "poly" and lat_ms > 0:
                self.net_stats["poly_latency_ms"] = round(lat_ms, 1)
            elif source == "binance" and lat_ms > 0:
                self.net_stats["binance_latency_ms"] = round(lat_ms, 1)
            elif source == "chainlink" and lat_ms > 0:
                self.net_stats["chainlink_latency_ms"] = round(lat_ms, 1)

            self.net_stats["total_bytes"] += byte_count
            self.net_stats["bytes_window"] += byte_count
            self.net_stats["msgs_window"] += 1

            dt = now - self.net_stats["last_calc_t"]
            if dt >= 1.0:
                self.net_stats["downstream_kbps"] = round((self.net_stats["bytes_window"] / 1024.0) / dt, 1)
                self.net_stats["msg_rate"] = round(self.net_stats["msgs_window"] / dt, 1)
                self.net_stats["bytes_window"] = 0
                self.net_stats["msgs_window"] = 0
                self.net_stats["last_calc_t"] = now
        finally:
            if lock is not None:
                lock.release()

    def get_net_summary(self) -> dict:
        poly_lat = self.net_stats.get("poly_latency_ms", 0.0)
        binance_lat = self.net_stats.get("binance_latency_ms", 0.0)
        cl_lat = self.net_stats.get("chainlink_latency_ms", 0.0)
        cl_ts = getattr(self, "_last_poly_btc_t", 0.0)
        cl_age = round(max(0.0, time.time() - cl_ts), 1) if cl_ts > 0 else None
        valid_lats = [x for x in (poly_lat, binance_lat, cl_lat) if x and x > 0]
        avg_lat = round(sum(valid_lats) / len(valid_lats), 1) if valid_lats else 0.0
        sig_minus_venue = (
            round(float(binance_lat) - float(poly_lat), 1)
            if (binance_lat and binance_lat > 0 and poly_lat and poly_lat > 0)
            else None
        )
        return {
            "poly_latency_ms": poly_lat,
            "binance_latency_ms": binance_lat,
            "chainlink_latency_ms": cl_lat,
            "chainlink_ws_connected": bool(getattr(self, "_chainlink_ws_active", False)),
            "signal_minus_venue_ms": sig_minus_venue,
            "spot_provider": getattr(getattr(self, "_spot_ws", None), "active_provider", "binance"),
            "chainlink_age_s": cl_age,
            "avg_latency_ms": avg_lat,
            "downstream_kbps": self.net_stats.get("downstream_kbps", 0.0),
            "total_mb": round(self.net_stats.get("total_bytes", 0) / (1024 * 1024), 2),
            "msg_rate": self.net_stats.get("msg_rate", 0.0),
            "clock_offset_s": round(float(getattr(self, "clock_offset_s", 0.0) or 0.0), 3),
        }

    def record_api_schema_ok(self, source: str):
        """Reset schema drift counter for `source`; auto-resume if this source caused pause."""
        lock = getattr(self, "_schema_lock", None)
        if lock is None:
            return
        with lock:
            self._schema_counts[source] = 0
            if self._schema_guard.get("paused") and self._schema_guard.get("source") == source:
                self._schema_guard = {"paused": False, "source": "", "reason": "", "detected_at": 0.0}
                print(f"\n[MarketFeed] API schema recovered for {source}; resuming trading.")

    def record_api_schema_error(self, source: str, detail: str, threshold: int = 3):
        """Track consecutive upstream protocol/schema anomalies and trip safe pause on persistent drift."""
        lock = getattr(self, "_schema_lock", None)
        if lock is None:
            return
        with lock:
            cnt = int(self._schema_counts.get(source, 0)) + 1
            self._schema_counts[source] = cnt
            if cnt >= max(1, int(threshold)):
                was_paused = bool(self._schema_guard.get("paused"))
                self._schema_guard = {
                    "paused": True,
                    "source": source,
                    "reason": f"{source} 接口格式异常/已变更 ({detail})",
                    "detected_at": round(time.time(), 1),
                }
                if not was_paused:
                    print(f"\n[MarketFeed] CRITICAL: {self._schema_guard['reason']} -> pausing trading safely!")

    def get_api_schema_guard(self) -> dict:
        lock = getattr(self, "_schema_lock", None)
        if lock is None:
            return {"paused": False, "source": "", "reason": "", "detected_at": 0.0}
        with lock:
            return dict(self._schema_guard)

    @staticmethod
    def _is_valid_btc_price(px: float, prev_px: float = 0.0) -> bool:
        if not math.isfinite(px) or px < 1000.0 or px > 10_000_000.0:
            return False
        if prev_px > 0.0 and abs(px - prev_px) / prev_px > 0.20:
            return False
        return True

    def _update_clock_from_clob_resp(self, resp, t_local_mid: float):
        try:
            raw = resp.text.strip().strip('"')
            srv_t = float(raw)
            if srv_t > 1e12:
                srv_t /= 1000.0
            elif "." not in raw:
                # Polymarket /time returns a floored integer second; add +0.5s expected
                # sub-second midpoint so integer truncation doesn't fake a -0.5s..-1.0s lag.
                srv_t += 0.5
            if math.isfinite(srv_t) and 1_600_000_000.0 < srv_t < 4_000_000_000.0:
                self.clock_offset_s = round(t_local_mid - srv_t, 3)
                self._last_clock_check_t = time.monotonic()
        except Exception:
            pass

    def check_clock_offset(self, sess: requests.Session = None) -> Optional[float]:
        """Measure host clock offset (local - server) with millisecond precision.

        Prefers millisecond-resolution Binance /api/v3/time to eliminate the
        +/-0.5s integer-second truncation noise of Polymarket /time, falling back
        to Polymarket /time when unreachable.
        """
        http = sess or self.session
        for url in (
            "https://data-api.binance.vision/api/v3/time",
            "https://api.binance.com/api/v3/time",
        ):
            try:
                t0 = time.time()
                resp = http.get(url, timeout=(1.5, 2.0))
                t1 = time.time()
                if resp.status_code == 200:
                    data = resp.json()
                    srv_ms = float(data.get("serverTime", 0) or 0) if isinstance(data, dict) else 0.0
                    srv_t = srv_ms / 1000.0
                    if math.isfinite(srv_t) and 1_600_000_000.0 < srv_t < 4_000_000_000.0:
                        self.clock_offset_s = round(0.5 * (t0 + t1) - srv_t, 3)
                        self._last_clock_check_t = time.monotonic()
                        return self.clock_offset_s
            except Exception:
                continue
        try:
            t0 = time.time()
            resp = http.get("https://clob.polymarket.com/time", timeout=(1.5, 2.0))
            t1 = time.time()
            if resp.status_code == 200:
                self._update_clock_from_clob_resp(resp, 0.5 * (t0 + t1))
                return self.clock_offset_s
        except Exception:
            pass
        return None

    def _test_connectivity(self):
        """Verify whether direct connection or proxy works best for active spot provider & CLOB."""
        spot_ws = getattr(self, "_spot_ws", None)
        prov = str(
            getattr(spot_ws, "active_provider", None)
            or config.PARAMS.get("SPOT_WS_PROVIDER", "binance")
            or "binance"
        ).strip().lower()
        spot_url_map = {
            "coinbase": "https://api.exchange.coinbase.com/products/BTC-USD/ticker",
            "kraken": "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
            "okx": "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT",
        }
        spot_test = spot_url_map.get(prov, "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT")
        clob_test = "https://clob.polymarket.com/time"
        # Prefer direct connection if both endpoints succeed without proxy (lowest latency)
        try:
            r1 = self.session.get(spot_test, proxies={}, timeout=2.5)
            t0 = time.time()
            r2 = self.session.get(clob_test, proxies={}, timeout=2.5)
            t1 = time.time()
            if r1.status_code == 200 and r2.status_code == 200:
                self._update_clock_from_clob_resp(r2, 0.5 * (t0 + t1))
                self.session.proxies = {}
                set_alert_proxy(None)
                return
        except Exception:
            pass
        if self.proxy:
            try:
                px_cfg = {"http": self.proxy, "https": self.proxy}
                r1 = self.session.get(spot_test, proxies=px_cfg, timeout=3.0)
                t0 = time.time()
                r2 = self.session.get(clob_test, proxies=px_cfg, timeout=3.0)
                t1 = time.time()
                if r1.status_code == 200 and r2.status_code == 200:
                    self._update_clock_from_clob_resp(r2, 0.5 * (t0 + t1))
                    self.session.proxies = px_cfg
                    set_alert_proxy(self.proxy)
                    return
            except Exception:
                pass
        self.session.proxies = {}
        set_alert_proxy(None)

    def reprobe_connectivity(self, now: Optional[float] = None) -> bool:
        """Re-probe direct vs proxy connectivity after sustained outage and sync all sessions/WS clients."""
        t = time.time() if now is None else float(now)
        lock = getattr(self, "_reprobe_lock", None)
        if lock is not None and not lock.acquire(blocking=False):
            return False
        try:
            if (t - getattr(self, "_last_reprobe_t", 0.0)) < 30.0:
                return False
            self._last_reprobe_t = t
            old_proxies = dict(self.session.proxies or {})
            self._test_connectivity()
            new_proxies = dict(self.session.proxies or {})
            if new_proxies != old_proxies:
                self._proxy_gen = getattr(self, "_proxy_gen", 0) + 1
                ps_lock = getattr(self, "_poly_sess_lock", None)
                if ps_lock is not None:
                    with ps_lock:
                        if getattr(self, "_poly_session", None) is not None:
                            self._poly_session.proxies = dict(new_proxies)
                elif getattr(self, "_poly_session", None) is not None:
                    self._poly_session.proxies = dict(new_proxies)
                ws_proxy = new_proxies.get("https") or new_proxies.get("http")
                for ws_client in (
                    getattr(self, "_spot_ws", None),
                    getattr(self, "_book_ws", None),
                    getattr(self, "_trade_ws", None),
                ):
                    if ws_client is not None:
                        ws_client.proxy = ws_proxy
                        if hasattr(ws_client, "force_reconnect"):
                            try:
                                ws_client.force_reconnect()
                            except Exception:
                                pass
                route_label = ws_proxy or "direct"
                print(f"\n[MarketFeed] Connectivity route switched to: {route_label}")
                return True
            return False
        finally:
            if lock is not None:
                lock.release()

    def _flush_raw_batch(self, batch: list):
        """Write a batch of (category, date_str, json_line) entries grouped by file."""
        if not batch:
            return
        grouped = defaultdict(list)
        for cat, day_str, line in batch:
            grouped[(cat, day_str)].append(line)
        lock = getattr(self, "_record_lock", None)
        for (cat, day_str), lines in grouped.items():
            try:
                target_dir = config.MARKET_DIR / cat / day_str
                target_dir.mkdir(parents=True, exist_ok=True)
                target_file = target_dir / f"{cat}_{day_str}.jsonl"
                joined = "".join(lines)
                if lock is not None:
                    with lock:
                        with open(target_file, "a", encoding="utf-8") as f:
                            f.write(joined)
                else:
                    with open(target_file, "a", encoding="utf-8") as f:
                        f.write(joined)
            except Exception as e:
                print(f"[MarketFeed] Raw data recording warning ({cat}): {e}")

    def _raw_disk_writer_loop(self):
        """Background batched disk writer for high-frequency raw market events."""
        q = self._raw_write_queue
        while True:
            try:
                first = q.get(timeout=0.5)
            except queue.Empty:
                continue
            if first is None:
                return
            batch = [first]
            stop_after = False
            while len(batch) < 64:
                try:
                    nxt = q.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    stop_after = True
                    break
                batch.append(nxt)
            self._flush_raw_batch(batch)
            if stop_after:
                return

    def record_raw_event(self, category: str, record: dict):
        """Append an immutable record to data/1_market/<category>/<date>/.

        High-frequency streams are queued to a background batched writer so the
        trading loop never stalls on disk I/O; rare official settlements write
        synchronously for immediate durability.
        """
        if category != "settlements" and not is_raw_recording_allowed():
            return
        try:
            today = time.strftime("%Y-%m-%d", time.gmtime())
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            q = getattr(self, "_raw_write_queue", None)
            th = getattr(self, "_raw_writer_thread", None)
            if category != "settlements" and q is not None and th is not None and th.is_alive():
                try:
                    q.put_nowait((category, today, line))
                    return
                except queue.Full:
                    return
            self._flush_raw_batch([(category, today, line)])
        except Exception as e:
            # Data recording should never crash the engine
            print(f"[MarketFeed] Raw data recording warning ({category}): {e}")

    def get_current_slug_info(self, now: float = None) -> dict:
        """Derive 5-minute window slug and boundary timestamps."""
        t = now or time.time()
        start_t = int(t // 300) * 300
        end_t = start_t + 300
        slug = f"btc-updown-5m-{start_t}"
        return {
            "slug": slug,
            "start_t": float(start_t),
            "end_t": float(end_t),
            "tau_s": max(0.0, end_t - t)
        }

    def resolve_tokens(self, slug: str) -> dict:
        """Query Gamma API to obtain Token IDs for UP and DOWN."""
        if slug in self.slug_cache:
            return self.slug_cache[slug]

        url = f"https://gamma-api.polymarket.com/events?slug={slug}"
        try:
            resp = self.session.get(url, timeout=4.0)
            if resp.status_code == 200:
                data = resp.json()
                if not isinstance(data, list):
                    self.record_api_schema_error("gamma_tokens", f"expected list, got {type(data).__name__}", threshold=3)
                    return {}
                if not data:
                    # Empty list is normal while a brand-new 5m contract is still deploying
                    return {}
                ev0 = data[0]
                if not isinstance(ev0, dict) or "markets" not in ev0 or not ev0["markets"]:
                    self.record_api_schema_error("gamma_tokens", "missing 'markets' field in event payload", threshold=3)
                    return {}
                m = ev0["markets"][0]
                raw_tokens = m.get("clobTokenIds", [])
                outcomes = m.get("outcomes", [])
                if isinstance(raw_tokens, str):
                    raw_tokens = json.loads(raw_tokens)
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)

                token_up = None
                token_down = None
                for outcome, tid in zip(outcomes, raw_tokens):
                    if str(outcome).lower() == "up":
                        token_up = tid
                    elif str(outcome).lower() == "down":
                        token_down = tid

                if token_up and token_down:
                    self.record_api_schema_ok("gamma_tokens")
                    meta = {
                        "slug": slug,
                        "condition_id": m.get("conditionId"),
                        "token_up": token_up,
                        "token_down": token_down,
                        "resolved": bool(m.get("closed") or m.get("umaResolutionStatus") == "resolved")
                    }
                    self.slug_cache[slug] = meta
                    if len(self.slug_cache) > 50:
                        oldest = next(iter(self.slug_cache))
                        if oldest != slug:
                            self.slug_cache.pop(oldest, None)
                    return meta
                self.record_api_schema_error("gamma_tokens", "missing Up/Down clobTokenIds", threshold=3)
            elif resp.status_code in (400, 404, 410, 422):
                self.record_api_schema_error("gamma_tokens", f"HTTP {resp.status_code}", threshold=3)
        except Exception as e:
            print(f"[MarketFeed] Token resolution warning for {slug}: {e}")
            
        return {}

    @staticmethod
    def _poly_twap_params(start_t: float, include_end: bool = True) -> dict:
        iso_start = datetime.datetime.fromtimestamp(start_t, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        params = {
            "symbol": "BTC",
            "eventStartTime": iso_start,
            "variant": "fiveminute",
            "twapEnabled": "true",
            "twapLookbackSeconds": "60",
        }
        if include_end:
            params["endDate"] = datetime.datetime.fromtimestamp(
                start_t + 300, datetime.timezone.utc
            ).isoformat().replace("+00:00", "Z")
        return params

    def fetch_window_open_price(self, start_t: float) -> Tuple[float, float]:
        """Fetch BTC reference open price: (poly_official_twap_open, binance_spot_open).
        
        Guarantees that Polymarket official Chainlink TWAP is prioritized, with continuous
        previous-round closePrice stitching if current round open is still computing.
        """
        poly_open = 0.0
        binance_open = 0.0
        poly_is_official = False

        # 1. Primary: Polymarket official Chainlink BTC/USD TWAP
        try:
            poly_url = "https://polymarket.com/api/crypto/crypto-price"
            resp = self.session.get(poly_url, params=self._poly_twap_params(start_t, include_end=True), timeout=(1.2, 1.8))
            if resp.status_code == 200:
                body = resp.json()
                open_px = body.get("openPrice")
                if open_px and float(open_px) > 0:
                    poly_open = float(open_px)
                    poly_is_official = True
        except Exception:
            pass

        # 1b. If poly_open still 0, stitch from previous round's official closePrice as provisional anchor
        if poly_open <= 0:
            prev_settlement = self.fetch_window_settlement_price(start_t - 300)
            if prev_settlement and prev_settlement[1] > 0:
                poly_open = float(prev_settlement[1])

        # 2. Spot Open Anchor: 若启用了非 Binance 现货源（如 Coinbase/Kraken BTC/USD），
        # 优先取同源现货 WS 或同源历史 K 线作为开盘锚点，避免将 BTC/USD 现价与 BTC/USDT K 线混算。
        spot_ws = getattr(self, "_spot_ws", None)
        active_prov = str(
            getattr(spot_ws, "active_provider", None)
            or config.PARAMS.get("SPOT_WS_PROVIDER", "binance")
            or "binance"
        ).strip().lower()
        if spot_ws is not None and active_prov != "binance" and abs(time.time() - start_t) <= 12.0:
            ws_sample = spot_ws.get_latest(max_age_s=3.0)
            if ws_sample is not None and ws_sample[0] > 0:
                binance_open = float(ws_sample[0])

        if binance_open <= 0 and active_prov == "coinbase":
            try:
                iso_s = datetime.datetime.fromtimestamp(start_t, datetime.timezone.utc).isoformat()
                iso_e = datetime.datetime.fromtimestamp(start_t + 60.0, datetime.timezone.utc).isoformat()
                c_resp = self.session.get(
                    "https://api.exchange.coinbase.com/products/BTC-USD/candles",
                    params={"start": iso_s, "end": iso_e, "granularity": 60},
                    timeout=(1.2, 1.8),
                )
                if c_resp.status_code == 200:
                    candles = c_resp.json()
                    if isinstance(candles, list) and candles:
                        # Coinbase returns [[bucket_ts, low, high, open, close, volume], ...]
                        candles_sorted = sorted(
                            [c for c in candles if isinstance(c, (list, tuple)) and len(c) >= 4],
                            key=lambda c: abs(float(c[0]) - start_t),
                        )
                        if candles_sorted and abs(float(candles_sorted[0][0]) - start_t) <= 120.0:
                            c_open = float(candles_sorted[0][3] or 0.0)
                            if c_open > 0:
                                binance_open = c_open
            except Exception:
                pass
            if binance_open <= 0 and abs(time.time() - start_t) <= 15.0:
                try:
                    t_resp = self.session.get(
                        "https://api.exchange.coinbase.com/products/BTC-USD/ticker",
                        timeout=(1.0, 1.5),
                    )
                    if t_resp.status_code == 200:
                        td = t_resp.json()
                        cb_b = float(td.get("bid", 0.0) or 0.0)
                        cb_a = float(td.get("ask", 0.0) or 0.0)
                        cb_px = (cb_b + cb_a) * 0.5 if (cb_b > 0 and cb_a > 0) else float(td.get("price", 0.0) or 0.0)
                        if cb_px > 0:
                            binance_open = cb_px
                except Exception:
                    pass

        # 默认/降级：Binance Spot 5m Kline Open (prefer data-api.binance.vision to bypass HTTP 451 geo-blocks)
        if binance_open <= 0:
            start_ms = int(start_t * 1000)
            for base_url in (
                "https://data-api.binance.vision/api/v3/klines",
                "https://api.binance.com/api/v3/klines",
            ):
                url = f"{base_url}?symbol=BTCUSDT&interval=5m&startTime={start_ms}&limit=1"
                try:
                    resp = self.session.get(url, timeout=(1.2, 1.8))
                    if resp.status_code == 200:
                        klines = resp.json()
                        if klines and len(klines) > 0:
                            binance_open = float(klines[0][1])
                            if binance_open > 0:
                                break
                except Exception:
                    pass

        self.last_open_is_official = poly_is_official

        # Fallback cross-assignment if either is missing
        if poly_open <= 0 and binance_open > 0:
            poly_open = binance_open
        if binance_open <= 0 and poly_open > 0:
            binance_open = poly_open

        # Record raw open events with true source provenance
        if poly_open > 0:
            self.record_raw_event("oracle", {
                "t": time.time(), "kind": "window_open",
                "source": "polymarket_chainlink_twap" if poly_is_official else "provisional_fallback",
                "start_t": start_t, "open_price": poly_open
            })
        if binance_open > 0:
            spot_src_tag = f"{active_prov}_spot" if active_prov != "binance" else "binance_spot"
            self.record_raw_event("oracle", {
                "t": time.time(), "kind": "window_open", "source": spot_src_tag,
                "start_t": start_t, "open_price": binance_open
            })

        return poly_open, binance_open

    def fetch_window_settlement_price(
        self,
        start_t: float,
        sess: requests.Session = None,
        timeout=(1.2, 2.0),
    ) -> Optional[Tuple[float, float, str]]:
        """Fetch official settlement open, close, and winner from Polymarket's crypto-price API."""
        http = sess or self.session
        try:
            poly_url = "https://polymarket.com/api/crypto/crypto-price"
            resp = http.get(poly_url, params=self._poly_twap_params(start_t, include_end=True), timeout=timeout)
            if resp.status_code == 200:
                body = resp.json()
                if body.get("completed") and body.get("closePrice") is not None:
                    open_px = float(body.get("openPrice", 0) or 0)
                    close_px = float(body.get("closePrice", 0) or 0)
                    if math.isfinite(open_px) and math.isfinite(close_px) and open_px > 0 and close_px > 0:
                        winner = "Up" if close_px >= open_px else "Down"
                        return open_px, close_px, winner
        except Exception:
            pass
        return None

    def _poly_poller_loop(self):
        """Single long-lived background poller for Polymarket Chainlink TWAP (zero per-tick thread churn)."""
        while not self._poly_poller_stop.is_set():
            triggered = self._poly_poller_wake.wait(timeout=1.0)
            if self._poly_poller_stop.is_set():
                break
            if not triggered:
                continue
            self._poly_poller_wake.clear()
            req_t = float(getattr(self, "_poly_poll_req_t", 0.0) or time.time())
            try:
                ps_lock = getattr(self, "_poly_sess_lock", None)
                if ps_lock is not None:
                    with ps_lock:
                        if getattr(self, "_poly_session", None) is None:
                            sess = requests.Session()
                            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4)
                            sess.mount("https://", adapter)
                            sess.mount("http://", adapter)
                            sess.headers.update(self.session.headers)
                            sess.proxies = dict(self.session.proxies)
                            self._poly_session = sess
                        sess = self._poly_session
                else:
                    sess = self._poly_session or self.session
                url = "https://polymarket.com/api/crypto/crypto-price"
                t0 = time.perf_counter()
                resp = sess.get(url, params=self._poly_twap_params(req_t, include_end=False), timeout=(1.2, 1.8))
                cl_lat_ms = (time.perf_counter() - t0) * 1000.0
                if resp.status_code == 200:
                    body = resp.json()
                    if not isinstance(body, dict) or ("openPrice" not in body and "closePrice" not in body):
                        self.record_api_schema_error("poly_twap", "missing openPrice/closePrice in JSON", threshold=5)
                        continue
                    px_raw = body.get("openPrice")
                    px_f = float(px_raw) if px_raw is not None else 0.0
                    raw_srv_ts = body.get("timestamp")
                    srv_ts = 0.0
                    if raw_srv_ts is not None:
                        try:
                            srv_ts = float(raw_srv_ts)
                            if srv_ts > 1e11:
                                srv_ts /= 1000.0
                        except (TypeError, ValueError):
                            srv_ts = 0.0
                    if self._is_valid_btc_price(px_f):
                        self.record_api_schema_ok("poly_twap")
                        self._record_net("chainlink", cl_lat_ms, len(resp.content))
                        accepted = True
                        lock = getattr(self, "_poly_lock", None)
                        if lock:
                            lock.acquire()
                        try:
                            prev_srv_ts = float(getattr(self, "_last_poly_srv_ts", 0.0) or 0.0)
                            # 丢弃 Cloudflare 多边缘节点返回的过期旧缓存帧，彻底消除斜坡锯齿回跳
                            if srv_ts > 0.0 and prev_srv_ts > 0.0 and srv_ts < prev_srv_ts - 0.001:
                                accepted = False
                            else:
                                self._last_poly_btc = px_f
                                self._last_poly_btc_t = time.time()
                                self._last_poly_btc_req_t = float(req_t)
                                if srv_ts > 0.0:
                                    self._last_poly_srv_ts = srv_ts
                        finally:
                            if lock:
                                lock.release()
                        if accepted:
                            self.record_raw_event("oracle", {
                                "t": time.time(), "ref_t": float(req_t), "kind": "poly_twap_live", "price": px_f
                            })
                elif resp.status_code in (400, 404, 410, 422):
                    self.record_api_schema_error("poly_twap", f"HTTP {resp.status_code}", threshold=5)
            except Exception:
                pass
            finally:
                self._last_poly_poll_t = time.monotonic()
                self._poly_btc_poller_active = False

    def _ensure_poly_poller(self):
        th = getattr(self, "_poly_poller_thread", None)
        if th is not None and th.is_alive():
            return
        if not hasattr(self, "_poly_poller_stop"):
            self._poly_poller_stop = threading.Event()
        if not hasattr(self, "_poly_poller_wake"):
            self._poly_poller_wake = threading.Event()
        self._poly_poller_stop.clear()
        th = threading.Thread(target=self._poly_poller_loop, daemon=True, name="PolyTwapPoller")
        self._poly_poller_thread = th
        th.start()

    def get_polymarket_btc_price(self, now: float = None) -> Optional[float]:
        """Fetch Polymarket official real-time Chainlink BTC/USD price.

        Primary path: sub-10ms Polymarket RTDS WebSocket (`crypto_prices_chainlink` btc/usd)
        multiplexed on TradeStreamClient — zero HTTP polling overhead when healthy.
        Fallback path: asynchronous REST poller (`polymarket.com/api/crypto/crypto-price`)
        activated seamlessly whenever WS is disconnected or silent > 3.5s.
        """
        current_t = now or time.time()
        trade_ws = getattr(self, "_trade_ws", None)
        ws_getter = getattr(trade_ws, "get_latest_chainlink_btc", None) if trade_ws is not None else None
        ws_sample = ws_getter(max_age_s=3.5, now=current_t) if callable(ws_getter) else None
        if ws_sample is not None:
            px, recv_ts, oracle_ts, msg_cnt, tot_bytes = ws_sample
            if self._is_valid_btc_price(px):
                ref_ts = float(oracle_ts) if (oracle_ts > 0.0 and abs(recv_ts - oracle_ts) <= 5.0) else float(recv_ts)
                lock = getattr(self, "_poly_lock", None)
                if lock:
                    lock.acquire()
                try:
                    self._last_poly_btc = float(px)
                    self._last_poly_btc_t = float(recv_ts)
                    self._last_poly_btc_req_t = ref_ts
                    if ref_ts > float(getattr(self, "_last_poly_srv_ts", 0.0) or 0.0):
                        self._last_poly_srv_ts = ref_ts
                finally:
                    if lock:
                        lock.release()
                self._chainlink_ws_active = True
                self.last_poly_btc_ts = ref_ts
                prev_cnt = int(getattr(self, "_last_cl_ws_msg_count", 0))
                if msg_cnt != prev_cnt:
                    prev_bytes = int(getattr(self, "_last_cl_ws_bytes", 0))
                    delta_bytes = max(0, tot_bytes - prev_bytes) if tot_bytes >= prev_bytes else max(0, tot_bytes)
                    self._last_cl_ws_msg_count = msg_cnt
                    self._last_cl_ws_bytes = tot_bytes
                    ws_rtt = float(getattr(trade_ws, "rtt_ms", 0.0) or 0.0)
                    self._record_net("chainlink", ws_rtt, delta_bytes)
                    self.record_api_schema_ok("poly_twap")
                    self.record_raw_event("oracle", {
                        "t": float(recv_ts),
                        "ref_t": ref_ts,
                        "kind": "poly_twap_live",
                        "price": float(px),
                        "source": "rtds_ws",
                    })
                return float(px)

        self._chainlink_ws_active = False
        mono_now = time.monotonic()
        if not getattr(self, "_poly_btc_poller_active", False) and (mono_now - getattr(self, "_last_poly_poll_t", 0.0) >= 0.30):
            self._poly_btc_poller_active = True
            self._poly_poll_req_t = current_t
            self._ensure_poly_poller()
            self._poly_poller_wake.set()

        lock = getattr(self, "_poly_lock", None)
        if lock:
            lock.acquire()
        try:
            px = getattr(self, "_last_poly_btc", None)
            recv_ts = getattr(self, "_last_poly_btc_t", 0.0)
            req_ts = getattr(self, "_last_poly_btc_req_t", 0.0)
        finally:
            if lock:
                lock.release()
        if px is None:
            self.last_poly_btc_ts = 0.0
            return None
        max_age = float(config.PARAMS.get("POLY_PRICE_MAX_AGE_S", 10.0))
        if (time.time() - recv_ts) > max_age:
            self.last_poly_btc_ts = 0.0
            return None
        # 对外暴露请求参考时刻用于严格对齐 60s TWAP 窗口（兼容仅注入 _last_poly_btc_t 的旧测试）
        self.last_poly_btc_ts = float(req_ts if req_ts > 0.0 else recv_ts)
        return px

    def _get_worker_session(self) -> requests.Session:
        """Return a thread-local requests.Session synced to the active proxy generation."""
        sess = getattr(self._thread_local, "session", None)
        cur_gen = getattr(self, "_proxy_gen", 0)
        if sess is None:
            sess = requests.Session()
            adapter = HTTPAdapter(pool_connections=6, pool_maxsize=6)
            sess.mount("https://", adapter)
            sess.mount("http://", adapter)
            sess.headers.update(self.session.headers)
            sess.proxies = dict(self.session.proxies)
            self._thread_local.session = sess
            self._thread_local.proxy_gen = cur_gen
            ws_lock = getattr(self, "_worker_sess_lock", None)
            if ws_lock is not None:
                with ws_lock:
                    self._worker_sessions.append(sess)
        elif getattr(self._thread_local, "proxy_gen", -1) != cur_gen:
            sess.proxies = dict(self.session.proxies)
            self._thread_local.proxy_gen = cur_gen
        return sess

    def close(self):
        """Shut down background executor, poller thread and all persistent HTTP sessions cleanly."""
        if getattr(self, "_poly_poller_stop", None) is not None:
            self._poly_poller_stop.set()
        if getattr(self, "_poly_poller_wake", None) is not None:
            self._poly_poller_wake.set()
        th = getattr(self, "_poly_poller_thread", None)
        if th is not None and th.is_alive():
            th.join(timeout=1.5)
        raw_th = getattr(self, "_raw_writer_thread", None)
        raw_q = getattr(self, "_raw_write_queue", None)
        if raw_th is not None and raw_th.is_alive() and raw_q is not None:
            try:
                raw_q.put(None, timeout=0.5)
            except Exception:
                pass
            raw_th.join(timeout=2.0)
        if getattr(self, "_executor", None) is not None:
            try:
                self._executor.shutdown(wait=False)
            except Exception:
                pass
        for s_attr in ("_poly_session", "session"):
            s_obj = getattr(self, s_attr, None)
            if s_obj is not None:
                try:
                    s_obj.close()
                except Exception:
                    pass
        ws_lock = getattr(self, "_worker_sess_lock", None)
        if ws_lock is not None:
            with ws_lock:
                for ws_sess in self._worker_sessions:
                    try:
                        ws_sess.close()
                    except Exception:
                        pass
                self._worker_sessions.clear()

    @staticmethod
    def _spot_currency_of(source: str) -> str:
        """Identify spot quote currency ('USD' vs 'USDT') to avoid false jumps across failover."""
        s = str(source or "").strip().lower()
        if "coinbase" in s or "kraken" in s:
            return "USD"
        if "binance" in s or "okx" in s:
            return "USDT"
        return ""

    def _fetch_live_spot_price(self, sess: requests.Session = None) -> Tuple[Optional[float], str, float, int]:
        """Fetch live BTC spot price with same-currency (USD/USDT) failover priority.

        Returns:
            (price, source_name, latency_ms, byte_count) or (None, 'stale', 0.0, 0)
        """
        http = sess or self.session
        spot_ws = getattr(self, "_spot_ws", None)
        active_prov = str(
            getattr(spot_ws, "active_provider", None)
            or config.PARAMS.get("SPOT_WS_PROVIDER", "binance")
            or "binance"
        ).strip().lower()
        base_endpoints = [
            ("binance_vision", "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT"),
            ("binance_com", "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"),
            ("okx", "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT"),
        ]
        cb_ep = ("coinbase", "https://api.exchange.coinbase.com/products/BTC-USD/ticker")
        kr_ep = ("kraken", "https://api.kraken.com/0/public/Ticker?pair=XBTUSD")
        # 若主源为 BTC/USD（Coinbase / Kraken），REST 降级优先互备同币种 USD 源，
        # 仅在双 USD 源均不可用时才降级至 USDT 源，防止瞬时混入 USD/USDT 溢价跳变。
        if active_prov == "kraken":
            endpoints = [kr_ep, cb_ep] + base_endpoints
        elif active_prov == "coinbase":
            endpoints = [cb_ep, kr_ep] + base_endpoints
        elif active_prov == "okx":
            endpoints = [("okx", "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT")] + base_endpoints[:2]
        else:
            endpoints = base_endpoints

        for src, url in endpoints:
            t0 = time.perf_counter()
            try:
                resp = http.get(url, timeout=(1.0, 1.5))
                lat_ms = (time.perf_counter() - t0) * 1000.0
                byte_count = len(resp.content)
                if resp.status_code == 200:
                    data = resp.json()
                    px = None
                    if src.startswith("binance"):
                        px = float(data.get("price", 0.0) or 0.0)
                    elif src == "okx":
                        rows = data.get("data") or []
                        if rows:
                            px = float(rows[0].get("last", 0.0) or 0.0)
                    elif src == "coinbase":
                        bid = float(data.get("bid", 0.0) or 0.0)
                        ask = float(data.get("ask", 0.0) or 0.0)
                        px = (bid + ask) * 0.5 if (bid > 0 and ask > 0) else float(data.get("price", 0.0) or 0.0)
                    elif src == "kraken":
                        result = data.get("result") or {}
                        pair_data = next(iter(result.values()), None) if isinstance(result, dict) else None
                        if isinstance(pair_data, dict):
                            b_arr = pair_data.get("b") or []
                            a_arr = pair_data.get("a") or []
                            c_arr = pair_data.get("c") or []
                            bid = float(b_arr[0]) if b_arr else 0.0
                            ask = float(a_arr[0]) if a_arr else 0.0
                            px = (bid + ask) * 0.5 if (bid > 0 and ask > 0) else (float(c_arr[0]) if c_arr else 0.0)
                    if px and self._is_valid_btc_price(px, prev_px=float(getattr(self, "_last_binance_px", 0.0) or 0.0)):
                        return px, src, lat_ms, byte_count
            except Exception:
                continue
        return None, "stale", 0.0, 0

    def _build_spot_state(
        self,
        open_price: float,
        t: float,
        live_px: Optional[float],
        src: str,
        lat_ms: float,
        byte_count: int,
        binance_open_price: float = None,
        poly_spot: float = None,
        spot_sample_ts: Optional[float] = None,
    ) -> SpotState:
        is_stale = False
        spot_ts = float(spot_sample_ts) if (spot_sample_ts is not None and float(spot_sample_ts) > 0.0) else t

        if live_px is not None and live_px > 0:
            current_px = live_px
            self._consecutive_spot_fails = 0
            cur_ccy = self._spot_currency_of(src)
            prev_ccy = getattr(self, "_last_spot_currency", "")
            if cur_ccy and prev_ccy and cur_ccy != prev_ccy:
                # 计价币种在 USD <-> USDT 间切换时，重置连续性基准时刻，防止把交易所溢价差误算成瞬时价格跳变
                self._spot_continuity_t = spot_ts
            if cur_ccy:
                self._last_spot_currency = cur_ccy
            self._last_binance_px = current_px
            self._last_binance_t = spot_ts
            self._last_spot_source = src
            self._record_net("binance", lat_ms, byte_count)
            # Avoid pushing duplicate identical timestamps when WS price hasn't ticked yet
            if not self.spot_history or spot_ts > self.spot_history[-1][0] + 1e-6:
                self.spot_history.append((spot_ts, current_px))
        else:
            is_stale = True
            self._consecutive_spot_fails = getattr(self, "_consecutive_spot_fails", 0) + 1
            if self._consecutive_spot_fails >= 3 and (t - getattr(self, "_last_reprobe_t", 0.0)) >= 30.0:
                if getattr(self, "_executor", None) is not None:
                    try:
                        self._executor.submit(self.reprobe_connectivity, t)
                    except Exception:
                        pass
            current_px = float(getattr(self, "_last_binance_px", 0.0) or 0.0)
            spot_ts = float(getattr(self, "_last_binance_t", 0.0) or 0.0)
            src = "stale"

        # Calculate 2-3s rapid shift velocity in bps (never span across a USD<->USDT currency failover or >5s outage)
        velocity_3s = 0.0
        cont_t = float(getattr(self, "_spot_continuity_t", 0.0) or 0.0)
        if current_px > 0 and not is_stale:
            for old_t, old_px in reversed(self.spot_history):
                if old_t < cont_t - 1e-9:
                    break
                dt_v = t - old_t
                if dt_v > 5.0:
                    break
                if dt_v >= 2.0:
                    if old_px > 0:
                        velocity_3s = ((current_px - old_px) / old_px) * 10000.0
                    break

        bin_open = binance_open_price or open_price
        return_bps = 0.0
        if bin_open > 0 and current_px > 0:
            return_bps = ((current_px - bin_open) / bin_open) * 10000.0

        toxic_thresh = float(config.PARAMS.get("TOXICITY_SHOCK_BPS", 3.5))
        is_shock = abs(velocity_3s) >= toxic_thresh

        vol_floor = float(config.PARAMS.get("MIN_MODEL_VOL_BPS", 14.0))
        if (
            len(self.spot_history) > 120
            and getattr(self, "_last_vol_calc_t", 0.0) > 0.0
            and 0.0 <= (t - self._last_vol_calc_t) < 1.0
        ):
            realized_vol_bps = getattr(self, "_cached_vol_bps", vol_floor)
        else:
            realized_vol_bps = calculate_dual_horizon_vol(
                list(self.spot_history),
                baseline_vol_bps=vol_floor,
            )
            self._cached_vol_bps = realized_vol_bps
            self._last_vol_calc_t = t
        _, _, regime = calculate_adaptive_spread_params(realized_vol_bps)

        if not is_stale and current_px > 0:
            prev_rec_t = getattr(self, "_last_spot_rec_t", 0.0)
            prev_rec_px = getattr(self, "_last_spot_rec_px", 0.0)
            if (t - prev_rec_t) >= 0.15 or abs(current_px - prev_rec_px) >= 0.10:
                self._last_spot_rec_t = t
                self._last_spot_rec_px = current_px
                self.record_raw_event("oracle", {
                    "t": round(t, 2), "kind": "spot", "price": current_px,
                    "source": src, "return_bps": round(return_bps, 2),
                    "realized_vol_bps": realized_vol_bps, "regime": regime
                })

        return SpotState(
            timestamp=spot_ts,
            price=current_px,
            open_price=open_price,
            return_bps=return_bps,
            realized_vol_bps=realized_vol_bps,
            is_toxic_shock=is_shock,
            regime=regime,
            poly_price=poly_spot,
            binance_open_price=binance_open_price,
            spot_source=src,
            is_stale=is_stale,
            velocity_bps=round(velocity_3s, 3),
        )

    def fetch_spot_state(
        self,
        open_price: float,
        now: float = None,
        binance_open_price: float = None,
        poly_spot: float = None
    ) -> SpotState:
        """Fetch live BTC/USDT spot, compute velocity, return bps, and dual-horizon volatility."""
        t = now or time.time()
        live_px, src, lat_ms, byte_count = self._fetch_live_spot_price()
        return self._build_spot_state(
            open_price, t, live_px, src, lat_ms, byte_count,
            binance_open_price=binance_open_price, poly_spot=poly_spot,
        )

    def _fetch_order_book_raw(
        self,
        token_id: str,
        sess: requests.Session = None,
        max_attempts: int = 2,
        timeout: tuple = (0.9, 1.2),
    ) -> Tuple[Optional[OrderBook], float, int, Optional[dict]]:
        http = sess or self.session
        url = f"https://clob.polymarket.com/book?token_id={token_id}"
        fail_lock = getattr(self, "_fail_lock", None)

        def _mark_poly_ok():
            if fail_lock is not None:
                with fail_lock:
                    self._consecutive_poly_fails = 0
            else:
                self._consecutive_poly_fails = 0

        def _mark_poly_fail():
            should_reprobe = False
            now_f = time.time()
            if fail_lock is not None:
                with fail_lock:
                    self._consecutive_poly_fails = getattr(self, "_consecutive_poly_fails", 0) + 1
                    if self._consecutive_poly_fails >= 4 and (now_f - getattr(self, "_last_reprobe_t", 0.0)) >= 30.0:
                        self._last_reprobe_t = now_f
                        should_reprobe = True
            else:
                self._consecutive_poly_fails = getattr(self, "_consecutive_poly_fails", 0) + 1
                if self._consecutive_poly_fails >= 4 and (now_f - getattr(self, "_last_reprobe_t", 0.0)) >= 30.0:
                    self._last_reprobe_t = now_f
                    should_reprobe = True
            if should_reprobe and getattr(self, "_executor", None) is not None:
                try:
                    self._executor.submit(self.reprobe_connectivity)
                except Exception:
                    pass

        for attempt in range(max(1, int(max_attempts))):
            t0 = time.perf_counter()
            try:
                resp = http.get(url, timeout=timeout)
                lat_ms = (time.perf_counter() - t0) * 1000.0
                byte_count = len(resp.content)
                if resp.status_code == 200:
                    data = resp.json()
                    if not isinstance(data, dict) or ("bids" not in data or "asks" not in data):
                        self.record_api_schema_error("clob_book", "missing bids/asks keys in CLOB response", threshold=3)
                        return None, lat_ms, byte_count, None
                    raw_bids = data.get("bids", [])
                    raw_asks = data.get("asks", [])
                    if not isinstance(raw_bids, list) or not isinstance(raw_asks, list):
                        self.record_api_schema_error("clob_book", "bids/asks are not lists", threshold=3)
                        return None, lat_ms, byte_count, None
                    last_trade_str = data.get("last_trade_price")
                    last_trade_px = float(last_trade_str) if last_trade_str is not None else None

                    bids_sorted = sorted(
                        [
                            (float(b["price"]), float(b["size"]))
                            for b in raw_bids
                            if math.isfinite(float(b["price"])) and math.isfinite(float(b["size"]))
                            and 0.0 < float(b["price"]) < 1.0 and float(b["size"]) > 0
                        ],
                        key=lambda x: x[0], reverse=True,
                    )
                    asks_sorted = sorted(
                        [
                            (float(a["price"]), float(a["size"]))
                            for a in raw_asks
                            if math.isfinite(float(a["price"])) and math.isfinite(float(a["size"]))
                            and 0.0 < float(a["price"]) < 1.0 and float(a["size"]) > 0
                        ],
                        key=lambda x: x[0],
                    )
                    self.record_api_schema_ok("clob_book")
                    _mark_poly_ok()
                    now_t = time.time()
                    raw_ev = {
                        "t": round(now_t, 2),
                        "token_id": token_id,
                        "best_bid": bids_sorted[0][0] if bids_sorted else None,
                        "best_ask": asks_sorted[0][0] if asks_sorted else None,
                        "bid_depth": bids_sorted[:5],
                        "ask_depth": asks_sorted[:5],
                        "last_trade_price": last_trade_px,
                    }
                    book = OrderBook(
                        bids=bids_sorted,
                        asks=asks_sorted,
                        last_trade_price=last_trade_px,
                        source_t=now_t,
                        received_at=now_t,
                    )
                    return book, lat_ms, byte_count, raw_ev
                # Retry once on transient rate-limit or 5xx upstream errors (respect Retry-After on 429)
                if resp.status_code == 429 and attempt + 1 < max_attempts:
                    ra = resp.headers.get("Retry-After")
                    wait_s = 0.5
                    if ra:
                        try:
                            wait_s = min(3.0, max(0.2, float(ra)))
                        except ValueError:
                            wait_s = 0.5
                    time.sleep(wait_s)
                    continue
                if resp.status_code in (500, 502, 503, 504) and attempt + 1 < max_attempts:
                    time.sleep(0.15)
                    continue
                if resp.status_code in (400, 410, 422):
                    self.record_api_schema_error("clob_book", f"HTTP {resp.status_code}", threshold=3)
                _mark_poly_fail()
                return None, lat_ms, byte_count, None
            except Exception:
                if attempt + 1 < max_attempts:
                    time.sleep(0.10)
                    continue
                _mark_poly_fail()
                return None, 0.0, 0, None
        return None, 0.0, 0, None

    def fetch_order_book(
        self,
        token_id: str,
        outcome_name: str = "token",
        use_worker_session: bool = False,
        max_attempts: int = 2,
        timeout: tuple = (0.9, 1.2),
    ) -> OrderBook:
        """Fetch Polymarket CLOB order book and last_trade_price (thread-safe when use_worker_session=True)."""
        sess = self._get_worker_session() if use_worker_session else None
        book, lat_ms, byte_count, raw_ev = self._fetch_order_book_raw(
            token_id, sess=sess, max_attempts=max_attempts, timeout=timeout
        )
        if lat_ms > 0:
            self._record_net("poly", lat_ms, byte_count)
        if book is not None and raw_ev is not None:
            raw_ev["outcome"] = outcome_name
            self.record_raw_event("events", raw_ev)
            return book
        return OrderBook(bids=[], asks=[], last_trade_price=None,
                         source_t=0.0, received_at=0.0)

    def attach_ws_clients(self, spot_ws=None, book_ws=None, trade_ws=None):
        """Attach optional real-time WebSocket clients for low-latency spot, book & trades."""
        self._spot_ws = spot_ws
        self._book_ws = book_ws
        self._trade_ws = trade_ws

    def fetch_tick_bundle(
        self,
        open_price: float,
        binance_open_price: float,
        token_up: str,
        token_down: str,
        now: float = None,
        poly_spot: float = None,
    ) -> Tuple[SpotState, OrderBook, OrderBook]:
        """Fetch BTC spot + Up book + Down book using WS push first, HTTP REST fallback."""
        t = now or time.time()
        spot_ws = getattr(self, "_spot_ws", None)
        book_ws = getattr(self, "_book_ws", None)

        ws_spot = spot_ws.get_latest(max_age_s=2.0, now=t) if spot_ws is not None else None
        ws_up = book_ws.get_order_book(token_up, max_age_s=3.0, now=t) if book_ws is not None else None
        ws_dn = book_ws.get_order_book(token_down, max_age_s=3.0, now=t) if book_ws is not None else None
        # 当 WS 盘口双边恢复有效推送时，解除此前 HTTP fallback 触发的 clob_book 暂停自锁
        if (
            ws_up is not None and ws_dn is not None
            and (ws_up.best_bid is not None or ws_up.best_ask is not None)
            and (ws_dn.best_bid is not None or ws_dn.best_ask is not None)
        ):
            self.record_api_schema_ok("clob_book")

        f_spot = (
            self._executor.submit(lambda: self._fetch_live_spot_price(self._get_worker_session()))
            if ws_spot is None else None
        )
        f_up = (
            self._executor.submit(lambda: self._fetch_order_book_raw(token_up, self._get_worker_session()))
            if ws_up is None else None
        )
        f_dn = (
            self._executor.submit(lambda: self._fetch_order_book_raw(token_down, self._get_worker_session()))
            if ws_dn is None else None
        )

        spot_sample_ts = None
        if ws_spot is not None:
            live_px, ws_ts, src = ws_spot
            spot_sample_ts = float(ws_ts)
            spot_lat = float(getattr(spot_ws, "rtt_ms", 0.0) or 0.0)
            spot_bytes = 96
        else:
            try:
                live_px, src, spot_lat, spot_bytes = f_spot.result(timeout=2.5)
            except Exception:
                live_px, src, spot_lat, spot_bytes = None, "stale", 0.0, 0

        def _book_to_raw_ev(book: OrderBook, tok: str) -> Optional[dict]:
            # Record every top-5 L2 change immediately (0ms delay), with 2 Hz heartbeat on quiet ticks
            rec_cache = getattr(self, "_last_ws_book_rec", None)
            if rec_cache is None:
                rec_cache = {}
                self._last_ws_book_rec = rec_cache
            fp = (tuple(book.bids[:5]), tuple(book.asks[:5]), book.last_trade_price)
            prev_fp, prev_rec_t = rec_cache.get(tok, (None, 0.0))
            if fp == prev_fp and (t - prev_rec_t) < 0.5:
                return None
            rec_cache[tok] = (fp, t)
            if len(rec_cache) > 32:
                oldest_tok = min(rec_cache, key=lambda k: rec_cache[k][1])
                if oldest_tok != tok:
                    rec_cache.pop(oldest_tok, None)
            return {
                "t": round(t, 2),
                "token_id": tok,
                "best_bid": book.best_bid,
                "best_ask": book.best_ask,
                "bid_depth": book.bids[:5],
                "ask_depth": book.asks[:5],
                "last_trade_price": book.last_trade_price,
            }

        ws_book_rtt = float(getattr(book_ws, "rtt_ms", 0.0) or 0.0) if book_ws is not None else 0.0
        if ws_up is not None:
            up_book, up_lat, up_bytes, up_ev = ws_up, ws_book_rtt, 256, _book_to_raw_ev(ws_up, token_up)
        else:
            try:
                up_book, up_lat, up_bytes, up_ev = f_up.result(timeout=2.5)
            except Exception:
                up_book, up_lat, up_bytes, up_ev = None, 0.0, 0, None

        if ws_dn is not None:
            dn_book, dn_lat, dn_bytes, dn_ev = ws_dn, ws_book_rtt, 256, _book_to_raw_ev(ws_dn, token_down)
        else:
            try:
                dn_book, dn_lat, dn_bytes, dn_ev = f_dn.result(timeout=2.5)
            except Exception:
                dn_book, dn_lat, dn_bytes, dn_ev = None, 0.0, 0, None

        spot = self._build_spot_state(
            open_price, t, live_px, src, spot_lat, spot_bytes,
            binance_open_price=binance_open_price, poly_spot=poly_spot,
            spot_sample_ts=spot_sample_ts,
        )

        def _finalize_book(book, lat_ms, byte_count, raw_ev, outcome):
            if lat_ms > 0:
                self._record_net("poly", lat_ms, byte_count)
            if book is not None:
                if raw_ev is not None:
                    raw_ev["outcome"] = outcome
                    self.record_raw_event("events", raw_ev)
                return book
            return OrderBook(bids=[], asks=[], last_trade_price=None, source_t=0.0, received_at=0.0)

        return (
            spot,
            _finalize_book(up_book, up_lat, up_bytes, up_ev, "Up"),
            _finalize_book(dn_book, dn_lat, dn_bytes, dn_ev, "Down"),
        )

    def verify_official_settlement(
        self,
        slug: str,
        sess: requests.Session = None,
        timeout=4.0,
        quiet: bool = False,
    ) -> Optional[dict]:
        """Query Gamma API to obtain officially resolved outcome truth.
        
        Returns:
            dict with 'winner' ('Up' or 'Down'), 'payouts', 'verified': True
            or None if market is not yet resolved by UMA/Chainlink.
        """
        http = sess or self.session
        url = f"https://gamma-api.polymarket.com/events?slug={slug}"
        try:
            resp = http.get(url, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                if data and "markets" in data[0] and data[0]["markets"]:
                    m = data[0]["markets"][0]
                    closed = m.get("closed") is True
                    uma_status = m.get("umaResolutionStatus")
                    outcomes = m.get("outcomes", [])
                    outcome_prices = m.get("outcomePrices", [])
                    if isinstance(outcomes, str):
                        outcomes = json.loads(outcomes)
                    if isinstance(outcome_prices, str):
                        outcome_prices = json.loads(outcome_prices)

                    # Official resolution condition: closed and outcomePrices has 1 and 0
                    if closed or uma_status == "resolved":
                        payouts = [float(p) for p in outcome_prices]
                        if len(payouts) == 2 and sorted(payouts) == [0.0, 1.0]:
                            winner = outcomes[payouts.index(1.0)]
                            res = {
                                "slug": slug,
                                "winner": winner,
                                "payouts": payouts,
                                "condition_id": m.get("conditionId"),
                                "verified": True,
                                "resolved_at": time.time()
                            }
                            # Save official settlement truth permanently
                            self.record_raw_event("settlements", res)
                            return res
        except Exception as e:
            if not quiet:
                print(f"[MarketFeed] Settlement verification notice ({slug}): {e}")

        return None
