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
from sim.replay import overridden_params, run_replay


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

        # Book snapshots every 2s, complement-consistent (ask sum = 0.47+0.53
        # = 1.00, bid sum = 0.45+0.51 = 0.96 <= 1.00) so make_complementary
        # leaves them untouched. Up bid 0.45 carries 5 shares of queue ahead;
        # the MWDE stage works a 5-share maker bid there. Down ask 0.53 lets a
        # taker hedge lock the pair at 0.45+0.53+fee <= $1.00.
        with open(events_dir / f"events_{self.date}.jsonl", "w", encoding="utf-8") as f:
            for i in range(150):
                t = self.start + 2 * i
                # Before t=start+72: Up 0.45/0.46, Down 0.52/0.54 (ask sum=1.00, bid sum=0.97)
                # After t=start+72: market moves up -> Up 0.48/0.50, Down 0.50/0.52 (locks pair <= 1.00)
                if i < 36:
                    u_bid, u_ask, d_bid, d_ask = 0.45, 0.46, 0.52, 0.54
                else:
                    u_bid, u_ask, d_bid, d_ask = 0.48, 0.50, 0.50, 0.52
                f.write(json.dumps({
                    "t": t, "token_id": token_up, "outcome": "Up",
                    "best_bid": u_bid, "best_ask": u_ask,
                    "bid_depth": [[u_bid, 5.0]], "ask_depth": [[u_ask, 50.0]],
                    "last_trade_price": u_bid,
                }) + "\n")
                f.write(json.dumps({
                    "t": t, "token_id": token_dn, "outcome": "Down",
                    "best_bid": d_bid, "best_ask": d_ask,
                    "bid_depth": [[d_bid, 50.0]], "ask_depth": [[d_ask, 50.0]],
                    "last_trade_price": d_bid,
                }) + "\n")

        # One 15-share print at our 0.45 bid, after the quote has gone live:
        # 5 shares clear the queue ahead, then our resting 5-share order fills
        # completely. The Down print is inert (no resting Down maker order).
        with open(events_dir / f"trades_{self.date}.jsonl", "w", encoding="utf-8") as f:
            for token, outcome in ((token_up, "Up"), (token_dn, "Down")):
                f.write(json.dumps({
                    "token_id": token, "slug": self.slug, "outcome": outcome,
                    "price": 0.45 if outcome == "Up" else 0.52,
                    "size": 15.0, "side": "SELL",
                    "timestamp": self.start + 70, "received_at": self.start + 70.2,
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
        with overridden_params({"ENABLE_ARBITRAGE_HEDGE": True, "ARBITRAGE_HEDGE_MAX_PAIR_COST": 1.00, "MIN_TAKER_EDGE": 0.99}):
            results = run_replay(limit=1, quiet=True, write=False)
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertTrue(r["replayed"])
        self.assertGreaterEqual(r["fills"], 2)  # maker entry + taker hedge

        # Entry: 5 shares @ 0.45 (maker, zero fee). Hedge: 5 shares @ 0.52 +
        # taker fee. A locked pair settles at 5 * (1 - pair_cost) whichever side
        # wins, so the replay PnL is fully determined and winner-independent.
        fee_per_share = 0.07 * 0.52 * (1.0 - 0.52)
        expected = round(5.0 * (1.0 - (0.45 + 0.52 + fee_per_share)), 4)
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
                    "timestamp": self.start + 70, "received_at": self.start + 70.2,
                }) + "\n")

        with overridden_params({"MIN_TAKER_EDGE": 0.99}):
            results = run_replay(limit=1, quiet=True, write=False)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["replay_traded"])
        self.assertEqual(results[0]["replay_pnl"], 0.0)

    def test_param_overrides_are_restored_on_error(self):
        original = config.PARAMS["LWAA_MIN_PROB"]
        with self.assertRaises(RuntimeError):
            with overridden_params({"LWAA_MIN_PROB": 0.5, "NOT_A_REAL_PARAM": 1}):
                self.assertEqual(config.PARAMS["LWAA_MIN_PROB"], 0.5)
                self.assertEqual(config.PARAMS["NOT_A_REAL_PARAM"], 1)
                raise RuntimeError("boom")
        self.assertEqual(config.PARAMS["LWAA_MIN_PROB"], original)
        self.assertNotIn("NOT_A_REAL_PARAM", config.PARAMS)


if __name__ == "__main__":
    unittest.main()
