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


def _clamp_px(px: float) -> float:
    return min(0.99, max(0.01, px))


def make_complementary(book_up: OrderBook, book_down: OrderBook) -> Tuple[OrderBook, OrderBook]:
    """Enforce no-arbitrage consistency between the two complementary token books.

    Up and Down redeem to $1.00 together, so buying both asks must cost >= $1.00
    and selling both bids must yield <= $1.00. The venue feeds the two books as
    independent snapshots that momentarily disagree near expiry (recorded ask
    sums as low as $0.80); consuming them verbatim let the arbitrage-hedge leg
    book a sub-$1 pair as risk-free profit. Shift the too-cheap asks up and the
    too-rich bids down by the minimal amount that removes the impossible free
    money. Already-consistent snapshots -- including legitimate post-move
    arbitrage locks -- are returned unchanged.
    """
    au, ad = book_up.best_ask, book_down.best_ask
    ask_delta = 0.0
    if au is not None and ad is not None and au + ad < 1.0 - 1e-9:
        ask_delta = (1.0 - (au + ad)) / 2.0
    bu, bd = book_up.best_bid, book_down.best_bid
    bid_delta = 0.0
    if bu is not None and bd is not None and bu + bd > 1.0 + 1e-9:
        bid_delta = -((bu + bd) - 1.0) / 2.0
    if ask_delta == 0.0 and bid_delta == 0.0:
        return book_up, book_down

    def shift(book: OrderBook) -> OrderBook:
        return OrderBook(
            bids=[(_clamp_px(px + bid_delta), sz) for px, sz in book.bids],
            asks=[(_clamp_px(px + ask_delta), sz) for px, sz in book.asks],
            last_trade_price=book.last_trade_price,
            source_t=book.source_t,
            received_at=book.received_at,
        )

    return shift(book_up), shift(book_down)


class QueueMatcher:
    def __init__(self, latency_s: float = 0.30):
        self.latency_s = latency_s
        self.queue_ahead: Dict[int, float] = {}
        # A crossed book is a *stock* of resting liquidity, not a flow. The
        # runner calls match_order() on every tick, so without this guard the
        # same unchanged snapshot was consumed repeatedly: a 2-share ask level
        # could fill a 5-share resting order with zero trade prints.
        self._crossed_snapshot: Dict[int, tuple] = {}

    @staticmethod
    def _snapshot_key(book: OrderBook) -> tuple:
        """Content fingerprint of the visible ladder (timestamps excluded)."""
        return (
            tuple((round(float(px), 6), round(float(sz), 6)) for px, sz in (book.asks or [])),
            tuple((round(float(px), 6), round(float(sz), 6)) for px, sz in (book.bids or [])),
        )

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
        self._crossed_snapshot.pop(order.id, None)

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
            # A real print changes the queue, so a later (new) snapshot may be
            # matched again. The runner's print_touched_ids guard already skips
            # the stale snapshot on this same tick.
            self._crossed_snapshot.pop(order.id, None)
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

        # One fill opportunity per distinct book snapshot. A static ladder is
        # not a stream of new sell orders and must never be consumed twice.
        snap = self._snapshot_key(book)
        if self._crossed_snapshot.get(order.id) == snap:
            return 0.0
        self._crossed_snapshot[order.id] = snap

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
