"""Unit tests for v2.3 Entry Window, Tail Freeze, Quote Attempts, and Lifecycle."""
import unittest
import tempfile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.types import OrderBook, Position, SpotState
from core.policy import plan_decision
from runtime.runner import PaperRunner


class TestV23EntryAndLifecycle(unittest.TestCase):
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
            ENABLE_ARBITRAGE_HEDGE=False,
        )

    def test_observation_period_standby(self):
        """First 60s (tau > 240s) stays in observation standby."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.50, 50.0)], asks=[(0.52, 50.0)])
        book_dn = OrderBook(bids=[(0.47, 50.0)], asks=[(0.49, 50.0)])
        spot = SpotState(timestamp=1020.0, price=75200.0, open_price=75000.0)
        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=260.0, params=self.params, p_fair=0.92)
        self.assertEqual(dec.action, "wait")
        self.assertTrue(dec.urgent_cancel)

    def test_tail_freeze_standby(self):
        """Tail window (tau < 15s) freezes all new entries and pulls quotes."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.40, 50.0)], asks=[(0.42, 50.0)])
        book_dn = OrderBook(bids=[(0.57, 50.0)], asks=[(0.59, 50.0)])
        spot = SpotState(timestamp=1290.0, price=75200.0, open_price=75000.0)
        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=10.0, params=self.params, p_fair=0.95)
        self.assertEqual(dec.action, "wait")
        self.assertIn("Tail freeze", dec.reason)
        self.assertTrue(dec.urgent_cancel)

    def test_inconsistent_book_rejected_strictly(self):
        """Crossed or sub-$1 ask sums are rejected rather than shifted."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.52, 50.0)], asks=[(0.55, 50.0)])
        book_dn = OrderBook(bids=[(0.52, 50.0)], asks=[(0.55, 50.0)])
        spot = SpotState(timestamp=1260.0, price=75010.0, open_price=75000.0)
        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=40.0, params=self.params, p_fair=0.54)
        self.assertEqual(dec.action, "wait")
        self.assertIn("Inconsistent book", dec.reason)

    def test_taker_buy_up_and_down(self):
        """Taker IOC executes on either Up or Down when net edge >= MIN_TAKER_EDGE."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.58, 50.0)], asks=[(0.60, 50.0)])
        book_dn = OrderBook(bids=[(0.38, 50.0)], asks=[(0.41, 50.0)])
        spot_up = SpotState(timestamp=1260.0, price=75150.0, open_price=75000.0)
        dec_up = plan_decision(pos, pos, book_up, book_dn, spot_up, tau_s=40.0, params=self.params, p_fair=0.94)
        self.assertEqual(dec_up.action, "taker_buy")
        self.assertEqual(dec_up.side, "Up")

        book_up2 = OrderBook(bids=[(0.35, 50.0)], asks=[(0.37, 50.0)])
        book_dn2 = OrderBook(bids=[(0.62, 50.0)], asks=[(0.64, 50.0)])
        spot_dn = SpotState(timestamp=1260.0, price=74850.0, open_price=75000.0)
        dec_dn = plan_decision(pos, pos, book_up2, book_dn2, spot_dn, tau_s=40.0, params=self.params, p_fair=0.05)
        self.assertEqual(dec_dn.action, "taker_buy")
        self.assertEqual(dec_dn.side, "Down")

    def test_maker_quote_when_ask_lacks_taker_edge(self):
        """When ask offers < 0.08 net edge but bid offers >= 0.04 maker edge, quote as maker."""
        pos = Position(0.0, 0.0)
        # p_fair=0.80: ask=0.74+fee=0.7535 (edge=0.046 < 0.08); bid=0.70 -> improve to 0.71 (edge=0.09 >= 0.04)
        book_up = OrderBook(bids=[(0.70, 50.0)], asks=[(0.74, 50.0)])
        book_dn = OrderBook(bids=[(0.25, 50.0)], asks=[(0.27, 50.0)])
        spot = SpotState(timestamp=1260.0, price=75100.0, open_price=75000.0)
        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=40.0, params=self.params, p_fair=0.80)
        self.assertEqual(dec.action, "quote")
        self.assertEqual(dec.side, "Up")
        self.assertAlmostEqual(dec.price, 0.71)

    def test_max_quote_attempts_respected_without_urgent_cancel(self):
        """Once quote_attempts hits MAX_QUOTE_ATTEMPTS, stop new quotes but keep resting quote alive."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.70, 50.0)], asks=[(0.74, 50.0)])
        book_dn = OrderBook(bids=[(0.25, 50.0)], asks=[(0.27, 50.0)])
        spot = SpotState(timestamp=1260.0, price=75100.0, open_price=75000.0)

        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=40.0, params=self.params, p_fair=0.80, quote_attempts=0)
        self.assertEqual(dec.action, "quote")

        dec2 = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=39.0, params=self.params, p_fair=0.80, quote_attempts=1)
        self.assertEqual(dec2.action, "wait")
        self.assertFalse(dec2.urgent_cancel)

    def test_end_to_end_runner_v23_lifecycle(self):
        """Full lifecycle: early standby -> edge taker buy -> hold -> settlement win."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            runner = PaperRunner(ledger_dir=tmp_dir)
            runner.start_window("btc-updown-5m-1000", 1000.0, 1300.0, open_px=75000.0, binance_open_px=75000.0)

            book_up = OrderBook(bids=[(0.58, 50.0)], asks=[(0.60, 50.0)], source_t=1020.0)
            book_dn = OrderBook(bids=[(0.39, 50.0)], asks=[(0.41, 50.0)], source_t=1020.0)
            spot_early = SpotState(timestamp=1020.0, price=75010.0, open_price=75000.0, realized_vol_bps=14.0, binance_open_price=75000.0)
            runner.step(1020.0, spot_early, book_up, book_dn)
            self.assertEqual(len(runner.fills), 0)

            book_up_late = OrderBook(bids=[(0.58, 50.0)], asks=[(0.60, 50.0)], source_t=1260.0)
            book_dn_late = OrderBook(bids=[(0.39, 50.0)], asks=[(0.41, 50.0)], source_t=1260.0)
            spot_late = SpotState(timestamp=1260.0, price=75200.0, open_price=75000.0, realized_vol_bps=14.0, binance_open_price=75000.0)
            runner.step(1260.0, spot_late, book_up_late, book_dn_late, replay_mode=True)
            self.assertEqual(len(runner.fills), 1)
            self.assertEqual(runner.pos_up.shares, 5.0)

            runner.close_window(close_px=75150.0, official_winner="Up")
            self.assertGreater(runner.total_pnl, 1.5)


if __name__ == "__main__":
    unittest.main()
