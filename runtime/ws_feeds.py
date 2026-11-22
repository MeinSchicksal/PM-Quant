"""Low-Latency Real-Time WebSocket Feeds (Binance Spot + Polymarket CLOB L2 Book).

Provides millisecond-level push updates with automatic idle-watchdog reconnects
and clean fallback to HTTP REST in MarketFeed whenever a WS stream is warming up
or transiently disconnected.
"""
import asyncio
import json
import math
import random
import threading
import time
from typing import Any, Dict, Optional, Sequence, Tuple

from core.types import OrderBook
from runtime.alerts import notify as send_alert

BINANCE_WS_URLS = (
    "wss://data-stream.binance.vision/ws/btcusdt@bookTicker",
    "wss://stream.binance.com:9443/ws/btcusdt@bookTicker",
)
POLY_CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None:
            return default
        out = float(val)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _ws_reconnect_backoff(attempt: int, err_text: str = "", base_s: float = 1.5, cap_s: float = 30.0) -> float:
    """Compute exponential backoff with jitter; slow down aggressively on 403/429/451."""
    low = str(err_text or "").lower()
    is_blocked = any(tok in low for tok in ("403", "429", "451", "forbidden", "too many requests", "geo"))
    eff_base = max(base_s, 12.0) if is_blocked else base_s
    eff_cap = max(cap_s, 60.0) if is_blocked else cap_s
    exp = min(eff_cap, eff_base * (2 ** min(max(0, int(attempt) - 1), 5)))
    return round(exp * random.uniform(0.85, 1.15), 2)


def _install_ws_loop_exception_filter(loop: asyncio.AbstractEventLoop):
    """Suppress harmless websockets 15.x teardown AttributeError on failed proxy handshakes."""
    def _handler(lp, context):
        exc = context.get("exception")
        if isinstance(exc, AttributeError) and "recv_messages" in str(exc):
            return
        lp.default_exception_handler(context)
    loop.set_exception_handler(_handler)


async def _ws_ping_rtt_loop(ws, on_rtt, interval_s: float = 5.0):
    """Measure real end-to-end WebSocket network RTT via RFC 6455 ping/pong frames."""
    while True:
        try:
            t0 = time.perf_counter()
            pong_waiter = await ws.ping()
            await asyncio.wait_for(pong_waiter, timeout=4.0)
            on_rtt(round((time.perf_counter() - t0) * 1000.0, 1))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(interval_s)


class BinanceSpotWSClient:
    """Real-time BTC/USDT bookTicker WebSocket client with idle watchdog."""

    def __init__(
        self,
        proxy: Optional[str] = None,
        idle_reconnect_s: float = 10.0,
        wake_event: Optional[threading.Event] = None,
        wake_delta_usd: float = 1.0,
    ):
        self.proxy = proxy
        self.idle_reconnect_s = max(0.05, float(idle_reconnect_s))
        self._wake_event = wake_event
        self._wake_delta_usd = max(0.01, float(wake_delta_usd))
        self.running = False
        self.connected = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._reconnect_flag: bool = False
        self._lock = threading.Lock()

        self._price: float = 0.0
        self._last_wake_px: float = 0.0
        self._updated_t: float = 0.0
        self._last_pong_t: float = 0.0
        self._source: str = "binance_ws"
        self.rtt_ms: float = 0.0
        self.msg_count: int = 0
        self.reconnect_count: int = 0
        self.last_error: str = ""

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="BinanceSpotWS")
        self._thread.start()

    def force_reconnect(self):
        """Request an immediate clean reconnect (e.g. after proxy route failover)."""
        self._reconnect_flag = True

    def stop(self):
        self.running = False
        self.connected = False
        loop = self._loop
        if loop and loop.is_running():
            try:
                def _cancel_worker():
                    if self._worker_task is not None and not self._worker_task.done():
                        self._worker_task.cancel()
                loop.call_soon_threadsafe(_cancel_worker)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.5)

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
        should_wake = False
        with self._lock:
            self._price = px
            self._updated_t = ts
            self.msg_count += 1
            if self._last_wake_px <= 0.0 or abs(px - self._last_wake_px) >= self._wake_delta_usd:
                self._last_wake_px = px
                should_wake = True
        if should_wake and self._wake_event is not None:
            self._wake_event.set()
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
        _install_ws_loop_exception_filter(self._loop)
        try:
            self._worker_task = self._loop.create_task(self._ws_worker())
            self._loop.run_until_complete(self._worker_task)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            self._worker_task = None
            self._loop.close()

    async def _ws_worker(self):
        import websockets

        url_idx = 0
        consec_fails = 0
        recv_timeout = min(2.0, self.idle_reconnect_s)
        while self.running:
            url = BINANCE_WS_URLS[url_idx % len(BINANCE_WS_URLS)]
            self._reconnect_flag = False
            connected_at = 0.0
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
                    connected_at = time.monotonic()
                    def _on_rtt(ms: float):
                        self.rtt_ms = ms
                        with self._lock:
                            self._last_pong_t = time.time()

                    ping_task = asyncio.create_task(
                        _ws_ping_rtt_loop(ws, _on_rtt, interval_s=5.0)
                    )
                    last_activity_t = time.monotonic()
                    try:
                        while self.running and not self._reconnect_flag:
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
                                if consec_fails > 0 and (last_activity_t - connected_at) >= 30.0:
                                    consec_fails = 0
                            elif (time.monotonic() - last_activity_t) >= self.idle_reconnect_s:
                                raise TimeoutError(
                                    f"BinanceSpotWS idle watchdog: no tickers for {self.idle_reconnect_s:.1f}s"
                                )
                    finally:
                        ping_task.cancel()
                        await asyncio.gather(ping_task, return_exceptions=True)
                if self._reconnect_flag:
                    self.connected = False
                    consec_fails = 0
                    continue
            except asyncio.CancelledError:
                self.connected = False
                break
            except Exception as exc:
                self.connected = False
                self.rtt_ms = 0.0
                self.reconnect_count += 1
                consec_fails += 1
                self.last_error = str(exc)
                url_idx += 1
                if consec_fails in (5, 15) or any(c in self.last_error for c in ("403", "451", "429")):
                    send_alert(
                        "binance_spot_ws_err",
                        "Binance Spot WS 异常断连",
                        f"连续失败 {consec_fails} 次，错误: {self.last_error[:180]}",
                        level="warn",
                    )
                if self.running:
                    await asyncio.sleep(_ws_reconnect_backoff(consec_fails, self.last_error, base_s=1.5, cap_s=30.0))


