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
POLY_RTDS_WS_URL = "wss://ws-live-data.polymarket.com"

# 多源实时现货 WS 配置（伦敦节点优先推荐使用 kraken / coinbase 或 auto 自动测速择优，消除 310ms 东京往返瓶颈）
SPOT_WS_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "binance": {
        "urls": BINANCE_WS_URLS,
        "subscribe": None,
        "source": "binance_ws",
        "label": "Binance (BTC/USDT, Tokyo)",
    },
    "kraken": {
        "urls": ("wss://ws.kraken.com/v2",),
        "subscribe": {"method": "subscribe", "params": {"channel": "ticker", "symbol": ["BTC/USD"]}},
        "source": "kraken_ws",
        "label": "Kraken v2 (BTC/USD, EU/LD4)",
    },
    "coinbase": {
        "urls": ("wss://ws-feed.exchange.coinbase.com",),
        "subscribe": {"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker", "heartbeat"]},
        "source": "coinbase_ws",
        "label": "Coinbase (BTC/USD, US/CF)",
    },
    "okx": {
        "urls": ("wss://ws.okx.com:8443/ws/v5/public",),
        "subscribe": {"op": "subscribe", "args": [{"channel": "bbo-tbt", "instId": "BTC-USDT"}]},
        "source": "okx_ws",
        "label": "OKX (BTC/USDT, Global)",
    },
}


async def _probe_single_ws(url: str, proxy: Optional[str] = None, timeout_s: float = 3.0) -> Dict[str, Any]:
    """测试单个 WebSocket 端点的握手耗时与 RFC 6455 Ping RTT。"""
    import websockets

    connect_kwargs: Dict[str, Any] = {
        "ping_interval": None,
        "open_timeout": timeout_s,
    }
    if proxy:
        connect_kwargs["proxy"] = proxy
    t0 = time.perf_counter()
    try:
        async with websockets.connect(url, **connect_kwargs) as ws:
            hs_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            rtts = []
            for _ in range(2):
                tp = time.perf_counter()
                pong_waiter = await ws.ping()
                await asyncio.wait_for(pong_waiter, timeout=timeout_s)
                rtts.append((time.perf_counter() - tp) * 1000.0)
            rtt_ms = round(min(rtts), 1) if rtts else hs_ms
            return {"ok": True, "url": url, "handshake_ms": hs_ms, "rtt_ms": rtt_ms, "error": ""}
    except Exception as exc:
        return {"ok": False, "url": url, "handshake_ms": None, "rtt_ms": None, "error": str(exc)[:140]}


async def _async_probe_network(proxy: Optional[str] = None, timeout_s: float = 3.0) -> Dict[str, Any]:
    """并发测速 Polymarket 交易所端点及全部候选现货 WS 源。"""
    targets = [
        ("poly_clob", "Polymarket CLOB WS", POLY_CLOB_WS_URL),
        ("poly_rtds", "Polymarket RTDS Trades WS", POLY_RTDS_WS_URL),
    ]
    for pname, pcfg in SPOT_WS_PROVIDERS.items():
        targets.append((f"spot_{pname}", pcfg["label"], pcfg["urls"][0]))

    results = await asyncio.gather(
        *[_probe_single_ws(url, proxy=proxy, timeout_s=timeout_s) for _, _, url in targets],
        return_exceptions=False,
    )
    out: Dict[str, Any] = {"endpoints": {}, "spot_providers": {}}
    for (key, label, _), res in zip(targets, results):
        entry = dict(res, label=label)
        out["endpoints"][key] = entry
        if key.startswith("spot_"):
            pname = key[len("spot_"):]
            out["spot_providers"][pname] = entry

    venue_rtt = out["endpoints"]["poly_clob"].get("rtt_ms")
    ok_spots = [
        (pname, info["rtt_ms"])
        for pname, info in out["spot_providers"].items()
        if info.get("ok") and info.get("rtt_ms") is not None
    ]
    ok_spots.sort(key=lambda kv: kv[1])
    best_provider = ok_spots[0][0] if ok_spots else "binance"
    best_rtt = ok_spots[0][1] if ok_spots else None
    out["venue_rtt_ms"] = venue_rtt
    out["best_spot_provider"] = best_provider
    out["best_spot_rtt_ms"] = best_rtt
    out["best_signal_minus_venue_ms"] = (
        round(best_rtt - venue_rtt, 1) if (best_rtt is not None and venue_rtt is not None) else None
    )
    return out


