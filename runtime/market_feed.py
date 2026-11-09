"""PM-Quant Market Data Feed & Polymarket 5-Minute Market Resolver.

Handles Binance spot price streaming, Polymarket 5m event discovery,
CLOB book parsing, and resilient proxy fallback.
"""
import os
import sys
import time
import json
import requests
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.types import BookLevel, OrderBook, SpotState


class MarketFeed:
    def __init__(self, proxy: str = None):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        
        # Configure proxy fallback
        self.proxy = proxy if proxy is not None else os.environ.get("PM_PROXY", "http://127.0.0.1:7897")
        self._test_connectivity()
        
        self.slug_cache = {}
        self.spot_history = deque(maxlen=30)  # (timestamp, price) history for 3s velocity calculation

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
        # Try direct
        try:
            r = self.session.get(test_url, timeout=3.0)
            if r.status_code == 200:
                self.session.proxies = {}
        except Exception:
            pass

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
                            "token_down": token_down
                        }
                        self.slug_cache[slug] = meta
                        return meta
        except Exception as e:
            print(f"[MarketFeed] Token resolution warning for {slug}: {e}")
            
        return {}

    def fetch_window_open_price(self, start_t: float) -> float:
        """Fetch BTC spot price at the start of the 5-minute window via Kline."""
        start_ms = int(start_t * 1000)
        url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m&startTime={start_ms}&limit=1"
        try:
            resp = self.session.get(url, timeout=3.0)
            if resp.status_code == 200:
                klines = resp.json()
                if klines and len(klines) > 0:
                    return float(klines[0][1])  # Open price
        except Exception:
            pass
        return 0.0

    def fetch_spot_state(self, open_price: float, now: float = None) -> SpotState:
        """Fetch current Binance BTC spot and calculate velocity & price change timing."""
        t = now or time.time()
        url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        current_px = open_price
        try:
            resp = self.session.get(url, timeout=2.0)
            if resp.status_code == 200:
                current_px = float(resp.json().get("price", open_price))
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

        # Calculate return in bps from window open
        return_bps = 0.0
        if open_price > 0:
            return_bps = ((current_px - open_price) / open_price) * 10000.0

        is_shock = abs(velocity_3s) >= config.PARAMS["TOXICITY_SHOCK_BPS"]

        return SpotState(
            timestamp=t,
            price=current_px,
            open_price=open_price,
            return_bps=return_bps,
            realized_vol_bps=15.0,
            is_toxic_shock=is_shock
        )

    def fetch_order_book(self, token_id: str) -> OrderBook:
        """Fetch Polymarket CLOB order book for a given token."""
        url = f"https://clob.polymarket.com/book?token_id={token_id}"
        try:
            resp = self.session.get(url, timeout=2.0)
            if resp.status_code == 200:
                data = resp.json()
                raw_bids = data.get("bids", [])
                raw_asks = data.get("asks", [])

                bids = [(float(b["price"]), float(b["size"])) for b in raw_bids if float(b["size"]) > 0]
                asks = [(float(a["price"]), float(a["size"])) for a in raw_asks if float(a["size"]) > 0]

                # Bids sorted descending (highest bid first)
                bids_sorted = sorted(bids, key=lambda x: x[0], reverse=True)
                # Asks sorted ascending (lowest ask first)
                asks_sorted = sorted(asks, key=lambda x: x[0])

                return OrderBook(
                    bids=bids_sorted,
                    asks=asks_sorted,
                    received_at=time.time()
                )
        except Exception:
            pass

        return OrderBook(bids=[], asks=[], received_at=time.time())
