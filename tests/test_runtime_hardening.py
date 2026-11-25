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
import shutil
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

    def _step(self, now, trade_prints=None, up_ask=0.46):
        book_up = _book(0.44, 5.0, up_ask, 10.0)
        book_down = _book(0.54, 10.0, 0.56, 10.0)
        spot = SpotState(
            timestamp=now, price=75010.0, open_price=75000.0,
            return_bps=1.33, realized_vol_bps=8.0, regime="calm",
        )
        decide = Decision(action="quote", side="Up", price=0.46, size=5.0, reason="test quote")
        with patch("runtime.runner.plan_decision", return_value=decide):
            self.runner.step(now, spot, book_up, book_down, trade_prints=trade_prints)

    def test_print_fill_not_duplicated_by_stale_crossed_book(self):
        # Tick 1 places the resting Up bid @0.46 while ask is 0.48 (queue ahead is 0: book bid is 0.44).
        self._step(self.t0 + 10.0, up_ask=0.48)
        self.assertEqual(len(self.runner.working_orders), 1)
        # Tick 1b activates the resting Post-Only order cleanly once wire latency elapses.
        self._step(self.t0 + 10.5, up_ask=0.48)
        self.assertTrue(self.runner.working_orders[0].activated)

        # Tick 2: a print for 3 shares fills 3 of the 5 shares, leaving 2. The
        # book snapshot in the same tick shows 10 shares at 0.46 (already
        # consumed by the print), so a naive crossed-book match would fill the
        # remaining 2 from liquidity that is no longer there.
        self._step(
            self.t0 + 11.0,
            trade_prints=[{"outcome": "Up", "price": 0.46, "size": 3.0, "timestamp": self.t0 + 11.0}],
            up_ask=0.46,
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


class TestTradeStreamIdleWatchdog(unittest.TestCase):
    def test_idle_stream_triggers_reconnect(self):
        import asyncio
        import sys
        import types

        client = TradeStreamClient(record_raw=False, idle_reconnect_s=0.1)
        client.running = True
        connect_calls = 0

        class _FakeWS:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def send(self, _msg):
                return None

            async def recv(self):
                # Stop the test loop after the watchdog reconnects once
                if connect_calls >= 2:
                    client.running = False
                await asyncio.sleep(0.08)
                return json.dumps({"type": "pong"})  # non-trade keepalive without payload

        def _fake_connect(*_args, **_kwargs):
            nonlocal connect_calls
            connect_calls += 1
            return _FakeWS()

        fake_websockets = types.SimpleNamespace(connect=_fake_connect)
        with patch.dict(sys.modules, {"websockets": fake_websockets}):
            asyncio.run(client._ws_worker())

        self.assertGreaterEqual(client.reconnect_count, 1)
        self.assertIn("idle watchdog", client.last_error)


class TestOrderBookRetry(unittest.TestCase):
    def test_transient_timeout_recovers_on_retry(self):
        from types import SimpleNamespace
        from runtime.market_feed import MarketFeed

        feed = MarketFeed.__new__(MarketFeed)
        ok_resp = SimpleNamespace(
            status_code=200,
            content=b"{}",
            json=lambda: {
                "bids": [{"price": "0.48", "size": "12"}],
                "asks": [{"price": "0.52", "size": "15"}],
                "last_trade_price": "0.50",
            },
        )
        sess = SimpleNamespace(get=None)
        calls = []

        def _fake_get(url, timeout=None):
            calls.append((url, timeout))
            if len(calls) == 1:
                raise TimeoutError("transient read timeout")
            return ok_resp

        sess.get = _fake_get
        book, lat_ms, _, _ = feed._fetch_order_book_raw("tok-up", sess=sess)
        self.assertIsNotNone(book)
        self.assertEqual(len(calls), 2)
        self.assertAlmostEqual(book.best_bid, 0.48, places=4)
        self.assertAlmostEqual(book.best_ask, 0.52, places=4)

    def test_client_404_is_not_retried(self):
        from types import SimpleNamespace
        from runtime.market_feed import MarketFeed

        feed = MarketFeed.__new__(MarketFeed)
        calls = 0

        def _fake_get(_url, timeout=None):
            nonlocal calls
            calls += 1
            return SimpleNamespace(status_code=404, content=b"not found")

        sess = SimpleNamespace(get=_fake_get)
        book, _, _, _ = feed._fetch_order_book_raw("tok-missing", sess=sess)
        self.assertIsNone(book)
        self.assertEqual(calls, 1)


class TestEmptyBookProbabilityCarryForward(unittest.TestCase):
    def test_empty_book_carries_forward_last_prob_and_complements(self):
        tmp = tempfile.mkdtemp(prefix="pm_prob_carry_")
        runner = PaperRunner(ledger_dir=tmp)
        t0 = 1800000000.0
        runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0)

        def _spot(now):
            return SpotState(
                timestamp=now, price=64900.0, open_price=65000.0,
                return_bps=-15.38, realized_vol_bps=14.0, regime="normal",
            )

        wait_dec = Decision(action="wait", reason="test wait")
        with patch("runtime.runner.plan_decision", return_value=wait_dec):
            # Tick 1: extreme Down certainty (Up = 0.01 ask, no bid; Down = 0.99 bid, no ask)
            b_up_valid = OrderBook(bids=[], asks=[(0.01, 100.0)], source_t=t0 + 10, received_at=t0 + 10)
            b_dn_valid = OrderBook(bids=[(0.99, 100.0)], asks=[], source_t=t0 + 10, received_at=t0 + 10)
            runner.step(t0 + 10.0, _spot(t0 + 10.0), b_up_valid, b_dn_valid)
            self.assertAlmostEqual(runner.price_history[-1]["poly_up"], 0.01, places=4)

            # Tick 2: Up book transiently empty, Down book still 0.985 -> complement 0.015 (not 0.50!)
            empty_book = OrderBook(bids=[], asks=[], source_t=0.0, received_at=0.0)
            b_dn_valid2 = _book(0.98, 50.0, 0.99, 50.0)
            runner.step(t0 + 11.0, _spot(t0 + 11.0), empty_book, b_dn_valid2)
            self.assertAlmostEqual(runner.price_history[-1]["poly_up"], 0.015, places=4)

            # Tick 3: both books transiently empty -> carry forward 0.015 (never revert to 0.50!)
            runner.step(t0 + 12.0, _spot(t0 + 12.0), empty_book, empty_book)
            self.assertAlmostEqual(runner.price_history[-1]["poly_up"], 0.015, places=4)
            self.assertAlmostEqual(runner.price_history[-1]["poly_down"], 0.985, places=4)