def probe_network_latency(proxy: Optional[str] = None, timeout_s: float = 3.0) -> Dict[str, Any]:
    """同步封装：并发测速 Polymarket 与多所 BTC 现货 WebSocket 延迟。"""
    loop = asyncio.new_event_loop()
    try:
        _install_ws_loop_exception_filter(loop)
        return loop.run_until_complete(_async_probe_network(proxy=proxy, timeout_s=timeout_s))
    finally:
        loop.close()


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


class _BaseWSClient:
    """Shared asyncio event-loop lifecycle for background WebSocket clients."""

    def stop(self):
        """Stop background WebSocket client cleanly."""
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


class BinanceSpotWSClient(_BaseWSClient):
    """Real-time BTC Spot WebSocket client supporting Binance / Kraken / Coinbase / OKX / auto."""

    def __init__(
        self,
        proxy: Optional[str] = None,
        idle_reconnect_s: float = 10.0,
        wake_event: Optional[threading.Event] = None,
        wake_delta_usd: float = 1.0,
        provider: str = "binance",
    ):
        self.proxy = proxy
        self.idle_reconnect_s = max(0.05, float(idle_reconnect_s))
        self._wake_event = wake_event
        self._wake_delta_usd = max(0.01, float(wake_delta_usd))
        req_prov = str(provider or "binance").strip().lower()
        self.provider = req_prov if (req_prov == "auto" or req_prov in SPOT_WS_PROVIDERS) else "binance"
        self.active_provider = "binance" if self.provider == "auto" else self.provider
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
        self._last_heartbeat_t: float = 0.0
        self._last_pong_t: float = 0.0
        self._source: str = SPOT_WS_PROVIDERS[self.active_provider]["source"]
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

    def apply_message(self, payload: Dict[str, Any], now: Optional[float] = None) -> bool:
        """Parse one BTC spot ticker JSON object (Binance / Kraken v2 / Coinbase / OKX) and update latest mid."""
        if not isinstance(payload, dict):
            return False
        px = 0.0
        # 1. Binance bookTicker / trade
        bid = _safe_float(payload.get("b"), 0.0)
        ask = _safe_float(payload.get("a"), 0.0)
        if bid > 0.0 and ask > 0.0:
            px = (bid + ask) * 0.5
        elif payload.get("p") is not None or payload.get("c") is not None:
            px = _safe_float(payload.get("p") or payload.get("c"), 0.0)
        # 2. Coinbase Exchange ticker / heartbeat
        elif payload.get("type") == "heartbeat":
            ts = time.time() if now is None else float(now)
            with self._lock:
                self._last_heartbeat_t = ts
            return False
        elif payload.get("type") == "ticker":
            cb_bid = _safe_float(payload.get("best_bid"), 0.0)
            cb_ask = _safe_float(payload.get("best_ask"), 0.0)
            if cb_bid > 0.0 and cb_ask > 0.0:
                px = (cb_bid + cb_ask) * 0.5
            else:
                px = _safe_float(payload.get("price"), 0.0)
        # 3. Kraken v2 ticker
        elif payload.get("channel") == "ticker" and isinstance(payload.get("data"), list) and payload["data"]:
            row = payload["data"][0]
            if isinstance(row, dict):
                kr_bid = _safe_float(row.get("bid"), 0.0)
                kr_ask = _safe_float(row.get("ask"), 0.0)
                if kr_bid > 0.0 and kr_ask > 0.0:
                    px = (kr_bid + kr_ask) * 0.5
                else:
                    px = _safe_float(row.get("last"), 0.0)
        # 4. OKX bbo-tbt / tickers
        elif isinstance(payload.get("data"), list) and payload["data"] and isinstance(payload.get("arg"), dict):
            row = payload["data"][0]
            if isinstance(row, dict):
                bids = row.get("bids") or []
                asks = row.get("asks") or []
                ob_bid = _safe_float(bids[0][0], 0.0) if bids and isinstance(bids[0], (list, tuple)) and bids[0] else 0.0
                ob_ask = _safe_float(asks[0][0], 0.0) if asks and isinstance(asks[0], (list, tuple)) and asks[0] else 0.0
                if ob_bid > 0.0 and ob_ask > 0.0:
                    px = (ob_bid + ob_ask) * 0.5
                else:
                    px = _safe_float(row.get("last"), 0.0)

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
        """Return (price, updated_ts, source) if fresh (or live heartbeat on quiet book), else None."""
        t = time.time() if now is None else float(now)
        with self._lock:
            if self._price <= 0.0 or self._updated_t <= 0.0:
                return None
            if (t - self._updated_t) <= max_age_s:
                return self._price, self._updated_t, self._source
            if (
                self.connected
                and self._last_heartbeat_t > 0.0
                and (t - self._last_heartbeat_t) <= max_age_s
                and (t - self._updated_t) <= self.idle_reconnect_s
            ):
                return self._price, t, self._source
            return None

    async def _ws_worker(self):
        import websockets

        auto_resolved = (self.provider != "auto")
        url_idx = 0
        consec_fails = 0
        recv_timeout = min(2.0, self.idle_reconnect_s)
        while self.running:
            if not auto_resolved:
                try:
                    probe_res = await _async_probe_network(proxy=self.proxy, timeout_s=2.5)
                    best = str(probe_res.get("best_spot_provider") or "binance")
                    best_rtt = probe_res.get("best_spot_rtt_ms")
                    if best in SPOT_WS_PROVIDERS and best_rtt is not None:
                        self.active_provider = best
                        self._source = SPOT_WS_PROVIDERS[best]["source"]
                        auto_resolved = True
                        url_idx = 0
                        print(f"[SpotWS] Auto-selected lowest-RTT provider: {best} ({best_rtt}ms)")
                except Exception:
                    pass

            prov_cfg = SPOT_WS_PROVIDERS.get(self.active_provider, SPOT_WS_PROVIDERS["binance"])
            urls = prov_cfg["urls"]
            sub_msg = prov_cfg.get("subscribe")
            url = urls[url_idx % len(urls)]
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
                    if sub_msg is not None:
                        await asyncio.wait_for(ws.send(json.dumps(sub_msg)), timeout=5.0)
                    def _on_rtt(ms: float):
                        self.rtt_ms = ms
                        with self._lock:
                            self._last_pong_t = time.time()

                    ping_task = asyncio.create_task(
                        _ws_ping_rtt_loop(ws, _on_rtt, interval_s=5.0)
                    )
                    last_activity_t = time.monotonic()
                    prev_hb_t = self._last_heartbeat_t
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

                            hb_advanced = self._last_heartbeat_t > prev_hb_t
                            if hb_advanced:
                                prev_hb_t = self._last_heartbeat_t
                            if accepted or hb_advanced:
                                last_activity_t = time.monotonic()
                                if accepted and consec_fails > 0 and (last_activity_t - connected_at) >= 30.0:
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
                    if self.provider == "auto":
                        auto_resolved = False
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


