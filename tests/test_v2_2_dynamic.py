"""Unit tests for PM-Quant v2.2 Continuous Dynamic Absorption & Dynamic Quoting:
1. Dynamic price tracking: Quotes join bid or improve inside spread, never quoting fixed/ridiculously low prices.
2. Anti-self-cancellation: Reaching quote_attempts does NOT set urgent_cancel=True; resting quote survives in queue.
3. Order price adjustment: When market moves >= 2 ticks, plan_order_actions generates "盘口调价重挂".
4. Hard chase ceiling: Asks > $0.70 strictly reject taker chase and clamp bids to <= $0.65.
5. Queue timeout: Quotes wait full MAX_QUEUE_WAIT_S (20s) before timing out.
"""
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


class TestV22DynamicFeatures(unittest.TestCase):
    def setUp(self):
        self.params = dict(
            config.PARAMS,
            STRATEGY_MODE="LWAA",
            LWAA_MAX_TAU_S=120.0,
            LWAA_MIN_TAU_S=15.0,
            LWAA_MIN_PROB=0.80,
            LWAA_MAX_ENTRY_PRICE=0.65,
            LWAA_HARD_CHASE_CEILING=0.70,
            LWAA_MAX_QUOTE_ATTEMPTS=1,
            MAX_QUEUE_WAIT_S=20.0
        )

    def test_dynamic_maker_price_tracks_current_bid(self):
        """Invariant 1: Maker quote dynamically anchors to current market bid (not a fixed 0.65 or low 0.35)."""
        pos = Position(0.0, 0.0)
        # Market bid is 0.58, ask is 0.68
        book_up = OrderBook(bids=[(0.58, 20.0)], asks=[(0.68, 20.0)])
        book_dn = OrderBook(bids=[(0.28, 20.0)], asks=[(0.32, 20.0)])
        spot = SpotState(timestamp=1250.0, price=75100.0, open_price=75000.0)

        # tau = 50s, p_fair = 0.90
        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=50.0, params=self.params, p_fair=0.90)
        self.assertEqual(dec.action, "quote")
        self.assertEqual(dec.side, "Up")
        # Price should track market bid (0.58)
        self.assertAlmostEqual(dec.price, 0.58)
        self.assertIn("结合市价挂单", dec.reason)

    def test_dynamic_maker_price_improves_inside_spread(self):
        """Invariant 2: When best_bid + 0.01 < best_ask and <= 0.65, quote joins bid or improves."""
        pos = Position(0.0, 0.0)
        # Market bid is 0.62, ask is 0.68
        book_up = OrderBook(bids=[(0.62, 20.0)], asks=[(0.68, 20.0)])
        book_dn = OrderBook(bids=[(0.28, 20.0)], asks=[(0.32, 20.0)])
        spot = SpotState(timestamp=1250.0, price=75100.0, open_price=75000.0)

        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=50.0, params=self.params, p_fair=0.90)
        self.assertEqual(dec.action, "quote")
        self.assertLessEqual(dec.price, 0.65)
        self.assertGreaterEqual(dec.price, 0.62)

    def test_no_one_second_urgent_self_cancellation(self):
        """Invariant 3: Reaching quote_attempts does NOT trigger urgent_cancel; resting order stays in queue."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.58, 20.0)], asks=[(0.68, 20.0)])
        book_dn = OrderBook(bids=[(0.28, 20.0)], asks=[(0.32, 20.0)])
        spot = SpotState(timestamp=1250.0, price=75100.0, open_price=75000.0)

        # Tick 2: quote_attempts is 1 (already maxed)
        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=49.0, params=self.params, p_fair=0.90, quote_attempts=1)
        self.assertEqual(dec.action, "wait")
        self.assertFalse(dec.urgent_cancel, "Must NOT set urgent_cancel=True on quote attempt limit!")

        # Verify plan_order_actions does NOT cancel the working order after only 1s
        now = 1251.0
        resting_order = Order(
            id=123, client_order_id="pm_1", side="Up", price=0.58,
            size=5.0, remaining=5.0, filled_size=0.0, purpose="entry",
            placed_at=1250.0, live_at=1250.3
        )
        to_cancel, next_action = plan_order_actions([resting_order], dec, now=now, max_queue_wait_s=20.0)
        self.assertEqual(len(to_cancel), 0, "Resting order must NOT be cancelled after only 1 second!")
        self.assertIsNone(next_action)

    def test_quote_times_out_after_max_queue_wait(self):
        """Invariant 4: Order is only cancelled once queue wait time >= MAX_QUEUE_WAIT_S (20s)."""
        dec = Decision(action="wait", reason="waiting for fill", urgent_cancel=False)
        order_time = 1000.0
        resting_order = Order(
            id=123, client_order_id="pm_1", side="Up", price=0.58,
            size=5.0, remaining=5.0, filled_size=0.0, purpose="entry",
            placed_at=order_time, live_at=order_time + 0.3
        )

        # At t = 1019s (19s elapsed < 20s): no cancel
        to_cancel, _ = plan_order_actions([resting_order], dec, now=1019.0, max_queue_wait_s=20.0)
        self.assertEqual(len(to_cancel), 0)

        # At t = 1021s (21s elapsed >= 20s): timeout cancel
        to_cancel, _ = plan_order_actions([resting_order], dec, now=1021.0, max_queue_wait_s=20.0)
        self.assertEqual(len(to_cancel), 1)
        self.assertIn("排队超时撤单", to_cancel[0].cancel_reason)

    def test_post_large_move_prioritizes_higher_priced_side(self):
        """Invariant 6: After large move (prev_move >= 40), be alert to momentum stall; buy higher-priced side (<= 0.65)."""
        pos = Position(0.0, 0.0)
        # Previous round moved $80.0. Market prices Down at 0.60, Up at 0.40.
        post_shock_params = dict(self.params, PREV_ROUND_ABS_MOVE=80.0, POST_SHOCK_MIN_MOVE=40.0)
        book_up = OrderBook(bids=[(0.38, 20.0)], asks=[(0.40, 20.0)])
        book_dn = OrderBook(bids=[(0.58, 20.0)], asks=[(0.60, 20.0)])
        spot = SpotState(timestamp=1100.0, price=75010.0, open_price=75000.0)

        # In mid-window (tau = 180s):
        # Even though Up is cheap at 0.40 (<= 0.45), policy MUST NOT buy the underdog.
        # It must prioritize the higher-priced market favored side: Down @ 0.60!
        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=180.0, params=post_shock_params, p_fair=0.45)
        self.assertEqual(dec.action, "taker_buy")
        self.assertEqual(dec.side, "Down", "Must buy the higher-priced side (Down @ 0.60), not the cheap underdog (Up @ 0.40)!")
        self.assertAlmostEqual(dec.price, 0.60)
        self.assertIn("Post-Shock Consensus", dec.reason)

    def test_dynamic_price_re_quote_on_market_movement(self):
        """Invariant 5: When market price changes by >= 2 ticks, plan_order_actions re-quotes at new price."""
        now = 1005.0
        resting_order = Order(
            id=123, client_order_id="pm_1", side="Up", price=0.55,
            size=5.0, remaining=5.0, filled_size=0.0, purpose="entry",
            placed_at=1000.0, live_at=1000.3
        )
        # Market price moved: new desired quote is 0.58 (>= 0.02 diff)
        new_dec = Decision(action="quote", side="Up", price=0.58, size=5.0, utility=0.20, reason="dynamic price adjust")
        to_cancel, action = plan_order_actions([resting_order], new_dec, now=now, max_queue_wait_s=20.0)
        self.assertEqual(len(to_cancel), 1)
        self.assertIn("盘口调价重挂", to_cancel[0].cancel_reason)
        self.assertIn("0.55 → $0.58", to_cancel[0].cancel_reason)
        self.assertIsNotNone(action)
        self.assertEqual(action.price, 0.58)


if __name__ == "__main__":
    unittest.main()
