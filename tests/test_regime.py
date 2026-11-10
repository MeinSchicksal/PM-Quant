"""Unit Tests for Market Regime Classification & Adaptive Quoting Invariants.

Tests the multi-dimensional regime classification engine, boosted sizing in
range-bound markets, strict block of toxic/trend regimes, and Leg-2 maker preference.
"""
import unittest
import config
from core.types import Position, OrderBook, SpotState, Order
from core.policy import plan_decision, evaluate_unpaired_utility
from core.regime import classify_market_regime, calculate_drift_snr
from runtime.runner import PaperRunner
import tempfile


class TestMarketRegimeClassifier(unittest.TestCase):

    def test_range_bound_detection_and_boosted_sizing(self):
        """Invariant 20: Mean-reverting range regime (SNR <= 1.0, |ret| <= 8 bps) enables boosted sizing."""
        book_up = OrderBook(bids=[(0.48, 100.0)], asks=[(0.50, 100.0)])
        book_dn = OrderBook(bids=[(0.48, 100.0)], asks=[(0.50, 100.0)])
        
        # Spot is 75,015 vs open 75,000 (+2.0 bps), realized vol 12.0 bps, tau 240s
        reg_info = classify_market_regime(
            spot=75015.0, open_price=75000.0, tau_s=240.0, vol_bps=12.0,
            book_up=book_up, book_down=book_dn, params=config.PARAMS
        )
        self.assertEqual(reg_info.regime, "range_bound")
        self.assertTrue(reg_info.can_quote_up)
        self.assertTrue(reg_info.can_quote_down)
        self.assertEqual(reg_info.recommended_lots, 1)

    def test_strong_trend_detection_blocks_counter_trend(self):
        """Invariant 21: Strong momentum trends (SNR > 1.8) strictly block counter-trend maker quoting."""
        book_up = OrderBook(bids=[(0.48, 50.0)], asks=[(0.52, 50.0)])
        book_dn = OrderBook(bids=[(0.48, 50.0)], asks=[(0.52, 50.0)])
        
        # Bullish trend: +30 bps drift, vol 10.0 bps, tau 240s -> SNR = 3.0 / sqrt(240/300) = ~3.35 > 1.8
        reg_up = classify_market_regime(
            spot=75225.0, open_price=75000.0, tau_s=240.0, vol_bps=10.0,
            book_up=book_up, book_down=book_dn, params=config.PARAMS
        )
        self.assertEqual(reg_up.regime, "trending_up")
        self.assertFalse(reg_up.can_quote_down)
        self.assertFalse(reg_up.can_quote_up)

        # Bearish trend: -30 bps drift
        reg_down = classify_market_regime(
            spot=74775.0, open_price=75000.0, tau_s=240.0, vol_bps=10.0,
            book_up=book_up, book_down=book_dn, params=config.PARAMS
        )
        self.assertEqual(reg_down.regime, "trending_down")
        self.assertFalse(reg_down.can_quote_up)
        self.assertFalse(reg_down.can_quote_down)

    def test_wide_spread_blocks_quoting(self):
        """Invariant 22: Orderbook spread > 0.06 strictly blocks new Leg-1 entries."""
        book_wide = OrderBook(bids=[(0.40, 50.0)], asks=[(0.50, 50.0)]) # spread = 0.10 > 0.06
        book_normal = OrderBook(bids=[(0.49, 50.0)], asks=[(0.51, 50.0)])
        
        reg_wide = classify_market_regime(
            spot=75000.0, open_price=75000.0, tau_s=240.0, vol_bps=12.0,
            book_up=book_wide, book_down=book_normal, params=config.PARAMS
        )
        self.assertEqual(reg_wide.regime, "wide_spread")
        self.assertFalse(reg_wide.can_quote_up)
        self.assertFalse(reg_wide.can_quote_down)

    def test_smart_evaluate_unpaired_utility_maker_preference(self):
        """Invariant 23: In balanced oscillation without adverse drift, Leg-2 maker quoting dominates."""
        book_held = OrderBook(bids=[(0.44, 50.0)], asks=[(0.48, 50.0)])
        book_opp = OrderBook(bids=[(0.44, 50.0)], asks=[(0.56, 50.0)])
        
        # Bought Down at 0.46, current odds balanced (p_fair_held = 0.50)
        # Down ask is 0.56 (pair_cost = 0.46 + 0.56 + fee = 1.037 > 1.00)
        # Policy must NOT panic and pay 0.56 taker; it must work the Leg-2 maker quote on Up!
        dec = evaluate_unpaired_utility(
            held_side="Down",
            held_shares=5.0,
            held_basis_price=0.46,
            p_fair_held=0.50,
            book_held=book_held,
            book_opp=book_opp,
            tau_s=200.0,
            regime_name="range_bound"
        )
        self.assertEqual(dec.action, "quote")
        self.assertEqual(dec.side, "Up")
        self.assertLessEqual(dec.price, 0.54) # target price is at most 1.00 - 0.46 = 0.54

    def test_smart_evaluate_unpaired_utility_arbitrage_lock(self):
        """Invariant 24: When opposite ask offers pair_cost <= 1.00, Taker Hedge immediately locks profit."""
        book_held = OrderBook(bids=[(0.44, 50.0)], asks=[(0.48, 50.0)])
        # Up ask is cheap: 0.50 -> pair cost = 0.46 + 0.50 + 0.0175 fee = 0.9775 <= 1.00!
        book_opp = OrderBook(bids=[(0.48, 50.0)], asks=[(0.50, 50.0)])
        
        dec = evaluate_unpaired_utility(
            held_side="Down",
            held_shares=5.0,
            held_basis_price=0.46,
            p_fair_held=0.50,
            book_held=book_held,
            book_opp=book_opp,
            tau_s=200.0,
            regime_name="normal_oscillation"
        )
        self.assertEqual(dec.action, "taker_hedge")
        self.assertEqual(dec.side, "Up")
        self.assertEqual(dec.price, 0.50)
        self.assertIn("arbitrage", dec.reason)

    def test_partial_fill_does_not_double_count_entries(self):
        """Invariant 25: Partial fills of the same order must not consume multiple entries_count."""
        with tempfile.TemporaryDirectory() as tmp:
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("test-partial", 1000.0, 1300.0, 75000.0)
            
            # Simulated partial fill 1 of order id 999: 2.0 shares
            runner._record_fill(1005.0, "Down", 0.48, 2.0, "maker", "entry", order_id=999)
            self.assertEqual(runner.entries_count, 1)
            
            # Simulated partial fill 2 of order id 999: 3.0 shares
            runner._record_fill(1006.0, "Down", 0.48, 3.0, "maker", "entry", order_id=999)
            # Must STILL be 1, NOT 2!
            self.assertEqual(runner.entries_count, 1)


if __name__ == "__main__":
    unittest.main()
