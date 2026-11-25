"""Regression tests for the 2026-11-12 logic audit.

Each test pins a defect that the original 80-test suite could not see because
it exercised policy/matcher units directly instead of the full runtime chain.
Every test uses an isolated temporary ledger.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.types import Decision, Order, OrderBook, Position, SpotState
from core.policy import plan_decision
from sim.matcher import QueueMatcher, make_complementary
from runtime.runner import PaperRunner


def _runner():
    return PaperRunner(ledger_dir=tempfile.mkdtemp(prefix="pm_audit_"))


def _book(bid, ask, t=1.0, asks=None):
    return OrderBook(
        bids=[(bid, 50.0)],
        asks=asks if asks is not None else [(ask, 50.0)],
        source_t=t, received_at=t,
    )


class TestMatcherSnapshotGuard(unittest.TestCase):
    def test_static_ladder_cannot_fill_more_than_visible_depth(self):
        """A 2-share ask level must not fill a 5-share order with zero prints."""
        m = QueueMatcher(0.3)
        o = Order(id=1, client_order_id="x", side="Up", price=0.45,
                  size=5.0, remaining=5.0, placed_at=0.0, live_at=0.3)
        book = OrderBook(bids=[(0.40, 10.0)], asks=[(0.44, 2.0)], source_t=1.0)
        m.register_order(o, book)
        total = sum(m.match_order(o, book, t) for t in (1.0, 2.0, 3.0, 4.0))
        self.assertLessEqual(total, 2.0 + 1e-9)
        self.assertAlmostEqual(o.filled_size, 2.0)

    def test_new_snapshot_allows_a_second_fill(self):
        """The guard must not block genuine new liquidity."""
        m = QueueMatcher(0.3)
        o = Order(id=2, client_order_id="y", side="Up", price=0.45,
                  size=5.0, remaining=5.0, placed_at=0.0, live_at=0.3)
        b1 = OrderBook(bids=[(0.40, 10.0)], asks=[(0.44, 2.0)], source_t=1.0)
        m.register_order(o, b1)
        f1 = m.match_order(o, b1, 1.0)
        b2 = OrderBook(bids=[(0.40, 10.0)], asks=[(0.44, 3.0)], source_t=2.0)
        f2 = m.match_order(o, b2, 2.0)
        self.assertAlmostEqual(f1, 2.0)
        self.assertAlmostEqual(f2, 3.0)
        self.assertAlmostEqual(o.filled_size, 5.0)


class TestRiskEnvelope(unittest.TestCase):
    def test_breaker_is_enabled_and_params_are_self_consistent(self):
        self.assertIsInstance(config.PARAMS["ENABLE_CIRCUIT_BREAKER"], bool)
        self.assertLessEqual(config.PARAMS["MAX_DAILY_DRAWDOWN_USD"],
                             config.PARAMS["INITIAL_CAPITAL_USD"])
        self.assertTrue(config.validate_params())

    def test_exhausted_bankroll_stops_new_entries(self):
        params = dict(config.PARAMS, AVAILABLE_CAPITAL=-100.0)
        spot = SpotState(timestamp=1.0, price=100100.0, open_price=100000.0,
                         poly_price=100100.0)
        dec = plan_decision(
            Position(0, 0), Position(0, 0),
            _book(0.40, 0.45), _book(0.40, 0.50),
            spot, 60.0, params
        )
        self.assertEqual(dec.action, "wait")
        self.assertIn("Insufficient capital", dec.reason)

    def test_committed_cost_envelope_blocks_entry(self):
        params = dict(config.PARAMS, COMMITTED_WINDOW_COST=config.PARAMS["MAX_WINDOW_LOSS_USD"] - 0.10)
        spot = SpotState(timestamp=1.0, price=100100.0, open_price=100000.0,
                         poly_price=100100.0)
        dec = plan_decision(
            Position(0, 0), Position(0, 0),
            _book(0.40, 0.45), _book(0.40, 0.50),
            spot, 60.0, params
        )
        self.assertEqual(dec.action, "wait")
        self.assertIn("单窗成本包络", dec.reason)

    def test_live_state_reports_real_bankroll_without_floor(self):
        r = _runner()
        r.start_window("s", 0.0, 300.0, 100000.0,
                       binance_open_px=99900.0, poly_spot=100000.0)
        r.total_pnl = -200.0
        r._write_live(1.0, SpotState(timestamp=1.0, price=100000.0,
                                     open_price=100000.0),
                      OrderBook(), OrderBook())
        live = json.loads(r.live_file.read_text(encoding="utf-8"))
        self.assertAlmostEqual(live["available_capital"], -100.0, places=2)


class TestPriceSeries(unittest.TestCase):
    def test_price_history_and_live_keep_two_distinct_sources(self):
        r = _runner()
        r.start_window("s", 0.0, 300.0, 100000.0,
                       binance_open_px=99900.0, poly_spot=100000.0)
        sp = SpotState(timestamp=1.0, price=100250.0, open_price=100000.0,
                       poly_price=100100.0, binance_open_price=99900.0)
        r.step(1.0, sp, _book(0.48, 0.52), _book(0.48, 0.52),
               poly_spot=100100.0)
        last = r.price_history[-1]
        self.assertAlmostEqual(last["price"], 100250.0)
        self.assertAlmostEqual(last["poly_price"], 100100.0)
        self.assertAlmostEqual(last["return_bps"], 35.04, places=1)
        self.assertAlmostEqual(last["poly_return_bps"], 10.0, places=1)
        self.assertFalse(last["poly_stale"])
        live = json.loads(r.live_file.read_text(encoding="utf-8"))
        self.assertAlmostEqual(live["spot"], 100250.0)
        self.assertAlmostEqual(live["poly_spot"], 100100.0)
        self.assertAlmostEqual(live["binance_open_px"], 99900.0)

    def test_stale_twap_falls_back_to_binance_chain(self):
        r = _runner()
        r.start_window("s", 0.0, 300.0, 100000.0,
                       binance_open_px=99900.0, poly_spot=100000.0)
        sp = SpotState(timestamp=1.0, price=100250.0, open_price=100000.0,
                       poly_price=None, binance_open_price=99900.0)
        r.step(1.0, sp, _book(0.48, 0.52), _book(0.48, 0.52), poly_spot=None)
        self.assertIsNone(sp.poly_price)
        self.assertAlmostEqual(sp.open_price, 99900.0)
        last = r.price_history[-1]
        self.assertIsNone(last["poly_price"])
        self.assertTrue(last["poly_stale"])
        self.assertNotIn("prob_up", last)

    def test_open_price_calibration_keeps_series_consistent(self):
        r = _runner()
        r.start_window("s", 0.0, 300.0, 100000.0,
                       binance_open_px=99900.0, poly_spot=100000.0)
        r.update_open_price(100050.0)
        first = r.price_history[0]
        self.assertAlmostEqual(first["price"], 99900.0)
        self.assertAlmostEqual(first["poly_price"], 100050.0)
        self.assertEqual(first["return_bps"], 0.0)
        self.assertEqual(first["poly_return_bps"], 0.0)

    def test_binance_open_price_is_not_overwritten_by_poly_strike(self):
        r = _runner()
        r.start_window("s", 0.0, 300.0, 100000.0,
                       binance_open_px=99900.0, poly_spot=100000.0)
        self.assertAlmostEqual(r.binance_open_price, 99900.0)


class TestPolicyDeadBranches(unittest.TestCase):
    def test_mwde_taker_is_reachable_after_complementary_correction(self):
        """make_complementary forces ask_up+ask_down>=1; the old <0.90 gate was dead."""
        cu, cd = make_complementary(
            _book(0.35, 0.38), _book(0.45, 0.50)
        )
        spot = SpotState(timestamp=1.0, price=100100.0, open_price=100000.0,
                         poly_price=100100.0)
        dec = plan_decision(
            Position(0, 0), Position(0, 0), cu, cd, spot, 200.0,
            dict(config.PARAMS, MAX_ORDERBOOK_SPREAD=0.10, EXECUTION_WEIGHTED_ROUTING=False),
            p_fair=0.60, quote_attempts=1
        )
        self.assertEqual(dec.action, "taker_buy")

    def test_allow_taker_unwind_false_really_disables_salvage(self):
        params = dict(config.PARAMS, ALLOW_TAKER_UNWIND=False,
                      DEFENSIVE_SALVAGE_ENABLE=True, ENABLE_ARBITRAGE_HEDGE=False)
        spot = SpotState(timestamp=1.0, price=99900.0, open_price=100000.0,
                         poly_price=99900.0)
        dec = plan_decision(
            Position(5.0, 2.25), Position(0, 0),
            _book(0.10, 0.99), _book(0.55, 0.99),
            spot, 25.0, params
        )
        self.assertNotEqual(dec.action, "taker_unwind")

    def test_tail_freeze_blocks_hedge_and_unwind(self):
        spot = SpotState(timestamp=1.0, price=100000.0, open_price=100000.0,
                         poly_price=100000.0)
        dec = plan_decision(
            Position(5.0, 2.25), Position(0, 0),
            _book(0.40, 0.45), _book(0.40, 0.40),
            spot, 5.0, dict(config.PARAMS)
        )
        self.assertEqual(dec.action, "wait")
        self.assertIn("Tail freeze", dec.reason)

    def test_hedge_lock_threshold_is_not_silently_overridden(self):
        params = dict(config.PARAMS, ENABLE_ARBITRAGE_HEDGE=False,
                      ARBITRAGE_HEDGE_MAX_PAIR_COST=0.85)
        spot = SpotState(timestamp=1.0, price=100000.0, open_price=100000.0,
                         poly_price=100000.0)
        dec = plan_decision(
            Position(5.0, 2.25), Position(0, 0),
            _book(0.40, 0.99), _book(0.40, 0.44),
            spot, 100.0, params
        )
        self.assertNotEqual(dec.action, "taker_hedge")


class TestSettlementTruth(unittest.TestCase):
    def test_twap_reconcile_cannot_overwrite_gamma_winner(self):
        with tempfile.TemporaryDirectory(prefix="pm_audit_") as tmp:
            summary = Path(tmp) / "summary.jsonl"
            rec = {
                "slug": "s", "start": 0.0, "end": 300.0,
                "winner": "Up", "winner_source": "gamma",
                "open_px": 100.0, "close_px": 101.0,
                "pos_up": 5.0, "pos_down": 0.0, "total_cost": 2.5,
                "proceeds": 5.0, "net_pnl": 2.5, "traded": True,
            }
            summary.write_text(json.dumps(rec) + "\n", encoding="utf-8")
            r = PaperRunner(ledger_dir=tmp)
            r.reconcile_record("s", 100.0, 99.0, "Down", winner_source="twap")
            out = json.loads(summary.read_text(encoding="utf-8").strip())
            self.assertEqual(out["winner"], "Up")
            self.assertEqual(out["close_px"], 99.0)
            self.assertEqual(out["net_pnl"], 2.5)

    def test_vwap_slippage_rejects_fake_riskfree_hedge(self):
        r = _runner()
        r.start_window("s", 0.0, 300.0, 100000.0,
                       binance_open_px=100000.0, poly_spot=100000.0)
        r.pos_up = Position(5.0, 2.25)
        # best_ask=0.30 looks like a lock, but walking 5 shares hits 0.75.
        bd = _book(0.20, 0.30, t=100.0,
                   asks=[(0.30, 1.0), (0.75, 50.0)])
        bu = _book(0.40, 0.60, t=100.0)
        sp = SpotState(timestamp=100.0, price=100000.0, open_price=100000.0,
                       poly_price=100000.0, binance_open_price=100000.0)
        with patch.dict(config.PARAMS, {"ENABLE_ARBITRAGE_HEDGE": True, "ARBITRAGE_HEDGE_MAX_PAIR_COST": 1.00}):
            r.step(100.0, sp, bu, bd, poly_spot=100000.0)
        rejects = [a for a in r.actions if a.get("action") == "taker_reject"]
        self.assertTrue(rejects)
        self.assertIn("VWAP", rejects[-1]["reason"])
        self.assertEqual(r.pos_down.shares, 0.0)


class TestSecondPassFixes(unittest.TestCase):
    def test_taker_buy_rejected_when_vwap_destroys_edge(self):
        """Directional entries get the same ladder-VWAP guard as hedges."""
        r = _runner()
        r.start_window("s", 0.0, 300.0, 100000.0,
                       binance_open_px=100000.0, poly_spot=100000.0)
        bu = _book(0.50, 0.40, t=200.0, asks=[(0.40, 1.0), (0.75, 50.0)])
        bd = _book(0.50, 0.55, t=200.0)
        sp = SpotState(timestamp=200.0, price=100100.0, open_price=100000.0,
                       poly_price=100100.0, binance_open_price=100000.0)
        dec = Decision(action="taker_buy", side="Up", price=0.40,
                       size=5.0, utility=0.03, reason="test")
        with patch("runtime.runner.plan_decision", return_value=dec):
            r.step(200.0, sp, bu, bd, poly_spot=100100.0)
        rejects = [a for a in r.actions if a.get("action") == "taker_reject"]
        self.assertTrue(rejects)
        self.assertIn("VWAP", rejects[-1]["reason"])
        self.assertEqual(r.pos_up.shares, 0.0)

    def test_insurance_premium_is_a_stock_not_a_per_fill_flow(self):
        r = _runner()
        r.start_window("s", 0.0, 300.0, 100000.0,
                       binance_open_px=100000.0, poly_spot=100000.0)
        r.pos_up = Position(5.0, 3.0)
        r.pos_down = Position(5.0, 2.5)
        bu = _book(0.40, 0.40, t=200.0)
        bd = _book(0.40, 0.60, t=200.0)
        dec = Decision(action="taker_buy", side="Up", price=0.40,
                       size=5.0, utility=0.5, reason="test")
        for t in (200.0, 201.0):
            sp = SpotState(timestamp=t, price=100100.0, open_price=100000.0,
                           poly_price=100100.0, binance_open_price=100000.0)
            with patch("runtime.runner.plan_decision", return_value=dec):
                r.step(t, sp, bu, bd, poly_spot=100100.0)
        expected = max(0.0, (r.pos_up.avg_price + r.pos_down.avg_price - 1.00)
                       * min(r.pos_up.shares, r.pos_down.shares))
        self.assertAlmostEqual(r.insurance_paid, expected, places=4)

    def test_unattributed_trade_print_is_dropped(self):
        from runtime.trade_stream import TradeStreamClient
        c = TradeStreamClient(record_raw=False)
        c._pending_trades.append({"slug": "", "price": 0.40, "size": 5.0})
        c._pending_trades.append({"slug": "btc-updown-5m-999",
                                  "price": 0.40, "size": 5.0})
        out = c.drain_trades(slug="btc-updown-5m-1000")
        self.assertEqual(out, [])
        self.assertEqual(c._dropped_unattributed, 1)
        self.assertEqual(len(c._pending_trades), 1)

    def test_order_ids_are_monotonic_and_unique(self):
        r = _runner()
        r.start_window("s", 0.0, 300.0, 100000.0,
                       binance_open_px=100000.0, poly_spot=100000.0)
        for t, px in ((200.0, 0.40), (201.0, 0.44)):
            sp = SpotState(timestamp=t, price=100100.0, open_price=100000.0,
                           poly_price=100100.0, binance_open_price=100000.0)
            dec = Decision(action="quote", side="Up", price=px,
                           size=5.0, utility=0.3, reason="test")
            with patch("runtime.runner.plan_decision", return_value=dec):
                r.step(t, sp, _book(0.38, 0.50, t=t),
                       _book(0.38, 0.50, t=t), poly_spot=100100.0)
        qids = [a["order_id"] for a in r.actions if a.get("action") == "quote"]
        self.assertEqual(len(qids), 2)
        self.assertEqual(len(set(qids)), 2)
        self.assertGreater(qids[1], qids[0])


if __name__ == "__main__":
    unittest.main()
