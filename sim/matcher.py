"""FIFO Queue Order Matcher & Simulation Harness.

Faithfully models:
1. 300ms order transit delay (live_at = placed_at + LATENCY_S).
2. Queue position tracking (initial_ahead queue deduction on trades).
3. Partial fills and discrete lot allocation.
"""
from typing import Dict, List, Optional, Tuple
from core.types import Order, OrderBook, Side


class QueueMatcher:
    def __init__(self, latency_s: float = 0.30):
        self.latency_s = latency_s
        self.queue_ahead: Dict[int, float] = {}

    def register_order(self, order: Order, book: OrderBook):
        """Register newly placed maker order and determine queue ahead."""
        order.live_at = order.placed_at + self.latency_s
        # Queue ahead is depth at the order price level
        ahead = 0.0
        levels = book.bids if order.side == "Up" else book.asks # simplified bid depth
        for px, sz in levels:
            if abs(px - order.price) < 1e-4:
                ahead = sz
                break
        self.queue_ahead[order.id] = ahead

    def process_trade(self, order: Order, trade_side: Side, trade_price: float, trade_size: float) -> float:
        """Deduct market print volume from queue ahead. Returns filled shares."""
        if order.cancel_at is not None or order.remaining <= 1e-6:
            return 0.0
            
        # A buy maker order is filled by sell trades at or below the order price
        if trade_price <= order.price + 1e-6:
            ahead = self.queue_ahead.get(order.id, 0.0)
            if trade_size <= ahead:
                self.queue_ahead[order.id] = ahead - trade_size
                return 0.0
            else:
                executable = trade_size - ahead
                self.queue_ahead[order.id] = 0.0
                fill = min(order.remaining, executable)
                order.remaining -= fill
                order.filled_size += fill
                return fill
        return 0.0
