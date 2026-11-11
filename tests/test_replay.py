"""End-to-end tests for the event-driven replay engine.

These tests build a tiny synthetic recording on disk (book snapshots + trade
prints + oracle spot + a settled ledger) and assert that the replay produces
the fill a human would compute by hand. They are deliberately offline: no
network, no real ledger files, no dependence on the recorded 120 MB corpus.
"""
import json
import tempfile
import unittest
from pathlib import Path

import config
from sim.replay import GATE_OFF_OVERRIDES, overridden_params, run_replay


class TestEventDrivenReplay(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._orig = (config.MARKET_DIR, config.LEDGERS_DIR,
                      config.CURRENT_LEDGER_DIR, config.REPORTS_DIR)

        self.start = 1_800_000_000.0
        self.slug = f"btc-updown-5m-{int(self.start)}"
        self.date = "2026-11-10"

        market = self.tmp / "1_market"
        events_dir = market / "events" / self.date
        oracle_dir = market / "oracle" / self.date
        events_dir.mkdir(parents=True)
        oracle_dir.mkdir(parents=True)

        token_up = "tok_up"
        token_dn = "tok_dn"

        # Book snapshots every 2s: Up bid 0.46 (5 shares ahead), Down ask 0.50
        # so a taker hedge immediately locks a pair cost below $1.00.
        with open(events_dir / f"events_{self.date}.jsonl", "w", encoding="utf-8") as f:
            for i in range(150):
                t = self.start + 2 * i
                f.write(json.dumps({
                    "t": t, "token_id": token_up, "outcome": "Up",
                    "best_bid": 0.46, "best_ask": 0.48,
                    "bid_depth": [[0.46, 5.0]], "ask_depth": [[0.48, 50.0]],
                    "last_trade_price": 0.47,
                }) + "\n")
                f.write(json.dumps({
                    "t": t, "token_id": token_dn, "outcome": "Down",
                    "best_bid": 0.48, "best_ask": 0.50,
                    "bid_depth": [[0.48, 50.0]], "ask_depth": [[0.50, 50.0]],
                    "last_trade_price": 0.49,
                }) + "\n")

        # One 15-share print at our bid: 5 shares clear the queue, 10 fill us
        # (only a 5-share order is resting, so exactly 5 shares execute).
        with open(events_dir / f"trades_{self.date}.jsonl", "w", encoding="utf-8") as f:
            for token, outcome in ((token_up, "Up"), (token_dn, "Down")):
                f.write(json.dumps({
                    "token_id": token, "slug": self.slug, "outcome": outcome,
                    "price": 0.46 if outcome == "Up" else 0.49,
                    "size": 15.0, "side": "SELL",
                    "timestamp": self.start + 10, "received_at": self.start + 10.2,
                }) + "\n")

        with open(oracle_dir / f"oracle_{self.date}.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps({"t": self.start, "kind": "window_open",
                                "source": "polymarket_chainlink_twap",
                                "start_t": self.start, "open_price": 75000.0}) + "\n")
            for i in range(300):
                f.write(json.dumps({
                    "t": self.start + i, "kind": "spot", "price": 75010.0,
                    "return_bps": 1.33, "realized_vol_bps": 8.0, "regime": "calm",
                }) + "\n")
                f.write(json.dumps({"t": self.start + i, "kind": "poly_twap_live",
                                    "price": 75008.0}) + "\n")

        # Settlement truth: the ledger says the round was traded and won on Up.
        ledger = self.tmp / "2_ledgers" / config.VERSION
        ledger.mkdir(parents=True)
        with open(ledger / "summary.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "slug": self.slug, "start": self.start, "end": self.start + 300,
                "winner": "Up", "open_px": 75000.0, "close_px": 75010.0,
                "net_pnl": 0.0, "traded": True, "pos_up": 0, "pos_down": 0,
                "total_cost": 0.0,
            }) + "\n")

        config.MARKET_DIR = market
        config.LEDGERS_DIR = self.tmp / "2_ledgers"
        config.CURRENT_LEDGER_DIR = ledger
        config.REPORTS_DIR = self.tmp / "3_reports"

    def tearDown(self):
        (config.MARKET_DIR, config.LEDGERS_DIR,
         config.CURRENT_LEDGER_DIR, config.REPORTS_DIR) = self._orig
        self._tmp.cleanup()

    def test_replay_fills_only_on_recorded_print_and_locks_pair(self):
        results = run_replay(limit=1, overrides={"STRATEGY_MODE": "legacy_mm"})
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertTrue(r["replayed"])
        self.assertGreaterEqual(r["fills"], 2)  # maker entry + taker hedge

        # Entry: 5 shares @ 0.46. Hedge: 5 shares @ 0.48 + taker fee.
        # A locked pair must settle at 5 * (1 - pair_cost) whichever side wins,
        # so the replay PnL is fully determined and winner-independent.
        fee_per_share = 0.07 * 0.50 * (1.0 - 0.50)
        expected = round(5.0 * (1.0 - (0.46 + 0.50 + fee_per_share)), 4)
        self.assertAlmostEqual(r["replay_pnl"], expected, places=3)
        # The attribution label must come from the entry tick, not a tick-vote.
        self.assertEqual(r["regime_source"], "entry_time")

    def test_print_above_bid_does_not_fill(self):
        # Prints that never reach our bid must not consume the queue or fill:
        # the round stays flat instead of inventing a fill from a stale price.
        trades = config.MARKET_DIR / "events" / self.date / f"trades_{self.date}.jsonl"
        with open(trades, "w", encoding="utf-8") as f:
            for token, outcome in (("tok_up", "Up"), ("tok_dn", "Down")):
                f.write(json.dumps({
                    "token_id": token, "slug": self.slug, "outcome": outcome,
                    "price": 0.60, "size": 100.0, "side": "BUY",
                    "timestamp": self.start + 10, "received_at": self.start + 10.2,
                }) + "\n")

        results = run_replay(limit=1)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["replay_traded"])
        self.assertEqual(results[0]["replay_pnl"], 0.0)

    def _move_recording_to_last_100s(self):
        """Keep only the final 100 seconds: tau < MIN_ENTRY_TAU_S (180s)."""
        ev_path = config.MARKET_DIR / "events" / self.date / f"events_{self.date}.jsonl"
        rows = [json.loads(line) for line in ev_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        keep = [r for r in rows if float(r["t"]) >= self.start + 200]
        ev_path.write_text("\n".join(json.dumps(r) for r in keep) + "\n", encoding="utf-8")

        tr_path = config.MARKET_DIR / "events" / self.date / f"trades_{self.date}.jsonl"
        trades = [json.loads(line) for line in tr_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for t in trades:
            t["timestamp"] = self.start + 210
            t["received_at"] = self.start + 210.2
        tr_path.write_text("\n".join(json.dumps(t) for t in trades) + "\n", encoding="utf-8")

    def test_late_window_is_blocked_by_default_and_trades_with_gates_off(self):
        # Same recording, but every quote/fill happens inside the last 100s.
        self._move_recording_to_last_100s()

        gated = run_replay(limit=1, quiet=True, write=False)[0]
        self.assertTrue(gated["replayed"])
        self.assertFalse(gated["replay_traded"], "tail freeze must block entries")
        self.assertAlmostEqual(gated["replay_pnl"], 0.0, places=6)

        # Counterfactual: the same window with the regime gates removed.
        off = run_replay(
            limit=1,
            quiet=True,
            write=False,
            overrides=GATE_OFF_OVERRIDES,
        )[0]
        self.assertTrue(off["replay_traded"], "gates-off must quote and fill")
        self.assertGreater(off["fills"], 0)

    def test_param_overrides_are_restored_on_error(self):
        original = config.PARAMS["TAIL_FREEZE_S"]
        with self.assertRaises(RuntimeError):
            with overridden_params({"TAIL_FREEZE_S": 0.0, "NOT_A_REAL_PARAM": 1}):
                self.assertEqual(config.PARAMS["TAIL_FREEZE_S"], 0.0)
                self.assertEqual(config.PARAMS["NOT_A_REAL_PARAM"], 1)
                raise RuntimeError("boom")
        self.assertEqual(config.PARAMS["TAIL_FREEZE_S"], original)
        self.assertNotIn("NOT_A_REAL_PARAM", config.PARAMS)


if __name__ == "__main__":
    unittest.main()
