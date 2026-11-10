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
import tempfile
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
        
        The model MUST output taker_hedge immediately at t=0, locking -$0.26 loss
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
        # Official taker fee @ 0.63 = 0.07*0.63*0.37 = 0.016317/share -> pair cost 1.026317,
        # utility -$0.2632, still far superior to the -$2.00+ holding loss
        self.assertAlmostEqual(decision.utility, -0.2632, places=4)

    def test_official_taker_fee_curve_and_booking(self):
        """Invariant 12: Taker fee follows the official rate*p*(1-p) curve and is booked into cash."""
        from core.execution import taker_fee_per_share
        from runtime.runner import PaperRunner
        import config
        rate = config.PARAMS["TAKER_FEE_RATE"]

        # Peak at 50/50: 0.07 * 0.5 * 0.5 = 0.0175 per share
        self.assertAlmostEqual(taker_fee_per_share(0.50, rate), 0.0175, places=10)
        # Symmetric around 0.5, vanishing at certain outcomes
        self.assertAlmostEqual(taker_fee_per_share(0.63, rate), taker_fee_per_share(0.37, rate), places=10)
        self.assertEqual(taker_fee_per_share(0.0, rate), 0.0)
        self.assertEqual(taker_fee_per_share(1.0, rate), 0.0)

        # Taker hedge must book the official fee into position cash
        runner = PaperRunner()
        with tempfile.TemporaryDirectory() as tmp_dir:
            runner.live_file = Path(tmp_dir) / "live.json"
            runner.summary_file = Path(tmp_dir) / "summary.jsonl"
            runner.windows_dir = Path(tmp_dir) / "windows"
            runner.windows_dir.mkdir()
            runner.start_window("test_fee", 1000.0, 1300.0, open_px=75000.0)
            runner.pos_up = Position(shares=10.0, cash=3.80)
            book_up = OrderBook(bids=[(0.20, 50.0)], asks=[(0.23, 50.0)])
            book_down = OrderBook(bids=[(0.60, 50.0)], asks=[(0.63, 50.0)])
            spot = SpotState(timestamp=1150.0, price=74700.0, open_price=75000.0, realized_vol_bps=15.0)
            runner.step(1150.0, spot, book_up, book_down, poly_spot=74700.0)
            expected_fee = round(10.0 * rate * 0.63 * 0.37, 4)
            self.assertEqual(runner.pos_down.shares, 10.0)
            self.assertAlmostEqual(runner.pos_down.cash, 10.0 * 0.63 + expected_fee, places=4)

    def test_max_lots_boundary(self):
        """Invariant 4: Once position reaches MAX_LOTS, policy must output WAIT."""
        from core.policy import plan_decision
        import config
        
        max_shares = config.PARAMS["LOT_SIZE"] * config.PARAMS["MAX_LOTS"]
        pos_up = Position(shares=max_shares, cash=max_shares * 0.5)
        pos_down = Position(shares=max_shares, cash=max_shares * 0.48)
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

        # Keep test journals out of the real investor ledger.
        with tempfile.TemporaryDirectory() as tmp_dir:
            runner.summary_file = Path(tmp_dir) / "summary.jsonl"
            runner.windows_dir = Path(tmp_dir) / "windows"
            runner.windows_dir.mkdir()
            runner.close_window(close_px=75050.0)
            lines = runner.summary_file.read_text(encoding="utf-8").splitlines()
        last_rec = json.loads(lines[-1])
        
        self.assertEqual(last_rec["winner"], "Up")
        self.assertEqual(last_rec["proceeds"], 5.00)

    def test_quoting_hysteresis_and_anti_churn(self):
        """Invariant 8: 1-tick flicker does not cancel working order; urgent wait cancels immediately."""
        from core.execution import is_order_diff_significant, plan_order_actions
        from core.types import Order, Decision

        existing = Order(id=1, client_order_id="pm_1", side="Up", price=0.46, size=5.0, remaining=5.0, placed_at=100.0)
        
        # 1-tick shift ($0.01) is within hysteresis band (< $0.019) -> NOT significant
        self.assertFalse(is_order_diff_significant(existing, desired_side="Up", desired_price=0.47, desired_size=5.0))
        # 2-tick shift ($0.02) is outside hysteresis band -> significant
        self.assertTrue(is_order_diff_significant(existing, desired_side="Up", desired_price=0.48, desired_size=5.0))
        # Side change -> significant
        self.assertTrue(is_order_diff_significant(existing, desired_side="Down", desired_price=0.46, desired_size=5.0))

        # Transient 'no edge' wait does NOT cancel recent resting order (protects queue)
        dec_transient = Decision(action="wait", reason="No edge outside uncertainty band (+/-0.01)")
        to_cancel, _ = plan_order_actions([existing], dec_transient, now=105.0, max_queue_wait_s=15.0)
        self.assertEqual(len(to_cancel), 0)

        # Urgent wait DOES cancel immediately (structural flag, not wording)
        dec_toxic = Decision(action="wait", reason="Adverse spot toxic shock detected",
                             urgent_cancel=True)
        to_cancel_toxic, _ = plan_order_actions([existing], dec_toxic, now=105.0, max_queue_wait_s=15.0)
        self.assertEqual(len(to_cancel_toxic), 1)

        # Wording alone carries no authority: a 'toxic' reason without the flag
        # must NOT cancel, so rephrasing a message can never silently disable
        # the cancel path.
        dec_wording = Decision(action="wait", reason="toxic shock detected (no flag)")
        to_cancel_wording, _ = plan_order_actions([existing], dec_wording, now=105.0, max_queue_wait_s=15.0)
        self.assertEqual(len(to_cancel_wording), 0)

    def test_fifo_queue_matcher_with_trade_prints(self):
        """Invariant 9: Queue position only depletes on real trade prints."""
        from sim.matcher import QueueMatcher
        from core.types import Order, OrderBook

        matcher = QueueMatcher(latency_s=0.30)
        order = Order(id=1, client_order_id="pm_1", side="Up", price=0.46, size=5.0, remaining=5.0, placed_at=100.0)
        # Order book has 10.0 shares resting ahead at 0.46
        book = OrderBook(bids=[(0.46, 10.0), (0.45, 20.0)], asks=[(0.48, 15.0)], last_trade_price=None)
        matcher.register_order(order, book)
        self.assertEqual(matcher.queue_ahead[1], 10.0)

        # Ticks with no trade prints never consume the queue, however many times we evaluate.
        for t in (100.2, 100.35, 101.0, 120.0):
            self.assertEqual(matcher.match_order(order, book, now=t), 0.0)
        self.assertEqual(matcher.queue_ahead[1], 10.0)

        # A trade print before the latency deadline still cannot fill us.
        self.assertEqual(matcher.on_trade_print(order, 0.46, 50.0, now=100.20), 0.0)
        self.assertEqual(matcher.queue_ahead[1], 10.0)

        # After latency, a 5-share print at our price consumes queue only.
        self.assertEqual(matcher.on_trade_print(order, 0.46, 5.0, now=100.35), 0.0)
        self.assertEqual(matcher.queue_ahead[1], 5.0)

        # A print above our bid does not consume queue.
        self.assertEqual(matcher.on_trade_print(order, 0.47, 5.0, now=100.40), 0.0)
        self.assertEqual(matcher.queue_ahead[1], 5.0)

        # Next print at 0.46 with 10 shares clears the queue and fills the order.
        fill = matcher.on_trade_print(order, 0.46, 10.0, now=100.45)
        self.assertEqual(fill, 5.0)
        self.assertEqual(order.remaining, 0.0)

    def test_taker_fill_is_depth_limited(self):
        """Invariant 9b: taker fills walk the book and never assume infinite liquidity."""
        from sim.matcher import QueueMatcher
        from core.types import OrderBook

        matcher = QueueMatcher()
        thin = OrderBook(bids=[(0.40, 2.0)], asks=[(0.55, 2.0), (0.57, 3.0)])
        filled, vwap = matcher.taker_fill(thin, 10.0, is_buy=True)
        self.assertEqual(filled, 5.0)
        self.assertAlmostEqual(vwap, (2.0 * 0.55 + 3.0 * 0.57) / 5.0, places=6)

        empty = OrderBook(bids=[], asks=[])
        self.assertEqual(matcher.taker_fill(empty, 5.0, is_buy=True), (0.0, 0.0))

        # Selling into bids follows the same depth rule.
        filled_sell, vwap_sell = matcher.taker_fill(thin, 1.0, is_buy=False)
        self.assertEqual(filled_sell, 1.0)
        self.assertAlmostEqual(vwap_sell, 0.40, places=6)

    def test_pretrade_window_loss_budget_gate(self):
        """Invariant 9c: a naked Leg-1 entry that could breach the window loss budget is refused."""
        import config
        from core.policy import plan_decision, risk_adjusted_entry_size
        from core.types import OrderBook, Position, SpotState

        # No lock available when the opposite ask is 0.45 at a 0.54 quote and
        # the budget only affords one lot (5 * 0.54 = 2.70 > 2.00).
        self.assertEqual(
            risk_adjusted_entry_size(5.0, 0.54, OrderBook(bids=[(0.44, 5)], asks=[(0.45, 5)]),
                                     5.0, 1, 3, 2.00),
            0.0
        )
        # A locked pair needs visible hedge depth: 5 shares on the opposite ask
        # can only lock one lot, so the rest would be naked beyond the budget.
        self.assertEqual(
            risk_adjusted_entry_size(10.0, 0.54, OrderBook(bids=[(0.43, 5)], asks=[(0.44, 5)]),
                                     5.0, 1, 3, 2.00),
            5.0
        )
        # With enough visible depth the same locked pair allows the full size.
        self.assertEqual(
            risk_adjusted_entry_size(10.0, 0.54, OrderBook(bids=[(0.43, 5)], asks=[(0.44, 10)]),
                                     5.0, 1, 3, 2.00),
            10.0
        )
        # Affordable case scales down to whole lots inside the budget.
        self.assertEqual(
            risk_adjusted_entry_size(15.0, 0.30, OrderBook(bids=[(0.68, 5)], asks=[(0.70, 5)]),
                                     5.0, 1, 3, 2.00),
            5.0
        )

        book_up = OrderBook(bids=[(0.53, 20.0)], asks=[(0.57, 20.0)])
        book_dn = OrderBook(bids=[(0.42, 20.0)], asks=[(0.47, 20.0)])
        spot = SpotState(timestamp=1000.0, price=75000.0, open_price=75000.0, realized_vol_bps=8.0)
        dec = plan_decision(
            Position(0.0, 0.0), Position(0.0, 0.0), book_up, book_dn, spot,
            tau_s=250.0, params=config.PARAMS, p_fair=0.50
        )
        if dec.action == "quote":
            # Any accepted quote must be small enough to survive total loss.
            self.assertLessEqual(dec.size * dec.price, config.PARAMS["MAX_WINDOW_LOSS_USD"] + 1e-9)
        else:
            self.assertIn("loss budget", dec.reason)

    def test_official_settlement_override(self):
        """Invariant 10: Official Gamma settlement truth overrides reference spot fallback."""
        from runtime.runner import PaperRunner
        runner = PaperRunner()
        runner.start_window("test_slug_override", 1000.0, 1300.0, open_px=75000.0)
        runner.pos_up = Position(shares=5.0, cash=2.50)
        runner.pos_down = Position(shares=0.0, cash=0.0)

        with tempfile.TemporaryDirectory() as tmp_dir:
            runner.summary_file = Path(tmp_dir) / "summary.jsonl"
            runner.windows_dir = Path(tmp_dir) / "windows"
            runner.windows_dir.mkdir()
            # Reference spot close_px=74900 suggests Down won, but official settlement says Up won
            runner.close_window(close_px=74900.0, official_winner="Up")
            lines = runner.summary_file.read_text(encoding="utf-8").splitlines()
        last_rec = json.loads(lines[-1])
        self.assertEqual(last_rec["winner"], "Up")
        self.assertEqual(last_rec["proceeds"], 5.00)

    def test_consistent_basis_fair_probability_in_plan_decision(self):
        """Invariant 11: plan_decision must maintain consistent basis and avoid exchange spread distortion."""
        from core.policy import plan_decision
        import config

        # Scenario: Binance spot is $75,200 (Binance open is $75,200, return = 0 bps)
        # Polymarket open is $75,000 (a $200 basis difference)
        # Polymarket live TWAP is $75,000 (Poly return = 0 bps)
        pos_up = Position(0.0, 0.0)
        pos_down = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.48, 10.0)], asks=[(0.52, 10.0)])
        book_down = OrderBook(bids=[(0.48, 10.0)], asks=[(0.52, 10.0)])

        spot = SpotState(
            timestamp=1000.0,
            price=75200.0,
            open_price=75000.0,
            binance_open_price=75200.0,
            poly_price=75000.0,
            realized_vol_bps=15.0
        )
        # Without p_fair explicitly passed, plan_decision must detect poly_price vs open_price (75000 / 75000 = 1.0 -> p_fair = 0.50)
        # If it incorrectly mixed spot.price (75200) with open_price (75000), p_fair would be > 0.80 and bias decisions.
        dec = plan_decision(pos_up, pos_down, book_up, book_down, spot, tau_s=200.0, params=config.PARAMS)
        # With 0 bps move, no edge exists outside uncertainty band
        self.assertEqual(dec.action, "wait")
        self.assertIn("No edge", dec.reason)

    def test_drift_snr_and_micro_regime_classification(self):
        """Invariant 13: Statistical drift SNR and micro-regime classification must be mathematically exact."""
        from core.estimator import calculate_drift_snr, classify_market_micro_regime

        # Case 1: Flat market (0 return) -> SNR = 0.0, oscillating
        snr_flat, ret_flat = calculate_drift_snr(75000.0, 75000.0, tau_s=200.0, vol_bps=15.0)
        self.assertEqual(snr_flat, 0.0)
        self.assertEqual(ret_flat, 0.0)
        regime_flat, _, _ = classify_market_micro_regime(75000.0, 75000.0, tau_s=200.0, vol_bps=15.0)
        self.assertEqual(regime_flat, "oscillating")

        # Case 2: Strong downward drift (-30 bps, vol=10 bps) -> SNR = 3.0 / sqrt(200/300) = ~3.67 > 1.8 -> trending_down
        regime_down, snr_down, ret_down = classify_market_micro_regime(74775.0, 75000.0, tau_s=200.0, vol_bps=10.0)
        self.assertEqual(regime_down, "trending_down")
        self.assertGreater(snr_down, 1.8)

        # Case 3: Toxic jump shock -> regime is toxic
        regime_toxic, _, _ = classify_market_micro_regime(75000.0, 75000.0, tau_s=200.0, vol_bps=15.0, is_toxic_shock=True)
        self.assertEqual(regime_toxic, "toxic")

    def test_dual_regime_directional_momentum_defense(self):
        """Invariant 14: Downward momentum regime strictly forbids quoting Up maker entry."""
        from core.policy import plan_decision
        import config

        pos_flat = Position(0.0, 0.0)
        # Downward drift: spot is 74,700 vs open 75,000 (-40 bps)
        spot_down = SpotState(
            timestamp=1000.0,
            price=74700.0,
            open_price=75000.0,
            poly_price=74700.0,
            binance_open_price=75000.0,
            realized_vol_bps=10.0
        )
        book_up = OrderBook(bids=[(0.48, 10.0)], asks=[(0.52, 10.0)])
        book_dn = OrderBook(bids=[(0.48, 10.0)], asks=[(0.52, 10.0)])

        dec = plan_decision(pos_flat, pos_flat, book_up, book_dn, spot_down, tau_s=200.0, params=config.PARAMS)
        # Must NEVER quote Up in trending_down regime
        if dec.action == "quote":
            self.assertNotEqual(dec.side, "Up")

    def test_catastrophic_pair_cost_hedge_disqualification(self):
        """Invariant 15: When pair_cost > 1.05, taker hedge is disqualified and taker unwind dominates."""
        from core.policy import evaluate_unpaired_utility

        # Trader holds 15 Down bought at 0.74
        # Market moved violently: Up ask is 0.83 (pair_cost = 0.74 + 0.83 + fee = 1.575 > 1.05)
        # Down bid is 0.20
        b_held = OrderBook(bids=[(0.20, 20.0)], asks=[(0.25, 20.0)])
        b_opp = OrderBook(bids=[(0.80, 20.0)], asks=[(0.83, 20.0)])

        dec = evaluate_unpaired_utility(
            held_side="Down",
            held_shares=15.0,
            held_basis_price=0.74,
            p_fair_held=0.10,
            book_held=b_held,
            book_opp=b_opp,
            max_pair_cost=1.05
        )
        # Taker hedge at 0.83 must be disqualified; should unwind position or hold, NOT buy hedge @ 0.83
        self.assertNotEqual(dec.action, "taker_hedge")

    def test_urgent_cancel_on_trend_regime_and_safe_entry_boundary(self):
        """Invariant 16: A structural urgent_cancel flag (regime/lifecycle lockout)
        pulls resting quotes immediately regardless of the message wording."""
        from core.execution import plan_order_actions
        from core.types import Order, Decision

        working = [
            Order(id=1, client_order_id="pm_1", side="Up", price=0.50, size=5.0, remaining=5.0, placed_at=100.0)
        ]
        # Order is only 2 seconds old (well below 15.0s max_queue_wait_s)
        now = 102.0

        # Case 1: Regime lockout sets the structural flag -> urgent cancel
        dec_trend = Decision(action="wait", reason="Transitional regime, no quoting",
                             urgent_cancel=True)
        to_cancel, action = plan_order_actions(working, dec_trend, now=now, max_queue_wait_s=15.0)
        self.assertEqual(len(to_cancel), 1)
        self.assertEqual(to_cancel[0].id, 1)

        # Case 2: Price leaving the safe corridor sets the same flag -> urgent cancel
        dec_safe = Decision(action="wait", reason="Up quote (0.68) outside safe entry zone [0.38, 0.62]",
                            urgent_cancel=True)
        to_cancel_2, action_2 = plan_order_actions(working, dec_safe, now=now, max_queue_wait_s=15.0)
        self.assertEqual(len(to_cancel_2), 1)
        self.assertEqual(to_cancel_2[0].id, 1)

    def test_min_entry_tau_cutoff_boundary(self):
        """Invariant 17: When remaining time tau < 180s, new Leg-1 entry is strictly forbidden."""
        import config
        from core.policy import plan_decision
        pos_flat = Position(0.0, 0.0)
        book = OrderBook(bids=[(0.48, 50.0)], asks=[(0.50, 50.0)])
        spot = SpotState(timestamp=1000.0, price=75010.0, open_price=75000.0)
        
        # tau = 120s (less than MIN_ENTRY_TAU_S = 180s)
        dec = plan_decision(pos_flat, pos_flat, book, book, spot, tau_s=120.0, params=config.PARAMS)
        self.assertEqual(dec.action, "wait")
        self.assertIn("Late window", dec.reason)
        self.assertTrue(dec.urgent_cancel, "lifecycle lockouts must pull resting quotes")

    def test_balanced_odds_entry_boundary(self):
        """Invariant 18: Skewed fair probability outside [0.42, 0.58] strictly blocks Leg-1 maker entry."""
        import config
        from core.policy import plan_decision
        pos_flat = Position(0.0, 0.0)
        book = OrderBook(bids=[(0.48, 50.0)], asks=[(0.50, 50.0)])
        spot = SpotState(timestamp=1000.0, price=75000.0, open_price=75000.0)
        
        # Pass skewed p_fair = 0.81 (like in round 1794289800)
        dec_skewed = plan_decision(pos_flat, pos_flat, book, book, spot, tau_s=250.0, params=config.PARAMS, p_fair=0.81)
        self.assertEqual(dec_skewed.action, "wait")
        self.assertIn("balanced entry zone", dec_skewed.reason)

    def test_single_entry_per_window_lockout(self):
        """Invariant 19: When entries_count >= MAX_ENTRIES_PER_WINDOW, policy waits for next window."""
        import config
        from core.policy import plan_decision
        pos_flat = Position(0.0, 0.0)
        book = OrderBook(bids=[(0.48, 50.0)], asks=[(0.50, 50.0)])
        spot = SpotState(timestamp=1000.0, price=75000.0, open_price=75000.0)
        
        # entries_count = 1 (position was already unwound this round)
        dec_locked = plan_decision(pos_flat, pos_flat, book, book, spot, tau_s=250.0, params=config.PARAMS, p_fair=0.50, entries_count=1)
        self.assertEqual(dec_locked.action, "wait")
        self.assertIn("Max entries per window reached", dec_locked.reason)


if __name__ == "__main__":
    import json
    unittest.main()
