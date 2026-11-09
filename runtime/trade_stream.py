"""Polymarket Real-Time WebSocket Trade Prints Stream.

Connects to Polymarket RTDS WebSocket, captures tick-by-tick real trade prints
for the active BTC 5-minute markets, and feeds exact trade volumes into the
QueueMatcher to drive physical queue depth deduction.
"""
import sys
import json
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

WS_URL = "wss://ws-live-data.polymarket.com"


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


class TradeStreamClient:
    def __init__(self, record_raw: bool = True):
        self.record_raw = record_raw
        self.running = False
        self.connected = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self._active_slugs: Set[str] = set()
        self._active_tokens: Set[str] = set()
        self._lock = threading.Lock()
        self._pending_trades: deque = deque(maxlen=1000)

        self.last_trade_t = 0.0
        self.trade_count = 0

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
                match = True
                if slug and tr.get("slug") and tr.get("slug") != slug:
                    match = False
                if token_id and tr.get("token_id") and tr.get("token_id") != token_id:
                    match = False

                if match:
                    trades.append(tr)
                else:
                    remaining.append(tr)
            self._pending_trades = remaining
        return trades

    def start(self):
        """Launch background WebSocket listener thread."""
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="TradeStreamWS")
        self._thread.start()

    def stop(self):
        """Stop background WebSocket client."""
        self.running = False
        self.connected = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._ws_worker())
        except Exception as e:
            pass
        finally:
            self._loop.close()

    async def _ws_worker(self):
        import websockets
        while self.running:
            try:
                async with websockets.connect(
                    WS_URL,
                    ping_interval=15,
                    ping_timeout=10,
                    open_timeout=5.0
                ) as ws:
                    self.connected = True
                    # Subscribe to global trades activity stream
                    sub_msg = {
                        "action": "subscribe",
                        "subscriptions": [{"topic": "activity", "type": "trades"}]
                    }
                    await ws.send(json.dumps(sub_msg))

                    while self.running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            continue

                        if not raw or not raw.strip():
                            continue

                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue

                        payload = msg.get("payload") if isinstance(msg, dict) else None
                        if not payload or not isinstance(payload, dict):
                            continue

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

                        if is_relevant:
                            trade_record = {
                                "token_id": tr_asset,
                                "slug": tr_slug,
                                "outcome": payload.get("outcome", ""),
                                "price": _safe_float(payload.get("price", 0.0)),
                                "size": _safe_float(payload.get("size", 0.0)),
                                "side": payload.get("side", ""),
                                "timestamp": _safe_float(payload.get("timestamp"), time.time()),
                                "received_at": time.time()
                            }
                            with self._lock:
                                self._pending_trades.append(trade_record)
                                self.last_trade_t = trade_record["received_at"]
                                self.trade_count += 1

                            if self.record_raw:
                                self._record_trade_event(trade_record)

            except Exception:
                self.connected = False
                if self.running:
                    await asyncio.sleep(2.0)

    def _record_trade_event(self, trade: dict):
        try:
            today = time.strftime("%Y-%m-%d", time.gmtime())
            target_dir = config.MARKET_DIR / "events" / today
            target_dir.mkdir(parents=True, exist_ok=True)
            target_file = target_dir / f"trades_{today}.jsonl"
            with open(target_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(trade, ensure_ascii=False) + "\n")
        except Exception:
            pass
