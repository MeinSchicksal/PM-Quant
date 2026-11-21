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
    def test_delayed_taker_rejects_collapsing_knife(self):
        tmp = tempfile.mkdtemp(prefix="pm_knife_")
        runner = PaperRunner(ledger_dir=tmp)
        t0 = 1800000000.0
        runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0)
        b_up = _book(0.27, 20.0, 0.28, 20.0)
        b_dn = _book(0.72, 20.0, 0.73, 20.0)
        crashed_up = _book(0.09, 20.0, 0.10, 20.0)
        spot = SpotState(
            timestamp=t0 + 260.0, price=65005.0, open_price=65000.0,
            return_bps=0.77, realized_vol_bps=14.0, regime="normal",
        )
        take_dec = Decision(action="taker_buy", side="Up", price=0.28, size=5.0, reason="test taker")
        with patch("runtime.runner.plan_decision", return_value=take_dec):
            runner.step(
                t0 + 260.0, spot, b_up, b_dn,
                delayed_book_fetcher=lambda _side: crashed_up,
                replay_mode=True,
            )
        self.assertEqual(runner.pos_up.shares, 0.0, "collapsing knife fill ($0.28 -> $0.10) must be rejected")
        self.assertTrue(any(a.get("action") == "taker_reject" and "接飞刀" in (a.get("reason") or "") for a in runner.actions))


class TestV26PolicyUpgrades(unittest.TestCase):
    def test_blackswan_lock_triggers_when_opp_ask_at_or_below_006(self):
        import config
        from core.policy import plan_decision

        pos_up = Position(shares=5.0, cash=2.40)  # @ 0.48
        pos_dn = Position(0.0, 0.0)
        b_up = _book(0.94, 50.0, 0.95, 50.0)
        b_dn = _book(0.05, 50.0, 0.06, 50.0)
        spot = SpotState(timestamp=1250.0, price=65050.0, open_price=65000.0, realized_vol_bps=14.0)
        dec = plan_decision(pos_up, pos_dn, b_up, b_dn, spot, tau_s=45.0, params=config.PARAMS, entries_count=1)
        self.assertEqual(dec.action, "taker_hedge")
        self.assertEqual(dec.side, "Down")
        self.assertAlmostEqual(dec.price, 0.06, places=4)

    def test_pyramid_confirm_add_triggers_on_strong_winner(self):
        import config
        from core.policy import plan_decision

        pos_up = Position(shares=5.0, cash=2.40)  # @ 0.48
        pos_dn = Position(0.0, 0.0)
        b_up = _book(0.55, 50.0, 0.57, 50.0)
        b_dn = _book(0.43, 50.0, 0.45, 50.0)
        spot = SpotState(timestamp=1150.0, price=65080.0, open_price=65000.0, realized_vol_bps=14.0)
        dec = plan_decision(pos_up, pos_dn, b_up, b_dn, spot, tau_s=150.0, params=config.PARAMS, p_fair=0.72, entries_count=1)
        self.assertIn(dec.action, ("taker_buy", "quote"))
        self.assertEqual(dec.side, "Up")
        self.assertIn("顺势确认加仓", dec.reason)

    def test_reversal_flip_triggers_when_leg1_collapses_and_opp_has_edge(self):
        import config
        from core.policy import plan_decision

        pos_up = Position(shares=5.0, cash=2.40)  # @ 0.48
        pos_dn = Position(0.0, 0.0)
        b_up = _book(0.41, 50.0, 0.43, 50.0)
        b_dn = _book(0.57, 50.0, 0.59, 50.0)
        spot = SpotState(timestamp=1180.0, price=64910.0, open_price=65000.0, realized_vol_bps=14.0)
        dec = plan_decision(pos_up, pos_dn, b_up, b_dn, spot, tau_s=120.0, params=config.PARAMS, p_fair=0.25, entries_count=1)
        self.assertIn(dec.action, ("taker_buy", "quote"))
        self.assertEqual(dec.side, "Down")
        self.assertIn("坍塌反手", dec.reason)

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
        p_early = dict(config.PARAMS, SECONDS_SINCE_LAST_ENTRY=0.5)
        dec_early = plan_decision(pos_up, pos_dn, b_up, b_dn, spot, tau_s=170.0, params=p_early, p_fair=0.20, entries_count=1)
        self.assertEqual(dec_early.action, "wait")

        # 16.0s after Leg-1 fill -> cooldown satisfied, pyramid add allowed
        p_ready = dict(config.PARAMS, SECONDS_SINCE_LAST_ENTRY=16.0)
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
        p = dict(config.PARAMS, SECONDS_SINCE_LAST_ENTRY=30.0)
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

        # 2. At t0+75.0, urgent cancel is requested -> order enters pending_cancels for 300ms [75.0, 75.30)
        cancel_dec = Decision(action="wait", reason="toxic shock", urgent_cancel=True)
        with patch("runtime.runner.plan_decision", return_value=cancel_dec):
            runner.step(t0 + 75.0, spot, b_up, b_dn, replay_mode=True)
        self.assertEqual(len(runner.working_orders), 0)
        self.assertEqual(len(runner.pending_cancels), 1)

        # 3. Trade print arrives at t0+75.15 (150ms after cancel request, still in flight!) -> must fill!
        with patch("runtime.runner.plan_decision", return_value=Decision(action="wait", reason="hold")):
            runner.step(
                t0 + 75.15, spot, b_up, b_dn,
                trade_prints=[{"outcome": "Up", "price": 0.50, "size": 5.0, "timestamp": t0 + 75.15}],
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


if __name__ == "__main__":
    unittest.main()
