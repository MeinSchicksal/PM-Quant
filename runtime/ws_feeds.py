"""Low-Latency Real-Time WebSocket Feeds (Binance Spot + Polymarket CLOB L2 Book).

Provides millisecond-level push updates with automatic idle-watchdog reconnects
and clean fallback to HTTP REST in MarketFeed whenever a WS stream is warming up
or transiently disconnected.
"""
import asyncio
import json
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.types import OrderBook

BINANCE_WS_URLS = (
    "wss://data-stream.binance.vision/ws/btcusdt@bookTicker",
    "wss://stream.binance.com:9443/ws/btcusdt@bookTicker",
)
POLY_CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


class BinanceSpotWSClient:
    """Real-time BTC/USDT bookTicker WebSocket client with idle watchdog."""

    def __init__(self, proxy: Optional[str] = None, idle_reconnect_s: float = 10.0):
        self.proxy = proxy
        self.idle_reconnect_s = max(0.05, float(idle_reconnect_s))
        self.running = False
        self.connected = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()

        self._price: float = 0.0
        self._updated_t: float = 0.0
        self._source: str = "binance_ws"
        self.msg_count: int = 0
        self.reconnect_count: int = 0
        self.last_error: str = ""

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="BinanceSpotWS")
        self._thread.start()

    def stop(self):
        self.running = False
        self.connected = False
        if self._loop and self._loop.is_running():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def apply_message(self, payload: Dict[str, Any], now: Optional[float] = None) -> bool:
        """Parse one Binance bookTicker / trade JSON object and update latest mid."""
        if not isinstance(payload, dict):
            return False
        bid = _safe_float(payload.get("b"), 0.0)
        ask = _safe_float(payload.get("a"), 0.0)
        px = 0.0
        if bid > 0.0 and ask > 0.0:
            px = (bid + ask) * 0.5
        else:
            px = _safe_float(payload.get("p") or payload.get("c"), 0.0)
        if px <= 0.0:
            return False
        ts = time.time() if now is None else float(now)
        with self._lock:
            self._price = px
            self._updated_t = ts
            self.msg_count += 1
        return True

    def get_latest(self, max_age_s: float = 2.0, now: Optional[float] = None) -> Optional[Tuple[float, float, str]]:
        """Return (price, updated_ts, source) if fresh, else None."""
        t = time.time() if now is None else float(now)
        with self._lock:
            if self._price <= 0.0 or self._updated_t <= 0.0:
                return None
            if (t - self._updated_t) > max_age_s:
                return None
            return self._price, self._updated_t, self._source

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._ws_worker())
        except Exception:
            pass
        finally:
            self._loop.close()

    async def _ws_worker(self):
        import websockets

        url_idx = 0
        recv_timeout = min(2.0, self.idle_reconnect_s)
        while self.running:
            url = BINANCE_WS_URLS[url_idx % len(BINANCE_WS_URLS)]
            try:
                connect_kwargs: Dict[str, Any] = {
                    "ping_interval": 15,
                    "ping_timeout": 10,
                    "open_timeout": 5.0,
                }
                if self.proxy:
                    connect_kwargs["proxy"] = self.proxy
                async with websockets.connect(url, **connect_kwargs) as ws:
                    self.connected = True
                    last_activity_t = time.monotonic()
                    while self.running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                        except asyncio.TimeoutError:
                            raw = None

                        accepted = False
                        if raw and isinstance(raw, str) and raw.strip():
                            try:
                                accepted = self.apply_message(json.loads(raw))
                            except Exception:
                                accepted = False

                        if accepted:
                            last_activity_t = time.monotonic()
                        elif (time.monotonic() - last_activity_t) >= self.idle_reconnect_s:
                            raise TimeoutError(
                                f"BinanceSpotWS idle watchdog: no tickers for {self.idle_reconnect_s:.1f}s"
                            )
            except Exception as exc:
                self.connected = False
                self.reconnect_count += 1
                self.last_error = str(exc)
                url_idx += 1
                if self.running:
                    await asyncio.sleep(1.5)