class TestLowLatencyWSFeeds(unittest.TestCase):
    def test_binance_spot_ws_parse_and_freshness(self):
        from runtime.ws_feeds import BinanceSpotWSClient

        ws = BinanceSpotWSClient()
        self.assertIsNone(ws.get_latest(max_age_s=2.0, now=1000.0))
        self.assertTrue(ws.apply_message({"b": "64000.0", "a": "64002.0"}, now=1000.0))
        got = ws.get_latest(max_age_s=2.0, now=1001.5)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got[0], 64001.0, places=4)
        self.assertEqual(got[2], "binance_ws")
        # Stale beyond max_age_s returns None so MarketFeed falls back to HTTP
        self.assertIsNone(ws.get_latest(max_age_s=2.0, now=1003.0))

    def test_polymarket_book_ws_snapshot_and_incremental_deltas(self):
        from runtime.ws_feeds import PolymarketBookWSClient

        ws = PolymarketBookWSClient()
        tok = "tok-up-1"
        # Delta before snapshot must be ignored
        self.assertFalse(ws.apply_message({
            "event_type": "price_change",
            "price_changes": [{"asset_id": tok, "price": "0.50", "size": "20", "side": "BUY"}],
        }, now=1000.0))
        self.assertIsNone(ws.get_order_book(tok, now=1000.0))

        # Full book snapshot
        self.assertTrue(ws.apply_message([{
            "event_type": "book",
            "asset_id": tok,
            "bids": [{"price": "0.48", "size": "100"}, {"price": "0.47", "size": "50"}],
            "asks": [{"price": "0.50", "size": "80"}, {"price": "0.51", "size": "40"}],
            "last_trade_price": "0.49",
        }], now=1001.0))
        bk = ws.get_order_book(tok, max_age_s=3.0, now=1002.0)
        self.assertIsNotNone(bk)
        self.assertAlmostEqual(bk.best_bid, 0.48, places=4)
        self.assertAlmostEqual(bk.best_ask, 0.50, places=4)
        self.assertAlmostEqual(bk.last_trade_price, 0.49, places=4)

        # Incremental delta: remove 0.48 bid, add 0.49 bid, update 0.50 ask
        self.assertTrue(ws.apply_message({
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": tok, "price": "0.48", "size": "0", "side": "BUY"},
                {"asset_id": tok, "price": "0.49", "size": "25", "side": "BUY"},
                {"asset_id": tok, "price": "0.50", "size": "15", "side": "SELL"},
            ],
        }, now=1002.5))
        bk2 = ws.get_order_book(tok, max_age_s=3.0, now=1003.0)
        self.assertAlmostEqual(bk2.best_bid, 0.49, places=4)
        self.assertEqual(bk2.bids[0], (0.49, 25.0))
        self.assertEqual(bk2.asks[0], (0.50, 15.0))
        # Stale book returns None
        self.assertIsNone(ws.get_order_book(tok, max_age_s=3.0, now=1006.0))


class TestTakerKnifeDropProtection(unittest.TestCase):
    def test_delayed_taker_honestly_fills_collapsing_knife_without_lookahead_reject(self):
        """Anti-self-deception invariant (v2.10): once an IOC buy limit ($0.30) arrives at the CLOB
        and the ask has crashed to $0.10, a real exchange ALWAYS fills it into the knife. The paper
        simulator must honestly record the fill at $0.10 rather than inventing a post-arrival reject."""
        tmp = tempfile.mkdtemp(prefix="pm_knife_")
        runner = PaperRunner(ledger_dir=tmp)
        t0 = 1800000000.0
        runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0)
        b_up = _book(0.27, 20.0, 0.28, 20.0)
        b_dn = _book(0.72, 20.0, 0.73, 20.0)
        crashed_up = _book(0.09, 20.0, 0.10, 20.0)
        crashed_dn = _book(0.90, 20.0, 0.91, 20.0)
        spot = SpotState(
            timestamp=t0 + 260.0, price=65005.0, open_price=65000.0,
            return_bps=0.77, realized_vol_bps=14.0, regime="normal",
        )
        take_dec = Decision(action="taker_buy", side="Up", price=0.28, size=5.0, reason="test taker")
        with patch("runtime.runner.plan_decision", return_value=take_dec):
            runner.step(
                t0 + 260.0, spot, b_up, b_dn,
                delayed_book_fetcher=lambda _side: (crashed_up, crashed_dn),
                replay_mode=True,
            )
        self.assertAlmostEqual(runner.pos_up.shares, 5.0, places=4, msg="Real CLOB fills IOC buy limit into crash at $0.10")
        self.assertAlmostEqual(runner.fills[-1]["price"], 0.10, places=4)


