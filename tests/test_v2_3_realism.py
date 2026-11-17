"""v2.3 Realism & Zero-Self-Deception Regression Tests.

Pins the core engineering fixes made in v2.3:
1. Binance 451 failover + stale spot timestamp preservation tripping dead-man switch.
2. Basis-adjusted live spot + Asian 60s TWAP variance in estimate_p_fair (never using lagging TWAP as spot).
3. Strict dual-book consistency (no artificial price shifting when STRICT_BOOK_CONSISTENCY=True).
4. Realistic post-latency Taker IOC check preventing fills when book moves during wire transit.
"""
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.types import OrderBook, Position, SpotState, Decision
from core.estimator import compute_objective_lead, calculate_dual_horizon_vol
from sim.matcher import is_books_consistent, QueueMatcher
from runtime.runner import PaperRunner
from runtime.market_feed import MarketFeed
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import threading


class TestV23Realism(unittest.TestCase):
    def test_stale_spot_failover_preserves_old_timestamp_and_trips_deadman(self):
        feed = MarketFeed.__new__(MarketFeed)
        feed.session = MagicMock()
        feed.spot_history = []
        feed._last_binance_px = 64000.0
        feed._last_binance_t = 1000.0
        feed._last_spot_source = "binance_vision"
        feed.net_stats = {"last_calc_t": 1000.0, "bytes_window": 0, "msgs_window": 0, "total_bytes": 0}
        feed.record_raw_event = MagicMock()

        # Simulate all spot endpoints failing (e.g. HTTP 451 / network drop)
        bad_resp = MagicMock(status_code=451, content=b"restricted")
        feed.session.get.return_value = bad_resp

        spot = feed.fetch_spot_state(open_price=64000.0, now=1010.0)
        self.assertTrue(spot.is_stale)
        self.assertEqual(spot.timestamp, 1000.0)

        with tempfile.TemporaryDirectory() as tmp:
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-900", 900.0, 1200.0, 64000.0, binance_open_px=64000.0)
            b_up = OrderBook(bids=[(0.48, 20.0)], asks=[(0.50, 20.0)], source_t=1010.0)
            b_dn = OrderBook(bids=[(0.48, 20.0)], asks=[(0.50, 20.0)], source_t=1010.0)
            runner.step(1010.0, spot, b_up, b_dn)
            self.assertIn("Dead-man switch active", runner.decisions[-1]["reason"])
            self.assertEqual(len(runner.fills), 0)

    def test_objective_lead_never_uses_lagging_twap_as_spot(self):
        sp = SpotState(
            timestamp=1200.0, price=73920.0, open_price=73800.0,
            poly_price=73740.0, binance_open_price=73900.0, basis_ema=20.0
        )
        lead_bps, delta_usd, eff_px = compute_objective_lead(sp, open_px=73800.0, tau_s=90.0)
        self.assertGreater(lead_bps, 10.0)
        self.assertAlmostEqual(delta_usd, 100.0, places=1)
        self.assertAlmostEqual(eff_px, 73900.0, places=1)

    def test_asian_twap_last_60s_locked_average_dominates(self):
        sp = SpotState(
            timestamp=1280.0, price=74990.0, open_price=75000.0,
            basis_ema=0.0, twap_realized_60s=75100.0
        )
        lead_bps, delta_usd, eff_px = compute_objective_lead(sp, open_px=75000.0, tau_s=20.0)
        self.assertGreater(lead_bps, 8.0)
        self.assertGreater(delta_usd, 60.0)
        self.assertAlmostEqual(eff_px, 75063.33, places=1)

    def test_strict_book_consistency_never_shifts_prices(self):
        b_up = OrderBook(bids=[(0.60, 10.0)], asks=[(0.40, 10.0)])
        b_dn = OrderBook(bids=[(0.50, 10.0)], asks=[(0.50, 10.0)])
        self.assertFalse(is_books_consistent(b_up, b_dn))

    def test_delayed_taker_ioc_misses_when_ask_moves_above_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-1000", 1000.0, 1300.0, 75000.0, binance_open_px=75000.0)
            b_up = OrderBook(bids=[(0.40, 20.0)], asks=[(0.42, 20.0)], source_t=1200.0)
            b_dn = OrderBook(bids=[(0.56, 20.0)], asks=[(0.58, 20.0)], source_t=1200.0)
            sp = SpotState(timestamp=1200.0, price=75100.0, open_price=75000.0, binance_open_price=75000.0, basis_ema=0.0)

            # During 300ms wire transit, market makers reprice Up ask from 0.42 to 0.55
            delayed_up = OrderBook(bids=[(0.52, 20.0)], asks=[(0.55, 20.0)], source_t=1200.3)
            dec = Decision(action="taker_buy", side="Up", price=0.42, size=5.0, utility=0.20, reason="test")
            with patch("runtime.runner.plan_decision", return_value=dec), \
                 patch("runtime.runner.time.sleep"):
                runner.step(1200.0, sp, b_up, b_dn, delayed_book_fetcher=lambda side: delayed_up)

            self.assertEqual(runner.pos_up.shares, 0.0)
            rejects = [a for a in runner.actions if a.get("action") == "taker_reject"]
            self.assertTrue(rejects)
            self.assertIn("IOC 错失防滑点", rejects[-1]["reason"])

    def test_dual_horizon_vol_preserves_macro_vol_during_brief_lull(self):
        # 20 minutes of volatile moves followed by 60s calm pause
        hist = []
        px = 75000.0
        for s in range(0, 1200, 2):
            px += (25.0 if (s // 2) % 2 == 0 else -24.5)
            hist.append((float(s), px))
        for s in range(1200, 1260):
            hist.append((float(s), px))
        vol = calculate_dual_horizon_vol(hist, baseline_vol_bps=14.0)
        self.assertGreater(vol, 25.0)

    def test_concurrent_fetch_tick_bundle_uses_thread_local_sessions(self):
        feed = MarketFeed.__new__(MarketFeed)
        feed.session = MagicMock()
        feed.session.headers = {"User-Agent": "test"}
        feed.session.proxies = {}
        feed.spot_history = deque(maxlen=1800)
        feed._last_binance_px = 0.0
        feed._last_binance_t = 0.0
        feed._last_spot_source = "binance_vision"
        feed._thread_local = threading.local()
        feed._executor = ThreadPoolExecutor(max_workers=3)
        feed.net_stats = {"last_calc_t": 1000.0, "bytes_window": 0, "msgs_window": 0, "total_bytes": 0, "poly_latency_ms": 0.0, "binance_latency_ms": 0.0}
        feed.record_raw_event = MagicMock()

        def _fake_spot(sess=None):
            return 64200.0, "binance_vision", 12.0, 40

        def _fake_book(tok, sess=None):
            b = OrderBook(bids=[(0.48, 10.0)], asks=[(0.50, 10.0)], source_t=1000.0, received_at=1000.0)
            return b, 15.0, 120, {"t": 1000.0, "token_id": tok}

        feed._fetch_live_spot_price = _fake_spot
        feed._fetch_order_book_raw = _fake_book
        try:
            sp, bu, bd = feed.fetch_tick_bundle(64100.0, 64150.0, "tok_u", "tok_d", now=1000.0)
            self.assertEqual(sp.price, 64200.0)
            self.assertFalse(sp.is_stale)
            self.assertEqual(bu.best_bid, 0.48)
            self.assertEqual(bd.best_ask, 0.50)
            self.assertEqual(len(feed.spot_history), 1)
        finally:
            feed.close()

    def test_provisional_open_price_does_not_apply_cross_source_basis_ema(self):
        """When poly_open_is_provisional=True, runner must not subtract basis_ema from Binance spot against Binance open."""
        with tempfile.TemporaryDirectory() as tmp:
            runner = PaperRunner(ledger_dir=tmp)
            # Simulate previous window having built a +$60 Binance-Chainlink basis_ema
            runner._basis_ema = 60.0
            runner._basis_updated_t = 999.0
            # New window starts with provisional Binance open (64500 == 64500)
            runner.start_window("btc-updown-5m-1000", 1000.0, 1300.0, open_px=64500.0, binance_open_px=64500.0)
            self.assertTrue(runner.poly_open_is_provisional)

            b_up = OrderBook(bids=[(0.49, 20.0)], asks=[(0.51, 20.0)], source_t=1001.0)
            b_dn = OrderBook(bids=[(0.49, 20.0)], asks=[(0.51, 20.0)], source_t=1001.0)
            sp = SpotState(timestamp=1001.0, price=64500.0, open_price=64500.0, binance_open_price=64500.0)
            runner.step(1001.0, sp, b_up, b_dn, poly_spot=64440.0)
            # Must stay on Binance chain without subtracting $60 basis, and no prob_up field stored
            self.assertIsNone(sp.basis_ema)
            self.assertEqual(sp.open_price, 64500.0)
            self.assertNotIn("prob_up", runner.price_history[-1])

    def test_resting_maker_quote_cancelled_when_direction_reverses_or_bid_retreats(self):
        """Resting Maker order is cancelled when objective preferred_side flips or bid retreats >= 2 ticks."""
        from core.execution import plan_order_actions
        from core.types import Order
        order = Order(
            id=1, client_order_id="pm_1", side="Up", price=0.48,
            size=5.0, remaining=5.0, placed_at=1000.0, live_at=1000.3
        )
        wait_dec = Decision(action="wait", reason="Waiting", urgent_cancel=False)
        b_up = OrderBook(bids=[(0.47, 20.0)], asks=[(0.49, 20.0)])
        b_dn = OrderBook(bids=[(0.51, 20.0)], asks=[(0.53, 20.0)])

        # 1. Preferred side still Up and bid within 1 tick: keep order
        cancels_ok, _ = plan_order_actions([order], wait_dec, now=1002.0, preferred_side="Up", book_up=b_up, book_down=b_dn)
        self.assertEqual(len(cancels_ok), 0)

        # 2. Objective direction reverses to Down: cancel Up order immediately!
        cancels_flip, _ = plan_order_actions([order], wait_dec, now=1003.0, preferred_side="Down", book_up=b_up, book_down=b_dn)
        self.assertEqual(len(cancels_flip), 1)
        self.assertIn("客观方向反转撤单", cancels_flip[0].cancel_reason)

        # 3. Market best_bid retreats 2 ticks to 0.46 while order sits at 0.48: kept under 3-tick tolerance;
        # retreats 3 ticks to 0.45: cancel immediately!
        order2 = Order(
            id=2, client_order_id="pm_2", side="Up", price=0.48,
            size=5.0, remaining=5.0, placed_at=1000.0, live_at=1000.3
        )
        b_up_2ticks = OrderBook(bids=[(0.46, 20.0)], asks=[(0.49, 20.0)])
        cancels_2t, _ = plan_order_actions([order2], wait_dec, now=1003.0, preferred_side="Up", book_up=b_up_2ticks, book_down=b_dn)
        self.assertEqual(len(cancels_2t), 0)

        b_up_retreat = OrderBook(bids=[(0.45, 20.0)], asks=[(0.49, 20.0)])
        cancels_bid, _ = plan_order_actions([order2], wait_dec, now=1003.0, preferred_side="Up", book_up=b_up_retreat, book_down=b_dn)
        self.assertEqual(len(cancels_bid), 1)
        self.assertIn("盘口买一退守撤单", cancels_bid[0].cancel_reason)

        # 4. Anti-ping-pong cooldown: 0.44 -> 0.42 within 2.5s reuses resting order; after 2.5s requotes.
        order3 = Order(
            id=3, client_order_id="pm_3", side="Up", price=0.44,
            size=5.0, remaining=5.0, placed_at=1000.0, live_at=1000.3
        )
        q_dec = Decision(action="quote", side="Up", price=0.42, size=5.0, reason="reprice")
        c_cool, a_cool = plan_order_actions([order3], q_dec, now=1000.8, min_requote_age_s=2.5)
        self.assertEqual(len(c_cool), 0)
        self.assertIsNone(a_cool)
        c_warm, a_warm = plan_order_actions([order3], q_dec, now=1003.0, min_requote_age_s=2.5)
        self.assertEqual(len(c_warm), 1)
        self.assertIsNotNone(a_warm)

    def test_partial_fill_preserves_remaining_quote_unless_toxic(self):
        """A partially filled 2/5 share position does not self-cancel its remaining 3 shares unless toxic."""
        from core.policy import plan_decision
        b_up = OrderBook(bids=[(0.48, 20.0)], asks=[(0.50, 20.0)])
        b_dn = OrderBook(bids=[(0.50, 20.0)], asks=[(0.52, 20.0)])
        sp_calm = SpotState(timestamp=1100.0, price=75050.0, open_price=75000.0, velocity_bps=0.5)
        dec_partial = plan_decision(Position(2.0, 0.96), Position(0.0, 0.0), b_up, b_dn, sp_calm, 180.0, config.PARAMS)
        self.assertEqual(dec_partial.action, "wait")
        self.assertFalse(dec_partial.urgent_cancel)

        # Full 5-share lot sets urgent_cancel=True
        dec_full = plan_decision(Position(5.0, 2.40), Position(0.0, 0.0), b_up, b_dn, sp_calm, 180.0, config.PARAMS)
        self.assertTrue(dec_full.urgent_cancel)

        # Partial fill during toxic velocity shock sets urgent_cancel=True
        sp_toxic = SpotState(timestamp=1100.0, price=75050.0, open_price=75000.0, velocity_bps=4.0, is_toxic_shock=True)
        dec_toxic = plan_decision(Position(2.0, 0.96), Position(0.0, 0.0), b_up, b_dn, sp_toxic, 180.0, config.PARAMS)
        self.assertTrue(dec_toxic.urgent_cancel)

    def test_wide_spread_blocks_maker_quote_and_zero_noise_filtered(self):
        """Maker quote is skipped when book spread > MAX_ORDERBOOK_SPREAD, and zero-noise chop is filtered."""
        from core.policy import plan_decision
        sp = SpotState(timestamp=1100.0, price=75050.0, open_price=75000.0)
        # 1. Wide spread (0.36 / 0.46 -> spread = 0.10 > 0.06): do not quote Maker into vacuum
        b_up = OrderBook(bids=[(0.54, 20.0)], asks=[(0.64, 20.0)])
        b_dn_wide = OrderBook(bids=[(0.36, 20.0)], asks=[(0.46, 20.0)])
        dec_wide = plan_decision(Position(0, 0), Position(0, 0), b_up, b_dn_wide, sp, 180.0, config.PARAMS)
        self.assertEqual(dec_wide.action, "wait")

        # 2. Zero-noise chop (+0.5 bps < 1.5 bps) in MWDE is filtered out
        sp_chop = SpotState(timestamp=1100.0, price=75003.0, open_price=75000.0)
        b_up_tight = OrderBook(bids=[(0.42, 20.0)], asks=[(0.44, 20.0)])
        b_dn_tight = OrderBook(bids=[(0.56, 20.0)], asks=[(0.58, 20.0)])
        dec_chop = plan_decision(Position(0, 0), Position(0, 0), b_up_tight, b_dn_tight, sp_chop, 180.0, config.PARAMS)
        self.assertEqual(dec_chop.action, "wait")
        self.assertIn("过滤零附近震荡噪音", dec_chop.reason)

    def test_explicit_poly_open_provisional_and_gamma_gz_preserved(self):
        """Explicit poly_open_provisional stays True even if stitched open != binance_open, and gamma gz isn't overwritten by twap."""
        import gzip
        import json
        with tempfile.TemporaryDirectory() as tmp:
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window(
                "btc-updown-5m-1000", 1000.0, 1300.0,
                open_px=64960.0, binance_open_px=65000.0, poly_open_provisional=True
            )
            self.assertTrue(runner.poly_open_is_provisional)
            runner.update_open_price(65020.0)
            self.assertFalse(runner.poly_open_is_provisional)

            runner.pos_down = Position(5.0, 2.0)
            runner.close_window(close_px=65010.0, official_winner="Down", winner_source="gamma")
            # Reconcile later with conflicting TWAP -> gamma winner and winner_source must remain in BOTH summary and gz
            runner.reconcile_record("btc-updown-5m-1000", open_px=65000.0, close_px=65050.0, winner="Up", winner_source="twap")

            summary_rows = [json.loads(l) for l in (Path(tmp) / "summary.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(summary_rows[0]["winner"], "Down")
            self.assertEqual(summary_rows[0]["winner_source"], "gamma")

            with gzip.open(Path(tmp) / "windows" / "btc-updown-5m-1000.json.gz", "rt", encoding="utf-8") as fh:
                detail = json.load(fh)
            self.assertEqual(detail["winner"], "Down")
            self.assertEqual(detail["winner_source"], "gamma")

    def test_min_opp_ask_seen_vwap_immune_to_thin_book_spike(self):
        """A 1-share stale low ask must not pollute min_opp_ask_seen for a 5-share hedge lot."""
        with tempfile.TemporaryDirectory() as tmp:
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-1000", 1000.0, 1300.0, open_px=75000.0, binance_open_px=75000.0)
            # Basis $0.80 keeps Deep Arbitrage inactive so we isolate min_opp_ask_seen VWAP tracking
            runner.pos_up = Position(5.0, 4.00)

            b_up = OrderBook(bids=[(0.50, 20.0)], asks=[(0.52, 20.0)], source_t=1150.0)
            # Insufficient total ask depth (1 share < 5 shares) -> ignored
            b_dn_insufficient = OrderBook(bids=[(0.46, 20.0)], asks=[(0.48, 1.0)], source_t=1150.0)
            sp = SpotState(timestamp=1150.0, price=75030.0, open_price=75000.0, binance_open_price=75000.0)
            runner.step(1150.0, sp, b_up, b_dn_insufficient)
            self.assertIsNone(runner.min_opp_ask_seen)

            # 1-share spike @ 0.48 + 10 shares @ 0.60 -> 5-share VWAP is (0.48 + 4*0.60)/5 = 0.576
            b_dn_spike = OrderBook(bids=[(0.46, 20.0)], asks=[(0.48, 1.0), (0.60, 10.0)], source_t=1151.0)
            sp2 = SpotState(timestamp=1151.0, price=75030.0, open_price=75000.0, binance_open_price=75000.0)
            runner.step(1151.0, sp2, b_up, b_dn_spike)
            self.assertAlmostEqual(runner.min_opp_ask_seen, 0.576, places=4)

            # Genuine deep level @ 0.54 updates min_opp_ask_seen
            b_up3 = OrderBook(bids=[(0.44, 20.0)], asks=[(0.46, 20.0)], source_t=1152.0)
            b_dn_deep = OrderBook(bids=[(0.52, 20.0)], asks=[(0.54, 10.0)], source_t=1152.0)
            sp3 = SpotState(timestamp=1152.0, price=75030.0, open_price=75000.0, binance_open_price=75000.0)
            runner.step(1152.0, sp3, b_up3, b_dn_deep)
            self.assertAlmostEqual(runner.min_opp_ask_seen, 0.54, places=4)

    def test_queue_matcher_handles_merge_float_print_slack(self):
        """Polymarket CLOB float rounding prints like 0.120000014 still fill a $0.12 resting bid."""
        from core.types import Order
        matcher = QueueMatcher(latency_s=0.3)
        order = Order(
            id=1, client_order_id="pm_float", side="Up", price=0.12,
            size=5.0, remaining=5.0, placed_at=100.0, live_at=100.3
        )
        matcher.register_order(order, OrderBook(bids=[], asks=[]))
        filled = matcher.on_trade_print(order, trade_price=0.120000014, trade_size=5.0, now=101.0)
        self.assertAlmostEqual(filled, 5.0)

    def test_pipeline_imports_json_for_gamma_reconcile_and_feed_record_lock(self):
        """Ensure runtime.pipeline can parse stringified Gamma outcomes and MarketFeed serializes raw JSONL writes."""
        import json
        import runtime.pipeline as pl
        self.assertTrue(hasattr(pl, "json"))
        parsed = pl.json.loads('["Up", "Down"]')
        self.assertEqual(parsed, ["Up", "Down"])

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(config, "MARKET_DIR", Path(tmp)):
                feed = MarketFeed.__new__(MarketFeed)
                import threading
                feed._record_lock = threading.Lock()
                for i in range(10):
                    feed.record_raw_event("oracle", {"t": float(i), "kind": "spot", "price": 65000.0 + i})
                today_files = list((Path(tmp) / "oracle").rglob("*.jsonl"))
                self.assertEqual(len(today_files), 1)
                rows = [json.loads(line) for line in today_files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
                self.assertEqual(len(rows), 10)


if __name__ == "__main__":
    unittest.main()
