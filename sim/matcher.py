"""FIFO Queue Order Matcher & Microstructure Simulation.

Faithfully models:
1. 300ms order wire transit latency (live_at = placed_at + LATENCY_S).
2. Queue ahead tracking based on order book depth at order placement.
3. Queue exhaustion from market trade prints at or below the order price.
4. Immediate cross fills when best ask crosses down to or below our bid,
   capped by the visible ask depth that is actually available.

There is deliberately NO synthetic volume here. A resting maker order can only
fill from (a) a real trade print that executes at or below its price, or
(b) a genuinely crossed book. Callers must pass the trade prints recorded /
streamed from the venue; absence of prints means absence of fills.
"""
from typing import Dict, Iterable, Tuple

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
                ahead = float(sz)
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
            now: Timestamp of the print (not of the polling tick).

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
            executable = trade_size - ahead
            self.queue_ahead[order.id] = 0.0
            fill = min(order.remaining, executable)
            order.remaining -= fill
            order.filled_size += fill
            return fill

        return 0.0

    @staticmethod
    def walk_levels(levels: Iterable[Tuple[float, float]], size: float) -> Tuple[float, float]:
        """Consume a price-level ladder for `size` shares.

        Returns:
            (filled_shares, volume_weighted_average_price)
        """
        filled = 0.0
        notional = 0.0
        for px, sz in levels or []:
            if filled >= size - 1e-9:
                break
            take = min(size - filled, float(sz))
            if take <= 0:
                continue
            filled += take
            notional += take * float(px)
        if filled <= 1e-9:
            return 0.0, 0.0
        return filled, notional / filled

    def taker_fill(self, book: OrderBook, size: float, is_buy: bool = True) -> Tuple[float, float]:
        """Depth-aware immediate execution against the visible book.

        A taker order cannot fill more than the liquidity currently resting on
        the book; the returned VWAP is the true cost of walking the ladder.
        """
        levels = book.asks if is_buy else book.bids
        return self.walk_levels(levels, size)

    def match_order(
        self,
        order: Order,
        book: OrderBook,
        now: float
    ) -> float:
        """Evaluate a resting maker order against a crossed book only.

        Trade-print driven queue depletion is handled by `on_trade_print`; this
        method never invents volume from a stale `last_trade_price`.

        Returns:
            Filled shares on this tick (0.0 if still waiting in queue).
        """
        if order.cancel_at is not None or order.remaining <= 1e-6:
            return 0.0

        # Physical latency constraint: cannot fill before arrival at matching engine
        if order.live_at is None or now < order.live_at:
            return 0.0

        # Case: Crossed book (market ask is at or below our bid)
        if book.best_ask is None or book.best_ask > order.price + 1e-6:
            return 0.0

        # Only liquidity priced at or below our bid can hit it.
        available = sum(
            float(sz) for px, sz in book.asks if px <= order.price + 1e-6
        )
        if available <= 1e-9:
            return 0.0

        ahead = self.queue_ahead.get(order.id, 0.0)
        if available <= ahead:
            self.queue_ahead[order.id] = ahead - available
            return 0.0

        executable = available - ahead
        self.queue_ahead[order.id] = 0.0
        fill = min(order.remaining, executable)
        order.remaining -= fill
        order.filled_size += fill
        return fill
