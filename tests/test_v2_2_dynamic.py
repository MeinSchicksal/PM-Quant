"""Unit tests for v2.3 Dynamic Quoting, Hysteresis, Defensive Salvage & Anti-Toxicity."""
import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.types import OrderBook, Position, SpotState, Decision, Order
from core.policy import plan_decision
from core.execution import plan_order_actions


class TestV23DynamicFeatures(unittest.TestCase):
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
            MAX_QUOTE_ATTEMPTS=1,
            MAX_QUEUE_WAIT_S=20.0,
        )

    def test_dynamic_maker_price_tracks_current_bid_when_ask_has_no_taker_edge(self):
        """When ask offers < MIN_TAKER_EDGE, maker quote anchors to current bid or improves 1 tick."""
        pos = Position(0.0, 0.0)
        # p_fair=0.78; ask=0.73+fee=0.7438 (edge=0.036 < 0.08); bid=0.71 -> improve to 0.72 (edge=0.06 >= 0.04)
        book_up = OrderBook(bids=[(0.71, 20.0)], asks=[(0.73, 20.0)])
        book_dn = OrderBook(bids=[(0.26, 20.0)], asks=[(0.28, 20.0)])
        spot = SpotState(timestamp=1250.0, price=75100.0, open_price=75000.0)

        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=50.0, params=self.params, p_fair=0.78)
        self.assertEqual(dec.action, "quote")
        self.assertEqual(dec.side, "Up")
        self.assertAlmostEqual(dec.price, 0.72)

    def test_no_one_second_urgent_self_cancellation(self):
        """Reaching MAX_QUOTE_ATTEMPTS does NOT set urgent_cancel=True; resting quote stays in queue."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.71, 20.0)], asks=[(0.73, 20.0)])
        book_dn = OrderBook(bids=[(0.26, 20.0)], asks=[(0.28, 20.0)])
        spot = SpotState(timestamp=1250.0, price=75100.0, open_price=75000.0)

        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=49.0, params=self.params, p_fair=0.78, quote_attempts=1)
        self.assertEqual(dec.action, "wait")
        self.assertFalse(dec.urgent_cancel)

        resting_order = Order(
            id=123, client_order_id="pm_1", side="Up", price=0.72,
            size=5.0, remaining=5.0, filled_size=0.0, purpose="entry",
            placed_at=1250.0, live_at=1250.3
        )
        to_cancel, next_action = plan_order_actions([resting_order], dec, now=1251.0, max_queue_wait_s=20.0)
        self.assertEqual(len(to_cancel), 0)
        self.assertIsNone(next_action)

    def test_quote_times_out_after_max_queue_wait(self):
        """Order is only cancelled once queue wait time >= MAX_QUEUE_WAIT_S (20s)."""
        dec = Decision(action="wait", reason="waiting for fill", urgent_cancel=False)
        resting_order = Order(
            id=123, client_order_id="pm_1", side="Up", price=0.58,
            size=5.0, remaining=5.0, filled_size=0.0, purpose="entry",
            placed_at=1000.0, live_at=1000.3
        )
        to_cancel, _ = plan_order_actions([resting_order], dec, now=1019.0, max_queue_wait_s=20.0)
        self.assertEqual(len(to_cancel), 0)

        to_cancel, _ = plan_order_actions([resting_order], dec, now=1021.0, max_queue_wait_s=20.0)
        self.assertEqual(len(to_cancel), 1)
        self.assertIn("排队超时撤单", to_cancel[0].cancel_reason)

    def test_anti_toxicity_shock_pulls_resting_quotes(self):
        """Rapid Binance spot move >= ANTI_TOXICITY_SHOCK_BPS pulls resting maker quotes."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.55, 20.0)], asks=[(0.57, 20.0)])
        book_dn = OrderBook(bids=[(0.42, 20.0)], asks=[(0.44, 20.0)])
        spot = SpotState(timestamp=1100.0, price=75030.0, open_price=75000.0, velocity_bps=3.0)

        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=180.0, params=self.params, p_fair=0.65)
        self.assertEqual(dec.action, "wait")
        self.assertTrue(dec.urgent_cancel)
        self.assertIn("Anti-toxicity", dec.reason)

    def test_dynamic_price_re_quote_on_market_movement(self):
        """When market price changes by >= 2 ticks, plan_order_actions re-quotes at new price."""
        resting_order = Order(
            id=123, client_order_id="pm_1", side="Up", price=0.55,
            size=5.0, remaining=5.0, filled_size=0.0, purpose="entry",
            placed_at=1000.0, live_at=1000.3
        )
        new_dec = Decision(action="quote", side="Up", price=0.58, size=5.0, utility=0.20, reason="dynamic price adjust")
        to_cancel, action = plan_order_actions([resting_order], new_dec, now=1005.0, max_queue_wait_s=20.0)
        self.assertEqual(len(to_cancel), 1)
        self.assertIn("盘口调价重挂", to_cancel[0].cancel_reason)
        self.assertEqual(action.price, 0.58)

    def test_entry_max_price_blocks_high_chase(self):
        """Prices above ENTRY_MAX_PRICE (0.75) are blocked from quoting or taking."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.76, 20.0)], asks=[(0.80, 20.0)])
        book_dn = OrderBook(bids=[(0.19, 20.0)], asks=[(0.22, 20.0)])
        spot = SpotState(timestamp=1250.0, price=75100.0, open_price=75000.0)

        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=50.0, params=self.params, p_fair=0.90)
        self.assertEqual(dec.action, "wait")

    def test_defensive_salvage_when_held_probability_collapses(self):
        """When tau <= 30s and held leg probability collapses (< 0.12), unwind at market bid."""
        pos_up = Position(shares=5.0, cash=2.75)
        pos_dn = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.08, 20.0)], asks=[(0.12, 20.0)])
        book_dn = OrderBook(bids=[(0.88, 20.0)], asks=[(0.92, 20.0)])
        spot = SpotState(timestamp=1275.0, price=74900.0, open_price=75000.0)

        dec = plan_decision(pos_up, pos_dn, book_up, book_dn, spot, tau_s=25.0, params=self.params, p_fair=0.05)
        self.assertEqual(dec.action, "taker_unwind")
        self.assertEqual(dec.side, "Up")
        self.assertAlmostEqual(dec.price, 0.08)


if __name__ == "__main__":
    unittest.main()
