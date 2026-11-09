"""FIFO Queue Order Matcher & Microstructure Simulation.

Faithfully models:
1. 300ms order wire transit latency (live_at = placed_at + LATENCY_S).
2. Queue ahead tracking based on order book depth at order placement.
3. Queue exhaustion from market trade prints at or below the order price.
4. Immediate cross fills when best ask crosses down to or below our bid.
"""
from typing import Dict, Optional
from core.types import Order, OrderBook


class QueueMatcher:
    def __init__(self, latency_s: float = 0.30):
        self.latency_s = latency_s
        self.queue_ahead: Dict[int, float] = {}

    def register_order(self, order: Order, book: OrderBook):
        """Register newly placed maker order and record initial queue ahead."""
        order.live_at = (order.placed_at or 0.0) + self.latency_s
        # Initial queue ahead is the resting bid volume at the same price
        ahead = 0.0
        for px, sz in book.bids:
            if abs(px - order.price) < 1e-4:
                ahead = sz
                break
        self.queue_ahead[order.id] = ahead

    def on_trade_print(
        self,
        order: Order,
        trade_price: float,
        trade_size: float,
        now: float
    ) -> float:
        """Physical queue deduction driven by a real trade print event.
        
        Args:
            order: Active working maker order.
            trade_price: Real executed price in the trade print.
            trade_size: Exact executed volume in shares.
            now: Current timestamp.
            
        Returns:
            Filled shares on this print (0.0 if queue not yet exhausted).
        """
        if order.cancel_at is not None or order.remaining <= 1e-6:
            return 0.0
            
        # Physical latency constraint: cannot fill before arrival at matching engine
        if order.live_at is None or now < order.live_at:
            return 0.0

        # Maker bid fills when real trades execute at or below our bid price
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

    def match_order(
        self,
        order: Order,
        book: OrderBook,
        now: float,
        trade_volume: float = 5.0
    ) -> float:
        """Evaluate order against book cross and market trade prints.
        
        Returns:
            Filled shares on this tick (0.0 if still waiting in queue).
        """
        if order.cancel_at is not None or order.remaining <= 1e-6:
            return 0.0
            
        # Physical latency constraint: cannot fill before arrival at matching engine
        if order.live_at is None or now < order.live_at:
            return 0.0

        # Case 1: Crossed Book (Market ask is at or below our bid)
        if book.best_ask is not None and book.best_ask <= order.price + 1e-6:
            fill = order.remaining
            order.remaining = 0.0
            order.filled_size += fill
            self.queue_ahead[order.id] = 0.0
            return fill

        # Case 2: Market Trade Print at or below our bid level
        last_trade = book.last_trade_price
        if last_trade is not None and last_trade <= order.price + 1e-6:
            ahead = self.queue_ahead.get(order.id, 0.0)
            if trade_volume <= ahead:
                self.queue_ahead[order.id] = ahead - trade_volume
                return 0.0
            else:
                executable = trade_volume - ahead
                self.queue_ahead[order.id] = 0.0
                fill = min(order.remaining, executable)
                order.remaining -= fill
                order.filled_size += fill
                return fill

        return 0.0
