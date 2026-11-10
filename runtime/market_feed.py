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
from collections import deque
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.types import BookLevel, OrderBook, SpotState
from core.estimator import calculate_dynamic_realized_vol, calculate_adaptive_spread_params


class MarketFeed:
    def __init__(self, proxy: str = None):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        
        # Configure proxy fallback
        self.proxy = proxy if proxy is not None else os.environ.get("PM_PROXY", "http://127.0.0.1:7897")
        self._test_connectivity()
        
        self.slug_cache = {}
        self.spot_history = deque(maxlen=60)  # (timestamp, price) history for velocity & volatility

        # Polymarket official real-time Chainlink price cache & poller state
        self._last_poly_btc = None
        self._last_poly_btc_t = 0.0
        self._last_poly_poll_t = 0.0
        self._poly_btc_poller_active = False

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
        """Verify whether proxy or direct connection works best."""
        test_url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        if self.proxy:
            try:
                r = self.session.get(test_url, proxies={"http": self.proxy, "https": self.proxy}, timeout=2.5)
                if r.status_code == 200:
                    self.session.proxies = {"http": self.proxy, "https": self.proxy}
                    return
            except Exception:
                pass
        # Fallback to direct connection
        try:
            r = self.session.get(test_url, timeout=3.0)
            if r.status_code == 200:
                self.session.proxies = {}
        except Exception:
            pass

    def record_raw_event(self, category: str, record: dict):
        """Append an immutable record to data/1_market/<category>/<date>/."""
        try:
            today = time.strftime("%Y-%m-%d", time.gmtime())
            target_dir = config.MARKET_DIR / category / today
            target_dir.mkdir(parents=True, exist_ok=True)
            target_file = target_dir / f"{category}_{today}.jsonl"
            with open(target_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
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
            resp = self.session.get(poly_url, params=params, timeout=3.5)
            if resp.status_code == 200:
                body = resp.json()
                open_px = body.get("openPrice")
                if open_px and float(open_px) > 0:
                    poly_open = float(open_px)
        except Exception:
            pass

        # 1b. If poly_open still 0, stitch from previous round's closePrice (mathematically identical to start_t TWAP)
        if poly_open <= 0:
            prev_settlement = self.fetch_window_settlement_price(start_t - 300)
            if prev_settlement and prev_settlement[1] > 0:
                poly_open = float(prev_settlement[1])

        # 2. Binance Spot 5m Kline Open
        start_ms = int(start_t * 1000)
        url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m&startTime={start_ms}&limit=1"
        try:
            resp = self.session.get(url, timeout=3.0)
            if resp.status_code == 200:
                klines = resp.json()
                if klines and len(klines) > 0:
                    binance_open = float(klines[0][1])
        except Exception:
            pass

        # Fallback cross-assignment if either is missing
        if poly_open <= 0 and binance_open > 0:
            poly_open = binance_open
        if binance_open <= 0 and poly_open > 0:
            binance_open = poly_open

        # Record raw open events
        if poly_open > 0:
            self.record_raw_event("oracle", {
                "t": time.time(), "kind": "window_open", "source": "polymarket_chainlink_twap",
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
            resp = self.session.get(poly_url, params=params, timeout=3.5)
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
        feeding an old price into p_fair while Binance keeps moving.
        """
        current_t = now or time.time()
        if not getattr(self, "_poly_btc_poller_active", False) and (current_t - getattr(self, "_last_poly_poll_t", 0.0) >= 1.8):
            self._poly_btc_poller_active = True
            import threading
            def _worker():
                # Dedicated session: requests.Session is not thread-safe and this
                # worker runs concurrently with the pipeline's own requests.
                session = None
                try:
                    session = requests.Session()
                    session.headers.update(self.session.headers)
                    session.proxies = dict(self.session.proxies)
                    now_iso = datetime.datetime.fromtimestamp(current_t, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
                    url = "https://polymarket.com/api/crypto/crypto-price"
                    params = {
                        "symbol": "BTC",
                        "eventStartTime": now_iso,
                        "variant": "fiveminute",
                        "twapEnabled": "true",
                        "twapLookbackSeconds": "60"
                    }
                    resp = session.get(url, params=params, timeout=3.0)
                    if resp.status_code == 200:
                        body = resp.json()
                        px = body.get("openPrice")
                        if px and float(px) > 0:
                            self._last_poly_btc = float(px)
                            self._last_poly_btc_t = time.time()
                            self.record_raw_event("oracle", {
                                "t": time.time(), "kind": "poly_twap_live", "price": self._last_poly_btc
                            })
                except Exception:
                    pass
                finally:
                    if session is not None:
                        try:
                            session.close()
                        except Exception:
                            pass
                    self._last_poly_poll_t = time.time()
                    self._poly_btc_poller_active = False

            t = threading.Thread(target=_worker, daemon=True)
            t.start()

        px = getattr(self, "_last_poly_btc", None)
        if px is None:
            return None
        max_age = float(config.PARAMS.get("POLY_PRICE_MAX_AGE_S", 10.0))
        if (time.time() - getattr(self, "_last_poly_btc_t", 0.0)) > max_age:
            return None
        return px

    def fetch_spot_state(
        self,
        open_price: float,
        now: float = None,
        binance_open_price: float = None,
        poly_spot: float = None
    ) -> SpotState:
        """Fetch current Binance BTC spot, compute 3s velocity, return bps, and rolling volatility."""
        t = now or time.time()
        url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        current_px = open_price
        t0 = time.perf_counter()
        byte_count = 0
        try:
            resp = self.session.get(url, timeout=2.0)
            lat_ms = (time.perf_counter() - t0) * 1000.0
            byte_count = len(resp.content)
            if resp.status_code == 200:
                current_px = float(resp.json().get("price", open_price))
            self._record_net("binance", lat_ms, byte_count)
        except Exception:
            pass

        self.spot_history.append((t, current_px))

        # Calculate 3-second rapid shift velocity in bps
        velocity_3s = 0.0
        for old_t, old_px in reversed(self.spot_history):
            if (t - old_t) >= 2.5:
                if old_px > 0:
                    velocity_3s = ((current_px - old_px) / old_px) * 10000.0
                break

        # Calculate return in bps from window open (strictly Binance vs Binance open baseline)
        bin_open = binance_open_price or open_price
        return_bps = 0.0
        if bin_open > 0:
            return_bps = ((current_px - bin_open) / bin_open) * 10000.0

        is_shock = abs(velocity_3s) >= config.PARAMS["TOXICITY_SHOCK_BPS"]

        # Calculate high-frequency dynamic realized volatility (EWMA over rolling 60s)
        realized_vol_bps = calculate_dynamic_realized_vol(list(self.spot_history), baseline_vol_bps=15.0)
        _, _, regime = calculate_adaptive_spread_params(realized_vol_bps)

        # Record raw spot price to data/1_market/oracle/
        self.record_raw_event("oracle", {
            "t": round(t, 2), "kind": "spot", "price": current_px, "return_bps": round(return_bps, 2),
            "realized_vol_bps": realized_vol_bps, "regime": regime
        })

        return SpotState(
            timestamp=t,
            price=current_px,
            open_price=open_price,
            return_bps=return_bps,
            realized_vol_bps=realized_vol_bps,
            is_toxic_shock=is_shock,
            regime=regime,
            poly_price=poly_spot,
            binance_open_price=binance_open_price
        )

    def fetch_order_book(self, token_id: str, outcome_name: str = "token") -> OrderBook:
        """Fetch Polymarket CLOB order book and last_trade_price."""
        url = f"https://clob.polymarket.com/book?token_id={token_id}"
        t0 = time.perf_counter()
        try:
            resp = self.session.get(url, timeout=2.0)
            lat_ms = (time.perf_counter() - t0) * 1000.0
            byte_count = len(resp.content)
            self._record_net("poly", lat_ms, byte_count)
            if resp.status_code == 200:
                data = resp.json()
                raw_bids = data.get("bids", [])
                raw_asks = data.get("asks", [])
                last_trade_str = data.get("last_trade_price")
                last_trade_px = float(last_trade_str) if last_trade_str is not None else None

                bids = [(float(b["price"]), float(b["size"])) for b in raw_bids if float(b["size"]) > 0]
                asks = [(float(a["price"]), float(a["size"])) for a in raw_asks if float(a["size"]) > 0]

                # Bids sorted descending (highest bid first)
                bids_sorted = sorted(bids, key=lambda x: x[0], reverse=True)
                # Asks sorted ascending (lowest ask first)
                asks_sorted = sorted(asks, key=lambda x: x[0])

                # Record raw order book event to data/1_market/events/
                self.record_raw_event("events", {
                    "t": round(time.time(), 2),
                    "token_id": token_id,
                    "outcome": outcome_name,
                    "best_bid": bids_sorted[0][0] if bids_sorted else None,
                    "best_ask": asks_sorted[0][0] if asks_sorted else None,
                    "bid_depth": bids_sorted[:5],
                    "ask_depth": asks_sorted[:5],
                    "last_trade_price": last_trade_px
                })

                return OrderBook(
                    bids=bids_sorted,
                    asks=asks_sorted,
                    last_trade_price=last_trade_px,
                    received_at=time.time()
                )
        except Exception:
            pass

        return OrderBook(bids=[], asks=[], last_trade_price=None, received_at=time.time())

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