class PolymarketBookWSClient:
    """Real-time Polymarket CLOB L2 orderbook WebSocket client."""

    def __init__(self, proxy: Optional[str] = None, idle_reconnect_s: float = 20.0):
        self.proxy = proxy
        self.idle_reconnect_s = max(0.05, float(idle_reconnect_s))
        self.running = False
        self.connected = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()

        self._active_tokens: Tuple[str, ...] = ()
        self._token_gen: int = 0
        # asset_id -> {"bids": {px: sz}, "asks": {px: sz}, "last_trade": float|None, "t": float, "ready": bool}
        self._books: Dict[str, Dict[str, Any]] = {}
        self._last_ws_msg_t: float = 0.0
        self.msg_count: int = 0
        self.reconnect_count: int = 0
        self.last_error: str = ""

    def set_tokens(self, token_ids: Sequence[str]):
        """Update subscribed CLOB asset IDs; triggers immediate resubscription."""
        clean = tuple(sorted({str(t) for t in token_ids if t}))
        with self._lock:
            if clean == self._active_tokens:
                return
            self._active_tokens = clean
            self._token_gen += 1
            # Prune old tokens no longer subscribed
            keep = set(clean)
            for k in list(self._books.keys()):
                if k not in keep:
                    self._books.pop(k, None)

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="PolyBookWS")
        self._thread.start()

    def stop(self):
        self.running = False
        self.connected = False
        if self._loop and self._loop.is_running():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def apply_message(self, data: Any, now: Optional[float] = None) -> bool:
        """Apply a Polymarket CLOB WS book snapshot, price_change, or last_trade_price message."""
        ts = time.time() if now is None else float(now)
        if isinstance(data, list):
            changed = False
            for item in data:
                if self._apply_one(item, ts):
                    changed = True
            return changed
        if isinstance(data, dict):
            return self._apply_one(data, ts)
        return False

    def _apply_one(self, msg: Dict[str, Any], ts: float) -> bool:
        if not isinstance(msg, dict):
            return False
        etype = msg.get("event_type")
        if etype == "book" or ("bids" in msg and "asks" in msg and msg.get("asset_id")):
            aid = str(msg.get("asset_id") or "")
            if not aid:
                return False
            bids: Dict[float, float] = {}
            asks: Dict[float, float] = {}
            for b in msg.get("bids") or []:
                px = _safe_float(b.get("price"), -1.0)
                sz = _safe_float(b.get("size"), 0.0)
                if px > 0.0 and sz > 0.0:
                    bids[round(px, 6)] = sz
            for a in msg.get("asks") or []:
                px = _safe_float(a.get("price"), -1.0)
                sz = _safe_float(a.get("size"), 0.0)
                if px > 0.0 and sz > 0.0:
                    asks[round(px, 6)] = sz
            ltp = msg.get("last_trade_price")
            ltp_f = _safe_float(ltp, -1.0) if ltp is not None else -1.0
            with self._lock:
                self._books[aid] = {
                    "bids": bids,
                    "asks": asks,
                    "last_trade": ltp_f if ltp_f > 0.0 else None,
                    "t": ts,
                    "ready": True,
                }
                self._last_ws_msg_t = ts
                self.msg_count += 1
            return True

        if etype == "price_change" or isinstance(msg.get("price_changes"), list):
            changes = msg.get("price_changes") or []
            updated = False
            with self._lock:
                for ch in changes:
                    if not isinstance(ch, dict):
                        continue
                    aid = str(ch.get("asset_id") or "")
                    st = self._books.get(aid)
                    if not st or not st.get("ready"):
                        continue
                    px = round(_safe_float(ch.get("price"), -1.0), 6)
                    sz = _safe_float(ch.get("size"), 0.0)
                    side = str(ch.get("side") or "").upper()
                    if px <= 0.0:
                        continue
                    book_side = st["bids"] if side == "BUY" else (st["asks"] if side == "SELL" else None)
                    if book_side is None:
                        continue
                    if sz <= 1e-9:
                        book_side.pop(px, None)
                    else:
                        book_side[px] = sz
                    st["t"] = ts
                    updated = True
                if updated:
                    self._last_ws_msg_t = ts
                    self.msg_count += 1
            return updated

        if etype == "last_trade_price" and msg.get("asset_id"):
            aid = str(msg.get("asset_id"))
            px = _safe_float(msg.get("price"), -1.0)
            if px > 0.0:
                with self._lock:
                    st = self._books.get(aid)
                    if st and st.get("ready"):
                        st["last_trade"] = px
                        st["t"] = ts
                        self._last_ws_msg_t = ts
                        self.msg_count += 1
                        return True
        return False

    def get_order_book(self, token_id: str, max_age_s: float = 3.0, now: Optional[float] = None) -> Optional[OrderBook]:
        """Build a sorted OrderBook snapshot from WS memory if fresh, else None.

        When the WebSocket session is actively connected and receiving stream
        traffic within `idle_reconnect_s`, an unchanged quiet book is still live:
        its `received_at` is stamped at `now` while `source_t` preserves the last
        book-change timestamp.
        """
        t = time.time() if now is None else float(now)
        with self._lock:
            st = self._books.get(str(token_id))
            if not st or not st.get("ready"):
                return None
            src_t = float(st.get("t", 0.0))
            if self.connected:
                # Live WS stream: validate connection-level freshness against idle watchdog
                stream_age = t - float(self._last_ws_msg_t or src_t)
                if stream_age > max(max_age_s, self.idle_reconnect_s):
                    return None
                eff_t = t
            else:
                if (t - src_t) > max_age_s:
                    return None
                eff_t = src_t
            bids_sorted = sorted(st["bids"].items(), key=lambda x: x[0], reverse=True)
            asks_sorted = sorted(st["asks"].items(), key=lambda x: x[0])
            # Self-heal corrupted/crossed in-memory L2 book (dropped delta) by invalidating
            # and forcing an immediate resubscribe + HTTP fallback.
            if bids_sorted and asks_sorted and bids_sorted[0][0] >= asks_sorted[0][0] - 1e-9:
                st["ready"] = False
                self._token_gen += 1
                return None
            ltp = st.get("last_trade")
        return OrderBook(
            bids=bids_sorted,
            asks=asks_sorted,
            last_trade_price=ltp,
            source_t=eff_t,
            received_at=eff_t,
        )

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._ws_worker())
        except Exception:
            pass
        finally:
            self._loop.close()

    async def _ws_worker(self):
        import websockets

        recv_timeout = min(1.0, self.idle_reconnect_s)
        while self.running:
            with self._lock:
                tokens = self._active_tokens
                gen = self._token_gen
            if not tokens:
                self.connected = False
                await asyncio.sleep(0.25)
                continue

            try:
                connect_kwargs: Dict[str, Any] = {
                    "ping_interval": 15,
                    "ping_timeout": 10,
                    "open_timeout": 5.0,
                }
                if self.proxy:
                    connect_kwargs["proxy"] = self.proxy
                async with websockets.connect(POLY_CLOB_WS_URL, **connect_kwargs) as ws:
                    self.connected = True
                    await ws.send(json.dumps({"assets_ids": list(tokens), "type": "market"}))
                    last_activity_t = time.monotonic()

                    while self.running:
                        with self._lock:
                            cur_gen = self._token_gen
                        if cur_gen != gen:
                            # Token set changed (or crossed-book self-heal triggered) -> reconnect cleanly
                            self.connected = False
                            break

                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                        except asyncio.TimeoutError:
                            raw = None

                        accepted = False
                        if raw and isinstance(raw, str) and raw.strip():
                            try:
                                accepted = self.apply_message(json.loads(raw))
                            except Exception:
                                accepted = False

                        if accepted:
                            last_activity_t = time.monotonic()
                        elif (time.monotonic() - last_activity_t) >= self.idle_reconnect_s:
                            raise TimeoutError(
                                f"PolyBookWS idle watchdog: no book updates for {self.idle_reconnect_s:.1f}s"
                            )
            except Exception as exc:
                self.connected = False
                self.reconnect_count += 1
                self.last_error = str(exc)
                if self.running:
                    await asyncio.sleep(1.5)
