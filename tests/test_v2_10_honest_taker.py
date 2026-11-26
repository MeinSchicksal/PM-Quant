"""Regression tests for v2.10: Experimental Integrity, Honest Simulator & Scientific Diagnostics.

Locks down core invariants of v2.10 (v2.9 strategy baseline preserved intact + experiment-level fixes):
1. Strategy parameters match v2.9 baseline (Maker + Pyramid + Sub-$1.00 Flip restored; Blackswan & CircuitBreaker OFF).
2. Pre-submission exact IOC limit price; zero post-arrival look-ahead cancellation (even on crashes).
3. Honest high-RTT wire latency ceiling (no artificial 600ms cap on 2.2s connections).
4. Pre-submission visible L2 depth VWAP check blocks thin-ask sweeps.
5. Independent Alpha diagnostics: Brier Score (model P* vs market mid) recorded & reconciled cleanly,
   plus `signal_minus_venue_ms` relative latency diagnostic in MarketFeed.
"""
import json
import tempfile
import unittest
from unittest.mock import patch

import config
from core.policy import plan_decision
from core.types import Decision, OrderBook, Position, SpotState
from runtime.market_feed import MarketFeed
from runtime.runner import PaperRunner


def _book(bid_px, bid_sz, ask_px, ask_sz, t=1200.0):
    return OrderBook(
        bids=[(bid_px, bid_sz)],
        asks=[(ask_px, ask_sz)],
        source_t=t,
        received_at=t,
    )