class TestV26PolicyUpgrades(unittest.TestCase):
    def test_pyramid_confirm_add_triggers_on_strong_winner(self):
        import config
        from core.policy import plan_decision

        pos_up = Position(shares=5.0, cash=2.40)  # @ 0.48
        pos_dn = Position(0.0, 0.0)
        b_up = _book(0.55, 50.0, 0.57, 50.0)
        b_dn = _book(0.43, 50.0, 0.45, 50.0)
        spot = SpotState(timestamp=1150.0, price=65080.0, open_price=65000.0, realized_vol_bps=14.0)
        p_pyr = dict(
            config.PARAMS,
            ENABLE_PYRAMID_ADD=True,
            MAX_LOTS=2,
            MAX_ENTRIES_PER_WINDOW=2,
            PYRAMID_MIN_PROB=0.70,
            PYRAMID_MAX_ABOVE_AVG=0.20,
            PYRAMID_MAX_PRICE=0.72,
        )
        dec = plan_decision(pos_up, pos_dn, b_up, b_dn, spot, tau_s=150.0, params=p_pyr, p_fair=0.72, entries_count=1)
        self.assertIn(dec.action, ("taker_buy", "quote"))
        self.assertEqual(dec.side, "Up")
        self.assertIn("加仓", dec.reason)

    def test_reversal_flip_triggers_when_leg1_collapses_and_opp_has_edge(self):
        import config
        from core.policy import plan_decision

        pos_up = Position(shares=5.0, cash=2.40)  # @ 0.48
        pos_dn = Position(0.0, 0.0)
        b_up = _book(0.51, 50.0, 0.53, 50.0)
        b_dn = _book(0.46, 50.0, 0.48, 50.0)  # pair_cost = 0.48 + 0.48 + 0.0175 = 0.9775 <= 0.98 (< $1.00)
        spot = SpotState(timestamp=1180.0, price=64910.0, open_price=65000.0, realized_vol_bps=14.0)
        p_flip = dict(config.PARAMS, ENABLE_REVERSAL_FLIP=True, MAX_LOTS=2, MAX_ENTRIES_PER_WINDOW=2, FLIP_MAX_PAIR_COST=0.98)
        dec = plan_decision(pos_up, pos_dn, b_up, b_dn, spot, tau_s=120.0, params=p_flip, p_fair=0.25, entries_count=1)
        self.assertIn(dec.action, ("taker_buy", "quote"))
        self.assertEqual(dec.side, "Down")
        self.assertIn("动态不对劲反买", dec.reason)

        # When opposite ask pushes pair_cost > $1.00 (0.48 + 0.59 + fee = 1.087 > 0.98), pairing is strictly forbidden!
        b_up_low = _book(0.41, 50.0, 0.43, 50.0)
        b_dn_over1 = _book(0.57, 50.0, 0.59, 50.0)
        dec_over1 = plan_decision(pos_up, pos_dn, b_up_low, b_dn_over1, spot, tau_s=120.0, params=p_flip, p_fair=0.25, entries_count=1)
        self.assertEqual(dec_over1.action, "wait")

    def test_secondary_quote_uses_entry_purpose_and_fresh_quote_attempts(self):
        tmp = tempfile.mkdtemp(prefix="pm_sec_entry_")
        runner = PaperRunner(ledger_dir=tmp)
        t0 = 1800000000.0
        runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0)
        # Simulate Leg-1 having used all 3 quote attempts before filling
        runner.quote_attempts = 3
        runner.pos_up = Position(shares=5.0, cash=2.40)
        runner._record_fill(t0 + 80.0, "Up", 0.48, 5.0, "maker", "entry", order_id=3)
        self.assertEqual(runner.entries_count, 1)
        self.assertEqual(runner.quote_attempts, 0, "Leg-1 fill must reset quote_attempts for Leg-2")

        # Secondary pyramid quote must be tagged purpose='entry', increment quote_attempts, and count entries_count on fill
        b_up = _book(0.55, 50.0, 0.57, 50.0)
        b_dn = _book(0.43, 50.0, 0.45, 50.0)
        spot = SpotState(timestamp=t0 + 150.0, price=65080.0, open_price=65000.0, realized_vol_bps=14.0)
        sec_quote = Decision(action="quote", side="Up", price=0.56, size=5.0, reason="v2.6 顺势确认加仓 Maker")
        with patch("runtime.runner.plan_decision", return_value=sec_quote):
            runner.step(t0 + 150.0, spot, b_up, b_dn, replay_mode=True)
        self.assertEqual(len(runner.working_orders), 1)
        self.assertEqual(runner.working_orders[0].purpose, "entry")
        self.assertEqual(runner.quote_attempts, 1)

        # Fill secondary quote via trade print -> entries_count must advance to 2
        with patch("runtime.runner.plan_decision", return_value=Decision(action="wait", reason="hold")):
            runner.step(
                t0 + 151.0, spot, b_up, b_dn,
                trade_prints=[{"outcome": "Up", "price": 0.56, "size": 10.0, "timestamp": t0 + 151.0}],
                replay_mode=True,
            )
        self.assertEqual(runner.entries_count, 2)

    def test_connected_ws_quiet_book_stays_live_beyond_3s(self):
        from runtime.ws_feeds import PolymarketBookWSClient

        ws = PolymarketBookWSClient(idle_reconnect_s=20.0)
        ws.connected = True
        tok = "tok-quiet"
        ws.apply_message([{
            "event_type": "book",
            "asset_id": tok,
            "bids": [{"price": "0.49", "size": "100"}],
            "asks": [{"price": "0.51", "size": "100"}],
        }], now=1000.0)
        # 6 seconds later with no book change, connected WS within idle_reconnect_s stays live
        bk = ws.get_order_book(tok, max_age_s=3.0, now=1006.0)
        self.assertIsNotNone(bk)
        self.assertAlmostEqual(bk.source_t, 1006.0, places=4)
        self.assertAlmostEqual(bk.received_at, 1006.0, places=4)
        # Once past idle_reconnect_s (25s), returns None to force HTTP fallback
        self.assertIsNone(ws.get_order_book(tok, max_age_s=3.0, now=1025.0))

    def test_secondary_entry_cooldown_blocks_instant_double_tap(self):
        import config
        from core.policy import plan_decision

        pos_dn = Position(shares=5.0, cash=3.35)  # @ 0.67
        pos_up = Position(0.0, 0.0)
        b_up = _book(0.32, 50.0, 0.34, 50.0)
        b_dn = _book(0.66, 50.0, 0.67, 50.0)
        spot = SpotState(timestamp=1150.0, price=64900.0, open_price=65000.0, realized_vol_bps=14.0)

        # 0.5s after Leg-1 fill -> must NOT double-tap!
        p_base = dict(
            config.PARAMS,
            ENABLE_PYRAMID_ADD=True,
            MAX_LOTS=2,
            MAX_ENTRIES_PER_WINDOW=2,
            PYRAMID_MIN_PROB=0.70,
            PYRAMID_MAX_PRICE=0.72,
            PYRAMID_MAX_ABOVE_AVG=0.05,
        )
        p_early = dict(p_base, SECONDS_SINCE_LAST_ENTRY=0.5)
        dec_early = plan_decision(pos_up, pos_dn, b_up, b_dn, spot, tau_s=170.0, params=p_early, p_fair=0.20, entries_count=1)
        self.assertEqual(dec_early.action, "wait")

        # 16.0s after Leg-1 fill -> cooldown satisfied, pyramid add allowed
        p_ready = dict(p_base, SECONDS_SINCE_LAST_ENTRY=16.0)
        dec_ready = plan_decision(pos_up, pos_dn, b_up, b_dn, spot, tau_s=154.0, params=p_ready, p_fair=0.20, entries_count=1)
        self.assertIn(dec_ready.action, ("taker_buy", "quote"))
        self.assertEqual(dec_ready.side, "Down")

    def test_pyramid_checks_gross_shares_not_net_gap_after_partial_hedge(self):
        import config
        from core.policy import plan_decision

        # 10 Down (already at MAX_LOTS=2) + 4 Up partial hedge -> net_gap=6, must NOT pyramid Down again
        pos_dn = Position(shares=10.0, cash=5.00)
        pos_up = Position(shares=4.0, cash=0.24)
        b_up = _book(0.41, 50.0, 0.43, 50.0)
        b_dn = _book(0.57, 50.0, 0.59, 50.0)
        spot = SpotState(timestamp=1160.0, price=64920.0, open_price=65000.0, realized_vol_bps=14.0)
        p = dict(config.PARAMS, MAX_LOTS=2, MAX_NET_LOTS=2, SECONDS_SINCE_LAST_ENTRY=30.0)
        dec = plan_decision(pos_up, pos_dn, b_up, b_dn, spot, tau_s=140.0, params=p, p_fair=0.20, entries_count=1)
        self.assertEqual(dec.action, "wait")

    def test_crossed_ws_book_self_heals_and_invalidates(self):
        from runtime.ws_feeds import PolymarketBookWSClient

        ws = PolymarketBookWSClient()
        ws.connected = True
        tok = "tok-crossed"
        ws.apply_message([{
            "event_type": "book",
            "asset_id": tok,
            "bids": [{"price": "0.52", "size": "50"}],
            "asks": [{"price": "0.50", "size": "50"}],
        }], now=1000.0)
        gen_before = ws._token_gen
        self.assertIsNone(ws.get_order_book(tok, now=1001.0))
        self.assertGreater(ws._token_gen, gen_before)

    def test_cancel_latency_allows_adverse_fill_during_300ms_wire_flight(self):
        tmp = tempfile.mkdtemp(prefix="pm_cancel_lat_")
        runner = PaperRunner(ledger_dir=tmp)
        t0 = 1800000000.0
        runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0)
        b_up = _book(0.49, 10.0, 0.51, 10.0)
        b_dn = _book(0.49, 10.0, 0.51, 10.0)
        spot = SpotState(timestamp=t0 + 70.0, price=65010.0, open_price=65000.0, realized_vol_bps=14.0)

        # 1. Place 5-share Maker quote at t0+70.0 (live_at = t0+70.30, queue_ahead = 0 at 0.50)
        q_dec = Decision(action="quote", side="Up", price=0.50, size=5.0, reason="maker entry")
        with patch("runtime.runner.plan_decision", return_value=q_dec):
            runner.step(t0 + 70.0, spot, b_up, b_dn, replay_mode=True)
        self.assertEqual(len(runner.working_orders), 1)

        # 2. At t0+75.0, urgent cancel is requested -> order enters pending_cancels for CANCEL_LATENCY_S [75.0, 75.12)
        cancel_dec = Decision(action="wait", reason="toxic shock", urgent_cancel=True)
        with patch("runtime.runner.plan_decision", return_value=cancel_dec):
            runner.step(t0 + 75.0, spot, b_up, b_dn, replay_mode=True)
        self.assertEqual(len(runner.working_orders), 0)
        self.assertEqual(len(runner.pending_cancels), 1)

        # 3. Trade print arrives at t0+75.06 (60ms after cancel request, still in flight!) -> must fill!
        with patch("runtime.runner.plan_decision", return_value=Decision(action="wait", reason="hold")):
            runner.step(
                t0 + 75.06, spot, b_up, b_dn,
                trade_prints=[{"outcome": "Up", "price": 0.50, "size": 5.0, "timestamp": t0 + 75.06}],
                replay_mode=True,
            )
        self.assertAlmostEqual(runner.pos_up.shares, 5.0, places=4)
        self.assertEqual(len(runner.pending_cancels), 0)

    def test_minimum_5_share_order_rule_enforced_everywhere(self):
        import config
        from core.policy import plan_decision
        from core.execution import plan_order_actions

        # 1. Partial fill of 1.5 shares (< 5.0 shares) must NOT trigger sub-5-share black-swan hedge or salvage
        pos_dn = Position(shares=1.5, cash=0.87)  # @ 0.58
        pos_up = Position(0.0, 0.0)
        b_up = _book(0.05, 50.0, 0.06, 50.0)
        b_dn = _book(0.94, 50.0, 0.95, 50.0)
        spot = SpotState(timestamp=1200.0, price=64900.0, open_price=65000.0, realized_vol_bps=14.0)
        dec = plan_decision(pos_up, pos_dn, b_up, b_dn, spot, tau_s=100.0, params=config.PARAMS, entries_count=1)
        self.assertEqual(dec.action, "wait", "Sub-5-share inventory must never place a sub-5-share hedge order")

        # 2. plan_order_actions rejects any quote/taker decision < 5.0 shares
        bad_quote = Decision(action="quote", side="Up", price=0.50, size=3.5, reason="sub-5 quote")
        _, act = plan_order_actions([], bad_quote, now=1200.0, min_order_shares=5.0)
        self.assertIsNone(act)

    def test_v27_uncapped_high_vol_and_fat_tail_student_t5_pricing(self):
        from core.estimator import calculate_dynamic_realized_vol, normal_cdf, student_t5_cdf, estimate_p_fair

        # 1. Real liquidation cascade (> 80 bps/5m) must NOT be artificially clamped at 80 bps
        hist = [(1000.0 + i, 60000.0 * ((1.0 - 0.0012) if i % 2 else (1.0 + 0.0012))) for i in range(60)]
        vol = calculate_dynamic_realized_vol(hist, baseline_vol_bps=14.0, half_life_s=20.0)
        self.assertGreater(vol, 100.0, "High realized volatility must not be clamped at 80 bps")

        # 2. Student-t5 fat-tail CDF is strictly more conservative than Gaussian at high z-scores
        self.assertLess(student_t5_cdf(1.5), normal_cdf(1.5))
        self.assertLess(student_t5_cdf(2.5), normal_cdf(2.5))

        # 3. Cross-chain basis inversion guard: when BOTH Binance spot > BinOpen AND Chainlink TWAP > Open,
        # an inflated basis_ema must NOT flip estimate_p_fair to bearish (< 0.50)
        spot = SpotState(
            timestamp=1230.0,
            price=58959.51,
            open_price=58825.48,
            binance_open_price=58938.01,
            basis_ema=142.18,
            realized_vol_bps=25.0,
        )
        p_up = estimate_p_fair(spot, open_px=58825.48, tau_s=70.0, vol_bps=25.0, poly_price=58832.82)
        self.assertGreaterEqual(p_up, 0.50, "Must not hallucinate Down when both Binance and Chainlink are above open")

    def test_v28_pyramid_is_taker_only_and_requires_high_prob_and_tau(self):
        import config
        from core.policy import plan_decision

        pos_up = Position(shares=5.0, cash=2.80)  # avg 0.56
        pos_dn = Position(0.0, 0.0)
        b_dn_tight = _book(0.32, 50.0, 0.34, 50.0)
        b_dn_cheap = _book(0.43, 50.0, 0.45, 50.0)
        spot = SpotState(timestamp=1200.0, price=65060.0, open_price=65000.0, realized_vol_bps=18.0)
        params = dict(
            config.PARAMS,
            ENABLE_PYRAMID_ADD=True,
            MAX_LOTS=2,
            MAX_ENTRIES_PER_WINDOW=2,
            PYRAMID_MIN_PROB=0.70,
            PYRAMID_MIN_TAU_S=35.0,
            PYRAMID_MIN_TAKER_EDGE=0.055,
            PYRAMID_USE_DYNAMIC_EDGE=False,
            SECONDS_SINCE_LAST_ENTRY=30.0,
        )

        # A: Taker edge insufficient (0.72 - 0.68 - fee < 0.055) -> must NOT hang passive Maker pyramid bid
        b_up_tight = _book(0.66, 50.0, 0.68, 50.0)
        dec_a = plan_decision(pos_up, pos_dn, b_up_tight, b_dn_tight, spot, tau_s=80.0, params=params, p_fair=0.72, entries_count=1)
        self.assertEqual(dec_a.action, "wait")

        # B: Probability below 0.70 (0.68) -> blocked even with wide edge
        b_up_cheap = _book(0.55, 50.0, 0.57, 50.0)
        dec_b = plan_decision(pos_up, pos_dn, b_up_cheap, b_dn_cheap, spot, tau_s=80.0, params=params, p_fair=0.68, entries_count=1)
        self.assertEqual(dec_b.action, "wait")

        # C: tau < PYRAMID_MIN_TAU_S (35s) -> blocked from pyramiding near expiry
        dec_c = plan_decision(pos_up, pos_dn, b_up_cheap, b_dn_cheap, spot, tau_s=25.0, params=params, p_fair=0.74, entries_count=1)
        self.assertEqual(dec_c.action, "wait")

        # D: p_fair >= 0.70, tau >= 45s, Taker edge >= 0.055 -> executes Taker pyramid add
        dec_d = plan_decision(pos_up, pos_dn, b_up_cheap, b_dn_cheap, spot, tau_s=80.0, params=params, p_fair=0.74, entries_count=1)
        self.assertEqual(dec_d.action, "taker_buy")
        self.assertEqual(dec_d.side, "Up")
        self.assertIn("加仓", dec_d.reason)

    def test_v28_heavy_ask_wall_blocks_maker_but_allows_taker(self):
        import config
        from core.policy import plan_decision

        pos_empty = Position(0.0, 0.0)
        # Thin bid (20 sh) vs heavy ask wall (300 sh -> ratio 15x >= 4.0 and ask_d5 >= 200)
        b_up_wall = OrderBook(bids=[(0.48, 20.0)], asks=[(0.50, 300.0)], source_t=1200.0, received_at=1200.0)
        b_dn = _book(0.49, 50.0, 0.51, 50.0)
        spot = SpotState(timestamp=1200.0, price=65030.0, open_price=65000.0, realized_vol_bps=16.0)

        p_mk = dict(config.PARAMS, ENABLE_MAKER_ENTRY=True, MIN_MAKER_EDGE=0.05, MIN_TAKER_EDGE=0.065)
        # Edge enough for Maker (0.56 - 0.49 = 0.07) but not Taker (0.56 - 0.50 - 0.0175 = 0.0425 < 0.070)
        dec_mk = plan_decision(pos_empty, pos_empty, b_up_wall, b_dn, spot, tau_s=120.0, params=p_mk, p_fair=0.56)
        self.assertEqual(dec_mk.action, "wait")
        self.assertIn("卖墙压盘", dec_mk.reason)

        # Stronger edge (0.60 - 0.50 - 0.0175 = 0.0825 >= 0.070) still sweeps via Taker
        dec_tk = plan_decision(pos_empty, pos_empty, b_up_wall, b_dn, spot, tau_s=120.0, params=p_mk, p_fair=0.60)
        self.assertEqual(dec_tk.action, "taker_buy")
        self.assertEqual(dec_tk.side, "Up")

    def test_v28_extreme_vol_storm_pauses_entries_and_adds(self):
        import config
        from core.policy import plan_decision

        spot_storm = SpotState(timestamp=1200.0, price=65200.0, open_price=65000.0, realized_vol_bps=95.0)
        b_up = _book(0.54, 50.0, 0.56, 50.0)
        b_dn = _book(0.43, 50.0, 0.45, 50.0)

        # New entry must pause during extreme volatility storm (> 75 bps/5m)
        dec_new = plan_decision(
            Position(0.0, 0.0), Position(0.0, 0.0), b_up, b_dn, spot_storm,
            tau_s=120.0, params=config.PARAMS, p_fair=0.72,
        )
        self.assertEqual(dec_new.action, "wait")
        self.assertIn("极端巨震风暴避险", dec_new.reason)

        # Pyramid add also pauses during extreme storm
        dec_add = plan_decision(
            Position(shares=5.0, cash=2.70), Position(0.0, 0.0), b_up, b_dn, spot_storm,
            tau_s=120.0, params=dict(config.PARAMS, SECONDS_SINCE_LAST_ENTRY=30.0), p_fair=0.75, entries_count=1,
        )
        self.assertEqual(dec_add.action, "wait")

    def test_v28_ws_event_driven_wake_event_and_exception_filter(self):
        import asyncio
        import threading
        from runtime.ws_feeds import (
            BinanceSpotWSClient,
            PolymarketBookWSClient,
            _install_ws_loop_exception_filter,
        )

        ev = threading.Event()
        spot_ws = BinanceSpotWSClient(wake_event=ev, wake_delta_usd=1.0)
        spot_ws.apply_message({"b": "60000.0", "a": "60000.0"}, now=1000.0)
        self.assertTrue(ev.is_set(), "Initial spot price should trigger wake_event")

        ev.clear()
        spot_ws.apply_message({"b": "60000.3", "a": "60000.3"}, now=1000.1)
        self.assertFalse(ev.is_set(), "Sub-$1 spot jitter should not wake loop")

        spot_ws.apply_message({"b": "60001.5", "a": "60001.5"}, now=1000.2)
        self.assertTrue(ev.is_set(), ">=$1 spot move should wake loop immediately")

        ev.clear()
        book_ws = PolymarketBookWSClient(wake_event=ev)
        tok = "tok_wake"
        book_ws.apply_message([{
            "event_type": "book", "asset_id": tok,
            "bids": [{"price": "0.49", "size": "50"}],
            "asks": [{"price": "0.51", "size": "50"}],
        }], now=1000.0)
        self.assertTrue(ev.is_set(), "Book snapshot should wake loop")

        ev.clear()
        # Deep-level update (0.40 bid) does not change top-of-book -> no wake
        book_ws.apply_message({
            "event_type": "price_change",
            "price_changes": [{"asset_id": tok, "price": "0.40", "size": "100", "side": "BUY"}],
        }, now=1000.1)
        self.assertFalse(ev.is_set(), "Deep-level book update should not wake loop")

        # Top-of-book update (0.50 ask) -> wakes loop immediately
        book_ws.apply_message({
            "event_type": "price_change",
            "price_changes": [{"asset_id": tok, "price": "0.50", "size": "30", "side": "SELL"}],
        }, now=1000.2)
        self.assertTrue(ev.is_set(), "Top-of-book price change should wake loop immediately")

        # Exception filter suppresses websockets 15.x recv_messages AttributeError only
        loop = asyncio.new_event_loop()
        try:
            seen = []
            loop.default_exception_handler = lambda ctx: seen.append(ctx)
            _install_ws_loop_exception_filter(loop)
            loop.call_exception_handler({
                "message": "Exception in callback Connection.connection_lost()",
                "exception": AttributeError("'ClientConnection' object has no attribute 'recv_messages'"),
            })
            self.assertEqual(len(seen), 0, "recv_messages AttributeError must be silently suppressed")
            loop.call_exception_handler({
                "message": "real error",
                "exception": RuntimeError("boom"),
            })
            self.assertEqual(len(seen), 1, "Unrelated exceptions must still reach default handler")
        finally:
            loop.close()

    def test_runtime_hardening_patches_timeout_atomic_retry_quickedit_and_reprobe(self):
        import types
        import tempfile
        import shutil
        from pathlib import Path
        from unittest.mock import patch
        import main as pm_main
        from web.server import QuantHandler, _HardenedHTTPServer
        from runtime.market_feed import MarketFeed

        # 1. Slowloris / port-scanner request timeout guard
        self.assertEqual(getattr(QuantHandler, "timeout", None), 10)
        self.assertGreaterEqual(_HardenedHTTPServer.request_queue_size, 16)

        # 2. _atomic_replace retries transient Windows PermissionError cleanly
        tmp = Path(tempfile.mkdtemp(prefix="pm_atom_"))
        try:
            src = tmp / "a.tmp"
            dst = tmp / "a.jsonl"
            src.write_text("ok", encoding="utf-8")
            calls = {"n": 0}
            orig_replace = Path.replace

            def flaky_replace(self_p, target_p):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise PermissionError("simulated reader lock")
                return orig_replace(self_p, target_p)

            with patch.object(Path, "replace", flaky_replace):
                PaperRunner._atomic_replace(src, dst, retries=5)
            self.assertEqual(calls["n"], 3)
            self.assertEqual(dst.read_text(encoding="utf-8"), "ok")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        # 3. Windows QuickEdit helper never raises even in headless/redirected test runner
        pm_main._disable_windows_quick_edit()

        # 4. MarketFeed.reprobe_connectivity syncs worker session + all 3 WS clients on route flip
        with patch.object(MarketFeed, "_test_connectivity", return_value=None):
            feed = MarketFeed(proxy="http://127.0.0.1:7897")
        try:
            feed.session.proxies = {}
            w_sess = feed._get_worker_session()
            self.assertEqual(w_sess.proxies, {})
            ws_s = types.SimpleNamespace(proxy=None)
            ws_b = types.SimpleNamespace(proxy=None)
            ws_t = types.SimpleNamespace(proxy=None)
            feed.attach_ws_clients(spot_ws=ws_s, book_ws=ws_b, trade_ws=ws_t)

            def _flip_to_proxy():
                feed.session.proxies = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}

            with patch.object(feed, "_test_connectivity", side_effect=_flip_to_proxy):
                changed = feed.reprobe_connectivity(now=1000.0)
            self.assertTrue(changed)
            self.assertEqual(feed._get_worker_session().proxies.get("https"), "http://127.0.0.1:7897")
            self.assertEqual(ws_s.proxy, "http://127.0.0.1:7897")
            self.assertEqual(ws_b.proxy, "http://127.0.0.1:7897")
            self.assertEqual(ws_t.proxy, "http://127.0.0.1:7897")
        finally:
            feed.close()


