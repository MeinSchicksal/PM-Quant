"""Unit tests for PM-Quant v2.3 Unified Net-Edge Engine invariants."""
import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.types import OrderBook, Position, SpotState
from core.policy import plan_decision


class TestV23UnifiedPolicy(unittest.TestCase):
    def setUp(self):
        self.params = dict(
            config.PARAMS,
            ENTRY_MIN_TAU_S=15.0,
            ENTRY_MAX_TAU_S=240.0,
            ENTRY_MIN_PROB=0.50,
            ENTRY_MIN_PRICE=0.20,
            ENTRY_MAX_PRICE=0.75,
            MIN_MAKER_EDGE=0.04,
            MIN_TAKER_EDGE=0.08,
            MAX_QUOTE_ATTEMPTS=3,
            ENABLE_ARBITRAGE_HEDGE=False,
        )

    def test_maker_quote_priority_when_edge_between_maker_and_taker_thresholds(self):
        """When net taker edge < 0.08 but maker edge >= 0.04, place zero-fee maker quote."""
        pos = Position(0.0, 0.0)
        # Up bid=0.40, ask=0.46. p_fair=0.52 -> maker_px=0.41 (edge=0.11>=0.04); taker ask=0.46+fee~0.477 (edge~0.043<0.08)
        book_up = OrderBook(bids=[(0.40, 20.0)], asks=[(0.46, 20.0)])
        book_dn = OrderBook(bids=[(0.53, 20.0)], asks=[(0.56, 20.0)])
        spot = SpotState(timestamp=1100.0, price=75005.0, open_price=75000.0)

        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=200.0, params=self.params, p_fair=0.52)
        self.assertEqual(dec.action, "quote")
        self.assertEqual(dec.side, "Up")
        self.assertAlmostEqual(dec.price, 0.41)
        self.assertEqual(dec.size, 5.0)
        self.assertIn("Maker Quote", dec.reason)

    def test_taker_ioc_allowed_when_net_edge_exceeds_min_taker_edge(self):
        """When net edge after official fee >= MIN_TAKER_EDGE (0.08), execute Taker IOC."""
        pos = Position(0.0, 0.0)
        # Up ask=0.38, fee~0.0165 -> eff=0.3965. p_fair=0.52 -> edge=0.1235 >= 0.08
        book_up = OrderBook(bids=[(0.35, 20.0)], asks=[(0.38, 20.0)])
        book_dn = OrderBook(bids=[(0.60, 20.0)], asks=[(0.63, 20.0)])
        spot = SpotState(timestamp=1100.0, price=75005.0, open_price=75000.0)

        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=200.0, params=self.params, p_fair=0.52)
        self.assertEqual(dec.action, "taker_buy")
        self.assertEqual(dec.side, "Up")
        self.assertAlmostEqual(dec.price, 0.38)
        self.assertEqual(dec.size, 5.0)

    def test_rejects_expensive_ask_beyond_entry_ceiling(self):
        """Prices above ENTRY_MAX_PRICE (0.75) are strictly skipped."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.78, 20.0)], asks=[(0.80, 20.0)])
        book_dn = OrderBook(bids=[(0.19, 20.0)], asks=[(0.21, 20.0)])
        spot = SpotState(timestamp=1100.0, price=75150.0, open_price=75000.0)

        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=100.0, params=self.params, p_fair=0.95)
        self.assertEqual(dec.action, "wait")

    def test_holding_preserves_convexity_when_hedge_disabled(self):
        """v2.3 holds Leg-1 to settlement without capping winners via pair_cost <= 1.00."""
        pos_up = Position(shares=5.0, cash=2.10)
        pos_dn = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.60, 50.0)], asks=[(0.65, 50.0)])
        book_dn = OrderBook(bids=[(0.33, 50.0)], asks=[(0.35, 50.0)])
        spot = SpotState(timestamp=1200.0, price=75020.0, open_price=75000.0)

        dec = plan_decision(pos_up, pos_dn, book_up, book_dn, spot, tau_s=100.0, params=self.params, p_fair=0.60)
        self.assertEqual(dec.action, "wait")
        self.assertIn("Holding 5 Up", dec.reason)

    def test_optional_deep_arbitrage_hedge_when_explicitly_enabled(self):
        """When ENABLE_ARBITRAGE_HEDGE=True, deep pair_cost <= 0.90 executes hedge."""
        pos_up = Position(shares=5.0, cash=2.10)  # @ 0.42
        pos_dn = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.60, 50.0)], asks=[(0.65, 50.0)])
        book_dn = OrderBook(bids=[(0.30, 50.0)], asks=[(0.35, 50.0)])
        spot = SpotState(timestamp=1200.0, price=75020.0, open_price=75000.0)

        p = dict(self.params, ENABLE_ARBITRAGE_HEDGE=True, ARBITRAGE_HEDGE_MAX_PAIR_COST=0.90)
        dec = plan_decision(pos_up, pos_dn, book_up, book_dn, spot, tau_s=100.0, params=p, p_fair=0.60)
        self.assertEqual(dec.action, "taker_hedge")
        self.assertEqual(dec.side, "Down")

    def test_fully_hedged_pair_holds_to_settlement(self):
        """Balanced Up/Down pair holds cleanly for $1.00 redemption."""
        pos_up = Position(shares=5.0, cash=2.10)
        pos_dn = Position(shares=5.0, cash=1.80)
        book_up = OrderBook(bids=[(0.60, 50.0)], asks=[(0.65, 50.0)])
        book_dn = OrderBook(bids=[(0.30, 50.0)], asks=[(0.35, 50.0)])
        spot = SpotState(timestamp=1200.0, price=75000.0, open_price=75000.0)

        dec = plan_decision(pos_up, pos_dn, book_up, book_dn, spot, tau_s=100.0, params=self.params, p_fair=0.50)
        self.assertEqual(dec.action, "wait")
        self.assertIn("fully hedged pair", dec.reason.lower())

    def test_forbids_buying_below_half_probability(self):
        """Never enter a side whose modelled win probability < 0.50, even if cheap."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.28, 20.0)], asks=[(0.29, 20.0)])
        book_dn = OrderBook(bids=[(0.70, 20.0)], asks=[(0.72, 20.0)])
        spot = SpotState(timestamp=1100.0, price=75010.0, open_price=75000.0)

        # Up has p=0.33 (<0.50), Down has p=0.67 (<0.72 ask, no edge) -> must WAIT
        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=200.0, params=self.params, p_fair=0.33)
        self.assertEqual(dec.action, "wait")
        self.assertIn("No executable Net-Edge", dec.reason)


if __name__ == "__main__":
    unittest.main()
