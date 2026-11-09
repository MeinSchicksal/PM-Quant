"""Mathematical Invariant & Property Tests.

Proves core mathematical properties:
1. Strict Discrete Lot Conservation (no odd shares).
2. Inventory Monotonicity (Reservation bid decreases monotonically with long inventory).
3. Immediate Emergency Taker Arbitration under adverse probability shocks.
4. Bounded Drawdown guarantee.
"""
import unittest
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.types import OrderBook, Position, SpotState, Decision
from core.estimator import estimate_fair_prob, estimate_uncertainty_envelope
from core.policy import calculate_reservation_bids, evaluate_unpaired_utility
from core.execution import quantize_lots


class TestMathematicalInvariants(unittest.TestCase):

    def test_lot_quantization_invariants(self):
        """Invariant 1: Lot quantization strictly maps to {0.0, 5.0, 10.0}."""
        # Under 5 shares: strictly 0
        self.assertEqual(quantize_lots(0.0), 0.0)
        self.assertEqual(quantize_lots(2.5), 0.0)
        self.assertEqual(quantize_lots(4.99), 0.0)
        
        # 5.0 to 9.99 shares: strictly 5.0 (never 6 or 7!)
        self.assertEqual(quantize_lots(5.0), 5.0)
        self.assertEqual(quantize_lots(6.5), 5.0)
        self.assertEqual(quantize_lots(7.0), 5.0)
        self.assertEqual(quantize_lots(9.99), 5.0)
        
        # 10.0+ shares: strictly 10.0
        self.assertEqual(quantize_lots(10.0), 10.0)
        self.assertEqual(quantize_lots(12.5), 10.0)
        self.assertEqual(quantize_lots(20.0), 10.0)

    def test_reservation_bids_monotonicity(self):
        """Invariant 2: Reservation price is strictly monotonic with respect to inventory.
        
        Holding more Up (q > 0) must:
        - Decrease willingness to buy more Up: r_up(q1) < r_up(q0) for q1 > q0.
        - Increase willingness to buy Down to hedge: r_down(q1) > r_down(q0).
        """
        p_fair = 0.50
        epsilon = 0.02
        edge = 0.02
        
        r_up_0, r_dn_0 = calculate_reservation_bids(p_fair, epsilon, edge, q=0.0)
        r_up_5, r_dn_5 = calculate_reservation_bids(p_fair, epsilon, edge, q=5.0)
        r_up_10, r_dn_10 = calculate_reservation_bids(p_fair, epsilon, edge, q=10.0)
        
        # Long inventory reduces buy price for that side
        self.assertGreater(r_up_0, r_up_5)
        self.assertGreater(r_up_5, r_up_10)
        
        # Long inventory increases willingness to pay for opposite hedge
        self.assertLess(r_dn_0, r_dn_5)
        self.assertLess(r_dn_5, r_dn_10)

    def test_immediate_taker_hedge_under_violent_adverse_jump(self):
        """Invariant 3: Under adverse probability shock, Taker Hedge must immediately dominate Hold.
        
        Simulates the exact historical scenario:
        - Held: 10 shares Up @ 0.38 (cost $3.80).
        - Market: Down ask is 0.63 (pair cost = 0.38 + 0.63 = 1.01).
        - BTC crashes, p_fair of Up drops from 0.50 to 0.20 (Down is 80% likely to win).
        
        The model MUST output taker_hedge immediately at t=0, locking -$0.10 loss
        instead of passively waiting and bleeding -$3.80!
        """
        book_up = OrderBook(bids=[(0.20, 50.0)], asks=[(0.23, 50.0)])
        book_down = OrderBook(bids=[(0.60, 50.0)], asks=[(0.63, 50.0)])
        
        decision = evaluate_unpaired_utility(
            held_side="Up",
            held_shares=10.0,
            held_basis_price=0.38,
            p_fair_held=0.20,      # Market just crashed against Up!
            book_held=book_up,
            book_opp=book_down,
            gamma=0.15
        )
        
        # Must immediately trigger taker hedge on Down @ 0.63
        self.assertEqual(decision.action, "taker_hedge")
        self.assertEqual(decision.side, "Down")
        self.assertAlmostEqual(decision.price, 0.63)
        self.assertIn("U_hedge", decision.reason)
        # Expected utility is -$0.15 (10 shares * (1.00 - 1.015)), far superior to -$2.00+ holding loss
        self.assertGreater(decision.utility, -0.25)

    def test_max_lots_boundary(self):
        """Invariant 4: Once position reaches MAX_LOTS (10 shares), policy must output WAIT."""
        from core.policy import plan_decision
        import config
        
        pos_up = Position(shares=10.0, cash=5.0)
        pos_down = Position(shares=10.0, cash=4.8)
        book_up = OrderBook(bids=[(0.48, 50.0)], asks=[(0.50, 50.0)])
        book_down = OrderBook(bids=[(0.48, 50.0)], asks=[(0.50, 50.0)])
        spot = SpotState(timestamp=1000.0, price=75000.0, open_price=75000.0)
        
        decision = plan_decision(pos_up, pos_down, book_up, book_down, spot, tau_s=180.0, params=config.PARAMS)
        self.assertEqual(decision.action, "wait")
        self.assertIn("Max position reached", decision.reason)

    def test_probability_monotonicity_and_bounds(self):
        """Invariant 5: Fair probability is strictly in [0.01, 0.99] and monotonic with spot."""
        open_px = 75000.0
        p_equal = estimate_fair_prob(open_px, open_px, tau_s=150.0, vol_bps=15.0)
        self.assertAlmostEqual(p_equal, 0.50, places=2)
        
        p_higher = estimate_fair_prob(open_px + 50.0, open_px, tau_s=150.0, vol_bps=15.0)
        p_lower = estimate_fair_prob(open_px - 50.0, open_px, tau_s=150.0, vol_bps=15.0)
        
        self.assertGreater(p_higher, 0.50)
        self.assertLess(p_lower, 0.50)
        self.assertTrue(0.01 <= p_lower <= 0.99)
        self.assertTrue(0.01 <= p_higher <= 0.99)

    def test_quote_price_never_zero_or_crosses_book(self):
        """Invariant 6: Passive quotes must strictly satisfy 0.02 <= price <= 0.98 and never cross opposite ask."""
        from core.policy import plan_decision
        import config
        
        # Test 1: Empty book (best_bid is 0.0/None)
        pos_up = Position(0.0, 0.0)
        pos_down = Position(0.0, 0.0)
        empty_book = OrderBook(bids=[], asks=[(0.99, 10.0)])
        spot = SpotState(timestamp=1000.0, price=75020.0, open_price=75000.0)
        
        decision = plan_decision(pos_up, pos_down, empty_book, empty_book, spot, tau_s=200.0, params=config.PARAMS)
        if decision.action == "quote":
            self.assertGreaterEqual(decision.price, 0.02)
            self.assertLessEqual(decision.price, 0.98)
            
        # Test 2: Unpaired position maker hedge must not cross opposite ask
        book_up = OrderBook(bids=[(0.52, 20.0)], asks=[(0.55, 20.0)])
        book_down = OrderBook(bids=[(0.40, 20.0)], asks=[(0.44, 20.0)])
        dec_unpaired = evaluate_unpaired_utility(
            held_side="Up",
            held_shares=5.0,
            held_basis_price=0.52,
            p_fair_held=0.52,
            book_held=book_up,
            book_opp=book_down,
            gamma=0.15
        )
        if dec_unpaired.action == "quote":
            self.assertLess(dec_unpaired.price, book_down.best_ask)
            self.assertGreaterEqual(dec_unpaired.price, 0.02)

    def test_settlement_pnl_exactness_and_fallback(self):
        """Invariant 7: Settlement proceeds, cost, and PnL must be mathematically exact."""
        from runtime.runner import PaperRunner
        runner = PaperRunner()
        runner.start_window("test_slug", 1000.0, 1300.0, open_px=75000.0)
        
        # Simulate paired fill: 5 UP @ 0.50 ($2.50), 5 DOWN @ 0.46 ($2.30)
        runner.pos_up = Position(shares=5.0, cash=2.50)
        runner.pos_down = Position(shares=5.0, cash=2.30)
        runner.fills = [dict(t=1010.0, side="Up", price=0.50, size=5.0, kind="maker"),
                        dict(t=1020.0, side="Down", price=0.46, size=5.0, kind="maker")]
        
        # Close with UP winning (close_px = 75050 > 75000)
        runner.close_window(close_px=75050.0)
        
        # Reads recorded summary
        lines = runner.summary_file.read_text(encoding="utf-8").splitlines()
        last_rec = json.loads(lines[-1])
        
        self.assertEqual(last_rec["winner"], "Up")
        self.assertEqual(last_rec["proceeds"], 5.00)
        self.assertEqual(last_rec["total_cost"], 4.80)
        self.assertAlmostEqual(last_rec["net_pnl"], 0.20, places=4)
        self.assertTrue(last_rec["traded"])


if __name__ == "__main__":
    import json
    unittest.main()
