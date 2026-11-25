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


def is_books_consistent(
    book_up: OrderBook,
    book_down: OrderBook,
    tolerance: float = 0.015,
) -> bool:
    """Verify dual-token orderbook consistency without fabricating prices.

    Rejects self-crossed books and severe arbitrage inversions while tolerating
    1-tick (~1.5c) async millisecond frame skew between independent Up/Down WS
    feeds so valid single-side trades are not spuriously dropped.
    """
    au, ad = book_up.best_ask, book_down.best_ask
    bu, bd = book_up.best_bid, book_down.best_bid
    if au is None or ad is None or bu is None or bd is None:
        return False
    if bu >= au or bd >= ad:
        return False
    tol = max(0.0, float(tolerance))
    if au + ad < 1.0 - tol - 1e-9 or bu + bd > 1.0 + tol + 1e-9:
        return False
    return True


class QueueMatcher:
    def __init__(self, latency_s: float = 0.30):
        self.latency_s = latency_s
        self.queue_ahead: Dict[int, float] = {}
        # A crossed book is a *stock* of resting liquidity, not a flow. The
        # runner calls match_order() on every tick, so without this guard the
        # same unchanged snapshot was consumed repeatedly: a 2-share ask level
        # could fill a 5-share resting order with zero trade prints.
        self._crossed_snapshot: Dict[int, tuple] = {}
        self._crossed_avail: Dict[int, float] = {}

    @staticmethod
    def _crossed_ask_key(book: OrderBook, limit_price: float) -> tuple:
        """Fingerprint ONLY the ask levels priced at or below `limit_price`.

        Unrelated bid or higher-ask size fluctuations must never reset the
        crossed-book guard and cause the same static ask depth to be consumed
        multiple times.
        """
        return tuple(
            (round(float(px), 6), round(float(sz), 6))
            for px, sz in (book.asks or [])
            if float(px) <= limit_price + 1e-6
        )

    @staticmethod
    def _bid_depth_at_price(book: OrderBook, price: float) -> float:
        for px, sz in (book.bids or []):
            if abs(float(px) - price) < 1e-4:
                return float(sz)
        return 0.0

    def register_order(self, order: Order, book: OrderBook, latency_s: float = None):
        """Register newly placed maker order and record provisional queue ahead."""
        lat = self.latency_s if latency_s is None else max(0.0, float(latency_s))
        order.live_at = (order.placed_at or 0.0) + lat
        order.activated = False
        self.queue_ahead[order.id] = self._bid_depth_at_price(book, order.price)
        self._crossed_snapshot.pop(order.id, None)
        self._crossed_avail.pop(order.id, None)

    def activate_order(
        self,
        order: Order,
        book: OrderBook,
        now: float,
        post_only: bool = True,
    ) -> bool:
        """Activate a maker order when it physically arrives at the CLOB (`now >= live_at`).

        Models two real Polymarket CLOB rules:
        1. Post-Only arrival rejection: if the book has already moved down so
           `best_ask <= order.price` when the wire packet lands, a Post-Only
           maker bid is rejected instead of getting a free zero-fee fill.
        2. Arrival-time FIFO queue placement: `queue_ahead` is re-anchored to
           the resting bid depth at `live_at`, eliminating free 300ms in-flight
           queue jumping when price-improving into a level others already joined.

        Returns True if order is active/resting (or not yet arrived), False if
        Post-Only rejected on arrival.
        """
        if order.remaining <= 1e-6 or order.activated:
            return True
        if order.live_at is None or now < order.live_at - 1e-9:
            return True
        if post_only and book is not None and book.best_ask is not None and book.best_ask <= order.price + 1e-6:
            order.cancel_at = now
            order.cancel_effective_at = now
            order.cancel_reason = f"Post-Only 到达穿盘拒单(挂买=${order.price:.2f}, 现卖一=${book.best_ask:.2f})"
            return False
        order.activated = True
        if book is not None and book.bids:
            self.queue_ahead[order.id] = self._bid_depth_at_price(book, order.price)
        self._crossed_snapshot.pop(order.id, None)
        self._crossed_avail.pop(order.id, None)
        return True

    def sync_queue_depth(
        self,
        order: Order,
        book: OrderBook,
        now: float,
        conservative_mult: float = 1.03,
    ) -> float:
        """Conservatively cap queue_ahead using live visible L2 depth at our bid price.

        Physical conservation guarantee (never looser than real CLOB):
        Since our simulated order is NOT actually resting on the venue book,
        `vis_depth` is 100% composed of *other* real participants at `order.price`.
        Therefore, the true queue ahead of us can NEVER exceed `vis_depth` (some
        of `vis_depth` may even have joined *behind* us). Multiplying by a small
        `conservative_mult` (default 1.03, ~3% stricter) fixes the pathological
        phantom-queue bug where makers ahead cancel 95% of a 600-share level while
        the simulator stayed frozen at 600, without ever inventing optimistic fills.

        Never increases `queue_ahead` when new joiners arrive behind us, and never
        zeros out queue_ahead purely because a momentary empty snapshot arrived
        while best_bid is still above our price.
        """
        if order.remaining <= 1e-6 or not order.activated or book is None:
            return self.queue_ahead.get(order.id, 0.0)
        if order.live_at is None or now < order.live_at - 1e-9:
            return self.queue_ahead.get(order.id, 0.0)
        if not book.bids:
            return self.queue_ahead.get(order.id, 0.0)
        # Only cap against visible depth if our price lies within the published
        # bid ladder; if the feed truncated deeper levels, vis_depth==0 is NOT
        # evidence that the queue ahead cleared.
        lowest_vis_bid = min(float(px) for px, _ in book.bids)
        if order.price < lowest_vis_bid - 1e-6:
            return self.queue_ahead.get(order.id, 0.0)
        ahead = self.queue_ahead.get(order.id, 0.0)
        if ahead <= 1e-9:
            return 0.0
        vis_depth = self._bid_depth_at_price(book, order.price)
        mult = max(1.0, float(conservative_mult))
        capped = min(ahead, vis_depth * mult)
        if capped < ahead:
            self.queue_ahead[order.id] = capped
        return self.queue_ahead.get(order.id, 0.0)

    def on_trade_print(
        self,
        order: Order,
        trade_price: float,
        trade_size: float,
        now: float,
        trade_side: str = "",
    ) -> float:
        """Physical queue deduction driven by a real trade print event.

        Args:
            order: Active working maker order.
            trade_price: Real executed price in the trade print.
            trade_size: Exact executed volume in shares.
            now: Timestamp of the print (not of the polling tick).
            trade_side: Optional taker direction ('BUY' or 'SELL'). An aggressive
                BUY print at our exact bid price lifted an ask and does not hit
                our resting bid queue unless price strictly traded through below us.

        Returns:
            Filled shares on this print (0.0 if queue not yet exhausted).
        """
        if order.remaining <= 1e-6:
            return 0.0
        cancel_cutoff = getattr(order, "cancel_effective_at", None)
        if cancel_cutoff is not None:
            if now >= cancel_cutoff - 1e-9:
                return 0.0
        elif order.cancel_at is not None:
            return 0.0

        # Physical latency constraint: cannot fill before arrival at matching engine
        if order.live_at is None or now < order.live_at:
            return 0.0

        # Do not credit an aggressive BUY trade at our exact bid price as hitting our resting bid
        side_up = str(trade_side or "").upper()
        if side_up == "BUY" and trade_price >= order.price - 1e-4:
            return 0.0

        # Maker bid fills when real trades execute at or below our bid price
        # (1e-4 slack absorbs Polymarket CLOB merge/mint float rounding like 0.120000014)
        if trade_price <= order.price + 1e-4:
            # A real print changes the queue, so a later (new) snapshot may be
            # matched again. The runner's print_touched_ids guard already skips
            # the stale snapshot on this same tick.
            order.activated = True
            self._crossed_snapshot.pop(order.id, None)
            self._crossed_avail.pop(order.id, None)
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

    def taker_ioc_fill(
        self,
        book: OrderBook,
        size: float,
        limit_price: float,
        is_buy: bool = True,
    ) -> Tuple[float, float]:
        """Depth-aware IOC limit execution against the visible book.

        Only consumes levels at or better than `limit_price` (<= limit_price for
        buys, >= limit_price for sells), matching real exchange IOC limit behavior
        when an order arrives after network transit delay.
        """
        if is_buy:
            levels = [(px, sz) for px, sz in (book.asks or []) if px <= limit_price + 1e-6]
        else:
            levels = [(px, sz) for px, sz in (book.bids or []) if px >= limit_price - 1e-6]
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
        if order.remaining <= 1e-6:
            return 0.0
        cancel_cutoff = getattr(order, "cancel_effective_at", None)
        if cancel_cutoff is not None:
            if now >= cancel_cutoff - 1e-9:
                return 0.0
        elif order.cancel_at is not None:
            return 0.0

        # Physical latency constraint: cannot fill before arrival at matching engine
        if order.live_at is None or now < order.live_at:
            return 0.0

        # Case: Not crossed -> reset crossed-book stock tracking and wait
        if book.best_ask is None or book.best_ask > order.price + 1e-6:
            self._crossed_snapshot.pop(order.id, None)
            self._crossed_avail.pop(order.id, None)
            return 0.0

        # Fingerprint ONLY asks <= order.price so irrelevant bid/high-ask churn
        # cannot re-consume the same static ask stock across ticks.
        snap = self._crossed_ask_key(book, order.price)
        if self._crossed_snapshot.get(order.id) == snap:
            return 0.0
        self._crossed_snapshot[order.id] = snap

        # Only liquidity priced at or below our bid can hit it.
        available = sum(
            float(sz) for px, sz in book.asks if px <= order.price + 1e-6
        )
        prev_avail = self._crossed_avail.get(order.id, 0.0)
        self._crossed_avail[order.id] = available
        if available <= 1e-9:
            return 0.0

        ahead = self.queue_ahead.get(order.id, 0.0)
        strictly_through = book.best_ask < order.price - 1e-6
        if ahead > 1e-9 and not strictly_through:
            # At exact bid==ask touch with a queue ahead of us, only net-new ask
            # liquidity above the previously seen resting stock can eat queue;
            # shrinking or oscillating resting asks must never drain queue_ahead.
            effective_flow = max(0.0, available - prev_avail)
            if effective_flow <= 1e-9:
                return 0.0
        else:
            effective_flow = available

        if effective_flow <= ahead:
            self.queue_ahead[order.id] = ahead - effective_flow
            return 0.0

        executable = effective_flow - ahead
        self.queue_ahead[order.id] = 0.0
        fill = min(order.remaining, executable)
        order.remaining -= fill
        order.filled_size += fill
        return fill