class PolymarketBookWSClient(_BaseWSClient):
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
        self._pending_sub_tokens: list = []
        self._pending_unsub_tokens: list = []
        self._pending_resub_tokens: list = []
        self._token_sub_t: Dict[str, float] = {}
        self._last_resub_t: Dict[str, float] = {}
        self._resub_attempts: Dict[str, int] = {}
        # asset_id -> {"bids": {px: sz}, "asks": {px: sz}, "last_trade": float|None, "t": float, "ready": bool}
        self._books: Dict[str, Dict[str, Any]] = {}
        self._last_ws_msg_t: float = 0.0
        self._last_pong_t: float = 0.0
        self._min_srv_delta_s: Optional[float] = None
        self.last_msg_lag_s: float = 0.0
        self.max_msg_lag_s: float = 5.0
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
            self._min_srv_delta_s = None
            self.last_msg_lag_s = 0.0

    def _update_srv_delta_baseline_locked(self, msg: Dict[str, Any], recv_ts: float) -> Optional[float]:
        """更新服务端时间戳相对基线，返回当前帧相对基线的滞后秒数（不触发重连）。"""
        raw_ts = msg.get("timestamp")
        if raw_ts is None:
            return None
        srv_t = _safe_float(raw_ts, 0.0)
        if srv_t > 1e11:
            srv_t /= 1000.0
        if not (1_600_000_000.0 < srv_t < 4_000_000_000.0):
            return None
        delta = recv_ts - srv_t
        if self._min_srv_delta_s is None or delta < self._min_srv_delta_s:
            self._min_srv_delta_s = delta
        return max(0.0, delta - self._min_srv_delta_s)

    def _is_server_msg_lagged_locked(self, msg: Dict[str, Any], recv_ts: float) -> bool:
        """基于服务端时间戳相对基线漂移检测实时增量帧的 TCP/代理积压（免疫本机时钟偏移与静态快照）。"""
        lag = self._update_srv_delta_baseline_locked(msg, recv_ts)
        if lag is None:
            return False
        self.last_msg_lag_s = round(lag, 2)
        if lag > self.max_msg_lag_s:
            for st in self._books.values():
                st["ready"] = False
            self._token_gen += 1
            return True
        return False

    def invalidate_on_trade_divergence(
        self,
        token_id: str,
        trade_price: float,
        max_gap: float = 0.06,
        trade_recv_t: Optional[float] = None,
    ) -> bool:
        """若独立逐笔成交流的实时成交价已甩开盘口 WS >= max_gap，判定 WS 积压滞后并强制重连。

        因果防误杀：若提供 `trade_recv_t` 且盘口 WS 已在成交之后更新过（`st["t"] > trade_recv_t + 0.15`），
        说明是盘口比老成交更新，绝不误杀正常低延迟 WS。
        """
        if not (0.0 < trade_price < 1.0):
            return False
        with self._lock:
            st = self._books.get(str(token_id))
            if not st or not st.get("ready") or not st.get("bids") or not st.get("asks"):
                return False
            if trade_recv_t is not None and float(st.get("t", 0.0)) > float(trade_recv_t) + 0.15:
                return False
            best_bid = max(st["bids"])
            best_ask = min(st["asks"])
            if trade_price > best_ask + max_gap or trade_price < best_bid - max_gap:
                for b_st in self._books.values():
                    b_st["ready"] = False
                self._token_gen += 1
                return True
        return False

    def force_reconnect(self):
        """Force immediate resubscribe/reconnect (e.g. after proxy failover)."""
        with self._lock:
            self._token_gen += 1
        self._invalidate_all_books()

    def set_tokens(self, token_ids: Sequence[str]):
        """增量更新 CLOB 订阅：预热扩集与换窗退订旧 token 均不断连，保持已预热盘口 0ms 衔接。"""
        clean = tuple(sorted({str(t) for t in token_ids if t}))
        with self._lock:
            if clean == self._active_tokens:
                return
            old_set = set(self._active_tokens)
            new_set = set(clean)
            # 只要已连接且新旧集合存在交集（tau<=30s 预热扩集，或换窗瞬间退订旧 token），
            # 原地发送增量 subscribe / unsubscribe，绝不断连清空刚预热好的新窗口盘口。
            if self.connected and old_set and new_set and (old_set & new_set):
                added = sorted(new_set - old_set)
                removed = sorted(old_set - new_set)
                self._active_tokens = clean
                now_sub = time.time()
                for tok in added:
                    if tok in self._pending_unsub_tokens:
                        self._pending_unsub_tokens.remove(tok)
                    if tok not in self._pending_sub_tokens:
                        self._pending_sub_tokens.append(tok)
                    self._token_sub_t[tok] = now_sub
                for tok in removed:
                    if tok in self._pending_sub_tokens:
                        self._pending_sub_tokens.remove(tok)
                    if tok in self._pending_resub_tokens:
                        self._pending_resub_tokens.remove(tok)
                    self._token_sub_t.pop(tok, None)
                    self._last_resub_t.pop(tok, None)
                    self._resub_attempts.pop(tok, None)
                    if tok not in self._pending_unsub_tokens:
                        self._pending_unsub_tokens.append(tok)
                    self._books.pop(tok, None)
                return
            self._active_tokens = clean
            self._pending_sub_tokens.clear()
            self._pending_unsub_tokens.clear()
            self._pending_resub_tokens.clear()
            self._token_sub_t.clear()
            self._last_resub_t.clear()
            self._resub_attempts.clear()
            self._token_gen += 1
            # Prune old tokens no longer subscribed
            for k in list(self._books.keys()):
                if k not in new_set:
                    self._books.pop(k, None)

    def _schedule_token_resubscribe_locked(
        self,
        token_id: str,
        now_ts: float,
        allow_reconnect_escalation: bool = True,
    ) -> bool:
        """在持有 self._lock 下为静默断流的单 Token 触发原地重订；连续重订仍未收到快照才升级为整链重连。"""
        if not self.connected or token_id not in self._active_tokens:
            return False
        if (now_ts - self._last_resub_t.get(token_id, 0.0)) < 10.0:
            return False
        self._last_resub_t[token_id] = now_ts
        attempts = self._resub_attempts.get(token_id, 0) + 1
        self._resub_attempts[token_id] = attempts
        st = self._books.get(token_id)
        if st is not None:
            st["ready"] = False
        if allow_reconnect_escalation and attempts >= 3:
            # 多次原地重订仍收不到任何 book 快照，递增代际触发平滑重连
            self._resub_attempts[token_id] = 0
            self._token_gen += 1
            return True
        if token_id not in self._pending_resub_tokens:
            self._pending_resub_tokens.append(token_id)
        return True

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="PolyBookWS")
        self._thread.start()

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
                # 初始 book 快照的 timestamp 可能是该安静盘口上次变动时间，只用于校准基线、绝不触发滞后自杀重连
                self._update_srv_delta_baseline_locked(msg, ts)
                self._books[aid] = {
                    "bids": bids,
                    "asks": asks,
                    "last_trade": ltp_f if ltp_f > 0.0 else None,
                    "t": ts,
                    "ready": True,
                }
                self._resub_attempts.pop(aid, None)
                self._last_ws_msg_t = ts
                self.msg_count += 1
            if self._wake_event is not None:
                self._wake_event.set()
            return True

        if etype == "price_change" or isinstance(msg.get("price_changes"), list):
            changes = msg.get("price_changes") or []
            updated = False
            top_changed = False

            def _top_sig(book_st: Dict[str, Any]) -> Tuple[Optional[float], float, Optional[float], float]:
                b_px = max(book_st["bids"]) if book_st["bids"] else None
                b_sz = float(book_st["bids"][b_px]) if b_px is not None else 0.0
                a_px = min(book_st["asks"]) if book_st["asks"] else None
                a_sz = float(book_st["asks"][a_px]) if a_px is not None else 0.0
                return (b_px, b_sz, a_px, a_sz)

            with self._lock:
                if self._is_server_msg_lagged_locked(msg, ts):
                    return False
                prev_tops: Dict[str, Tuple[Optional[float], float, Optional[float], float]] = {}
                touched_aids = set()
                for ch in changes:
                    if not isinstance(ch, dict):
                        continue
                    aid = str(ch.get("asset_id") or "")
                    st = self._books.get(aid)
                    if not st or not st.get("ready"):
                        continue
                    if aid not in prev_tops:
                        prev_tops[aid] = _top_sig(st)
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
                    self._resub_attempts.pop(aid, None)
                    touched_aids.add(aid)
                    updated = True
                if updated:
                    self._last_ws_msg_t = ts
                    self.msg_count += 1
                    for aid in touched_aids:
                        st = self._books.get(aid)
                        if not st:
                            continue
                        nb_px, nb_sz, na_px, na_sz = _top_sig(st)
                        pb_px, pb_sz, pa_px, pa_sz = prev_tops.get(aid, (None, 0.0, None, 0.0))
                        # 价格变化或顶档深度剧变（>=25% 或 >=20 股）均立即唤醒主循环，消除队列抽空盲区
                        if nb_px != pb_px or na_px != pa_px:
                            top_changed = True
                            break
                        bid_diff = abs(nb_sz - pb_sz)
                        ask_diff = abs(na_sz - pa_sz)
                        if (
                            bid_diff >= 20.0
                            or ask_diff >= 20.0
                            or (pb_sz > 0.0 and bid_diff / pb_sz >= 0.25)
                            or (pa_sz > 0.0 and ask_diff / pa_sz >= 0.25)
                        ):
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
                    # Validate freshness of THIS specific token, not unrelated prewarmed tokens
                    if (t - src_t) > max_age_s:
                        return None
                # Tight single-token freeze guard (15s) so silent per-token drops cannot bypass deadman for 60s
                if src_t > 0.0 and (t - src_t) > max(max_age_s, 15.0):
                    # 单 Token 静默断流时主动触发原地重订自愈，避免整窗永久退化为慢速 REST 轮询
                    self._schedule_token_resubscribe_locked(str(token_id), t)
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

    async def _ws_worker(self):
        import websockets

        consec_fails = 0
        recv_timeout = min(1.0, self.idle_reconnect_s)
        while self.running:
            with self._lock:
                tokens = self._active_tokens
                gen = self._token_gen
                # 即将以最新 tokens 全量订阅，清空断连前残留的增量队列避免误退订
                self._pending_sub_tokens.clear()
                self._pending_unsub_tokens.clear()
                self._pending_resub_tokens.clear()
                self._last_resub_t.clear()
                self._resub_attempts.clear()
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
                    conn_wall = time.time()
                    with self._lock:
                        for aid in tokens:
                            self._token_sub_t[aid] = conn_wall
                    await asyncio.wait_for(
                        ws.send(json.dumps({"assets_ids": list(tokens), "type": "market"})),
                        timeout=5.0,
                    )
                    def _on_rtt(ms: float):
                        self.rtt_ms = ms
                        with self._lock:
                            self._last_pong_t = time.time()

                    ping_task = asyncio.create_task(
                        _ws_ping_rtt_loop(ws, _on_rtt, interval_s=5.0)
                    )
                    last_activity_t = time.monotonic()
                    last_token_audit_t = time.monotonic()
                    try:
                        while self.running:
                            now_mono = time.monotonic()
                            with self._lock:
                                cur_gen = self._token_gen
                                add_tokens = list(self._pending_sub_tokens)
                                rem_tokens = list(self._pending_unsub_tokens)
                                resub_tokens = list(self._pending_resub_tokens)
                                if add_tokens:
                                    self._pending_sub_tokens.clear()
                                if rem_tokens:
                                    self._pending_unsub_tokens.clear()
                                if resub_tokens:
                                    self._pending_resub_tokens.clear()
                                # 每 5 秒巡检已订阅 >=15s 的 Token：仅对缺失初始快照（未 ready）者补订，
                                # 绝不误杀刚预热的下一轮安静 Token 导致当前窗口断连。
                                if (now_mono - connected_at) >= 15.0 and (now_mono - last_token_audit_t) >= 5.0:
                                    last_token_audit_t = now_mono
                                    now_wall = time.time()
                                    add_set = set(add_tokens)
                                    for aid in self._active_tokens:
                                        if aid in add_set:
                                            continue
                                        sub_age = now_wall - float(self._token_sub_t.get(aid, 0.0) or 0.0)
                                        if sub_age < 15.0:
                                            continue
                                        st = self._books.get(aid)
                                        if not st or not st.get("ready"):
                                            self._schedule_token_resubscribe_locked(
                                                aid, now_wall, allow_reconnect_escalation=False
                                            )
                                    if self._pending_resub_tokens:
                                        resub_tokens.extend(t for t in self._pending_resub_tokens if t not in resub_tokens)
                                        self._pending_resub_tokens.clear()
                                    cur_gen = self._token_gen
                            if cur_gen != gen:
                                # 全量切换或交叉盘口/反复断流自愈 -> 平滑重连
                                self.connected = False
                                consec_fails = 0
                                break
                            if rem_tokens:
                                await asyncio.wait_for(
                                    ws.send(json.dumps({"assets_ids": rem_tokens, "operation": "unsubscribe"})),
                                    timeout=5.0,
                                )
                            if add_tokens:
                                await asyncio.wait_for(
                                    ws.send(json.dumps({"assets_ids": add_tokens, "operation": "subscribe"})),
                                    timeout=5.0,
                                )
                            if resub_tokens:
                                await asyncio.wait_for(
                                    ws.send(json.dumps({"assets_ids": resub_tokens, "operation": "unsubscribe"})),
                                    timeout=5.0,
                                )
                                await asyncio.wait_for(
                                    ws.send(json.dumps({"assets_ids": resub_tokens, "operation": "subscribe"})),
                                    timeout=5.0,
                                )

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
