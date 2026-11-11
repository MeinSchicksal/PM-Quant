"""Unit tests for the PM-Quant v2.0 LWAA (Late-Window Asymmetric Absorption) Strategy.

Verifies the mathematical invariants and execution behavior:
1. Early/mid-window standby (tau > 60s).
2. Late-window high-confidence horizon detection (tau in [15s, 60s], P* >= 0.88).
3. Taker absorption execution on mispriced asks (P* - ask - fee >= 0.02).
4. Maker absorption execution on bids when ask is expensive.
5. Strict anti-panic hold-to-expiration (taker unwinds disabled).
6. Risk-free arbitrage lock when pair_cost <= 1.00.
7. Tail freeze when tau < 15s.
8. End-to-end PaperRunner execution and settlement PnL.
"""
import unittest
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.types import OrderBook, Position, SpotState, Decision
from core.policy import plan_decision, plan_decision_lwaa
from runtime.runner import PaperRunner


class TestLWAAStrategy(unittest.TestCase):
    def setUp(self):
        self.params = dict(config.PARAMS, STRATEGY_MODE="LWAA")

    def test_early_window_standby(self):
        """Invariant 1: Early & mid window (tau > 150s) strictly stands by to avoid random-walk noise."""
        pos = Position(0.0, 0.0)
        book = OrderBook(bids=[(0.48, 50.0)], asks=[(0.50, 50.0)])
        spot = SpotState(timestamp=1000.0, price=75100.0, open_price=75000.0)

        # tau = 200s (> LWAA_MAX_TAU_S = 150s)
        dec = plan_decision(pos, pos, book, book, spot, tau_s=200.0, params=self.params, p_fair=0.92)
        self.assertEqual(dec.action, "wait")
        self.assertIn("Standby in early/mid window", dec.reason)

    def test_tail_freeze(self):
        """Invariant 2: Tail freeze (tau < 15s) freezes entry and pulls resting orders."""
        pos = Position(0.0, 0.0)
        book = OrderBook(bids=[(0.85, 50.0)], asks=[(0.87, 50.0)])
        spot = SpotState(timestamp=1290.0, price=75100.0, open_price=75000.0)

        # tau = 10s (< LWAA_MIN_TAU_S = 15s)
        dec = plan_decision(pos, pos, book, book, spot, tau_s=10.0, params=self.params, p_fair=0.95)
        self.assertEqual(dec.action, "wait")
        self.assertIn("Tail freeze", dec.reason)
        self.assertTrue(dec.urgent_cancel)

    def test_late_window_uncertain_outcome(self):
        """Invariant 3: Late window with uncertain odds (P* < 0.88) does not gamble."""
        pos = Position(0.0, 0.0)
        book = OrderBook(bids=[(0.52, 50.0)], asks=[(0.55, 50.0)])
        spot = SpotState(timestamp=1260.0, price=75005.0, open_price=75000.0)

        # tau = 40s, p_fair = 0.54 (uncertain)
        dec = plan_decision(pos, pos, book, book, spot, tau_s=40.0, params=self.params, p_fair=0.54)
        self.assertEqual(dec.action, "wait")
        self.assertIn("Outcome uncertain", dec.reason)

    def test_late_window_taker_absorption_up(self):
        """Invariant 4: When P*(Up) >= 0.80 and ask has net positive edge within max_entry_px (<= 0.65), immediate taker buy Up."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.58, 20.0)], asks=[(0.60, 20.0)])
        book_dn = OrderBook(bids=[(0.38, 20.0)], asks=[(0.40, 20.0)])
        spot = SpotState(timestamp=1260.0, price=75150.0, open_price=75000.0)

        # tau = 40s, p_fair = 0.94
        # ask = 0.60 <= 0.65, fee = 0.07 * 0.60 * 0.40 = 0.0168, net cost = 0.6168
        # edge = 0.94 - 0.6168 = 0.3232 >= 0.02
        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=40.0, params=self.params, p_fair=0.94)
        self.assertEqual(dec.action, "taker_buy")
        self.assertEqual(dec.side, "Up")
        self.assertAlmostEqual(dec.price, 0.60)
        self.assertEqual(dec.size, 5.0)
        self.assertIn("LWAA Taker Absorption", dec.reason)

    def test_late_window_taker_absorption_down(self):
        """Invariant 5: When P*(Down) >= 0.80 and ask has net positive edge within max_entry_px (<= 0.65), immediate taker buy Down."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.38, 20.0)], asks=[(0.40, 20.0)])
        book_dn = OrderBook(bids=[(0.56, 20.0)], asks=[(0.58, 20.0)])
        spot = SpotState(timestamp=1260.0, price=74850.0, open_price=75000.0)

        # tau = 40s, p_fair_up = 0.05 -> p_fair_down = 0.95
        # ask_down = 0.58 <= 0.65, fee = 0.07 * 0.58 * 0.42 = 0.017, net cost = 0.597
        # edge = 0.95 - 0.597 = 0.353 >= 0.02
        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=40.0, params=self.params, p_fair=0.05)
        self.assertEqual(dec.action, "taker_buy")
        self.assertEqual(dec.side, "Down")
        self.assertAlmostEqual(dec.price, 0.58)
        self.assertEqual(dec.size, 5.0)
        self.assertIn("LWAA Taker Absorption", dec.reason)

    def test_maker_absorption_when_ask_expensive(self):
        """Invariant 6: When ask is outside entry corridor (> 0.65), maker bid is placed at or below 0.65 at zero fee."""
        pos = Position(0.0, 0.0)
        # Ask is 0.75 (> max_entry_px 0.65), but bid is 0.60 with ample edge
        book_up = OrderBook(bids=[(0.60, 20.0)], asks=[(0.75, 20.0)])
        book_dn = OrderBook(bids=[(0.20, 20.0)], asks=[(0.25, 20.0)])
        spot = SpotState(timestamp=1260.0, price=75120.0, open_price=75000.0)

        # tau = 40s, p_fair = 0.92
        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=40.0, params=self.params, p_fair=0.92)
        self.assertEqual(dec.action, "quote")
        self.assertEqual(dec.side, "Up")
        self.assertAlmostEqual(dec.price, 0.60)
        self.assertIn("LWAA Maker Absorption", dec.reason)

    def test_single_leg_holding_strictly_never_unwinds(self):
        """Invariant 7: Once entered, single-leg position is held to settlement. Zero panic unwinds."""
        pos_up = Position(shares=5.0, cash=4.25) # 5 Up bought @ 0.85
        pos_dn = Position(0.0, 0.0)
        # Market temporarily shows a 0.10 bid and 0.85 ask on Down
        book_up = OrderBook(bids=[(0.10, 50.0)], asks=[(0.20, 50.0)])
        book_dn = OrderBook(bids=[(0.75, 50.0)], asks=[(0.85, 50.0)])
        spot = SpotState(timestamp=1270.0, price=74990.0, open_price=75000.0)

        # In v1.1, the panic unwind rule would have sold at 0.10 losing $3.75!
        # In LWAA, policy MUST NOT SELL. It holds to settlement!
        dec = plan_decision(pos_up, pos_dn, book_up, book_dn, spot, tau_s=30.0, params=self.params, p_fair=0.40)
        self.assertNotEqual(dec.action, "taker_unwind")
        self.assertEqual(dec.action, "wait")
        self.assertIn("Holding 5 Up", dec.reason)

    def test_arbitrage_lock_when_pair_cost_under_1(self):
        """Invariant 8: If arbitrage hedge is enabled and opposite ask locks pair_cost <= 1.00, taker hedge executes."""
        pos_up = Position(shares=5.0, cash=4.25) # 5 Up @ 0.85 basis
        pos_dn = Position(0.0, 0.0)
        # Down ask is 0.12: pair_cost = 0.85 + 0.12 + 0.007 = 0.977 <= 1.00!
        book_up = OrderBook(bids=[(0.84, 50.0)], asks=[(0.88, 50.0)])
        book_dn = OrderBook(bids=[(0.10, 50.0)], asks=[(0.12, 50.0)])
        spot = SpotState(timestamp=1270.0, price=75150.0, open_price=75000.0)

        hedge_params = dict(self.params, LWAA_ENABLE_ARBITRAGE_HEDGE=True)
        dec = plan_decision(pos_up, pos_dn, book_up, book_dn, spot, tau_s=30.0, params=hedge_params, p_fair=0.94)
        self.assertEqual(dec.action, "taker_hedge")
        self.assertEqual(dec.side, "Down")
        self.assertAlmostEqual(dec.price, 0.12)
        self.assertIn("Arbitrage lock", dec.reason)

    def test_end_to_end_runner_lwaa_trade_and_settlement(self):
        """Invariant 9: Full lifecycle - standby early, taker buy in late window, hold, settlement win."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            runner = PaperRunner(ledger_dir=tmp_dir)
            runner.live_file = Path(tmp_dir) / "live.json"
            runner.summary_file = Path(tmp_dir) / "summary.jsonl"
            runner.windows_dir = Path(tmp_dir) / "windows"
            runner.windows_dir.mkdir(parents=True, exist_ok=True)

            start_t = 1000.0
            end_t = 1300.0
            open_px = 75000.0
            runner.start_window("test_lwaa_window", start_t, end_t, open_px=open_px)

            book_up = OrderBook(bids=[(0.58, 20.0)], asks=[(0.60, 20.0)], source_t=1150.0)
            book_dn = OrderBook(bids=[(0.38, 20.0)], asks=[(0.40, 20.0)], source_t=1150.0)

            # Tick 1: Early window (tau = 200s > 150s) -> Standby, no trades
            spot_early = SpotState(timestamp=1100.0, price=75150.0, open_price=open_px, realized_vol_bps=15.0)
            runner._last_step_time = 1100.0
            runner.step(1100.0, spot_early, book_up, book_dn, poly_spot=75150.0)
            self.assertEqual(len(runner.fills), 0)

            # Tick 2: Late window (tau = 40s) -> High confidence P* >= 0.90 -> Taker Buy 5 shares Up @ 0.60
            book_up.source_t = 1260.0
            book_dn.source_t = 1260.0
            spot_late = SpotState(timestamp=1260.0, price=75150.0, open_price=open_px, realized_vol_bps=15.0)
            runner._last_step_time = 1260.0
            runner.step(1260.0, spot_late, book_up, book_dn, poly_spot=75150.0)
            self.assertEqual(len(runner.fills), 1)
            fill = runner.fills[0]
            self.assertEqual(fill["side"], "Up")
            self.assertEqual(fill["kind"], "taker")
            self.assertEqual(fill["size"], 5.0)
            self.assertAlmostEqual(fill["price"], 0.60)

            # Tick 3: Tail freeze (tau = 10s) -> Holding position, no new fills
            book_up.source_t = 1290.0
            book_dn.source_t = 1290.0
            spot_tail = SpotState(timestamp=1290.0, price=75140.0, open_price=open_px, realized_vol_bps=15.0)
            runner._last_step_time = 1290.0
            runner.step(1290.0, spot_tail, book_up, book_dn, poly_spot=75140.0)
            self.assertEqual(len(runner.fills), 1)

            # Settlement: Close at 75140.0 (> 75000.0), Up wins!
            runner.close_window(close_px=75140.0, official_winner="Up")
            self.assertGreater(runner.total_pnl, 0.0)
            self.assertAlmostEqual(runner.total_pnl, 5.00 - runner.pos_up.cash, places=3)

    def test_max_entry_price_ceiling_and_single_quote_attempt(self):
        """Invariant 10: Strict ceiling at $0.65 (never chase > $0.70) and stop re-quoting if quote fails."""
        pos = Position(0.0, 0.0)
        # Ask is 0.72 (> 0.70 hard chase ceiling)
        book_up = OrderBook(bids=[(0.68, 20.0)], asks=[(0.72, 20.0)])
        book_dn = OrderBook(bids=[(0.25, 20.0)], asks=[(0.28, 20.0)])
        spot = SpotState(timestamp=1260.0, price=75150.0, open_price=75000.0)

        # 1. Ask 0.72 is above max_entry_px 0.65 -> taker_buy rejected, maker bid clamped to <= 0.65
        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=40.0, params=self.params, p_fair=0.92, quote_attempts=0)
        self.assertEqual(dec.action, "quote")
        self.assertLessEqual(dec.price, 0.65)

        # 2. If already attempted quote once (quote_attempts >= 1), "挂不上也不要再挂了" -> wait
        dec2 = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=40.0, params=self.params, p_fair=0.92, quote_attempts=1)
        self.assertEqual(dec2.action, "wait")
        self.assertIn("已达最大挂单次数", dec2.reason)


if __name__ == "__main__":
    unittest.main()