class PolymarketBookWSClient:
    """Real-time Polymarket CLOB L2 orderbook WebSocket client."""

    def __init__(
        self,
        proxy: Optional[str] = None,
        idle_reconnect_s: float = 20.0,
        wake_event: Optional[threading.Event] = None,
    ):
        self.proxy = proxy
        self.idle_reconnect_s = max(0.05, float(idle_reconnect_s))
        self._wake_event = wake_event
        self.running = False
        self.connected = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._lock = threading.Lock()

        self._active_tokens: Tuple[str, ...] = ()
        self._token_gen: int = 0
        # asset_id -> {"bids": {px: sz}, "asks": {px: sz}, "last_trade": float|None, "t": float, "ready": bool}
        self._books: Dict[str, Dict[str, Any]] = {}
        self._last_ws_msg_t: float = 0.0
        self._last_pong_t: float = 0.0
        self.rtt_ms: float = 0.0
        self.msg_count: int = 0
        self.reconnect_count: int = 0
        self.last_error: str = ""

    def _invalidate_all_books(self):
        """Mark every cached L2 book not-ready and clear liveness timestamps on disconnect/reconnect.

        Prevents stale pre-disconnect orderbooks from being resurrected as 'fresh' by a
        brand-new connection's first ping/pong before fresh 'book' snapshots arrive.
        """
        with self._lock:
            for st in self._books.values():
                st["ready"] = False
            self._last_pong_t = 0.0
            self._last_ws_msg_t = 0.0

    def force_reconnect(self):
        """Force immediate resubscribe/reconnect (e.g. after proxy failover)."""
        with self._lock:
            self._token_gen += 1
        self._invalidate_all_books()

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
        loop = self._loop
        if loop and loop.is_running():
            try:
                def _cancel_worker():
                    if self._worker_task is not None and not self._worker_task.done():
                        self._worker_task.cancel()
                loop.call_soon_threadsafe(_cancel_worker)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.5)

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
            if self._wake_event is not None:
                self._wake_event.set()
            return True

        if etype == "price_change" or isinstance(msg.get("price_changes"), list):
            changes = msg.get("price_changes") or []
            updated = False
            top_changed = False
            with self._lock:
                prev_tops: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
                touched_aids = set()
                for ch in changes:
                    if not isinstance(ch, dict):
                        continue
                    aid = str(ch.get("asset_id") or "")
                    st = self._books.get(aid)
                    if not st or not st.get("ready"):
                        continue
                    if aid not in prev_tops:
                        prev_tops[aid] = (
                            max(st["bids"]) if st["bids"] else None,
                            min(st["asks"]) if st["asks"] else None,
                        )
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
                    touched_aids.add(aid)
                    updated = True
                if updated:
                    self._last_ws_msg_t = ts
                    self.msg_count += 1
                    for aid in touched_aids:
                        st = self._books.get(aid)
                        if not st:
                            continue
                        new_top = (
                            max(st["bids"]) if st["bids"] else None,
                            min(st["asks"]) if st["asks"] else None,
                        )
                        if new_top != prev_tops.get(aid):
                            top_changed = True
                            break
            if top_changed and self._wake_event is not None:
                self._wake_event.set()
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

    def get_order_book(
        self,
        token_id: str,
        max_age_s: float = 3.0,
        now: Optional[float] = None,
        require_recent_msg: bool = False,
    ) -> Optional[OrderBook]:
        """Build a sorted OrderBook snapshot from WS memory if fresh, else None.

        When the WebSocket session is actively connected and receiving stream
        traffic (or RFC 6455 pong keepalives) within `idle_reconnect_s`, an
        unchanged quiet book is still live: its `received_at` is stamped at
        `now`. Callers performing delayed IOC verification can pass
        `require_recent_msg=True` to require a recent book message within
        `max_age_s` or else fall back to HTTP REST.
        """
        t = time.time() if now is None else float(now)
        with self._lock:
            st = self._books.get(str(token_id))
            if not st or not st.get("ready"):
                return None
            src_t = float(st.get("t", 0.0))
            if self.connected:
                if require_recent_msg:
                    msg_age = t - float(self._last_ws_msg_t or src_t)
                    if msg_age > max_age_s:
                        return None
                # Guard against silent single-token freeze even if global pong is alive
                if src_t > 0.0 and (t - src_t) > max(60.0, self.idle_reconnect_s * 3.0):
                    return None
                # Live WS stream: validate connection-level liveness (msg or pong) against idle watchdog
                last_alive = max(float(self._last_ws_msg_t or 0.0), float(self._last_pong_t or 0.0), src_t)
                stream_age = t - last_alive
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
        _install_ws_loop_exception_filter(self._loop)
        try:
            self._worker_task = self._loop.create_task(self._ws_worker())
            self._loop.run_until_complete(self._worker_task)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            self._worker_task = None
            self._loop.close()

    async def _ws_worker(self):
        import websockets

        consec_fails = 0
        recv_timeout = min(1.0, self.idle_reconnect_s)
        while self.running:
            with self._lock:
                tokens = self._active_tokens
                gen = self._token_gen
            if not tokens:
                self.connected = False
                self._invalidate_all_books()
                await asyncio.sleep(0.25)
                continue

            # Invalidate any stale cached L2 snapshots BEFORE connecting so a new
            # session's first pong cannot resurrect pre-disconnect books.
            self._invalidate_all_books()
            connected_at = 0.0
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
                    connected_at = time.monotonic()
                    await ws.send(json.dumps({"assets_ids": list(tokens), "type": "market"}))
                    def _on_rtt(ms: float):
                        self.rtt_ms = ms
                        with self._lock:
                            self._last_pong_t = time.time()

                    ping_task = asyncio.create_task(
                        _ws_ping_rtt_loop(ws, _on_rtt, interval_s=5.0)
                    )
                    last_activity_t = time.monotonic()
                    try:
                        while self.running:
                            with self._lock:
                                cur_gen = self._token_gen
                            if cur_gen != gen:
                                # Token set changed (or crossed-book self-heal triggered) -> reconnect cleanly
                                self.connected = False
                                consec_fails = 0
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
                                if consec_fails > 0 and (last_activity_t - connected_at) >= 30.0:
                                    consec_fails = 0
                            elif (time.monotonic() - last_activity_t) >= self.idle_reconnect_s:
                                raise TimeoutError(
                                    f"PolyBookWS idle watchdog: no book updates for {self.idle_reconnect_s:.1f}s"
                                )
                    finally:
                        ping_task.cancel()
                        await asyncio.gather(ping_task, return_exceptions=True)
            except asyncio.CancelledError:
                self.connected = False
                self._invalidate_all_books()
                break
            except Exception as exc:
                self.connected = False
                self._invalidate_all_books()
                self.rtt_ms = 0.0
                self.reconnect_count += 1
                consec_fails += 1
                self.last_error = str(exc)
                if consec_fails in (5, 15) or any(c in self.last_error for c in ("403", "429")):
                    send_alert(
                        "poly_book_ws_err",
                        "Polymarket Book WS 异常断连",
                        f"连续失败 {consec_fails} 次，错误: {self.last_error[:180]}",
                        level="warn",
                    )
                if self.running:
                    await asyncio.sleep(_ws_reconnect_backoff(consec_fails, self.last_error, base_s=1.5, cap_s=30.0))
