"""Unit tests for PM-Quant Strict Asian TWAP Net-Edge Engine invariants."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.estimator import estimate_p_fair
from core.policy import determine_objective_side, plan_decision
from core.types import OrderBook, Position, SpotState
from runtime.runner import PaperRunner


class TestStrictNetEdgePolicy(unittest.TestCase):
    def setUp(self):
        self.params = dict(
            config.PARAMS,
            ENTRY_MIN_TAU_S=15.0,
            ENTRY_MAX_TAU_S=240.0,
            ENTRY_MIN_PRICE=0.20,
            ENTRY_MAX_PRICE=0.75,
            ENTRY_MIN_PROB=0.52,
            MIN_MAKER_EDGE=0.05,
            MIN_TAKER_EDGE=0.09,
            ENABLE_MAKER_ENTRY=True,
            MAX_QUOTE_ATTEMPTS=3,
            MAX_ORDERBOOK_SPREAD=0.06,
            ENABLE_ARBITRAGE_HEDGE=False,
        )

    def test_net_edge_maker_quote_when_model_edge_clears_05_cents(self):
        """Quotes zero-fee Maker when P* - maker_px >= 0.05 while Taker edge stays < 0.09."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.51, 20.0)], asks=[(0.55, 20.0)])
        book_dn = OrderBook(bids=[(0.45, 20.0)], asks=[(0.49, 20.0)])
        spot = SpotState(timestamp=1100.0, price=75015.0, open_price=75000.0)
        # Force explicit p_fair=0.60: maker_px=0.52 -> edge=0.08 >= 0.05; taker ask=0.55+fee -> edge ~0.033 < 0.09
        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=200.0, params=self.params, p_fair=0.60)
        self.assertEqual(dec.action, "quote")
        self.assertEqual(dec.side, "Up")
        self.assertAlmostEqual(dec.price, 0.52)
        self.assertEqual(dec.size, 5.0)
        self.assertIn("净边际挂单 Maker", dec.reason)

    def test_net_edge_taker_ioc_when_fee_adjusted_edge_clears_09_cents(self):
        """Executes Taker IOC when P* - ask - fee >= MIN_TAKER_EDGE (0.09)."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.58, 20.0)], asks=[(0.60, 20.0)])
        book_dn = OrderBook(bids=[(0.39, 20.0)], asks=[(0.42, 20.0)])
        spot = SpotState(timestamp=1240.0, price=75075.0, open_price=75000.0)

        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=60.0, params=self.params)
        self.assertEqual(dec.action, "taker_buy")
        self.assertEqual(dec.side, "Up")
        self.assertAlmostEqual(dec.price, 0.60)
        self.assertEqual(dec.size, 5.0)

    def test_rejects_expensive_ask_and_low_probability_sides(self):
        """Prices above ENTRY_MAX_PRICE ($0.75) and sides with P* < ENTRY_MIN_PROB (0.52) are skipped."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.78, 20.0)], asks=[(0.80, 20.0)])
        book_dn = OrderBook(bids=[(0.19, 20.0)], asks=[(0.21, 20.0)])
        spot = SpotState(timestamp=1200.0, price=75150.0, open_price=75000.0)
        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=100.0, params=self.params)
        self.assertEqual(dec.action, "wait")

        # Coin-flip P*=0.51 < 0.52 even if ask is cheap ($0.30) must wait
        b_u = OrderBook(bids=[(0.29, 20.0)], asks=[(0.31, 20.0)])
        b_d = OrderBook(bids=[(0.68, 20.0)], asks=[(0.70, 20.0)])
        dec_lowp = plan_decision(pos, pos, b_u, b_d, spot, tau_s=180.0, params=self.params, p_fair=0.51)
        self.assertEqual(dec_lowp.action, "wait")

    def test_holding_preserves_full_convexity_when_hedge_disabled(self):
        """Holds Leg-1 to settlement without capping winners at $0.49."""
        pos_up = Position(shares=5.0, cash=2.10)
        pos_dn = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.60, 50.0)], asks=[(0.65, 50.0)])
        book_dn = OrderBook(bids=[(0.33, 50.0)], asks=[(0.35, 50.0)])
        spot = SpotState(timestamp=1200.0, price=75020.0, open_price=75000.0)

        dec = plan_decision(pos_up, pos_dn, book_up, book_dn, spot, tau_s=100.0, params=self.params)
        self.assertEqual(dec.action, "wait")
        self.assertIn("持有 5 股 Up", dec.reason)

    def test_opt_in_arbitrage_hedge_locks_when_enabled(self):
        """When ENABLE_ARBITRAGE_HEDGE=True explicitly, deep pair_cost <= 0.90 executes hedge."""
        pos_up = Position(shares=5.0, cash=2.10)  # @ 0.42
        pos_dn = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.60, 50.0)], asks=[(0.65, 50.0)])
        book_dn = OrderBook(bids=[(0.30, 50.0)], asks=[(0.35, 50.0)])
        spot = SpotState(timestamp=1200.0, price=75020.0, open_price=75000.0)

        p = dict(self.params, ENABLE_ARBITRAGE_HEDGE=True, ARBITRAGE_HEDGE_MAX_PAIR_COST=0.90)
        dec = plan_decision(pos_up, pos_dn, book_up, book_dn, spot, tau_s=100.0, params=p)
        self.assertEqual(dec.action, "taker_hedge")
        self.assertEqual(dec.side, "Down")

    def test_fully_hedged_pair_holds_to_settlement(self):
        """Balanced Up/Down pair holds cleanly for $1.00 redemption."""
        pos_up = Position(shares=5.0, cash=2.10)
        pos_dn = Position(shares=5.0, cash=1.80)
        book_up = OrderBook(bids=[(0.60, 50.0)], asks=[(0.65, 50.0)])
        book_dn = OrderBook(bids=[(0.30, 50.0)], asks=[(0.35, 50.0)])
        spot = SpotState(timestamp=1200.0, price=75000.0, open_price=75000.0)

        dec = plan_decision(pos_up, pos_dn, book_up, book_dn, spot, tau_s=100.0, params=self.params)
        self.assertEqual(dec.action, "wait")
        self.assertIn("fully hedged pair", dec.reason.lower())

    def test_asian_twap_estimator_and_objective_side_alignment(self):
        """estimate_p_fair reflects basis-adjusted cl_est + realized 60s TWAP, driving determine_objective_side."""
        sp_up = SpotState(timestamp=1270.0, price=75120.0, open_price=75000.0, basis_ema=80.0, twap_realized_60s=75030.0)
        p_up = estimate_p_fair(sp_up, 75000.0, tau_s=30.0, vol_bps=14.0)
        self.assertGreater(p_up, 0.85)
        self.assertEqual(determine_objective_side(sp_up, 30.0, self.params), "Up")

        sp_flat = SpotState(timestamp=1150.0, price=75080.0, open_price=75000.0, basis_ema=80.0)
        self.assertIsNone(determine_objective_side(sp_flat, 150.0, self.params))

    def test_runner_same_chain_abs_move_excludes_cross_chain_basis(self):
        """Runner rollover abs move must not inflate Chainlink move with Binance-Chainlink spot basis."""
        with tempfile.TemporaryDirectory() as tmp:
            r = PaperRunner(ledger_dir=tmp)
            r.start_window("btc-updown-5m-1800000000", 1000.0, 1300.0, open_px=64240.0, binance_open_px=64320.0, poly_spot=64290.0, poly_open_provisional=False)
            r._basis_ema = 80.0
            r.poly_spot_fresh = True
            r.last_poly_spot = 64294.0
            r.close_window(close_px=64374.0, official_winner=None, winner_source="spot_fallback")
            self.assertLess(r.last_round_abs_move, 60.0)


if __name__ == "__main__":
    unittest.main()
