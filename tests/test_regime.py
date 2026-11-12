"""Unit Tests for Market Regime Classification & Runner Lifecycle Guards.

Tests the multi-dimensional regime classification engine (range-bound sizing,
strict block of toxic/trend/wide-spread regimes) and the runner's partial-fill
entry dedup and dead-man switch.
"""
import unittest
import config
from core.types import OrderBook, SpotState
from core.regime import classify_market_regime
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

    def test_deadman_switch_cancels_resting_orders_on_feed_gap(self):
        """Invariant 26: Feed drop or staleness > DEADMAN_TIMEOUT_S triggers dead-man switch and cancels resting orders."""
        from core.types import Order
        with tempfile.TemporaryDirectory() as tmp:
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("test-deadman", 1000.0, 1300.0, 75000.0)
            
            # Place a resting order
            o = Order(id=101, client_order_id="pm_test", side="Up", price=0.49, size=5.0, remaining=5.0, placed_at=1005.0)
            runner.working_orders.append(o)
            runner._last_step_time = 1005.0
            
            # Step at 1010.0 (5.0s gap > DEADMAN_TIMEOUT_S of 3.0s)
            spot = SpotState(timestamp=1010.0, price=75000.0, open_price=75000.0, realized_vol_bps=15.0)
            b_up = OrderBook(bids=[(0.48, 50.0)], asks=[(0.50, 50.0)])
            b_dn = OrderBook(bids=[(0.48, 50.0)], asks=[(0.50, 50.0)])
            
            runner.step(1010.0, spot, b_up, b_dn)
            
            # Resting order must have been cancelled by the dead-man switch
            self.assertEqual(len(runner.working_orders), 0)
            self.assertEqual(o.cancel_at, 1010.0)
            self.assertIn("Dead-man switch", runner.decisions[-1]["reason"])


if __name__ == "__main__":
    unittest.main()
