"""Unit tests for PM-Quant Dual-Stage Strategy:
MWDE (Mid-Window Trend-Biased Discount Entry) + LWAA (Late-Window Asymmetric Absorption).

Verifies the mathematical invariants:
1. Stage 1 (tau in [120s, 240s]): When consolidation corridor holds and spot delta/probability favors Up,
   an Ask <= $0.45 triggers immediate trend-biased dip-buying.
2. Stage 1 Down: When spot delta favors Down, an Ask <= $0.45 triggers Down dip-buying.
3. Ceiling enforcement: Ask > $0.45 in Stage 1 is rejected (never chase expensive dips).
4. Strict anti-panic holding: Once entered, single-leg position is held to settlement (no panic unwinds).
5. Arbitrage hedge: Only hedges if combined pair_cost <= 0.85 (or <= 1.00).
6. Stage 2 (tau in [15s, 120s]): High-certainty absorption triggers at <= $0.65.
"""
import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.types import OrderBook, Position, SpotState, Decision
from core.policy import plan_decision


class TestDualStageStrategy(unittest.TestCase):
    def setUp(self):
        self.params = dict(
            config.PARAMS,
            MWDE_ENABLE=True,
            MWDE_MAX_TAU_S=240.0,
            MWDE_MAX_ENTRY_PRICE=0.45,
            PREV_ROUND_ABS_MOVE=35.0,
            FIRST_60S_RANGE=5.0,
        )

    def test_mwde_trend_biased_dip_buy_up(self):
        """Invariant 1: When spot delta > 0 and P* >= 0.50, buy Up dip when Ask <= 0.45."""
        pos = Position(0.0, 0.0)
        # Up Ask is 0.42 (<= 0.45 dip), Down Ask is 0.58
        book_up = OrderBook(bids=[(0.40, 20.0)], asks=[(0.42, 20.0)])
        book_dn = OrderBook(bids=[(0.55, 20.0)], asks=[(0.58, 20.0)])
        # Spot is 75005 vs open 75000 (delta_s = +5.0)
        spot = SpotState(timestamp=1100.0, price=75005.0, open_price=75000.0)

        # tau = 200s (elapsed 100s, within [120s, 240s])
        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=200.0, params=self.params, p_fair=0.52)
        self.assertEqual(dec.action, "taker_buy")
        self.assertEqual(dec.side, "Up")
        self.assertAlmostEqual(dec.price, 0.42)
        self.assertEqual(dec.size, 5.0)
        self.assertIn("MWDE Taker Absorption", dec.reason)

    def test_mwde_trend_biased_dip_buy_down(self):
        """Invariant 2: When spot delta < 0 and P* <= 0.50, buy Down dip when Ask <= 0.45."""
        pos = Position(0.0, 0.0)
        # Down Ask is 0.38 (<= 0.45 dip), Up Ask is 0.62
        book_up = OrderBook(bids=[(0.60, 20.0)], asks=[(0.62, 20.0)])
        book_dn = OrderBook(bids=[(0.35, 20.0)], asks=[(0.38, 20.0)])
        # Spot is 74995 vs open 75000 (delta_s = -5.0)
        spot = SpotState(timestamp=1100.0, price=74995.0, open_price=75000.0)

        # tau = 180s (elapsed 120s)
        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=180.0, params=self.params, p_fair=0.48)
        self.assertEqual(dec.action, "taker_buy")
        self.assertEqual(dec.side, "Down")
        self.assertAlmostEqual(dec.price, 0.38)
        self.assertEqual(dec.size, 5.0)
        self.assertIn("MWDE Taker Absorption", dec.reason)

    def test_mwde_rejects_expensive_ask(self):
        """Invariant 3: In Stage 1, asks > $0.45 are strictly rejected (no chase)."""
        pos = Position(0.0, 0.0)
        # Up Ask is 0.49 (> 0.45)
        book_up = OrderBook(bids=[(0.42, 20.0)], asks=[(0.49, 20.0)])
        book_dn = OrderBook(bids=[(0.50, 20.0)], asks=[(0.53, 20.0)])
        spot = SpotState(timestamp=1100.0, price=75005.0, open_price=75000.0)

        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=200.0, params=self.params, p_fair=0.52)
        # Should not taker buy at 0.49
        self.assertNotEqual(dec.action, "taker_buy")
        if dec.action == "quote":
            self.assertLessEqual(dec.price, 0.45)

    def test_holding_strictly_never_unwinds_and_holds_to_settlement(self):
        """Invariant 4: Once entered at <= 0.45, hold single leg to settlement; zero panic sells."""
        pos_up = Position(shares=5.0, cash=2.10) # 5 Up @ 0.42
        pos_dn = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.20, 50.0)], asks=[(0.30, 50.0)])
        book_dn = OrderBook(bids=[(0.70, 50.0)], asks=[(0.80, 50.0)])
        spot = SpotState(timestamp=1200.0, price=74990.0, open_price=75000.0)

        dec = plan_decision(pos_up, pos_dn, book_up, book_dn, spot, tau_s=100.0, params=self.params, p_fair=0.35)
        self.assertNotEqual(dec.action, "taker_unwind")
        self.assertEqual(dec.action, "wait")
        self.assertIn("Holding 5 Up", dec.reason)
        self.assertIn("死扛让子弹飞", dec.reason)

    def test_arbitrage_lock_when_opp_drops_deeply(self):
        """Invariant 5: Only buy opposite side if combined cost <= 0.85 (or 1.00)."""
        pos_up = Position(shares=5.0, cash=2.10) # 5 Up @ 0.42
        pos_dn = Position(0.0, 0.0)
        # Down Ask is 0.35: pair_cost = 0.42 + 0.35 + fee = 0.786 <= 0.85!
        book_up = OrderBook(bids=[(0.60, 50.0)], asks=[(0.65, 50.0)])
        book_dn = OrderBook(bids=[(0.30, 50.0)], asks=[(0.35, 50.0)])
        spot = SpotState(timestamp=1200.0, price=75020.0, open_price=75000.0)

        dec = plan_decision(pos_up, pos_dn, book_up, book_dn, spot, tau_s=100.0, params=self.params, p_fair=0.60)
        self.assertEqual(dec.action, "taker_hedge")
        self.assertEqual(dec.side, "Down")
        self.assertAlmostEqual(dec.price, 0.35)
        self.assertIn("Arbitrage lock", dec.reason)

    def test_fully_hedged_pair_holds_to_settlement(self):
        """Invariant 6: When both sides are held (e.g. 5 UP and 5 DN), strictly hold for guaranteed settlement."""
        pos_up = Position(shares=5.0, cash=2.10)
        pos_dn = Position(shares=5.0, cash=1.80)
        book_up = OrderBook(bids=[(0.60, 50.0)], asks=[(0.65, 50.0)])
        book_dn = OrderBook(bids=[(0.30, 50.0)], asks=[(0.35, 50.0)])
        spot = SpotState(timestamp=1200.0, price=75000.0, open_price=75000.0)

        dec = plan_decision(pos_up, pos_dn, book_up, book_dn, spot, tau_s=100.0, params=self.params, p_fair=0.50)
        self.assertEqual(dec.action, "wait")
        self.assertIn("fully hedged arbitrage pair", dec.reason.lower())


if __name__ == "__main__":
    unittest.main()
