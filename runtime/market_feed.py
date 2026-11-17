"""PM-Quant Market Data Feed, Recorder, and Settlement Verifier.

Handles:
1. Binance BTC spot streaming and 5m open price resolution.
2. Polymarket 5m event discovery and CLOB book & last_trade_price parsing.
3. 7x24 immutable raw market stream recording into data/1_market/.
4. Gamma API official settlement truth verification.
"""
import os
import sys
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


class MarketFeed:
    def __init__(self, proxy: str = None):
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=12, pool_maxsize=12)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

        # Configure proxy fallback
        self.proxy = proxy if proxy is not None else os.environ.get("PM_PROXY", "http://127.0.0.1:7897")
        self._test_connectivity()

        self.slug_cache = {}
        # 30-minute rolling spot buffer (~3600 ticks at 0.5s cadence) for dual-horizon volatility
        self.spot_history = deque(maxlen=3600)
        self._last_binance_px = 0.0
        self._last_binance_t = 0.0
        self._last_spot_source = "binance_vision"
        self._thread_local = threading.local()
        self._net_lock = threading.Lock()
        self._poly_lock = threading.Lock()
        self._record_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pm_feed")
        self.last_open_is_official = False

        # Polymarket official real-time Chainlink price cache & poller state
        self._last_poly_btc = None
        self._last_poly_btc_t = 0.0
        self._last_poly_poll_t = 0.0
        self._poly_btc_poller_active = False
        self._poly_session = None

        # Real-time network speed, latency, and traffic accounting
        self.net_stats = {
            "poly_latency_ms": 0.0,
            "binance_latency_ms": 0.0,
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
            if source == "poly":
                self.net_stats["poly_latency_ms"] = round(lat_ms, 1)
            elif source == "binance":
                self.net_stats["binance_latency_ms"] = round(lat_ms, 1)

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
        avg_lat = round((poly_lat + binance_lat) / 2.0, 1) if (poly_lat and binance_lat) else (poly_lat or binance_lat)
        return {
            "poly_latency_ms": poly_lat,
            "binance_latency_ms": binance_lat,
            "avg_latency_ms": avg_lat,
            "downstream_kbps": self.net_stats.get("downstream_kbps", 0.0),
            "total_mb": round(self.net_stats.get("total_bytes", 0) / (1024 * 1024), 2),
            "msg_rate": self.net_stats.get("msg_rate", 0.0)
        }

    def _test_connectivity(self):
        """Verify whether direct connection or proxy works best for both spot & CLOB."""
        spot_test = "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT"
        clob_test = "https://clob.polymarket.com/time"
        # Prefer direct connection if both endpoints succeed without proxy (lowest latency)
        try:
            r1 = self.session.get(spot_test, timeout=2.5)
            r2 = self.session.get(clob_test, timeout=2.5)
            if r1.status_code == 200 and r2.status_code == 200:
                self.session.proxies = {}
                return
        except Exception:
            pass
        if self.proxy:
            try:
                px_cfg = {"http": self.proxy, "https": self.proxy}
                r1 = self.session.get(spot_test, proxies=px_cfg, timeout=3.0)
                r2 = self.session.get(clob_test, proxies=px_cfg, timeout=3.0)
                if r1.status_code == 200 and r2.status_code == 200:
                    self.session.proxies = px_cfg
                    return
            except Exception:
                pass
        self.session.proxies = {}

    def record_raw_event(self, category: str, record: dict):
        """Append an immutable record to data/1_market/<category>/<date>/."""
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
                if data and "markets" in data[0] and data[0]["markets"]:
                    m = data[0]["markets"][0]
                    raw_tokens = m.get("clobTokenIds", [])
                    outcomes = m.get("outcomes", [])
                    if isinstance(raw_tokens, str):
                        raw_tokens = json.loads(raw_tokens)
                    if isinstance(outcomes, str):
                        outcomes = json.loads(outcomes)

                    token_up = None
                    token_down = None
                    for outcome, tid in zip(outcomes, raw_tokens):
                        if outcome.lower() == "up":
                            token_up = tid
                        elif outcome.lower() == "down":
                            token_down = tid

                    if token_up and token_down:
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
        except Exception as e:
            print(f"[MarketFeed] Token resolution warning for {slug}: {e}")
            
        return {}

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
            iso_start = datetime.datetime.fromtimestamp(start_t, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
            iso_end = datetime.datetime.fromtimestamp(start_t + 300, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
            poly_url = "https://polymarket.com/api/crypto/crypto-price"
            params = {
                "symbol": "BTC",
                "eventStartTime": iso_start,
                "variant": "fiveminute",
                "endDate": iso_end,
                "twapEnabled": "true",
                "twapLookbackSeconds": "60"
            }
            resp = self.session.get(poly_url, params=params, timeout=(1.2, 1.8))
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
            iso_start = datetime.datetime.fromtimestamp(start_t, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
            iso_end = datetime.datetime.fromtimestamp(start_t + 300, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
            poly_url = "https://polymarket.com/api/crypto/crypto-price"
            params = {
                "symbol": "BTC",
                "eventStartTime": iso_start,
                "variant": "fiveminute",
                "endDate": iso_end,
                "twapEnabled": "true",
                "twapLookbackSeconds": "60"
            }
            resp = self.session.get(poly_url, params=params, timeout=(1.2, 2.0))
            if resp.status_code == 200:
                body = resp.json()
                if body.get("completed") and body.get("closePrice") is not None:
                    open_px = float(body.get("openPrice", 0))
                    close_px = float(body.get("closePrice", 0))
                    winner = "Up" if close_px >= open_px else "Down"
                    return open_px, close_px, winner
        except Exception:
            pass
        return None

    def get_polymarket_btc_price(self, now: float = None) -> Optional[float]:
        """Fetch Polymarket official real-time Chainlink BTC/USD TWAP asynchronously.

        Returns None when the cached TWAP is stale: a dead poller must not keep
        feeding an old price into basis_ema while Binance keeps moving.
        """
        current_t = now or time.time()
        if not getattr(self, "_poly_btc_poller_active", False) and (current_t - getattr(self, "_last_poly_poll_t", 0.0) >= 0.8):
            self._poly_btc_poller_active = True
            import threading
            def _worker():
                try:
                    # Reuse a dedicated persistent Session owned exclusively by this single-flight poller
                    # to avoid repeating TLS handshakes every 0.8s while keeping requests thread-safe.
                    if getattr(self, "_poly_session", None) is None:
                        sess = requests.Session()
                        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4)
                        sess.mount("https://", adapter)
                        sess.mount("http://", adapter)
                        sess.headers.update(self.session.headers)
                        sess.proxies = dict(self.session.proxies)
                        self._poly_session = sess
                    now_iso = datetime.datetime.fromtimestamp(current_t, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
                    url = "https://polymarket.com/api/crypto/crypto-price"
                    params = {
                        "symbol": "BTC",
                        "eventStartTime": now_iso,
                        "variant": "fiveminute",
                        "twapEnabled": "true",
                        "twapLookbackSeconds": "60"
                    }
                    resp = self._poly_session.get(url, params=params, timeout=(1.2, 1.8))
                    if resp.status_code == 200:
                        body = resp.json()
                        px = body.get("openPrice")
                        if px and float(px) > 0:
                            lock = getattr(self, "_poly_lock", None)
                            if lock:
                                lock.acquire()
                            try:
                                self._last_poly_btc = float(px)
                                self._last_poly_btc_t = time.time()
                            finally:
                                if lock:
                                    lock.release()
                            self.record_raw_event("oracle", {
                                "t": time.time(), "kind": "poly_twap_live", "price": float(px)
                            })
                except Exception:
                    pass
                finally:
                    self._last_poly_poll_t = time.time()
                    self._poly_btc_poller_active = False

            t = threading.Thread(target=_worker, daemon=True)
            t.start()

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
            return None
        max_age = float(config.PARAMS.get("POLY_PRICE_MAX_AGE_S", 10.0))
        if (time.time() - ts) > max_age:
            return None
        return px

    def _get_worker_session(self) -> requests.Session:
        """Return a thread-local requests.Session for safe concurrent HTTP fetching."""
        sess = getattr(self._thread_local, "session", None)
        if sess is None:
            sess = requests.Session()
            adapter = HTTPAdapter(pool_connections=6, pool_maxsize=6)
            sess.mount("https://", adapter)
            sess.mount("http://", adapter)
            sess.headers.update(self.session.headers)
            sess.proxies = dict(self.session.proxies)
            self._thread_local.session = sess
        return sess

    def close(self):
        """Shut down background executor and persistent poller session cleanly."""
        if getattr(self, "_executor", None) is not None:
            try:
                self._executor.shutdown(wait=False)
            except Exception:
                pass
        if getattr(self, "_poly_session", None) is not None:
            try:
                self._poly_session.close()
            except Exception:
                pass

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
                    if px and px > 0:
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
    ) -> SpotState:
        is_stale = False
        spot_ts = t

        if live_px is not None and live_px > 0:
            current_px = live_px
            self._last_binance_px = current_px
            self._last_binance_t = t
            self._last_spot_source = src
            self._record_net("binance", lat_ms, byte_count)
            self.spot_history.append((t, current_px))
        else:
            is_stale = True
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

        realized_vol_bps = calculate_dual_horizon_vol(
            list(self.spot_history),
            baseline_vol_bps=float(config.PARAMS.get("MIN_MODEL_VOL_BPS", 14.0)),
        )
        _, _, regime = calculate_adaptive_spread_params(realized_vol_bps)

        if not is_stale and current_px > 0:
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

    def _fetch_order_book_raw(self, token_id: str, sess: requests.Session = None) -> Tuple[Optional[OrderBook], float, int, Optional[dict]]:
        http = sess or self.session
        url = f"https://clob.polymarket.com/book?token_id={token_id}"
        t0 = time.perf_counter()
        try:
            resp = http.get(url, timeout=(1.0, 1.5))
            lat_ms = (time.perf_counter() - t0) * 1000.0
            byte_count = len(resp.content)
            if resp.status_code == 200:
                data = resp.json()
                raw_bids = data.get("bids", [])
                raw_asks = data.get("asks", [])
                last_trade_str = data.get("last_trade_price")
                last_trade_px = float(last_trade_str) if last_trade_str is not None else None

                bids_sorted = sorted(
                    [(float(b["price"]), float(b["size"])) for b in raw_bids if float(b["size"]) > 0],
                    key=lambda x: x[0], reverse=True,
                )
                asks_sorted = sorted(
                    [(float(a["price"]), float(a["size"])) for a in raw_asks if float(a["size"]) > 0],
                    key=lambda x: x[0],
                )
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
            return None, lat_ms, byte_count, None
        except Exception:
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

    def fetch_tick_bundle(
        self,
        open_price: float,
        binance_open_price: float,
        token_up: str,
        token_down: str,
        now: float = None,
        poly_spot: float = None,
    ) -> Tuple[SpotState, OrderBook, OrderBook]:
        """Concurrently fetch BTC spot + Up book + Down book (1x RTT instead of 3x RTT)."""
        t = now or time.time()
        f_spot = self._executor.submit(lambda: self._fetch_live_spot_price(self._get_worker_session()))
        f_up = self._executor.submit(lambda: self._fetch_order_book_raw(token_up, self._get_worker_session()))
        f_dn = self._executor.submit(lambda: self._fetch_order_book_raw(token_down, self._get_worker_session()))

        live_px, src, spot_lat, spot_bytes = f_spot.result()
        up_book, up_lat, up_bytes, up_ev = f_up.result()
        dn_book, dn_lat, dn_bytes, dn_ev = f_dn.result()

        spot = self._build_spot_state(
            open_price, t, live_px, src, spot_lat, spot_bytes,
            binance_open_price=binance_open_price, poly_spot=poly_spot,
        )

        def _finalize_book(book, lat_ms, byte_count, raw_ev, outcome):
            if lat_ms > 0:
                self._record_net("poly", lat_ms, byte_count)
            if book is not None and raw_ev is not None:
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
