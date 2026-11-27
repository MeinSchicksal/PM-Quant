"""Polymarket Real-Time WebSocket Trade Prints Stream.

Connects to Polymarket RTDS WebSocket, captures tick-by-tick real trade prints
for the active BTC 5-minute markets, and feeds exact trade volumes into the
QueueMatcher to drive physical queue depth deduction.
"""
import sys
import json
import queue
import time
import asyncio
import threading
from collections import deque
from pathlib import Path
from typing import List, Dict, Any, Optional, Set

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from runtime.alerts import notify as send_alert
from runtime.retention import is_raw_recording_allowed
from runtime.ws_feeds import (
    _BaseWSClient,
    _safe_float,
    _ws_ping_rtt_loop,
    _ws_reconnect_backoff,
)

WS_URL = "wss://ws-live-data.polymarket.com"


class TradeStreamClient(_BaseWSClient):
    def __init__(
        self,
        record_raw: bool = True,
        proxy: Optional[str] = None,
        idle_reconnect_s: float = 180.0,
        wake_event: Optional[threading.Event] = None,
    ):
        self.record_raw = record_raw
        self.proxy = proxy
        self.idle_reconnect_s = max(0.05, float(idle_reconnect_s))
        self._wake_event = wake_event
        self.running = False
        self.connected = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._reconnect_flag: bool = False
        self._write_queue: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=2048)
        self._writer_thread: Optional[threading.Thread] = None

        self._active_slugs: Set[str] = set()
        self._active_tokens: Set[str] = set()
        self._lock = threading.Lock()
        self._pending_trades: deque = deque(maxlen=1000)
        # Reconnect guard: the activity stream can replay prints already seen
        # before a disconnect. Feeding a print twice would double-consume the
        # maker queue and credit phantom fills.
        self._seen_keys: Set[tuple] = set()
        self._seen_order: deque = deque(maxlen=5000)

        self.last_trade_t = 0.0
        self.rtt_ms = 0.0
        self.trade_count = 0
        self.reconnect_count = 0
        self.last_error = ""
        self._last_err_log_t = 0.0
        self._dropped_unattributed = 0
        self._latest_price_by_token: Dict[str, tuple] = {}
        # Real-time Chainlink BTC/USD price pushed over the same RTDS WebSocket
        self._chainlink_btc_price: Optional[float] = None
        self._chainlink_btc_recv_t: float = 0.0
        self._chainlink_btc_oracle_t: float = 0.0
        self.chainlink_msg_count: int = 0
        self.chainlink_bytes: int = 0

    def get_latest_chainlink_btc(
        self, max_age_s: float = 3.5, now: Optional[float] = None
    ) -> Optional[tuple]:
        """返回 RTDS WS 最近 max_age_s 秒内的 Chainlink BTC/USD 推送 (price, recv_ts, oracle_ts, msg_count, bytes)。"""
        if not self.connected:
            return None
        t = time.time() if now is None else float(now)
        with self._lock:
            px = self._chainlink_btc_price
            recv_t = self._chainlink_btc_recv_t
            oracle_t = self._chainlink_btc_oracle_t
            cnt = self.chainlink_msg_count
            tot_b = self.chainlink_bytes
        if px is None or px <= 0.0 or recv_t <= 0.0:
            return None
        if (t - recv_t) > max_age_s:
            return None
        return (float(px), float(recv_t), float(oracle_t), int(cnt), int(tot_b))

    def get_latest_trade(
        self, token_id: str, max_age_s: float = 1.2, now: Optional[float] = None
    ) -> Optional[tuple]:
        """返回指定 Token 最近 max_age_s 秒内的 (最新成交价, 本地接收时间戳)，供盘口 WS 因果交叉校验。"""
        t = time.time() if now is None else float(now)
        with self._lock:
            rec = self._latest_price_by_token.get(str(token_id))
            if not rec:
                return None
            px, recv_t = rec
            if (t - recv_t) > max_age_s or px <= 0.0:
                return None
            return (float(px), float(recv_t))

    def clear_latest_price(self, token_id: str):
        """清除已消费的背离成交缓存，防止重连后被同一笔旧成交重复触发误杀。"""
        with self._lock:
            self._latest_price_by_token.pop(str(token_id), None)

    def get_latest_price(self, token_id: str, max_age_s: float = 3.0, now: Optional[float] = None) -> Optional[float]:
        """返回指定 Token 最近 max_age_s 秒内的真实逐笔成交价，供盘口 WS 滞后交叉校验。"""
        rec = self.get_latest_trade(token_id, max_age_s=max_age_s, now=now)
        return rec[0] if rec is not None else None

    def set_active_targets(self, slugs: List[str], token_ids: List[str] = None):
        """Update active slugs and token IDs to filter incoming trades."""
        with self._lock:
            self._active_slugs = set(s for s in slugs if s)
            if token_ids:
                self._active_tokens = set(t for t in token_ids if t)

    def drain_trades(self, slug: str = None, token_id: str = None) -> List[Dict[str, Any]]:
        """Atomically pop and return collected real trade prints."""
        trades = []
        with self._lock:
            remaining = deque(maxlen=1000)
            while self._pending_trades:
                tr = self._pending_trades.popleft()
                # A print without a slug cannot be attributed to any window.
                # The old `tr.get("slug") and ...` guard let those through, so
                # another window's volume could deplete this window's queue.
                if slug and tr.get("slug") != slug:
                    if tr.get("slug"):
                        remaining.append(tr)   # belongs to another (prewarmed) window
                    else:
                        self._dropped_unattributed += 1
                    continue
                if token_id and tr.get("token_id") and tr.get("token_id") != token_id:
                    remaining.append(tr)
                    continue
                trades.append(tr)
            self._pending_trades = remaining
        return trades

    def start(self):
        """Launch background WebSocket listener thread."""
        if self.running:
            return
        self.running = True
        if self.record_raw and (self._writer_thread is None or not self._writer_thread.is_alive()):
            self._writer_thread = threading.Thread(
                target=self._disk_writer_loop, daemon=True, name="TradeStreamDiskWriter"
            )
            self._writer_thread.start()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="TradeStreamWS")
        self._thread.start()

    def force_reconnect(self):
        """Request immediate clean reconnect (e.g. after proxy route failover)."""
        self._reconnect_flag = True

    def stop(self):
        """Stop background WebSocket client and flush writer thread."""
        super().stop()
        if self._writer_thread and self._writer_thread.is_alive():
            try:
                self._write_queue.put_nowait(None)
            except queue.Full:
                pass
            self._writer_thread.join(timeout=1.5)

    async def _ws_worker(self):
        import websockets
        consec_fails = 0
        while self.running:
            self._reconnect_flag = False
            connected_at = 0.0
            try:
                connect_kwargs = {
                    "ping_interval": 15,
                    "ping_timeout": 10,
                    "open_timeout": 5.0,
                }
                if self.proxy:
                    connect_kwargs["proxy"] = self.proxy
                async with websockets.connect(
                    WS_URL,
                    **connect_kwargs
                ) as ws:
                    self.connected = True
                    connected_at = time.monotonic()
                    # Subscribe to global trades activity stream + Chainlink BTC/USD oracle push
                    sub_msg = {
                        "action": "subscribe",
                        "subscriptions": [
                            {"topic": "activity", "type": "trades"},
                            {"topic": "crypto_prices_chainlink", "type": "*"},
                        ],
                    }
                    await asyncio.wait_for(ws.send(json.dumps(sub_msg)), timeout=5.0)
                    ping_task = asyncio.create_task(
                        _ws_ping_rtt_loop(ws, lambda ms: setattr(self, "rtt_ms", ms), interval_s=5.0)
                    )
                    # Application-level idle watchdog: RTDS can keep TCP/ping alive
                    # while silently stopping business trade pushes.
                    last_activity_t = time.monotonic()

                    recv_timeout = min(2.0, self.idle_reconnect_s)
                    try:
                        while self.running and not self._reconnect_flag:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                            except asyncio.TimeoutError:
                                raw = None

                            topic = ""
                            payload = None
                            raw_len = 0
                            if raw and raw.strip():
                                raw_len = len(raw.encode("utf-8", errors="ignore")) if isinstance(raw, str) else len(raw)
                                try:
                                    msg = json.loads(raw)
                                    if isinstance(msg, dict):
                                        topic = str(msg.get("topic") or "")
                                        p = msg.get("payload")
                                        if isinstance(p, dict) and p:
                                            payload = p
                                except Exception:
                                    payload = None

                            if payload is not None and topic == "crypto_prices_chainlink":
                                self._ingest_chainlink(payload, raw_bytes=raw_len)
                                # Do not reset trades idle watchdog on Chainlink ticks so a
                                # silent stall in the trades topic is still detected cleanly.
                                if (time.monotonic() - last_activity_t) >= self.idle_reconnect_s:
                                    raise TimeoutError(
                                        f"TradeStream idle watchdog: no trade payloads for {self.idle_reconnect_s:.1f}s"
                                    )
                                continue

                            if payload is None:
                                if (time.monotonic() - last_activity_t) >= self.idle_reconnect_s:
                                    raise TimeoutError(
                                        f"TradeStream idle watchdog: no trade payloads for {self.idle_reconnect_s:.1f}s"
                                    )
                                continue

                            last_activity_t = time.monotonic()
                            if consec_fails > 0 and (last_activity_t - connected_at) >= 30.0:
                                consec_fails = 0
                            self._ingest(payload)
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
                now_t = time.time()
                if now_t - self._last_err_log_t >= 30.0:
                    self._last_err_log_t = now_t
                    print(f"\n[TradeStream] WebSocket disconnected ({exc}); reconnecting...")
                if consec_fails in (5, 15) or any(c in self.last_error for c in ("403", "429")):
                    send_alert(
                        "trade_stream_ws_err",
                        "Polymarket TradeStream WS 异常断连",
                        f"连续失败 {consec_fails} 次，错误: {self.last_error[:180]}",
                        level="warn",
                    )
                if self.running:
                    await asyncio.sleep(_ws_reconnect_backoff(consec_fails, self.last_error, base_s=2.0, cap_s=30.0))

    def _ingest_chainlink(self, payload: Dict[str, Any], raw_bytes: int = 0) -> bool:
        """Ingest one RTDS crypto_prices_chainlink payload (keeps btc/usd only)."""
        sym = str(payload.get("symbol") or "").strip().lower()
        if sym != "btc/usd":
            return False
        px = _safe_float(payload.get("value"), 0.0)
        if not (1000.0 <= px <= 1_000_000.0):
            return False
        recv_t = time.time()
        raw_ts = _safe_float(payload.get("timestamp"), 0.0)
        if raw_ts > 1e11:
            raw_ts /= 1000.0
        oracle_t = raw_ts if raw_ts > 1e9 else recv_t
        with self._lock:
            self._chainlink_btc_price = float(px)
            self._chainlink_btc_recv_t = float(recv_t)
            self._chainlink_btc_oracle_t = float(oracle_t)
            self.chainlink_msg_count += 1
            self.chainlink_bytes += max(0, int(raw_bytes))
        if self._wake_event is not None:
            self._wake_event.set()
        return True

    def _ingest(self, payload: Dict[str, Any]) -> bool:
        """Filter, de-duplicate and queue one raw trade payload.

        Returns True when the print was newly accepted.
        """
        tr_slug = payload.get("slug") or payload.get("eventSlug") or ""
        tr_asset = payload.get("asset") or payload.get("asset_id") or ""

        # Check filter relevance
        with self._lock:
            is_relevant = False
            if tr_slug and tr_slug in self._active_slugs:
                is_relevant = True
            elif tr_asset and tr_asset in self._active_tokens:
                is_relevant = True
            elif not self._active_slugs and "btc-updown-5m" in tr_slug:
                is_relevant = True

        if not is_relevant:
            return False

        tx_id = str(payload.get("transactionHash") or payload.get("id") or payload.get("matchId") or "").strip()
        raw_ts = _safe_float(payload.get("timestamp"), time.time())
        # 兼容 Polymarket 偶尔返回 13 位毫秒级时间戳，防止落盘后卡死离线回放游标
        if raw_ts > 1e11:
            raw_ts /= 1000.0
        trade_record = {
            "token_id": tr_asset,
            "slug": tr_slug,
            "outcome": payload.get("outcome", ""),
            "price": _safe_float(payload.get("price", 0.0)),
            "size": _safe_float(payload.get("size", 0.0)),
            "side": payload.get("side", ""),
            "timestamp": raw_ts,
            "received_at": time.time()
        }
        # Prefer exchange transaction/match hash so distinct same-second trades at
        # identical price & size are never dropped due to whole-second RTDS timestamps.
        if tx_id:
            dedupe_key = (
                tx_id,
                trade_record["token_id"],
                str(trade_record["side"]).upper(),
                round(trade_record["price"], 6),
                round(trade_record["size"], 6),
            )
        else:
            dedupe_key = (
                trade_record["token_id"],
                str(trade_record["side"]).upper(),
                round(trade_record["timestamp"], 3),
                round(trade_record["price"], 6),
                round(trade_record["size"], 6),
            )
        with self._lock:
            if dedupe_key in self._seen_keys:
                return False
            if len(self._seen_order) == self._seen_order.maxlen:
                evicted = self._seen_order.popleft()
                self._seen_keys.discard(evicted)
            self._seen_order.append(dedupe_key)
            self._seen_keys.add(dedupe_key)
            self._pending_trades.append(trade_record)
            if trade_record["token_id"] and trade_record["price"] > 0.0:
                self._latest_price_by_token[str(trade_record["token_id"])] = (
                    float(trade_record["price"]),
                    float(trade_record["received_at"]),
                )
                if len(self._latest_price_by_token) > 64:
                    oldest_k = min(self._latest_price_by_token, key=lambda k: self._latest_price_by_token[k][1])
                    self._latest_price_by_token.pop(oldest_k, None)
            self.last_trade_t = trade_record["received_at"]
            self.trade_count += 1

        if self.record_raw:
            self._record_trade_event(trade_record)
        if self._wake_event is not None:
            self._wake_event.set()
        return True

    def _flush_trade_batch(self, batch: List[dict]):
        if not batch or not is_raw_recording_allowed():
            return
        try:
            today = time.strftime("%Y-%m-%d", time.gmtime())
            target_dir = config.MARKET_DIR / "events" / today
            target_dir.mkdir(parents=True, exist_ok=True)
            target_file = target_dir / f"trades_{today}.jsonl"
            lines = "".join(json.dumps(tr, ensure_ascii=False) + "\n" for tr in batch)
            with open(target_file, "a", encoding="utf-8") as f:
                f.write(lines)
        except Exception:
            pass

    def _disk_writer_loop(self):
        while True:
            try:
                first = self._write_queue.get(timeout=0.5)
            except queue.Empty:
                if not self.running:
                    break
                continue
            if first is None:
                break
            batch = [first]
            while len(batch) < 64:
                try:
                    nxt = self._write_queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self._flush_trade_batch(batch)
                    return
                batch.append(nxt)
            self._flush_trade_batch(batch)

    def _record_trade_event(self, trade: dict):
        if not is_raw_recording_allowed():
            return
        if self._writer_thread is not None and self._writer_thread.is_alive():
            try:
                self._write_queue.put_nowait(trade)
                return
            except queue.Full:
                return
        self._flush_trade_batch([trade])