class TestProductionHardeningV28(unittest.TestCase):
    def test_corrupted_summary_tail_does_not_wipe_history(self):
        tmp = Path(tempfile.mkdtemp(prefix="pm_corrupt_"))
        try:
            summary = tmp / "summary.jsonl"
            valid_row = {
                "slug": "btc-updown-5m-1800000000", "start": 1800000000.0, "end": 1800000300.0,
                "winner": "Up", "winner_source": "gamma", "pos_up": 5.0, "pos_down": 0.0,
                "total_cost": 2.5, "proceeds": 5.0, "net_pnl": 2.5, "fills_count": 1, "traded": True,
            }
            summary.write_text(json.dumps(valid_row) + "\n{\"slug\": \"truncated_half_line", encoding="utf-8")
            runner = PaperRunner(ledger_dir=tmp)
            self.assertAlmostEqual(runner.total_pnl, 2.5, places=4)
            self.assertIn("btc-updown-5m-1800000000", runner._settled_slugs)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_resume_from_live_restores_same_slug_only(self):
        tmp = Path(tempfile.mkdtemp(prefix="pm_resume_"))
        try:
            slug = "btc-updown-5m-1800000600"
            live_payload = {
                "slug": slug,
                "pos_up": {"shares": 5.0, "cash": 2.65, "avg_px": 0.53},
                "pos_down": {"shares": 0.0, "cash": 0.0, "avg_px": 0.0},
                "insurance_paid": 0.0,
                "regime": "normal_oscillation",
                "actions": [
                    {"t": 1800000680.0, "action": "fill", "side": "Up", "price": 0.53, "size": 5.0, "kind": "maker", "purpose": "entry", "order_id": 7}
                ],
                "price_history": [{"t": 1800000600.0, "price": 65000.0}, {"t": 1800000680.0, "price": 65020.0}],
                "decision_timeline": [{"t": 1800000680.0, "action": "quote"}],
            }
            (tmp / "live.json").write_text(json.dumps(live_payload), encoding="utf-8")

            # Default resume_from_live=False keeps clean flat state
            r_clean = PaperRunner(ledger_dir=tmp)
            r_clean.start_window(slug, 1800000600.0, 1800000900.0, 65000.0)
            self.assertEqual(r_clean.pos_up.shares, 0.0)
            self.assertEqual(r_clean.entries_count, 0)

            # Explicit resume_from_live=True restores matching slug state
            r_resumed = PaperRunner(ledger_dir=tmp)
            r_resumed.start_window(slug, 1800000600.0, 1800000900.0, 65000.0, resume_from_live=True)
            self.assertAlmostEqual(r_resumed.pos_up.shares, 5.0, places=4)
            self.assertAlmostEqual(r_resumed.pos_up.cash, 2.65, places=4)
            self.assertEqual(r_resumed.entries_count, 1)
            self.assertEqual(len(r_resumed.fills), 1)
            self.assertGreaterEqual(r_resumed._next_order_id, 7)

            # Different slug does not restore stale live.json
            r_other = PaperRunner(ledger_dir=tmp)
            r_other.start_window("btc-updown-5m-1800000900", 1800000900.0, 1800001200.0, 65000.0, resume_from_live=True)
            self.assertEqual(r_other.pos_up.shares, 0.0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_twap_realized_60s_is_time_weighted_under_variable_cadence(self):
        tmp = Path(tempfile.mkdtemp(prefix="pm_twap_"))
        try:
            t0 = 1800000000.0
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0, binance_open_px=65000.0, poly_open_provisional=False)
            runner._basis_ema = 0.0
            runner._basis_updated_t = t0 + 270.0
            # Quiet 20s at 2Hz (40 samples) around 65000, then volatile 2s at 10Hz (20 samples) around 65100.
            # True time-weighted average over [240, 262] is close to ~65009, while naive arithmetic average is ~65033.
            buf = []
            for i in range(41):
                buf.append((t0 + 240.0 + i * 0.5, 65000.0))
            for i in range(1, 21):
                buf.append((t0 + 260.0 + i * 0.1, 65100.0))
            runner._spot_buf = buf
            spot = SpotState(timestamp=t0 + 262.0, price=65100.0, open_price=65000.0, realized_vol_bps=14.0)
            b_up = _book(0.55, 50.0, 0.56, 50.0)
            b_dn = _book(0.44, 50.0, 0.45, 50.0)
            runner.step(t0 + 262.0, spot, b_up, b_dn, poly_spot=65010.0, replay_mode=True)
            self.assertIsNotNone(spot.twap_realized_60s)
            self.assertLess(spot.twap_realized_60s, 65015.0)
            self.assertGreater(spot.twap_realized_60s, 65004.0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_delayed_taker_cancels_when_pending_cancel_fills_in_transit(self):
        tmp = Path(tempfile.mkdtemp(prefix="pm_race_"))
        try:
            t0 = 1800000000.0
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0, binance_open_px=65000.0)
            b_up = _book(0.54, 50.0, 0.56, 50.0)
            b_dn = _book(0.44, 50.0, 0.46, 50.0)
            spot = SpotState(timestamp=t0 + 120.0, price=65030.0, open_price=65000.0, realized_vol_bps=14.0)

            # Step 1: place resting Maker bid (5 shares @ 0.55)
            quote_dec = Decision(action="quote", side="Up", price=0.55, size=5.0, reason="maker quote")
            with patch("runtime.runner.plan_decision", return_value=quote_dec):
                runner.step(t0 + 120.0, spot, b_up, b_dn, replay_mode=True)
            self.assertEqual(len(runner.working_orders), 1)

            # Step 2: 1s later, strategy flips to taker_buy (5 shares @ 0.56), cancelling the Maker bid
            # with 300ms wire cancel latency. During the 300ms transit, a trade print hits the Maker bid.
            take_dec = Decision(action="taker_buy", side="Up", price=0.56, size=5.0, reason="flip to taker")
            in_transit_prints = [{"outcome": "Up", "price": 0.55, "size": 10.0, "received_at": t0 + 121.10}]
            with patch("runtime.runner.plan_decision", return_value=take_dec):
                runner.step(
                    t0 + 121.0, spot, b_up, b_dn,
                    replay_mode=True,
                    delayed_book_fetcher=lambda _side: b_up,
                    delayed_prints_fetcher=lambda: in_transit_prints,
                )
            # Only the 5-share Maker fill should occur; duplicate 5-share Taker chase must be blocked.
            self.assertAlmostEqual(runner.pos_up.shares, 5.0, places=4)
            self.assertTrue(any(a.get("action") == "taker_reject" and "取消重复Taker追单" in (a.get("reason") or "") for a in runner.actions))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ws_book_require_recent_msg_and_pong_liveness(self):
        from runtime.ws_feeds import PolymarketBookWSClient

        ws = PolymarketBookWSClient(idle_reconnect_s=20.0)
        tok = "tok-up-live"
        ws.apply_message([{
            "event_type": "book",
            "asset_id": tok,
            "bids": [{"price": "0.49", "size": "50"}],
            "asks": [{"price": "0.51", "size": "50"}],
        }], now=1000.0)
        ws.connected = True
        ws._last_pong_t = 1004.5
        # Quiet connected book stays live for regular 10Hz ticks thanks to pong keepalive
        self.assertIsNotNone(ws.get_order_book(tok, max_age_s=3.0, now=1005.0, require_recent_msg=False))
        # Delayed Taker verification requires recent book msg within 1.5s -> returns None to trigger REST check
        self.assertIsNone(ws.get_order_book(tok, max_age_s=1.5, now=1005.0, require_recent_msg=True))


if __name__ == "__main__":
    unittest.main()
