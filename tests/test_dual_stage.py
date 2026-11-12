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

    def test_mwde_maker_quote_priority_over_taker(self):
        """Invariant 1: When book offers normal prices (sum of asks >= 0.90), priority is maker quoting, not early taker eating."""
        pos = Position(0.0, 0.0)
        # Up bid=0.40, ask=0.42. Down ask=0.58. Sum of asks = 0.42 + 0.58 = 1.00 >= 0.90
        book_up = OrderBook(bids=[(0.40, 20.0)], asks=[(0.42, 20.0)])
        book_dn = OrderBook(bids=[(0.55, 20.0)], asks=[(0.58, 20.0)])
        spot = SpotState(timestamp=1100.0, price=75005.0, open_price=75000.0)

        # tau = 200s (elapsed 100s, within [120s, 240s])
        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=200.0, params=self.params, p_fair=0.52)
        # Because sum of asks is 1.00 >= 0.90, policy must try MAKER QUOTE first (bid+0.01 = 0.41)
        self.assertEqual(dec.action, "quote")
        self.assertEqual(dec.side, "Up")
        self.assertAlmostEqual(dec.price, 0.41)
        self.assertEqual(dec.size, 5.0)
        self.assertIn("MWDE Maker Absorption", dec.reason)

    def test_mwde_taker_allowed_when_pair_sum_below_point_nine(self):
        """Invariant 2: When both asks sum to < 0.90 (deep discount arb), taking is permitted."""
        pos = Position(0.0, 0.0)
        # Up Ask is 0.38 (<= 0.45 dip), Down Ask is 0.50. Sum of asks = 0.38 + 0.50 = 0.88 < 0.90!
        # Quote attempts already reached 1, so quoting path is skipped
        book_up = OrderBook(bids=[(0.35, 20.0)], asks=[(0.38, 20.0)])
        book_dn = OrderBook(bids=[(0.48, 20.0)], asks=[(0.50, 20.0)])
        spot = SpotState(timestamp=1100.0, price=74995.0, open_price=75000.0)

        # Down trend: delta_s < 0, p_fair = 0.48
        # pref_side is Down. Down ask is 0.50 (> 0.45, so won't buy). Let's test Up trend:
        spot_up = SpotState(timestamp=1100.0, price=75005.0, open_price=75000.0)
        dec = plan_decision(pos, pos, book_up, book_dn, spot_up, tau_s=200.0, params=self.params, p_fair=0.52, quote_attempts=1)
        self.assertEqual(dec.action, "taker_buy")
        self.assertEqual(dec.side, "Up")
        self.assertAlmostEqual(dec.price, 0.38)
        self.assertEqual(dec.size, 5.0)
        self.assertIn("两侧合计极度便宜", dec.reason)

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

    def test_mwde_trend_strictly_forbids_counter_trend_cheap_dip(self):
        """Invariant 7: When spot delta > 0 (Bitcoin rising in first half), strictly forbid buying cheap Down side."""
        pos = Position(0.0, 0.0)
        # Up Ask is 0.60 (too expensive for MWDE <= 0.45), Down Ask is 0.40 (temptingly cheap!)
        book_up = OrderBook(bids=[(0.55, 20.0)], asks=[(0.60, 20.0)])
        book_dn = OrderBook(bids=[(0.35, 20.0)], asks=[(0.40, 20.0)])
        # Spot is up +8.0 (rising trend)
        spot = SpotState(timestamp=1100.0, price=75008.0, open_price=75000.0)

        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=200.0, params=self.params, p_fair=0.55)
        # Even though Down is cheap at 0.40, because trend is UP, policy MUST NOT buy Down! It must wait instead of catching falling knives!
        self.assertEqual(dec.action, "wait")
        self.assertIn("严格顺势防守", dec.reason)
        self.assertIn("严禁反向抄底便宜侧 Down", dec.reason)

    def test_mwde_down_trend_strictly_forbids_counter_trend_cheap_up(self):
        """Invariant 8: When spot delta < 0 (Bitcoin falling in first half), strictly forbid buying cheap Up side."""
        pos = Position(0.0, 0.0)
        # Down Ask is 0.60 (too expensive for MWDE <= 0.45), Up Ask is 0.40 (temptingly cheap!)
        book_up = OrderBook(bids=[(0.35, 20.0)], asks=[(0.40, 20.0)])
        book_dn = OrderBook(bids=[(0.55, 20.0)], asks=[(0.60, 20.0)])
        # Spot is down -8.0 (falling trend)
        spot = SpotState(timestamp=1100.0, price=74992.0, open_price=75000.0)

        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=200.0, params=self.params, p_fair=0.45)
        # Even though Up is cheap at 0.40, because trend is DOWN, policy MUST NOT buy Up! It must wait instead of catching falling knives!
        self.assertEqual(dec.action, "wait")
        self.assertIn("严格顺势防守", dec.reason)
        self.assertIn("严禁反向抄底便宜侧 Up", dec.reason)

    def test_mwde_strictly_forbids_entry_when_prob_below_half(self):
        """Invariant 9: When probability is below 0.50 (e.g. p_up=0.33 while delta_s > 0), strictly forbid buying the low-prob side."""
        pos = Position(0.0, 0.0)
        # Up is cheap at 0.29, Down is 0.71. But Up probability is only 0.33 (< 0.50)!
        book_up = OrderBook(bids=[(0.28, 20.0)], asks=[(0.29, 20.0)])
        book_dn = OrderBook(bids=[(0.70, 20.0)], asks=[(0.71, 20.0)])
        # Binance spot is up +5.0, but Polymarket settlement strike makes p_up only 0.33!
        spot = SpotState(timestamp=1100.0, price=75005.0, open_price=75000.0)

        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=200.0, params=self.params, p_fair=0.33)
        self.assertEqual(dec.action, "wait")
        self.assertIn("严禁介入劣势方", dec.reason)


if __name__ == "__main__":
    unittest.main()
