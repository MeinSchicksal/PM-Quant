"""Unit tests for v3.2: Unlocked Probabilistic Multi-Buy, No Profit-Lock Ceiling,
Directional Momentum Taker IOC, and Taker-Only Confirmed Reversal Rebalance.
"""
import tempfile
import unittest

import config
from core.policy import plan_decision
from core.types import OrderBook, Position, SpotState
from runtime.runner import PaperRunner


def _book(bid: float, ask: float, depth: float = 50.0, t: float = 1200.0) -> OrderBook:
    return OrderBook(
        bids=[(bid, depth)],
        asks=[(ask, depth)],
        last_trade_price=ask,
        source_t=t,
        received_at=t,
    )


def _spot(px: float = 60100.0, open_px: float = 60000.0, vel_bps: float = 0.5, vol_bps: float = 15.0) -> SpotState:
    return SpotState(
        timestamp=1200.0,
        price=px,
        open_price=open_px,
        realized_vol_bps=vol_bps,
        velocity_bps=vel_bps,
        is_toxic_shock=(abs(vel_bps) >= 3.5),
    )


class TestV32UnlockedMultibuy(unittest.TestCase):
    def test_no_profit_lock_cap_on_winning_position(self):
        """Profit-lock hedges stay OFF by default so winners run cleanly to $1.00."""
        pos_up = Position(shares=5.0, cash=2.25)  # @ 0.45
        pos_dn = Position(0.0, 0.0)
        b_up = _book(0.84, 0.86)
        b_dn = _book(0.14, 0.16)
        dec = plan_decision(
            pos_up, pos_dn, b_up, b_dn, _spot(vel_bps=0.5),
            tau_s=60.0, params=dict(config.PARAMS, SECONDS_SINCE_LAST_ENTRY=10.0),
            p_fair=0.88, entries_count=1,
        )
        self.assertEqual(dec.action, "wait")

    def test_directional_momentum_allows_tailwind_taker_but_blocks_adverse(self):
        """Spot shock +3.2 bps UP permits Taker IOC on Up, while blocking passive Maker and adverse Down."""
        b_up = _book(0.49, 0.51)
        b_dn = _book(0.49, 0.51)
        dec_tailwind = plan_decision(
            Position(), Position(), b_up, b_dn, _spot(vel_bps=+3.2),
            tau_s=150.0, params=config.PARAMS, p_fair=0.60, entries_count=0,
        )
        self.assertEqual(dec_tailwind.action, "taker_buy")
        self.assertEqual(dec_tailwind.side, "Up")

        dec_adverse = plan_decision(
            Position(), Position(), b_up, b_dn, _spot(vel_bps=-3.2),
            tau_s=150.0, params=config.PARAMS, p_fair=0.60, entries_count=0,
        )
        self.assertEqual(dec_adverse.action, "wait")

    def test_multibuy_scales_at_moderate_probability_and_reenters_after_neutralize(self):
        """Unlocks PYRAMID_MIN_PROB=0.70 and allows re-entry after net_gap returns to 0."""
        pos_up = Position(shares=5.0, cash=2.40)
        pos_dn = Position(0.0, 0.0)
        b_up = _book(0.49, 0.51)
        b_dn = _book(0.49, 0.51)
        # P*(Up)=0.58 (< 0.70 old gate) scales in 2nd lot when v3.2 relaxed add params are enabled
        v32_add_params = dict(
            config.PARAMS,
            SECONDS_SINCE_LAST_ENTRY=5.0,
            SECONDARY_ENTRY_COOLDOWN_S=4.0,
            PYRAMID_MIN_TAKER_EDGE=0.035,
            PYRAMID_MAX_ABOVE_AVG=None,
            ALLOW_REENTRY_AFTER_PAIR=True,
            REENTRY_COOLDOWN_S=4.0,
        )
        dec_add = plan_decision(
            pos_up, pos_dn, b_up, b_dn, _spot(vel_bps=+0.8),
            tau_s=180.0, params=v32_add_params,
            p_fair=0.58, entries_count=1,
        )
        self.assertEqual(dec_add.action, "taker_buy")
        self.assertEqual(dec_add.side, "Up")

        # After neutralizing (5 Up + 5 Down -> net_gap = 0), still re-enters when ALLOW_REENTRY_AFTER_PAIR=True
        dec_re = plan_decision(
            Position(5.0, 2.50), Position(5.0, 2.60), b_up, b_dn, _spot(vel_bps=-0.8),
            tau_s=120.0, params=v32_add_params,
            p_fair=0.40, entries_count=2,
        )
        self.assertEqual(dec_re.action, "taker_buy")
        self.assertEqual(dec_re.side, "Down")

    def test_reversal_flip_unlocked_pair_cost_and_never_quotes_passive_maker(self):
        """Flip fires Taker IOC even if pair_cost > $1.00 when opp has standalone EV, and never hangs Maker."""
        pos_up = Position(shares=5.0, cash=2.75)  # @ 0.55
        pos_dn = Position(0.0, 0.0)
        b_up = _book(0.44, 0.46)
        b_dn = _book(0.54, 0.56)  # pair_cost = 0.55 + 0.56 + fee = 1.127 > 0.98
        dec_flip = plan_decision(
            pos_up, pos_dn, b_up, b_dn, _spot(vel_bps=-1.0),
            tau_s=120.0, params=dict(config.PARAMS, ENABLE_REVERSAL_FLIP=True, SECONDS_SINCE_LAST_ENTRY=10.0, FLIPS_USED=0),
            p_fair=0.36, entries_count=1,
        )
        self.assertEqual(dec_flip.action, "taker_buy")
        self.assertEqual(dec_flip.side, "Down")

        # When Taker edge isn't met (ask=0.61 -> neg/tiny edge), Flip must NOT hang passive Maker bid!
        b_dn_wide = _book(0.52, 0.61)
        b_up_comp = _book(0.39, 0.48)
        dec_no_mk = plan_decision(
            pos_up, pos_dn, b_up_comp, b_dn_wide, _spot(vel_bps=-0.2),
            tau_s=120.0, params=dict(config.PARAMS, ENABLE_REVERSAL_FLIP=True, SECONDS_SINCE_LAST_ENTRY=10.0, FLIPS_USED=0),
            p_fair=0.38, entries_count=1,
        )
        self.assertEqual(dec_no_mk.action, "wait")

    def test_runner_tracks_flips_count_and_recovers_from_live_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            t0 = 1800000000.0
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 60000.0)
            runner.pos_up = Position(5.0, 2.50)
            runner._record_fill(t0 + 20.0, "Up", 0.50, 5.0, "taker", "entry", reason="leg1")
            self.assertEqual(runner.flips_count, 0)

            runner.pos_down = Position(5.0, 2.65)
            runner._record_fill(t0 + 40.0, "Down", 0.53, 5.0, "taker", "entry", reason="flip1")
            self.assertEqual(runner.flips_count, 1)

            runner._write_live(t0 + 41.0, _spot(), _book(0.46, 0.48), _book(0.52, 0.54))

            runner2 = PaperRunner(ledger_dir=tmp)
            runner2.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 60000.0, resume_from_live=True)
            self.assertEqual(runner2.entries_count, 2)
            self.assertEqual(runner2.flips_count, 1)

            runner2.close_window(close_px=59900.0, official_winner="Down", winner_source="twap")
            import json
            from pathlib import Path
            from web.server import _infer_entry_mode
            rec = json.loads(runner2.summary_file.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(rec["entry_mode"], "flip")
            self.assertEqual(_infer_entry_mode(Path(tmp), rec), "flip")

    def test_orphaned_partial_fill_does_not_deadlock_when_no_working_orders(self):
        p = dict(config.PARAMS)
        p["HAS_WORKING_ORDERS"] = False
        d_resumed = plan_decision(
            Position(2.0, 0.96),
            Position(0.0, 0.0),
            _book(0.49, 0.50),
            _book(0.49, 0.50),
            _spot(px=60120.0, open_px=60000.0),
            tau_s=180.0,
            params=p,
            p_fair=0.68,
            entries_count=1,
        )
        self.assertIn(d_resumed.action, ("taker_buy", "quote"))

        p_working = dict(config.PARAMS)
        p_working["HAS_WORKING_ORDERS"] = True
        d_wait = plan_decision(
            Position(2.0, 0.96),
            Position(0.0, 0.0),
            _book(0.49, 0.50),
            _book(0.49, 0.50),
            _spot(px=60120.0, open_px=60000.0),
            tau_s=180.0,
            params=p_working,
            p_fair=0.68,
            entries_count=1,
        )
        self.assertEqual(d_wait.action, "wait")

    def test_coinbase_heartbeat_keeps_ws_fresh_until_stale_or_disconnected(self):
        from runtime.ws_feeds import BinanceSpotWSClient

        ws = BinanceSpotWSClient(provider="coinbase", idle_reconnect_s=10.0)
        self.assertTrue(ws.apply_message({"type": "ticker", "best_bid": "60000", "best_ask": "60002"}, now=1000.0))
        ws.connected = True
        # Heartbeat arrives during a quiet market; should not emit a price tick,
        # but must keep get_latest() alive as long as heartbeat is fresh.
        self.assertFalse(ws.apply_message({"type": "heartbeat"}, now=1003.5))
        latest = ws.get_latest(max_age_s=2.0, now=1004.0)
        self.assertIsNotNone(latest)
        self.assertAlmostEqual(latest[0], 60001.0)
        self.assertEqual(latest[2], "coinbase_ws")

        # Expired heartbeat or disconnected socket returns None
        self.assertIsNone(ws.get_latest(max_age_s=2.0, now=1007.0))
        ws.connected = False
        self.assertIsNone(ws.get_latest(max_age_s=2.0, now=1004.0))

    def test_market_feed_uses_coinbase_probe_and_open_anchor(self):
        from unittest.mock import MagicMock, patch
        from runtime.market_feed import MarketFeed

        with patch.object(MarketFeed, "_test_connectivity", return_value=None):
            feed = MarketFeed()

        seen_urls = []

        def fake_get(url, **kwargs):
            seen_urls.append(url)
            resp = MagicMock()
            resp.status_code = 200
            if "exchange.coinbase.com" in url and "/candles" in url:
                resp.json.return_value = [[1800000000, 59950.0, 60100.0, 60042.5, 60080.0, 1.2]]
            elif "exchange.coinbase.com" in url and "/ticker" in url:
                resp.json.return_value = {"bid": "60040.0", "ask": "60045.0", "price": "60042.5"}
            elif "clob.polymarket.com/time" in url:
                resp.json.return_value = 1800000000
                resp.headers = {}
            elif "polymarket.com/api/crypto/crypto-price" in url:
                resp.json.return_value = {"openPrice": 60040.0}
            else:
                resp.status_code = 404
                resp.json.return_value = {}
            return resp

        with patch.dict(config.PARAMS, {"SPOT_WS_PROVIDER": "coinbase"}):
            with patch.object(feed.session, "get", side_effect=fake_get):
                feed._test_connectivity()
                self.assertTrue(any("api.exchange.coinbase.com/products/BTC-USD/ticker" in u for u in seen_urls))
                self.assertFalse(any("data-api.binance.vision" in u for u in seen_urls))

                seen_urls.clear()
                poly_open, spot_open = feed.fetch_window_open_price(1800000000.0)
                self.assertAlmostEqual(poly_open, 60040.0)
                self.assertAlmostEqual(spot_open, 60042.5)
                self.assertTrue(any("api.exchange.coinbase.com/products/BTC-USD/candles" in u for u in seen_urls))
                self.assertFalse(any("klines" in u for u in seen_urls))

    def test_validate_params_uses_net_lots_allowing_circuit_breaker_budget(self):
        ok_params = dict(config.PARAMS)
        ok_params.update({
            "ENABLE_CIRCUIT_BREAKER": True,
            "MAX_DAILY_DRAWDOWN_USD": 15.0,
            "MAX_WINDOW_LOSS_USD": 15.0,
        })
        self.assertTrue(config.validate_params(ok_params))

        too_small = dict(ok_params)
        too_small["MAX_WINDOW_LOSS_USD"] = 8.0  # Below LOT_SIZE (5) * MAX_NET_LOTS (2) = $10
        with self.assertRaises(ValueError):
            config.validate_params(too_small)

    def test_coinbase_rest_failover_prefers_kraken_usd_and_resets_velocity_across_ccy(self):
        from unittest.mock import MagicMock, patch
        from runtime.market_feed import MarketFeed

        with patch.object(MarketFeed, "_test_connectivity", return_value=None):
            feed = MarketFeed()
        try:
            seen_urls = []

            def fake_get_kraken_second(url, **kwargs):
                seen_urls.append(url)
                resp = MagicMock()
                if "coinbase.com" in url:
                    resp.status_code = 503
                    return resp
                if "kraken.com" in url:
                    resp.status_code = 200
                    resp.content = b"{}"
                    resp.json.return_value = {"result": {"XXBTZUSD": {"b": ["60010.0"], "a": ["60012.0"]}}}
                    return resp
                resp.status_code = 200
                resp.content = b"{}"
                resp.json.return_value = {"price": "60095.0"}
                return resp

            with patch.dict(config.PARAMS, {"SPOT_WS_PROVIDER": "coinbase"}):
                with patch.object(feed.session, "get", side_effect=fake_get_kraken_second):
                    px, src, _, _ = feed._fetch_live_spot_price()
            self.assertEqual(src, "kraken")
            self.assertAlmostEqual(px, 60011.0)
            self.assertFalse(any("binance" in u for u in seen_urls))

            # Build 2.5s of USD baseline via coinbase, then force 1 USDT sample +$90 higher:
            # velocity_bps across USD -> USDT failover must reset to 0.0 instead of firing a fake 15 bps spike.
            feed._build_spot_state(60000.0, 1000.0, 60000.0, "coinbase_ws", 5.0, 64, binance_open_price=60000.0)
            feed._build_spot_state(60000.0, 1002.2, 60001.0, "coinbase_ws", 5.0, 64, binance_open_price=60000.0)
            sp_usdt = feed._build_spot_state(60000.0, 1002.6, 60090.0, "binance_vision", 10.0, 64, binance_open_price=60000.0)
            self.assertAlmostEqual(sp_usdt.velocity_bps, 0.0)
            self.assertFalse(sp_usdt.is_toxic_shock)
        finally:
            feed.close()

    def test_async_taker_future_settle_nonblocking_and_urgent_cancel_while_in_flight(self):
        from concurrent.futures import Future
        from unittest.mock import patch
        from core.types import Decision, Order

        with tempfile.TemporaryDirectory() as tmp:
            t0 = 1800000000.0
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 60000.0)
            runner._basis_ema = 0.0
            runner._basis_updated_t = t0 + 50.0

            sp50 = SpotState(timestamp=t0 + 50.0, price=60080.0, open_price=60000.0, binance_open_price=60000.0, realized_vol_bps=14.0)
            dec = Decision(action="taker_buy", side="Up", price=0.52, size=5.0, limit_price=0.54, reason="Taker IOC")
            fut = Future()

            with patch.object(runner, "_effective_wire_latency", return_value=0.18), \
                 patch("runtime.runner.plan_decision", return_value=dec):
                runner.step(
                    t0 + 50.0, sp50, _book(0.51, 0.52, t=t0 + 50.0), _book(0.47, 0.49, t=t0 + 50.0),
                    delayed_book_fetcher=lambda _s: fut, async_taker_delay=True,
                )
            self.assertEqual(len(runner.pending_takers), 1)

            # While pending_takers is in flight, any resting working order MUST still get urgently cancelled
            # on sudden spot shock (instead of waiting silently without urgent_cancel).
            resting = Order(id=99, client_order_id="pm_dn_99", side="Down", price=0.45, size=5.0, remaining=5.0, placed_at=t0 + 49.0, live_at=t0 + 49.2, activated=True)
            runner.working_orders.append(resting)
            runner.step(
                t0 + 50.10,
                SpotState(timestamp=t0 + 50.10, price=60020.0, open_price=60000.0, binance_open_price=60000.0, velocity_bps=-4.2, is_toxic_shock=True),
                _book(0.51, 0.52, t=t0 + 50.10), _book(0.47, 0.49, t=t0 + 50.10),
                delayed_book_fetcher=lambda _s: fut, async_taker_delay=True,
            )
            self.assertEqual(len(runner.working_orders), 0)
            self.assertEqual(len(runner.pending_takers), 1)

            # At arrive_at (0.20s): future still pending -> does NOT block, keeps pending_taker
            sp502 = SpotState(timestamp=t0 + 50.20, price=60080.0, open_price=60000.0, binance_open_price=60000.0, realized_vol_bps=14.0)
            runner.step(
                t0 + 50.20, sp502, _book(0.51, 0.52, t=t0 + 50.20), _book(0.47, 0.49, t=t0 + 50.20),
                delayed_book_fetcher=lambda _s: fut, async_taker_delay=True,
            )
            self.assertEqual(len(runner.pending_takers), 1)
            self.assertEqual(runner.pos_up.shares, 0.0)

            # Future finishes -> settles immediately on next tick at post-arrival ask 0.53
            fut.set_result((_book(0.52, 0.53, t=t0 + 50.22), _book(0.46, 0.48, t=t0 + 50.22)))
            sp5022 = SpotState(timestamp=t0 + 50.22, price=60080.0, open_price=60000.0, binance_open_price=60000.0, realized_vol_bps=14.0)
            with patch("runtime.runner.plan_decision", return_value=Decision(action="wait", reason="hold")):
                runner.step(
                    t0 + 50.22, sp5022, _book(0.52, 0.53, t=t0 + 50.22), _book(0.46, 0.48, t=t0 + 50.22),
                    delayed_book_fetcher=lambda _s: fut, async_taker_delay=True,
                )
            self.assertEqual(len(runner.pending_takers), 0)
            self.assertAlmostEqual(runner.pos_up.shares, 5.0)

    def test_resume_from_live_dedups_partial_flip_order_and_guards_stale_or_cross_ccy_basis(self):
        import json, time
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            t0 = 1800000000.0
            now_real = time.time()
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 60000.0)
            runner._basis_ema = 55.0
            runner.pos_up = Position(5.0, 2.50)
            runner._record_fill(t0 + 10.0, "Up", 0.50, 5.0, "taker", "entry", reason="leg1")
            # Simulate 1 Maker flip order (order_id=7) filling in two partial chunks (2.0 + 3.0)
            runner.pos_down = Position(2.0, 1.04)
            runner._record_fill(t0 + 20.0, "Down", 0.52, 2.0, "maker", "entry", order_id=7, reason="flip")
            runner.pos_down = Position(5.0, 2.60)
            runner._record_fill(t0 + 22.0, "Down", 0.52, 3.0, "maker", "entry", order_id=7, reason="flip")
            self.assertEqual(runner.flips_count, 1)
            self.assertEqual(runner.entries_count, 2)

            with patch.dict(config.PARAMS, {"SPOT_WS_PROVIDER": "binance"}):
                runner._write_live(now_real - 5.0, _spot(), _book(0.49, 0.51), _book(0.49, 0.51))

            # Resume under same provider & fresh basis -> flips_count==1 (deduped) and basis_ema restored
            with patch.dict(config.PARAMS, {"SPOT_WS_PROVIDER": "binance"}):
                r_same = PaperRunner(ledger_dir=tmp)
                r_same.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 60000.0, resume_from_live=True)
            self.assertEqual(r_same.flips_count, 1)
            self.assertEqual(r_same.entries_count, 2)
            self.assertAlmostEqual(r_same._basis_ema, 55.0)

            # Switch provider from binance (USDT) -> coinbase (USD) and resume -> old USDT basis must NOT carry over
            with patch.dict(config.PARAMS, {"SPOT_WS_PROVIDER": "coinbase"}):
                r_cross = PaperRunner(ledger_dir=tmp)
                r_cross.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 60000.0, resume_from_live=True)
            self.assertIsNone(r_cross._basis_ema)

            # Stale live.json (> 120s old) -> old basis must NOT carry over even on same provider
            raw = json.loads(runner.live_file.read_text(encoding="utf-8"))
            raw["now"] = now_real - 200.0
            runner.live_file.write_text(json.dumps(raw), encoding="utf-8")
            with patch.dict(config.PARAMS, {"SPOT_WS_PROVIDER": "binance"}):
                r_stale = PaperRunner(ledger_dir=tmp)
                r_stale.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 60000.0, resume_from_live=True)
            self.assertIsNone(r_stale._basis_ema)

    def test_replay_wakes_on_spot_breakout_during_quiet_book(self):
        from sim.replay import replay_window

        with tempfile.TemporaryDirectory() as tmp:
            t0 = 1800000000.0
            runner = PaperRunner(ledger_dir=tmp)
            settlement = {"start": t0, "end": t0 + 300.0, "open_px": 60000.0, "winner": "Up", "close_px": 60050.0}
            # Only 1 book update at t0+30 (quiet book), while spot breaks out at t0+40, t0+45, t0+50
            events_up = [{"t": t0 + 30.0, "best_bid": 0.49, "best_ask": 0.51, "bid_depth": [[0.49, 30.0]], "ask_depth": [[0.51, 30.0]]}]
            events_dn = [{"t": t0 + 30.0, "best_bid": 0.49, "best_ask": 0.51, "bid_depth": [[0.49, 30.0]], "ask_depth": [[0.51, 30.0]]}]
            spot_rows = [
                {"t": t0 + 30.0, "price": 60000.0, "return_bps": 0.0, "realized_vol_bps": 14.0},
                {"t": t0 + 40.0, "price": 60025.0, "return_bps": 4.17, "realized_vol_bps": 14.0},
                {"t": t0 + 50.0, "price": 60050.0, "return_bps": 8.33, "realized_vol_bps": 14.0},
            ]
            res = replay_window(runner, "btc-updown-5m-1800000000", settlement, [], events_up, events_dn, spot_rows, [])
            self.assertTrue(res["replayed"])
            # Would have been only 1 tick before; now spot breakouts (>= $1) wake extra evaluation ticks.
            self.assertGreaterEqual(res["ticks"], 3)


if __name__ == "__main__":
    unittest.main()