class TestV210ExperimentalIntegrityAndDiagnostics(unittest.TestCase):
    def test_v210_preserves_v29_strategy_baseline_and_honest_latency_cap(self):
        self.assertEqual(config.VERSION, "v3.3")
        self.assertTrue(config.PARAMS["ENABLE_MAKER_ENTRY"])
        self.assertFalse(config.PARAMS["MODEL_T5_UNIT_VARIANCE"])
        self.assertTrue(config.PARAMS["DIRECTIONAL_ANTI_TOXICITY"])
        self.assertEqual(config.PARAMS["MAX_LOTS"], 4)
        self.assertEqual(config.PARAMS["MAX_NET_LOTS"], 2)
        self.assertEqual(config.PARAMS["MAX_ENTRIES_PER_WINDOW"], 4)
        self.assertTrue(config.PARAMS["ENABLE_PYRAMID_ADD"])
        self.assertTrue(config.PARAMS["ENABLE_REVERSAL_FLIP"])
        self.assertTrue(config.PARAMS["FLIP_PAYOFF_SIZING"])
        self.assertTrue(config.PARAMS["FLIP_REQUIRE_BREAKEVEN"])
        self.assertFalse(config.PARAMS["FLIP_ALLOW_MAKER"])
        self.assertFalse(config.PARAMS["ENABLE_CIRCUIT_BREAKER"])
        self.assertFalse(config.PARAMS["ENABLE_BOOK_DROP_GUARD"])
        self.assertAlmostEqual(config.PARAMS["BOOK_DROP_GUARD_MAX"], 0.03, places=4)
        self.assertAlmostEqual(config.PARAMS["ENTRY_MAX_ADVERSE_SPOT_BPS"], 1.5, places=4)
        self.assertAlmostEqual(config.PARAMS["ENTRY_MAX_PRICE"], 0.74, places=4)
        self.assertAlmostEqual(config.PARAMS["ENTRY_MIN_PROB"], 0.53, places=4)
        self.assertEqual(config.PARAMS["SPOT_WS_PROVIDER"], "coinbase")
        self.assertGreaterEqual(config.PARAMS["LATENCY_MAX_S"], 2.0)
        self.assertTrue(config.validate_params(config.PARAMS))

    def test_high_rtt_is_not_artificially_capped_at_600ms(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = PaperRunner(ledger_dir=tmp)
            runner.net_stats = {"poly_latency_ms": 2200.0}  # 1100ms one-way + 110ms overhead = 1.21s
            samples = [runner._effective_wire_latency(replay_mode=False) for _ in range(30)]
            self.assertGreater(min(samples), 0.90, "2.2s RTT must not be masked under a 600ms cap")

    def test_pre_submission_visible_l2_depth_blocks_thin_ask_sweep(self):
        pos_empty = Position(0.0, 0.0)
        # Best ask 0.48 only has 1 share; sweeping 5 shares walks into 0.65 (VWAP=0.616 -> negative net edge)
        b_up_thin = OrderBook(bids=[(0.46, 50.0)], asks=[(0.48, 1.0), (0.65, 50.0)], source_t=1150.0, received_at=1150.0)
        b_dn = _book(0.51, 50.0, 0.53, 50.0)
        spot = SpotState(timestamp=1150.0, price=65060.0, open_price=65000.0, realized_vol_bps=16.0)
        p_no_mk = dict(config.PARAMS, ENABLE_MAKER_ENTRY=False)
        dec = plan_decision(pos_empty, pos_empty, b_up_thin, b_dn, spot, tau_s=150.0, params=p_no_mk, p_fair=0.60)
        self.assertEqual(dec.action, "wait")

    def test_native_ioc_limit_fills_honestly_on_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-1000", 1000.0, 1300.0, 65000.0, binance_open_px=65000.0)
            b_up = _book(0.47, 50.0, 0.49, 50.0, t=1150.0)
            b_dn = _book(0.50, 50.0, 0.52, 50.0, t=1150.0)
            sp = SpotState(timestamp=1150.0, price=65080.0, open_price=65000.0, binance_open_price=65000.0, basis_ema=0.0)
            dec = Decision(action="taker_buy", side="Up", price=0.49, size=5.0, utility=0.11, limit_price=0.51, reason="test")
            # Delayed crash: Up crashes to 0.35 (legitimate dual book: 0.35 + 0.66 = 1.01) -> MUST fill at 0.35!
            crash_up = _book(0.34, 50.0, 0.35, 50.0, t=1150.2)
            crash_dn = _book(0.65, 50.0, 0.66, 50.0, t=1150.2)
            with patch("runtime.runner.plan_decision", return_value=dec), patch("runtime.runner.time.sleep"):
                runner.step(1150.0, sp, b_up, b_dn, delayed_book_fetcher=lambda _: (crash_up, crash_dn))
            self.assertAlmostEqual(runner.pos_up.shares, 5.0, places=4)
            self.assertAlmostEqual(runner.fills[-1]["price"], 0.35, places=4)

    def test_brier_score_benchmark_and_reconcile_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-1000", 1000.0, 1300.0, 65000.0, binance_open_px=65000.0)
            # Mid-window tick (tau=140s <= 150s): model P* > market mid (0.50)
            b_up = _book(0.49, 50.0, 0.51, 50.0, t=1160.0)
            b_dn = _book(0.49, 50.0, 0.51, 50.0, t=1160.0)
            sp = SpotState(timestamp=1160.0, price=65080.0, open_price=65000.0, binance_open_price=65000.0, realized_vol_bps=14.0)
            with patch("runtime.runner.plan_decision", return_value=Decision(action="wait", reason="w")):
                runner.step(1160.0, sp, b_up, b_dn, replay_mode=True)
            runner.close_window(close_px=65100.0, official_winner="Up", winner_source="twap")
            rec = json.loads(runner.summary_file.read_text(encoding="utf-8").splitlines()[-1])
            self.assertIn("brier_model", rec)
            self.assertIn("brier_market", rec)
            self.assertLess(rec["brier_model"], rec["brier_market"])

            # Reconciling official winner to Down must recompute Brier Scores consistently
            runner.reconcile_record("btc-updown-5m-1000", 65000.0, 64900.0, "Down", winner_source="gamma")
            rec2 = json.loads(runner.summary_file.read_text(encoding="utf-8").splitlines()[-1])
            self.assertGreater(rec2["brier_model"], rec2["brier_market"])

    def test_signal_minus_venue_latency_metric_exposed(self):
        with patch.object(MarketFeed, "_test_connectivity", return_value=None):
            mf = MarketFeed(proxy="")
            mf.net_stats["binance_latency_ms"] = 313.4
            mf.net_stats["poly_latency_ms"] = 6.8
            s = mf.get_net_summary()
            self.assertAlmostEqual(s["signal_minus_venue_ms"], 306.6, places=1)
            mf.close()


if __name__ == "__main__":
    unittest.main()
