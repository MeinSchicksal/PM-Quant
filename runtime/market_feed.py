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
import datetime
import requests
import threading
from concurrent.futures import ThreadPoolExecutor
from requests.adapters import HTTPAdapter
from collections import deque
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.types import OrderBook, SpotState
from core.estimator import calculate_dual_horizon_vol, calculate_adaptive_spread_params
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
        self._thread_local = threading.local()
        self._net_lock = threading.Lock()
        self._poly_lock = threading.Lock()
        self._record_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pm_feed")
        self.last_open_is_official = False

        # Polymarket official real-time Chainlink price cache & single persistent poller thread
        self._last_poly_btc = None
        self._last_poly_btc_t = 0.0
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
        return {
            "poly_latency_ms": poly_lat,
            "binance_latency_ms": binance_lat,
            "chainlink_latency_ms": cl_lat,
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
        """Measure host clock offset against Polymarket CLOB server time (local - server)."""
        http = sess or self.session
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
        """Verify whether direct connection or proxy works best for both spot & CLOB."""
        spot_test = "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT"
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
                    return
            except Exception:
                pass
        self.session.proxies = {}

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

    def record_raw_event(self, category: str, record: dict):
        """Append an immutable record to data/1_market/<category>/<date>/."""
        # Always preserve tiny official settlement records; heavy streams obey retention/disk-full guard
        if category != "settlements" and not is_raw_recording_allowed():
            return
        try:
            today = time.strftime("%Y-%m-%d", time.gmtime())
            target_dir = config.MARKET_DIR / category / today
            target_dir.mkdir(parents=True, exist_ok=True)
            target_file = target_dir / f"{category}_{today}.jsonl"
            line = json.dumps(record, ensure_ascii=False) + "\n"
            lock = getattr(self, "_record_lock", None)
            if lock is not None:
                with lock:
                    with open(target_file, "a", encoding="utf-8") as f:
                        f.write(line)
            else:
                with open(target_file, "a", encoding="utf-8") as f:
                    f.write(line)
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

        # 2. Binance Spot 5m Kline Open (prefer data-api.binance.vision to bypass HTTP 451 geo-blocks)
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
            self.record_raw_event("oracle", {
                "t": time.time(), "kind": "window_open", "source": "binance_spot",
                "start_t": start_t, "open_price": binance_open
            })

        return poly_open, binance_open

    def fetch_window_settlement_price(self, start_t: float) -> Optional[Tuple[float, float, str]]:
        """Fetch official settlement open, close, and winner from Polymarket's crypto-price API."""
        try:
            poly_url = "https://polymarket.com/api/crypto/crypto-price"
            resp = self.session.get(poly_url, params=self._poly_twap_params(start_t, include_end=True), timeout=(1.2, 2.0))
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
                    if self._is_valid_btc_price(px_f):
                        self.record_api_schema_ok("poly_twap")
                        self._record_net("chainlink", cl_lat_ms, len(resp.content))
                        lock = getattr(self, "_poly_lock", None)
                        if lock:
                            lock.acquire()
                        try:
                            self._last_poly_btc = px_f
                            self._last_poly_btc_t = time.time()
                        finally:
                            if lock:
                                lock.release()
                        self.record_raw_event("oracle", {
                            "t": time.time(), "kind": "poly_twap_live", "price": px_f
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
        """Fetch Polymarket official real-time Chainlink BTC/USD TWAP asynchronously.

        Returns None when the cached TWAP is stale: a dead poller must not keep
        feeding an old price into basis_ema while Binance keeps moving.
        """
        current_t = now or time.time()
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
            ts = getattr(self, "_last_poly_btc_t", 0.0)
        finally:
            if lock:
                lock.release()
        if px is None:
            self.last_poly_btc_ts = 0.0
            return None
        max_age = float(config.PARAMS.get("POLY_PRICE_MAX_AGE_S", 10.0))
        if (time.time() - ts) > max_age:
            self.last_poly_btc_ts = 0.0
            return None
        self.last_poly_btc_ts = float(ts)
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

    def _fetch_live_spot_price(self, sess: requests.Session = None) -> Tuple[Optional[float], str, float, int]:
        """Fetch live BTC/USDT spot price with multi-source failover.

        Returns:
            (price, source_name, latency_ms, byte_count) or (None, 'stale', 0.0, 0)
        """
        http = sess or self.session
        endpoints = (
            ("binance_vision", "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT"),
            ("binance_com", "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"),
            ("okx", "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT"),
        )
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

        # Calculate 2-3s rapid shift velocity in bps
        velocity_3s = 0.0
        if current_px > 0:
            for old_t, old_px in reversed(self.spot_history):
                if (t - old_t) >= 2.0:
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
    ) -> Tuple[Optional[OrderBook], float, int, Optional[dict]]:
        http = sess or self.session
        url = f"https://clob.polymarket.com/book?token_id={token_id}"
        for attempt in range(max(1, int(max_attempts))):
            t0 = time.perf_counter()
            try:
                resp = http.get(url, timeout=(0.9, 1.2))
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
                    self._consecutive_poly_fails = 0
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
                self._consecutive_poly_fails = getattr(self, "_consecutive_poly_fails", 0) + 1
                if self._consecutive_poly_fails >= 4 and (time.time() - getattr(self, "_last_reprobe_t", 0.0)) >= 30.0:
                    if getattr(self, "_executor", None) is not None:
                        try:
                            self._executor.submit(self.reprobe_connectivity)
                        except Exception:
                            pass
                return None, lat_ms, byte_count, None
            except Exception:
                if attempt + 1 < max_attempts:
                    time.sleep(0.10)
                    continue
                self._consecutive_poly_fails = getattr(self, "_consecutive_poly_fails", 0) + 1
                if self._consecutive_poly_fails >= 4 and (time.time() - getattr(self, "_last_reprobe_t", 0.0)) >= 30.0:
                    if getattr(self, "_executor", None) is not None:
                        try:
                            self._executor.submit(self.reprobe_connectivity)
                        except Exception:
                            pass
                return None, 0.0, 0, None
        return None, 0.0, 0, None

    def fetch_order_book(self, token_id: str, outcome_name: str = "token") -> OrderBook:
        """Fetch Polymarket CLOB order book and last_trade_price."""
        book, lat_ms, byte_count, raw_ev = self._fetch_order_book_raw(token_id)
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
            live_px, src, spot_lat, spot_bytes = f_spot.result()

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
            up_book, up_lat, up_bytes, up_ev = f_up.result()

        if ws_dn is not None:
            dn_book, dn_lat, dn_bytes, dn_ev = ws_dn, ws_book_rtt, 256, _book_to_raw_ev(ws_dn, token_down)
        else:
            dn_book, dn_lat, dn_bytes, dn_ev = f_dn.result()

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

    def verify_official_settlement(self, slug: str) -> Optional[dict]:
        """Query Gamma API to obtain officially resolved outcome truth.
        
        Returns:
            dict with 'winner' ('Up' or 'Down'), 'payouts', 'verified': True
            or None if market is not yet resolved by UMA/Chainlink.
        """
        url = f"https://gamma-api.polymarket.com/events?slug={slug}"
        try:
            resp = self.session.get(url, timeout=4.0)
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
            print(f"[MarketFeed] Settlement verification notice ({slug}): {e}")

        return None
