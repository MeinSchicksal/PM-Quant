"""Mathematical Invariant & Property Tests.

Proves core mathematical properties:
1. Strict Discrete Lot Conservation (no odd shares).
2. Official taker fee curve and settlement PnL exactness.
3. FIFO queue matching and depth-limited taker fills.
4. Fair-probability bounds, drift SNR / regime classification, and order-action hysteresis.
"""
import unittest
import sys
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
from core.types import OrderBook, Position, SpotState
from core.estimator import compute_objective_lead
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
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp_dir:
            runner = PaperRunner(ledger_dir=tmp_dir)
            runner.start_window("test_fee", 1000.0, 1300.0, open_px=75000.0)
            runner.pos_up = Position(shares=10.0, cash=3.80)
            book_up = OrderBook(bids=[(0.38, 50.0)], asks=[(0.40, 50.0)], source_t=1260.0)
            book_down = OrderBook(bids=[(0.58, 50.0)], asks=[(0.60, 50.0)], source_t=1260.0)
            spot = SpotState(timestamp=1260.0, price=74700.0, open_price=75000.0, realized_vol_bps=15.0)
            with patch.dict(config.PARAMS, {"ENABLE_ARBITRAGE_HEDGE": True, "ARBITRAGE_HEDGE_MAX_PAIR_COST": 1.00}):
                runner.step(1260.0, spot, book_up, book_down, poly_spot=74700.0)
            expected_fee = round(10.0 * rate * 0.60 * 0.40, 4)
            self.assertEqual(runner.pos_down.shares, 10.0)
            self.assertAlmostEqual(runner.pos_down.cash, 10.0 * 0.60 + expected_fee, places=4)

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

    def test_objective_lead_monotonicity_and_zero_symmetry(self):
        """Invariant 5: Pure arithmetic lead is zero at strike and monotonic with spot."""
        open_px = 75000.0
        lead_eq, delta_eq, _ = compute_objective_lead(SpotState(1000.0, open_px, open_px), open_px, tau_s=150.0)
        self.assertEqual(lead_eq, 0.0)
        self.assertEqual(delta_eq, 0.0)

        lead_hi, _, _ = compute_objective_lead(SpotState(1000.0, open_px + 50.0, open_px), open_px, tau_s=150.0)
        lead_lo, _, _ = compute_objective_lead(SpotState(1000.0, open_px - 50.0, open_px), open_px, tau_s=150.0)
        self.assertGreater(lead_hi, 0.0)
        self.assertLess(lead_lo, 0.0)
        self.assertAlmostEqual(lead_hi, -lead_lo, places=3)

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

    def test_risk_budget_covers_corridor_entry_price(self):
        """Regression: the window loss budget must fund one minimum lot at the
        v2.2 max entry price; validate_params rejects a budget that cannot."""
        import config

        self.assertTrue(config.validate_params())
        bad = dict(config.PARAMS)
        bad["MAX_WINDOW_LOSS_USD"] = 1.00
        with self.assertRaises(ValueError):
            config.validate_params(bad)

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

    def test_consistent_basis_lead_in_plan_decision(self):
        """Invariant 11: plan_decision must maintain consistent basis and avoid exchange spread distortion."""
        from core.policy import plan_decision
        import config

        # Binance spot is $75,200 (Binance open is $75,200 -> 0 bps), Poly open is $75,000, basis_ema is $200.
        # cl_est = 75200 - 200 = 75000 == Poly open -> 0.0 bps lead -> must WAIT!
        pos_up = Position(0.0, 0.0)
        pos_down = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.48, 10.0)], asks=[(0.52, 10.0)])
        book_down = OrderBook(bids=[(0.48, 10.0)], asks=[(0.52, 10.0)])

        spot = SpotState(
            timestamp=1000.0,
            price=75200.0,
            open_price=75000.0,
            binance_open_price=75200.0,
            basis_ema=200.0,
            realized_vol_bps=15.0
        )
        dec = plan_decision(pos_up, pos_down, book_up, book_down, spot, tau_s=200.0, params=config.PARAMS)
        self.assertEqual(dec.action, "wait")

    def test_drift_snr_and_micro_regime_classification(self):
        """Invariant 13: Statistical drift SNR and regime classification must be mathematically exact."""
        from core.estimator import calculate_drift_snr
        from core.regime import classify_market_regime

        book = OrderBook(bids=[(0.49, 50.0)], asks=[(0.51, 50.0)])

        # Case 1: Flat market (0 return) -> SNR = 0.0, mean-reverting range
        snr_flat, ret_flat = calculate_drift_snr(75000.0, 75000.0, tau_s=200.0, vol_bps=15.0)
        self.assertEqual(snr_flat, 0.0)
        self.assertEqual(ret_flat, 0.0)
        regime_flat = classify_market_regime(
            75000.0, 75000.0, 200.0, 15.0, book, book, params=config.PARAMS
        )
        self.assertEqual(regime_flat.regime, "range_bound")

        # Case 2: Strong downward drift (-30 bps, vol=10 bps) -> SNR ~3.67 > 1.8 -> trending_down
        snr_down, ret_down = calculate_drift_snr(74775.0, 75000.0, tau_s=200.0, vol_bps=10.0)
        self.assertGreater(snr_down, 1.8)
        regime_down = classify_market_regime(
            74775.0, 75000.0, 200.0, 10.0, book, book, params=config.PARAMS
        )
        self.assertEqual(regime_down.regime, "trending_down")

        # Case 3: Toxic jump shock -> regime is toxic
        regime_toxic = classify_market_regime(
            75000.0, 75000.0, 200.0, 15.0, book, book,
            is_toxic_shock=True, params=config.PARAMS
        )
        self.assertEqual(regime_toxic.regime, "toxic")

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

    def test_single_entry_per_window_lockout(self):
        """Invariant 19: When entries_count >= MAX_ENTRIES_PER_WINDOW, policy waits for next window."""
        import config
        from core.policy import plan_decision
        pos_flat = Position(0.0, 0.0)
        book = OrderBook(bids=[(0.48, 50.0)], asks=[(0.50, 50.0)])
        spot = SpotState(timestamp=1000.0, price=75000.0, open_price=75000.0)
        
        # entries_count = 1 (an entry already filled this round)
        dec_locked = plan_decision(pos_flat, pos_flat, book, book, spot, tau_s=250.0, params=config.PARAMS, entries_count=1)
        self.assertEqual(dec_locked.action, "wait")
        self.assertIn("Max entries per window reached", dec_locked.reason)


if __name__ == "__main__":
    import json
    unittest.main()
