"""Regression tests for the v1.1.1 runtime-hardening pass.

Covers four defects that silently inflated or corrupted the paper journal:

1. A window settled twice (engine restart mid-window) was appended twice,
   double-counting PnL and the circuit breaker.
2. A single trade print and a stale crossed-book snapshot could fill the same
   resting order from the same liquidity in one tick.
3. WebSocket reconnects replayed trade prints, double-consuming the maker queue.
4. A dead Polymarket TWAP poller kept feeding a stale price into p_fair.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.types import Decision, OrderBook, Position, SpotState
from runtime.runner import PaperRunner
from runtime.trade_stream import TradeStreamClient


def _book(bid, bid_sz, ask, ask_sz):
    return OrderBook(
        bids=[[bid, bid_sz]],
        asks=[[ask, ask_sz]],
        last_trade_price=None,
        source_t=0.0,
        received_at=0.0,
    )


class TestJournalIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pm_hardening_")
        self.runner = PaperRunner(ledger_dir=self.tmp)

    def test_duplicate_close_is_ignored(self):
        r = self.runner
        r.start_window("btc-updown-5m-1800000000", 1000.0, 1300.0, 75000.0)
        r.pos_up = Position(5.0, 2.30)  # plausible entry, settled as a win
        r.close_window(75010.0, "Up")
        total_after_first = r.total_pnl

        # Engine restart mid-window re-settles the same slug.
        r.close_window(75010.0, "Up")

        lines = [
            line for line in (Path(self.tmp) / "summary.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(lines), 1, "each window must be journaled exactly once")
        self.assertEqual(
            sum(json.loads(line)["net_pnl"] for line in lines),
            round(total_after_first, 4),
            "duplicate close must not double-count PnL",
        )
        self.assertAlmostEqual(r.total_pnl, total_after_first, places=6)


class TestSingleTickFills(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pm_hardening_")
        self.runner = PaperRunner(ledger_dir=self.tmp)
        self.t0 = 1000.0
        self.runner.start_window("btc-updown-5m-1800000000", self.t0, self.t0 + 300.0, 75000.0)

    def _step(self, now, trade_prints=None):
        book_up = _book(0.44, 5.0, 0.46, 10.0)
        book_down = _book(0.54, 10.0, 0.56, 10.0)
        spot = SpotState(
            timestamp=now, price=75010.0, open_price=75000.0,
            return_bps=1.33, realized_vol_bps=8.0, regime="calm",
        )
        decide = Decision(action="quote", side="Up", price=0.46, size=5.0, reason="test quote")
        with patch("runtime.runner.plan_decision", return_value=decide):
            self.runner.step(now, spot, book_up, book_down, trade_prints=trade_prints)

    def test_print_fill_not_duplicated_by_stale_crossed_book(self):
        # Tick 1 places the resting Up bid @0.46 (queue ahead is 0: book bid is 0.44).
        self._step(self.t0 + 10.0)
        self.assertEqual(len(self.runner.working_orders), 1)

        # Tick 2: a print for 3 shares fills 3 of the 5 shares, leaving 2. The
        # book snapshot in the same tick shows 10 shares at 0.46 (already
        # consumed by the print), so a naive crossed-book match would fill the
        # remaining 2 from liquidity that is no longer there.
        self._step(
            self.t0 + 11.0,
            trade_prints=[{"outcome": "Up", "price": 0.46, "size": 3.0, "timestamp": self.t0 + 11.0}],
        )
        self.assertAlmostEqual(self.runner.pos_up.shares, 3.0, places=6)
        self.assertEqual(len(self.runner.fills), 1)


class TestTradeStreamDedupe(unittest.TestCase):
    def test_replayed_print_is_dropped(self):
        client = TradeStreamClient(record_raw=False)
        slug = "btc-updown-5m-1800000000"
        client.set_active_targets([slug], ["tok-up"])
        payload = {
            "asset": "tok-up", "slug": slug, "outcome": "Up",
            "price": 0.46, "size": 7.0, "timestamp": 1800000010.0,
        }
        self.assertTrue(client._ingest(dict(payload)))
        self.assertFalse(client._ingest(dict(payload)), "reconnect replay must be ignored")
        self.assertEqual(len(client.drain_trades(slug=slug)), 1)

        fresh = dict(payload, timestamp=1800000011.0)
        self.assertTrue(client._ingest(fresh))
        self.assertEqual(len(client.drain_trades(slug=slug)), 1)


class TestPolymarketPriceFreshness(unittest.TestCase):
    def test_stale_twap_is_not_returned(self):
        from runtime.market_feed import MarketFeed

        feed = MarketFeed.__new__(MarketFeed)  # no network in the constructor
        feed._last_poly_btc = 12345.0
        feed._last_poly_poll_t = time.time()
        feed._poly_btc_poller_active = True  # prevent spawning the poller thread

        feed._last_poly_btc_t = time.time() - 60.0
        self.assertIsNone(feed.get_polymarket_btc_price(), "stale TWAP must be rejected")

        feed._last_poly_btc_t = time.time()
        self.assertEqual(feed.get_polymarket_btc_price(), 12345.0)


if __name__ == "__main__":
    unittest.main()
