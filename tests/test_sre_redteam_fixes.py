"""Regression tests for the SRE & Red-Team production hardening pass."""
import base64
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

import config
from core.execution import plan_order_actions
from core.policy import plan_decision
from core.types import Decision, Order, OrderBook, Position, SpotState
from runtime.alerts import notify as send_alert
from runtime.retention import cleanup_old_data, is_replay_archive_allowed
from runtime.runner import PaperRunner
from runtime.ws_feeds import PolymarketBookWSClient, _safe_float, _ws_reconnect_backoff
from sim.matcher import QueueMatcher
from web.server import (
    QuantHandler,
    _AUTH_LIMITER,
    _AuthRateLimiter,
    _HardenedHTTPServer,
    _downsample_curve,
    _is_safe_segment,
    _lookup_summary_record,
    _validate_bind_security,
)


def _book(bid, ask, sz=50.0, t=1200.0):
    return OrderBook(bids=[(bid, sz)], asks=[(ask, sz)], source_t=t, received_at=t)


class TestPolicyAndMatcherHardening(unittest.TestCase):
    def test_negative_ev_maker_quote_and_heavy_ask_wall_cancel(self):
        # 1. Heavy ask wall must set urgent_cancel=True so already-resting bids are pulled
        b_up_wall = OrderBook(bids=[(0.48, 20.0)], asks=[(0.50, 300.0)], source_t=1200.0, received_at=1200.0)
        b_dn = _book(0.49, 0.51)
        spot = SpotState(timestamp=1200.0, price=65030.0, open_price=65000.0, realized_vol_bps=16.0)
        dec = plan_decision(Position(0, 0), Position(0, 0), b_up_wall, b_dn, spot, tau_s=120.0, params=config.PARAMS, p_fair=0.56)
        self.assertEqual(dec.action, "wait")
        self.assertTrue(dec.urgent_cancel, "Heavy ask wall must urgently pull existing resting maker bids")

        # 2. Resting maker order whose live edge decayed below min_hold_maker_edge (even negative EV) must cancel
        resting = Order(
            id=10, client_order_id="pm_10", side="Up", price=0.55,
            size=5.0, remaining=5.0, purpose="entry", placed_at=1200.0, live_at=1200.3,
        )
        wait_dec = Decision(action="wait", reason="no new edge", urgent_cancel=False)
        to_cancel, _ = plan_order_actions(
            [resting], wait_dec, now=1202.0,
            preferred_side="Up", book_up=_book(0.53, 0.56), book_down=_book(0.44, 0.47),
            p_up=0.525, min_hold_maker_edge=0.015,
        )
        self.assertEqual(len(to_cancel), 1)
        self.assertIn("边际衰减撤单", to_cancel[0].cancel_reason)

    def test_circuit_breaker_is_true_reduce_only(self):
        spot = SpotState(timestamp=1200.0, price=65200.0, open_price=65000.0, realized_vol_bps=18.0)
        params = dict(config.PARAMS, ENABLE_CIRCUIT_BREAKER=True)

        # Flat account -> blocked from new Leg-1
        dec_flat = plan_decision(
            Position(0, 0), Position(0, 0), _book(0.54, 0.56), _book(0.44, 0.46),
            spot, tau_s=120.0, params=params, circuit_breaker_active=True,
            circuit_breaker_reason="Daily limit", p_fair=0.72,
        )
        self.assertEqual(dec_flat.action, "wait")
        self.assertIn("Circuit breaker", dec_flat.reason)

        # Existing winning position -> still allowed to execute explicit Black-Swan Lock under breaker
        dec_lock = plan_decision(
            Position(5.0, 2.80), Position(0, 0), _book(0.94, 0.96), _book(0.04, 0.05),
            spot, tau_s=120.0, params=dict(params, ENABLE_BLACKSWAN_LOCK=True), circuit_breaker_active=True,
            circuit_breaker_reason="Daily limit", p_fair=0.92, entries_count=1,
        )
        self.assertEqual(dec_lock.action, "taker_hedge")

        # Existing winning position -> blocked from pyramid add under breaker
        dec_pyr = plan_decision(
            Position(5.0, 2.80), Position(0, 0), _book(0.55, 0.57), _book(0.43, 0.45),
            spot, tau_s=80.0, params=dict(params, SECONDS_SINCE_LAST_ENTRY=30.0),
            circuit_breaker_active=True, circuit_breaker_reason="Daily limit",
            p_fair=0.75, entries_count=1,
        )
        self.assertEqual(dec_pyr.action, "wait")
        self.assertIn("reduce-only", dec_pyr.reason)

    def test_blackswan_lock_checks_book_consistency_and_free_capital(self):
        spot = SpotState(timestamp=1200.0, price=65200.0, open_price=65000.0, realized_vol_bps=18.0)
        # Inconsistent books: ask sum = 0.70 + 0.05 = 0.75 < 0.995 -> must NOT lock
        dec_bad_book = plan_decision(
            Position(5.0, 2.80), Position(0, 0), _book(0.68, 0.70), _book(0.04, 0.05),
            spot, tau_s=120.0, params=config.PARAMS, p_fair=0.92, entries_count=1,
        )
        self.assertNotEqual(dec_bad_book.action, "taker_hedge")

        # Insufficient remaining capital ($0.10 < 5 * $0.05 = $0.25) -> must NOT lock
        dec_no_cash = plan_decision(
            Position(5.0, 2.80), Position(0, 0), _book(0.94, 0.96), _book(0.04, 0.05),
            spot, tau_s=120.0, params=dict(config.PARAMS, AVAILABLE_CAPITAL=0.10),
            p_fair=0.92, entries_count=1,
        )
        self.assertNotEqual(dec_no_cash.action, "taker_hedge")

    def test_matcher_ignores_aggressive_buy_print_at_exact_bid(self):
        m = QueueMatcher(latency_s=0.3)
        o = Order(id=1, client_order_id="o1", side="Up", price=0.50, size=5.0, remaining=5.0, placed_at=100.0, live_at=100.3)
        m.register_order(o, _book(0.49, 0.51))
        # Aggressive BUY at exact bid 0.50 lifted an ask -> should not fill our resting bid
        filled_buy = m.on_trade_print(o, 0.50, 10.0, 101.0, trade_side="BUY")
        self.assertEqual(filled_buy, 0.0)
        # Aggressive SELL at 0.50 hits our resting bid -> fills 5 shares
        filled_sell = m.on_trade_print(o, 0.50, 10.0, 101.1, trade_side="SELL")
        self.assertAlmostEqual(filled_sell, 5.0, places=4)

    def test_in_transit_cancel_crossed_book_check_no_longer_dead_code(self):
        tmp = Path(tempfile.mkdtemp(prefix="pm_intransit_"))
        try:
            t0 = 1800000000.0
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0, binance_open_px=65000.0)
            b_up = _book(0.54, 0.56, t=t0 + 120.0)
            b_dn = _book(0.44, 0.46, t=t0 + 120.0)
            spot = SpotState(timestamp=t0 + 120.0, price=65030.0, open_price=65000.0, realized_vol_bps=14.0)

            with patch("runtime.runner.plan_decision", return_value=Decision(action="quote", side="Up", price=0.55, size=5.0, reason="q")):
                runner.step(t0 + 120.0, spot, b_up, b_dn, replay_mode=True)
            self.assertEqual(len(runner.working_orders), 1)

            # Next tick flips to taker_buy (cancels resting @0.55 with 0.30s cancel latency).
            # 300ms delayed post_book shows ask crossed down to 0.55 -> resting cancel must fill first & block dup taker.
            crossed_up = _book(0.53, 0.55, t=t0 + 121.3)
            with patch("runtime.runner.plan_decision", return_value=Decision(action="taker_buy", side="Up", price=0.56, size=5.0, reason="tk")):
                runner.step(
                    t0 + 121.0, spot, b_up, b_dn, replay_mode=True,
                    delayed_book_fetcher=lambda _s: crossed_up,
                    delayed_prints_fetcher=lambda: [],
                )
            self.assertAlmostEqual(runner.pos_up.shares, 5.0, places=4)
            self.assertTrue(any("取消重复Taker追单" in (a.get("reason") or "") for a in runner.actions))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestRuntimeAndIntegrityHardening(unittest.TestCase):
    def test_ws_book_invalidate_prevents_stale_resurrection_by_pong(self):
        ws = PolymarketBookWSClient(idle_reconnect_s=20.0)
        tok = "tok-zombie"
        ws.apply_message([{
            "event_type": "book", "asset_id": tok,
            "bids": [{"price": "0.49", "size": "50"}],
            "asks": [{"price": "0.51", "size": "50"}],
        }], now=1000.0)
        ws.connected = True
        self.assertIsNotNone(ws.get_order_book(tok, now=1001.0))

        # Simulate disconnect + reconnect where new session receives pong BEFORE fresh book snapshot
        ws._invalidate_all_books()
        ws.connected = True
        ws._last_pong_t = 1015.0
        self.assertIsNone(ws.get_order_book(tok, now=1015.1), "Pre-disconnect book must never be resurrected by pong")

        # Once fresh snapshot arrives on new session, book becomes readable again
        ws.apply_message([{
            "event_type": "book", "asset_id": tok,
            "bids": [{"price": "0.45", "size": "50"}],
            "asks": [{"price": "0.47", "size": "50"}],
        }], now=1015.5)
        fresh = ws.get_order_book(tok, now=1015.6)
        self.assertIsNotNone(fresh)
        self.assertAlmostEqual(fresh.best_bid, 0.45)

    def test_ws_safe_float_rejects_nan_and_inf_and_backoff_slows_on_403(self):
        self.assertEqual(_safe_float("NaN", -1.0), -1.0)
        self.assertEqual(_safe_float("Infinity", -1.0), -1.0)
        self.assertEqual(_safe_float("-inf", -1.0), -1.0)
        self.assertAlmostEqual(_safe_float("0.52", -1.0), 0.52)

        b_norm = _ws_reconnect_backoff(1, "connection reset", base_s=1.5, cap_s=30.0)
        b_403 = _ws_reconnect_backoff(1, "HTTP 403 Forbidden", base_s=1.5, cap_s=30.0)
        self.assertLess(b_norm, 3.0)
        self.assertGreaterEqual(b_403, 10.0)

    def test_settle_orphaned_live_recovers_cross_boundary_positions(self):
        tmp = Path(tempfile.mkdtemp(prefix="pm_orphan_"))
        try:
            old_slug = "btc-updown-5m-1800000000"
            new_slug = "btc-updown-5m-1800000300"
            live_payload = {
                "slug": old_slug,
                "start": 1800000000.0,
                "end": 1800000300.0,
                "open_px": 65000.0,
                "binance_open_px": 65000.0,
                "spot": 65080.0,
                "poly_spot": 65070.0,
                "pos_up": {"shares": 5.0, "cash": 2.60, "avg_px": 0.52},
                "pos_down": {"shares": 0.0, "cash": 0.0, "avg_px": 0.0},
                "actions": [{"t": 1800000100.0, "action": "fill", "side": "Up", "price": 0.52, "size": 5.0, "purpose": "entry"}],
            }
            (tmp / "live.json").write_text(json.dumps(live_payload), encoding="utf-8")

            runner = PaperRunner(ledger_dir=tmp)
            meta = runner.settle_orphaned_live(new_slug)
            self.assertIsNotNone(meta)
            self.assertEqual(meta["slug"], old_slug)
            self.assertIn(old_slug, runner._settled_slugs)
            self.assertAlmostEqual(runner.total_pnl, 2.40, places=4)
            lines = [ln for ln in (tmp / "summary.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["slug"], old_slug)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_summary_append_recovers_from_unterminated_corrupted_tail(self):
        tmp = Path(tempfile.mkdtemp(prefix="pm_tail_nl_"))
        try:
            summary = tmp / "summary.jsonl"
            summary.write_text('{"slug":"half_truncated_no_newline', encoding="utf-8")

            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-1800000300", 1800000300.0, 1800000600.0, 65000.0)
            runner.pos_up = Position(5.0, 2.50)
            runner.close_window(65050.0, "Up", winner_source="twap")

            # Reloading must parse the newly appended valid line cleanly (not glued onto truncated tail)
            r2 = PaperRunner(ledger_dir=tmp)
            self.assertIn("btc-updown-5m-1800000300", r2._settled_slugs)
            self.assertAlmostEqual(r2.total_pnl, 2.50, places=4)

            # Reconcile cleans out the corrupted line permanently
            r2.reconcile_record("btc-updown-5m-1800000300", 65000.0, 65060.0, "Up", winner_source="gamma")
            lines = [ln for ln in summary.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["winner_source"], "gamma")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_twap_realized_60s_rejects_low_coverage_after_outage(self):
        tmp = Path(tempfile.mkdtemp(prefix="pm_outage_twap_"))
        try:
            t0 = 1800000000.0
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0, binance_open_px=65000.0, poly_open_provisional=False)
            runner._basis_ema = 0.0
            runner._basis_updated_t = t0 + 280.0
            # tau=20s (40s elapsed in final 60s), but only 5s of data exist after a 35s outage
            runner._spot_buf = [(t0 + 275.0 + i, 65300.0) for i in range(6)]
            spot = SpotState(timestamp=t0 + 280.0, price=65300.0, open_price=65000.0, realized_vol_bps=14.0)
            runner.step(t0 + 280.0, spot, _book(0.55, 0.56), _book(0.44, 0.45), poly_spot=65100.0, replay_mode=True)
            self.assertIsNone(spot.twap_realized_60s, "5s sample must not extrapolate across 40s elapsed TWAP")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_retention_same_day_runaway_pauses_recording_and_cleans_stale_tmp(self):
        Usage = namedtuple("Usage", ["total", "used", "free"])
        healthy_disk = lambda _: Usage(total=100 * (1024 ** 3), used=30 * (1024 ** 3), free=70 * (1024 ** 3))
        tmp = Path(tempfile.mkdtemp(prefix="pm_runaway_"))
        try:
            now_ts = time.time()
            today_str = time.strftime("%Y-%m-%d", time.gmtime(now_ts))
            m_dir = tmp / "1_market"
            l_dir = tmp / "2_ledgers"
            g_dir = tmp / "4_runtime" / "logs"
            (m_dir / "events" / today_str).mkdir(parents=True)
            (m_dir / "events" / today_str / "huge.jsonl").write_bytes(b"x" * (2 * 1024 * 1024))
            (l_dir / "v2.8").mkdir(parents=True)
            stale_tmp = l_dir / "v2.8" / "summary.jsonl.tmp"
            stale_tmp.write_text("leftover", encoding="utf-8")
            os.utime(stale_tmp, (now_ts - 1200, now_ts - 1200))
            g_dir.mkdir(parents=True)

            with patch.dict(config.PARAMS, {"MAX_TODAY_MARKET_MB": 1.0}):
                res = cleanup_old_data(
                    market_dir=m_dir, ledgers_dir=l_dir, logs_dir=g_dir,
                    now=now_ts, disk_usage_fn=healthy_disk, update_guard_state=True,
                )
            self.assertTrue(res["disk_guard_paused"])
            self.assertFalse(is_replay_archive_allowed())
            self.assertFalse(stale_tmp.exists(), "Stale orphan .tmp file must be cleaned")
        finally:
            # Reset global guard state so later tests see healthy disk
            cleanup_old_data(
                market_dir=tmp / "empty_m", ledgers_dir=tmp / "empty_l", logs_dir=tmp / "empty_g",
                disk_usage_fn=healthy_disk, update_guard_state=True,
            )
            shutil.rmtree(tmp, ignore_errors=True)

    def test_alerts_cooldown_dedupe(self):
        with patch.dict(os.environ, {"PM_TELEGRAM_BOT_TOKEN": "", "PM_TELEGRAM_CHAT_ID": "", "PM_BARK_URL": "", "PM_ALERT_WEBHOOK_URL": ""}):
            self.assertFalse(send_alert("k1", "t", "m"))
        with patch.dict(os.environ, {"PM_BARK_URL": "https://api.day.app/fake"}), \
             patch("runtime.alerts._ensure_worker"), \
             patch("runtime.alerts._QUEUE.put_nowait") as mock_put:
            self.assertTrue(send_alert("dedupe_test_key", "Title", "Body", cooldown_s=300.0, now=1000.0))
            self.assertFalse(send_alert("dedupe_test_key", "Title", "Body", cooldown_s=300.0, now=1050.0))
            self.assertTrue(send_alert("dedupe_test_key", "Title", "Body", cooldown_s=300.0, now=1301.0))
            self.assertEqual(mock_put.call_count, 2)

    def test_estimate_twap_close_price_same_chain_and_rounding(self):
        """Provisional close_px must stay on the Chainlink 60s TWAP chain, not raw Binance spot."""
        tmp = Path(tempfile.mkdtemp(prefix="pm_twap_close_"))
        try:
            t0 = 1800000000.0
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window(
                "btc-updown-5m-1800000000", t0, t0 + 300.0,
                open_px=65000.123456, binance_open_px=65080.0,
                poly_spot=65010.0, poly_open_provisional=False,
            )
            # Basis is +80 (Binance 80 higher than Chainlink); last 60s Binance averages 65100 -> Chainlink TWAP ~ 65020
            runner._basis_ema = 80.0
            runner._spot_buf = [(t0 + 240.0 + i * 10.0, 65100.0) for i in range(7)]
            runner.last_spot_price = 65195.0  # Instantaneous spike that must NOT pollute 60s TWAP estimate

            est = runner.estimate_twap_close_price(t0 + 300.0)
            self.assertAlmostEqual(est, 65020.0, places=2)

            # Fallback when _spot_buf coverage < 15s: prefer fresh Chainlink poly_spot
            runner._spot_buf = [(t0 + 295.0, 65195.0), (t0 + 300.0, 65195.0)]
            runner.poly_spot_fresh = True
            runner.last_poly_spot = 65018.456
            self.assertAlmostEqual(runner.estimate_twap_close_price(t0 + 300.0), 65018.46, places=2)

            # close_window rounds open_px & close_px cleanly to 2 decimals
            runner.close_window(65018.456, official_winner=None, winner_source="spot_fallback")
            row = json.loads((tmp / "summary.jsonl").read_text(encoding="utf-8").strip())
            self.assertEqual(row["open_px"], 65000.12)
            self.assertEqual(row["close_px"], 65018.46)
            self.assertEqual(row["winner_source"], "spot_fallback")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_late_twap_updates_prices_after_gamma_confirmed_without_overwriting_winner(self):
        """If Gamma settles before TWAP publishes, later TWAP reconcile must fix open/close prices while preserving Gamma winner."""
        tmp = Path(tempfile.mkdtemp(prefix="pm_gamma_then_twap_"))
        try:
            slug = "btc-updown-5m-1800000900"
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window(slug, 1800000900.0, 1800001200.0, open_px=65000.0, poly_open_provisional=False)
            runner.pos_up = Position(5.0, 2.40)
            # Initial rollover wrote provisional fallback close price
            runner.close_window(64990.0, official_winner=None, winner_source="spot_fallback")

            # Gamma confirms winner="Up" first while TWAP is still missing (reusing provisional prices)
            runner.reconcile_record(slug, 65000.0, 64990.0, "Up", winner_source="gamma")
            row1 = json.loads((tmp / "summary.jsonl").read_text(encoding="utf-8").strip())
            self.assertEqual(row1["winner"], "Up")
            self.assertEqual(row1["winner_source"], "gamma")

            # Official Chainlink TWAP arrives later with true open/close prices
            runner.reconcile_record(slug, 65002.35, 65001.10, "Down", winner_source="twap")
            row2 = json.loads((tmp / "summary.jsonl").read_text(encoding="utf-8").strip())
            self.assertEqual(row2["open_px"], 65002.35)
            self.assertEqual(row2["close_px"], 65001.10)
            self.assertEqual(row2["winner"], "Up", "Gamma truth must never be overwritten by TWAP")
            self.assertEqual(row2["winner_source"], "gamma")
            self.assertAlmostEqual(row2["net_pnl"], 2.60, places=4)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestWebSecurityAndScalabilityHardening(unittest.TestCase):
    def test_web_path_traversal_dotdot_blocked_and_weak_password_rejected(self):
        self.assertFalse(_is_safe_segment(".."))
        self.assertFalse(_is_safe_segment("."))
        self.assertFalse(_is_safe_segment("../v2.8"))
        self.assertTrue(_is_safe_segment("v2.8"))
        self.assertTrue(_is_safe_segment("btc-updown-5m-1800000000"))

        for weak in ("", "short", "CHANGE_ME_TO_A_STRONG_PASSWORD", "password", "12345678"):
            with patch.dict(os.environ, {"PM_WEB_PASS": weak}):
                with self.assertRaises(RuntimeError):
                    _validate_bind_security("0.0.0.0")
        with patch.dict(os.environ, {"PM_WEB_PASS": "V3ryStr0ng!Pass"}):
            _validate_bind_security("0.0.0.0")

    def test_web_auth_rate_limiter_unit_and_http_429(self):
        lim = _AuthRateLimiter(max_failures=3, window_s=60.0, ban_s=120.0)
        self.assertEqual(lim.check_banned("1.2.3.4", now=10.0), 0.0)
        self.assertEqual(lim.record_failure("1.2.3.4", now=10.0), 0.0)
        self.assertEqual(lim.record_failure("1.2.3.4", now=11.0), 0.0)
        self.assertGreater(lim.record_failure("1.2.3.4", now=12.0), 0.0)
        self.assertGreater(lim.check_banned("1.2.3.4", now=20.0), 0.0)
        lim.record_success("1.2.3.4")
        self.assertEqual(lim.check_banned("1.2.3.4", now=21.0), 0.0)

    def test_web_endpoints_healthz_dotdot_pagination_and_etag(self):
        self.assertEqual(len(_downsample_curve([{"i": i} for i in range(5000)], max_points=600)), 600)

        httpd = _HardenedHTTPServer(("127.0.0.1", 0), QuantHandler)
        port = httpd.server_address[1]
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            # 1. /healthz works without Basic Auth even when PM_WEB_PASS is set
            with patch.dict(os.environ, {"PM_WEB_PASS": "V3ryStr0ng!Pass", "PM_WEB_USER": "admin"}):
                _AUTH_LIMITER.record_success("127.0.0.1")
                with patch("web.server._read_live", return_value={"slug": "s", "now": time.time()}):
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as resp:
                        self.assertEqual(resp.status, 200)
                        body = json.loads(resp.read().decode("utf-8"))
                        self.assertEqual(body["status"], "ok")
                        self.assertNotIn("total_pnl", body)

                with patch("web.server._read_live", return_value={"slug": "s", "now": time.time() - 120.0}):
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3)
                    self.assertEqual(ctx.exception.code, 503)

                # 2. batch=.. traversal blocked with 400 (authenticated)
                auth_hdr = "Basic " + base64.b64encode(b"admin:V3ryStr0ng!Pass").decode()
                req_bad = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/history?batch=..",
                    headers={"Authorization": auth_hdr},
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx2:
                    urllib.request.urlopen(req_bad, timeout=3)
                self.assertEqual(ctx2.exception.code, 400)

                # 3. Static asset returns ETag and honors If-None-Match -> 304
                req_st = urllib.request.Request(
                    f"http://127.0.0.1:{port}/app.js",
                    headers={"Authorization": auth_hdr},
                )
                with urllib.request.urlopen(req_st, timeout=3) as r_st:
                    self.assertEqual(r_st.status, 200)
                    etag = r_st.headers.get("ETag")
                    self.assertTrue(etag)
                req_304 = urllib.request.Request(
                    f"http://127.0.0.1:{port}/app.js",
                    headers={"Authorization": auth_hdr, "If-None-Match": etag},
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx3:
                    urllib.request.urlopen(req_304, timeout=3)
                self.assertEqual(ctx3.exception.code, 304)

                # 4. /healthz returns 503 paused_api_changed when API schema guard trips
                with patch("web.server._read_live", return_value={
                    "slug": "s", "now": time.time(),
                    "api_schema_guard": {"paused": True, "source": "gamma_tokens", "reason": "missing markets"},
                }):
                    with self.assertRaises(urllib.error.HTTPError) as ctx4:
                        urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3)
                    self.assertEqual(ctx4.exception.code, 503)
                    err_body = json.loads(ctx4.exception.read().decode("utf-8"))
                    self.assertEqual(err_body["status"], "paused_api_changed")
                    self.assertEqual(err_body["api_source"], "gamma_tokens")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_fast_tail_and_fallback_reconcile_plus_stream_lookup(self):
        tmp = Path(tempfile.mkdtemp(prefix="pm_1yr_perf_"))
        try:
            summary = tmp / "summary.jsonl"
            # Generate 400 rows (~140 KB > 64KB tail window) so row 0 is in prefix and row 399 is in tail
            with open(summary, "w", encoding="utf-8") as f:
                for i in range(400):
                    rec = {
                        "slug": f"btc-updown-5m-{1800000000 + i * 300}",
                        "start": float(1800000000 + i * 300),
                        "end": float(1800000300 + i * 300),
                        "winner": "Down",
                        "winner_source": "spot_fallback",
                        "open_px": 65000.0,
                        "close_px": 64950.0,
                        "pos_up": 5.0 if i in (0, 399) else 0.0,
                        "pos_down": 0.0,
                        "total_cost": 2.50 if i in (0, 399) else 0.0,
                        "proceeds": 0.0,
                        "net_pnl": -2.50 if i in (0, 399) else 0.0,
                        "fills_count": 1 if i in (0, 399) else 0,
                        "traded": i in (0, 399),
                        "regime": "normal_oscillation",
                        "regime_source": "entry_time",
                        "batch_id": "v2.8",
                    }
                    f.write(json.dumps(rec) + "\n")

            runner = PaperRunner(ledger_dir=tmp)
            self.assertAlmostEqual(runner.total_pnl, -5.0, places=4)

            # 1. Fast tail path (row 399 flips Down -> Up: -2.5 -> +2.5, delta +5.0)
            tail_slug = f"btc-updown-5m-{1800000000 + 399 * 300}"
            runner.reconcile_record(tail_slug, 65000.0, 65080.0, "Up", winner_source="gamma")
            self.assertAlmostEqual(runner.total_pnl, 0.0, places=4)

            # 2. Prefix streaming fallback path (row 0 flips Down -> Up: -2.5 -> +2.5, delta +5.0)
            head_slug = "btc-updown-5m-1800000000"
            runner.reconcile_record(head_slug, 65000.0, 65090.0, "Up", winner_source="gamma")
            self.assertAlmostEqual(runner.total_pnl, 5.0, places=4)

            # 3. Verify _lookup_summary_record finds both head and tail accurately
            found_tail = _lookup_summary_record(summary, tail_slug)
            found_head = _lookup_summary_record(summary, head_slug)
            self.assertEqual(found_tail["winner"], "Up")
            self.assertEqual(found_head["winner"], "Up")
            self.assertEqual(found_tail["winner_source"], "gamma")
            self.assertEqual(found_head["winner_source"], "gamma")
            self.assertEqual(len(summary.read_text(encoding="utf-8").splitlines()), 400)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_api_schema_drift_pauses_trading_cancels_orders_and_recovers(self):
        from runtime.market_feed import MarketFeed

        with patch.object(MarketFeed, "_test_connectivity", return_value=None):
            feed = MarketFeed(proxy="")
        tmp = Path(tempfile.mkdtemp(prefix="pm_schema_"))
        try:
            # 1 & 2 schema errors do not pause yet; 3rd consecutive error trips safe pause
            feed.record_api_schema_error("gamma_tokens", "missing markets", threshold=3)
            feed.record_api_schema_error("gamma_tokens", "missing markets", threshold=3)
            self.assertFalse(feed.get_api_schema_guard()["paused"])
            feed.record_api_schema_error("gamma_tokens", "missing markets", threshold=3)
            guard = feed.get_api_schema_guard()
            self.assertTrue(guard["paused"])
            self.assertEqual(guard["source"], "gamma_tokens")

            # Runner cancels resting order and refuses new entries when api_schema_guard is paused
            runner = PaperRunner(ledger_dir=tmp)
            t0 = 1800000000.0
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0)
            b_up = _book(0.54, 0.56, t=t0 + 100.0)
            b_dn = _book(0.44, 0.46, t=t0 + 100.0)
            spot = SpotState(timestamp=t0 + 100.0, price=65030.0, open_price=65000.0, realized_vol_bps=14.0)
            with patch("runtime.runner.plan_decision", return_value=Decision(action="quote", side="Up", price=0.55, size=5.0, reason="q")):
                runner.step(t0 + 100.0, spot, b_up, b_dn, replay_mode=True)
            self.assertEqual(len(runner.working_orders), 1)

            runner.step(t0 + 101.0, spot, b_up, b_dn, replay_mode=True, api_schema_guard=guard)
            self.assertEqual(len(runner.working_orders), 0, "Paused API schema guard must cancel resting quotes")
            self.assertIn("API schema guard", runner.decisions[-1]["reason"])

            # Recovery once valid response arrives again
            feed.record_api_schema_ok("gamma_tokens")
            self.assertFalse(feed.get_api_schema_guard()["paused"])
        finally:
            feed.close()
            shutil.rmtree(tmp, ignore_errors=True)


class TestAudit2026ScientificIntegrityFixes(unittest.TestCase):
    """独立审计修复回归测试：覆盖撮合真实性、在途撤单双仓防护、结算对账与看板口径。"""

    def test_maker_post_only_arrival_reject_and_static_ask_no_queue_drain(self):
        m = QueueMatcher()
        book_init = OrderBook(bids=[(0.50, 20.0)], asks=[(0.52, 20.0)], source_t=100.0, received_at=100.0)
        # 1. 到达 live_at 时卖一已经 <= 挂单价：Post-Only 应直接拒单，不白嫖吃单
        o_cross = Order(
            id=1, client_order_id="o1", side="Up", price=0.50, size=5.0, remaining=5.0,
            placed_at=100.0, live_at=100.3, purpose="entry",
        )
        m.register_order(o_cross, book_init)
        book_crossed = OrderBook(bids=[(0.48, 30.0)], asks=[(0.50, 15.0)], source_t=100.3, received_at=100.3)
        self.assertFalse(m.activate_order(o_cross, book_crossed, now=100.3, post_only=True))

        # 2. 正常到达重锚定队列 + 静态卖盘跨 tick（即使无关买盘跳动）绝不重复扣减 queue_ahead
        o_rest = Order(
            id=2, client_order_id="o2", side="Up", price=0.50, size=5.0, remaining=5.0,
            placed_at=100.0, live_at=100.3, purpose="entry",
        )
        m.register_order(o_rest, book_init)
        book_arrive = OrderBook(bids=[(0.50, 40.0)], asks=[(0.52, 20.0)], source_t=100.3, received_at=100.3)
        self.assertTrue(m.activate_order(o_rest, book_arrive, now=100.3, post_only=True))
        self.assertTrue(o_rest.activated)
        self.assertAlmostEqual(m.queue_ahead[o_rest.id], 40.0, places=4)

        # 连续 5 个 tick 出现静态同价 10 股卖盘（但无关买盘 0.49 每 tick 变化），总扣减只能有一次 10 股
        for i in range(5):
            b_touch = OrderBook(
                bids=[(0.49, 10.0 + i * 5.0)],
                asks=[(0.50, 10.0), (0.55, 50.0 + i)],
                source_t=101.0 + i,
                received_at=101.0 + i,
            )
            fill = m.match_order(o_rest, b_touch, now=101.0 + i)
            self.assertEqual(fill, 0.0, f"Static ask level must not drain queue_ahead across tick {i}")
        self.assertAlmostEqual(m.queue_ahead[o_rest.id], 30.0, places=4)

    def test_reprice_pending_cancel_fill_cancels_replacement_no_double_lot(self):
        tmp = tempfile.mkdtemp(prefix="pm_ghost_maker_")
        try:
            runner = PaperRunner(ledger_dir=tmp)
            t0 = 1800000000.0
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0)
            b_up = _book(0.50, 0.52, t=t0 + 50.0)
            b_dn = _book(0.48, 0.50, t=t0 + 50.0)
            spot = SpotState(timestamp=t0 + 50.0, price=65005.0, open_price=65000.0, realized_vol_bps=14.0)

            # Tick 1: 挂出旧单 @ 0.50
            with patch("runtime.runner.plan_decision", return_value=Decision(action="quote", side="Up", price=0.50, size=5.0, reason="q1")):
                runner.step(t0 + 50.0, spot, b_up, b_dn, replay_mode=True)
            self.assertEqual(len(runner.working_orders), 1)

            # Tick 2 (超过 MIN_REQUOTE_AGE_S=2.5s): 调价重挂 @ 0.53 (>= 2 ticks)，旧单进入 pending_cancels，新单进入 working_orders
            b_up2 = _book(0.53, 0.55, t=t0 + 53.0)
            b_dn2 = _book(0.45, 0.47, t=t0 + 53.0)
            spot2 = SpotState(timestamp=t0 + 53.0, price=65008.0, open_price=65000.0, realized_vol_bps=14.0)
            with patch("runtime.runner.plan_decision", return_value=Decision(action="quote", side="Up", price=0.53, size=5.0, reason="q2")):
                runner.step(t0 + 53.0, spot2, b_up2, b_dn2, replay_mode=True)
            self.assertEqual(len(runner.pending_cancels), 1)
            self.assertEqual(len(runner.working_orders), 1)
            # 使旧单队列清零并在撤单生效前成交
            old_order = runner.pending_cancels[0]
            runner.matcher.queue_ahead[old_order.id] = 0.0
            old_order.cancel_effective_at = t0 + 54.0

            # Tick 3: 成交打印把在途旧单吃满 5 股 -> 同侧替代新单必须被立即撤回，绝不残留成 10 股双仓
            prints = [{
                "outcome": "Up", "side": "SELL", "price": 0.50, "size": 5.0,
                "timestamp": t0 + 53.1, "received_at": t0 + 53.1,
            }]
            with patch("runtime.runner.plan_decision", return_value=Decision(action="wait", reason="hold")):
                runner.step(t0 + 53.1, spot2, b_up2, b_dn2, trade_prints=prints, replay_mode=True)
            self.assertAlmostEqual(runner.pos_up.shares, 5.0, places=4)
            self.assertEqual(len(runner.working_orders), 0, "Replacement working order must be cancelled once pending cancel fills")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_trade_stream_tx_hash_dedupe_and_integer_exch_ts_alignment(self):
        from runtime.trade_stream import TradeStreamClient

        ts_client = TradeStreamClient(proxy=None)
        ts_client.set_active_targets(["btc-updown-5m-1800000000"], ["tok_up"])
        # 同秒同价同量，但不同 transactionHash -> 两笔独立成交必须全部保留
        p1 = {
            "slug": "btc-updown-5m-1800000000", "asset": "tok_up", "price": "0.51", "size": "5.0",
            "timestamp": 1800000100, "side": "SELL", "transactionHash": "0xaaa",
        }
        p2 = {
            "slug": "btc-updown-5m-1800000000", "asset": "tok_up", "price": "0.51", "size": "5.0",
            "timestamp": 1800000100, "side": "SELL", "transactionHash": "0xbbb",
        }
        self.assertTrue(ts_client._ingest(p1))
        self.assertTrue(ts_client._ingest(p2))
        self.assertFalse(ts_client._ingest(p1), "Identical transactionHash must still dedupe")

        # 截断整秒 timestamp 配合正常网络延迟 received_at，必须对齐到 received_at
        aligned = PaperRunner._resolve_print_timestamp(
            {"timestamp": 1800000100.0, "received_at": 1800000100.45},
            fallback_now=1800000100.5,
        )
        self.assertAlmostEqual(aligned, 1800000100.45, places=4)
        # 已有亚秒精度的 timestamp 保持原值
        precise = PaperRunner._resolve_print_timestamp(
            {"timestamp": 1800000100.25, "received_at": 1800000100.45},
            fallback_now=1800000100.5,
        )
        self.assertAlmostEqual(precise, 1800000100.25, places=4)

    def test_cold_start_basis_requires_full_45s_window(self):
        tmp = tempfile.mkdtemp(prefix="pm_basis_cold_")
        try:
            runner = PaperRunner(ledger_dir=tmp)
            t0 = 1800000000.0
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0)
            b_up = _book(0.49, 0.51, t=t0)
            b_dn = _book(0.49, 0.51, t=t0)

            # 前 20 秒内持续采样 20 次：跨度不足 45s，_basis_ema 必须仍为 None
            for i in range(20):
                ti = t0 + i * 1.0
                sp = SpotState(timestamp=ti, price=65000.0 + i, open_price=65000.0, realized_vol_bps=12.0)
                with patch("runtime.runner.plan_decision", return_value=Decision(action="wait", reason="w")):
                    runner.step(ti, sp, _book(0.49, 0.51, t=ti), _book(0.49, 0.51, t=ti), poly_spot=64995.0 + i * 0.5, poly_spot_ts=ti, replay_mode=False)
            self.assertIsNone(runner._basis_ema, "Cold-start basis must not initialize before 45s span")

            # 跨度满 50 秒后，_basis_ema 才完成冷启动初始化
            for i in range(20, 52):
                ti = t0 + i * 1.0
                sp = SpotState(timestamp=ti, price=65000.0 + i, open_price=65000.0, realized_vol_bps=12.0)
                with patch("runtime.runner.plan_decision", return_value=Decision(action="wait", reason="w")):
                    runner.step(ti, sp, _book(0.49, 0.51, t=ti), _book(0.49, 0.51, t=ti), poly_spot=64995.0 + i * 0.5, poly_spot_ts=ti, replay_mode=False)
            self.assertIsNotNone(runner._basis_ema)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_delayed_taker_checks_both_books_and_opposite_pending_cancel(self):
        tmp = tempfile.mkdtemp(prefix="pm_delay_dual_")
        try:
            runner = PaperRunner(ledger_dir=tmp)
            t0 = 1800000000.0
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0)
            b_up = _book(0.50, 0.52, t=t0 + 60.0)
            b_dn = _book(0.48, 0.50, t=t0 + 60.0)
            spot = SpotState(timestamp=t0 + 60.0, price=65050.0, open_price=65000.0, realized_vol_bps=14.0)

            # 延迟返回的不一致双侧盘口（ask_up + ask_dn = 0.80 < 0.985）必须拒绝吃单
            def _inconsistent_dual(side):
                return (
                    _book(0.38, 0.40, t=t0 + 60.3),
                    _book(0.38, 0.40, t=t0 + 60.3),
                )

            dec = Decision(action="taker_buy", side="Up", price=0.55, size=5.0, reason="tb")
            with patch("runtime.runner.plan_decision", return_value=dec):
                runner.step(
                    t0 + 60.0, spot, b_up, b_dn,
                    delayed_book_fetcher=_inconsistent_dual,
                    replay_mode=True,
                )
            self.assertEqual(runner.pos_up.shares, 0.0, "Inconsistent post-latency dual book must block taker fill")
            self.assertTrue(any(a.get("action") == "taker_reject" for a in runner.actions))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_taker_unwind_preserves_avg_price_and_pnl_identity(self):
        from core.execution import taker_fee_per_share

        tmp = tempfile.mkdtemp(prefix="pm_unwind_acct_")
        try:
            runner = PaperRunner(ledger_dir=tmp)
            t0 = 1800000000.0
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0)
            # 先手动持有 10 股 UP @ $0.50（总成本 $5.00）
            runner.pos_up.shares = 10.0
            runner.pos_up.cash = 5.0
            self.assertAlmostEqual(runner.pos_up.avg_price, 0.50, places=6)

            # 触发 taker_unwind 平掉 5 股（满足 Polymarket >=5 股规则）@ bid=0.40
            b_up = _book(0.40, 0.42, sz=50.0, t=t0 + 200.0)
            b_dn = _book(0.58, 0.60, sz=50.0, t=t0 + 200.0)
            spot = SpotState(timestamp=t0 + 200.0, price=64950.0, open_price=65000.0, realized_vol_bps=14.0)
            dec = Decision(action="taker_unwind", side="Up", price=0.35, size=5.0, reason="salvage")
            with patch("runtime.runner.plan_decision", return_value=dec):
                runner.step(t0 + 200.0, spot, b_up, b_dn, replay_mode=True)

            # 剩余 5 股的均价必须依然精确为 $0.50，不被平仓现金流扭曲
            self.assertAlmostEqual(runner.pos_up.shares, 5.0, places=4)
            self.assertAlmostEqual(runner.pos_up.avg_price, 0.50, places=6)
            self.assertLess(runner.realized_unwind_pnl, 0.0)

            # 结算：若 UP 胜出，期末兑现 5.0 美元；精确净利 = 5.0 + 平仓净收入 - 初始 5.0 投入
            unwind_fill = runner.fills[-1]
            est_fee = round(unwind_fill["size"] * taker_fee_per_share(unwind_fill["price"], config.PARAMS["TAKER_FEE_RATE"]), 4)
            expected_net_pnl = 5.0 + (unwind_fill["size"] * unwind_fill["price"] - est_fee) - 5.0
            runner.close_window(65100.0, official_winner="Up", winner_source="gamma")
            rec = json.loads((Path(tmp) / "summary.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertAlmostEqual(rec["proceeds"] - rec["total_cost"], rec["net_pnl"], places=4)
            self.assertAlmostEqual(rec["net_pnl"], expected_net_pnl, places=4)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ws_book_set_tokens_incremental_subscribe_preserves_books(self):
        client = PolymarketBookWSClient(proxy=None)
        client.connected = True
        client._active_tokens = ("tok_dn_1", "tok_up_1")
        client._books["tok_up_1"] = {
            "bids": {0.50: 20.0}, "asks": {0.52: 20.0},
            "last_trade": None, "t": time.time(), "ready": True,
        }
        gen_before = client._token_gen

        # 模拟 tau <= 30s 预热下一窗口（在旧集合上追加新 token）
        client.set_tokens(["tok_up_1", "tok_dn_1", "tok_up_2", "tok_dn_2"])
        self.assertEqual(client._token_gen, gen_before, "Superset token prewarm must not force WS reconnect")
        self.assertIn("tok_up_1", client._books, "Existing active orderbook must not be cleared on prewarm")
        self.assertEqual(client._pending_sub_tokens, ["tok_dn_2", "tok_up_2"])

    def test_retention_cross_process_guard_resync_clears_stale_pause(self):
        from runtime import retention

        tmp = tempfile.mkdtemp(prefix="pm_ret_sync_")
        try:
            guard_file = Path(tmp) / "disk_guard.json"
            with patch.object(retention, "_DISK_GUARD_FILE", guard_file), \
                 patch.dict(retention._DISK_GUARD_STATE, {"paused": True, "reason": "stale", "checked_at": 1.0}):
                guard_file.write_text(
                    json.dumps({"paused": False, "reason": "", "checked_at": time.time()}),
                    encoding="utf-8",
                )
                retention._LAST_GUARD_SYNC_MONO = 0.0
                self.assertTrue(retention.is_raw_recording_allowed())
                self.assertTrue(retention.is_replay_archive_allowed())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_web_security_rejects_weak_pass_on_loopback_and_require_auth(self):
        from web.server import _clean_action

        # 1. 即使在 127.0.0.1，只要配置了占位/弱密码，必须拒绝启动
        with patch.dict(os.environ, {"PM_WEB_PASS": "CHANGE_ME_TO_A_STRONG_PASSWORD", "PM_REQUIRE_AUTH": ""}):
            with self.assertRaises(RuntimeError):
                _validate_bind_security("127.0.0.1")
        with patch.dict(os.environ, {"PM_WEB_PASS": "12345", "PM_REQUIRE_AUTH": ""}):
            with self.assertRaises(RuntimeError):
                _validate_bind_security("127.0.0.1")

        # 2. PM_REQUIRE_AUTH=1 时，127.0.0.1 空密码也必须拒绝启动
        with patch.dict(os.environ, {"PM_WEB_PASS": "", "PM_REQUIRE_AUTH": "1"}):
            with self.assertRaises(RuntimeError):
                _validate_bind_security("127.0.0.1")

        # 3. 强密码通过
        with patch.dict(os.environ, {"PM_WEB_PASS": "StrongPass#2026!", "PM_REQUIRE_AUTH": "1"}):
            _validate_bind_security("127.0.0.1")

        # 4. _clean_action 保留 taker_partial 以及 requested/filled 字段
        cleaned = _clean_action({
            "t": 1800000100.5, "action": "taker_partial", "side": "Up",
            "price": 0.48, "size": 3.0, "requested": 5.0, "filled": 3.0,
            "purpose": "entry", "kind": "taker",
        })
        self.assertEqual(cleaned["action"], "taker_partial")
        self.assertAlmostEqual(cleaned["size"], 3.0, places=4)
        self.assertAlmostEqual(cleaned["requested"], 5.0, places=4)
        self.assertAlmostEqual(cleaned["filled"], 3.0, places=4)

    def test_web_window_pruned_fallback_and_history_curve_real_idx(self):
        from web import server as web_server

        tmp = tempfile.mkdtemp(prefix="pm_web_pruned_")
        try:
            ledgers = Path(tmp) / "ledgers"
            batch_dir = ledgers / "v2.8"
            (batch_dir / "windows").mkdir(parents=True, exist_ok=True)
            rows = []
            for i in range(1, 5):
                rows.append(json.dumps({
                    "slug": f"btc-updown-5m-{1800000000 + i * 300}",
                    "start": 1800000000 + i * 300,
                    "end": 1800000300 + i * 300,
                    "winner": "Up",
                    "net_pnl": 1.25 if i % 2 else -0.5,
                    "total_cost": 2.5,
                    "proceeds": 3.75 if i % 2 else 2.0,
                    "pos_up": 5.0,
                    "pos_down": 0.0,
                    "traded": True,
                    "fills_count": 1,
                    "open_px": 65000.0,
                    "close_px": 65050.0,
                }))
            (batch_dir / "summary.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")

            with patch.object(config, "LEDGERS_DIR", ledgers), \
                 patch.object(config, "CURRENT_LEDGER_DIR", batch_dir), \
                 patch.object(config, "BATCH_ID", "v2.8"), \
                 patch.dict(os.environ, {"PM_WEB_PASS": ""}):
                web_server._HISTORY_CACHE.clear()
                web_server._SUMMARY_AGG_CACHE.clear()
                httpd = _HardenedHTTPServer(("127.0.0.1", 0), QuantHandler)
                port = httpd.server_address[1]
                th = threading.Thread(target=httpd.serve_forever, daemon=True)
                th.start()
                try:
                    # 1. /api/window 对已清理 .json.gz 但 summary 存在的轮次返回 archive_pruned=True
                    url_win = f"http://127.0.0.1:{port}/api/window?slug=btc-updown-5m-1800000300"
                    with urllib.request.urlopen(url_win, timeout=3) as resp:
                        self.assertEqual(resp.status, 200)
                        wdata = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(wdata.get("archive_pruned"))
                    self.assertAlmostEqual(wdata.get("net_pnl"), 1.25, places=4)

                    # 2. /api/history 返回带真实 idx 与 round_pnl 的 equity_curve，并写入静态聚合缓存
                    url_hist = f"http://127.0.0.1:{port}/api/history?batch=v2.8"
                    with urllib.request.urlopen(url_hist, timeout=3) as resp:
                        hdata = json.loads(resp.read().decode("utf-8"))
                    curve = hdata["equity_curve"]
                    self.assertEqual(len(curve), 4)
                    self.assertEqual(curve[0]["idx"], 1)
                    self.assertAlmostEqual(curve[0]["round_pnl"], 1.25, places=4)
                    self.assertEqual(curve[-1]["idx"], 4)
                    self.assertAlmostEqual(curve[-1]["round_pnl"], -0.5, places=4)
                    self.assertAlmostEqual(hdata["peak_pnl"], 2.0, places=4)
                    self.assertAlmostEqual(hdata["max_drawdown"], 0.5, places=4)
                    self.assertGreaterEqual(len(web_server._SUMMARY_AGG_CACHE), 1)
                finally:
                    httpd.shutdown()
                    httpd.server_close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ws_book_rollover_shrink_unsubscribes_without_disconnect(self):
        client = PolymarketBookWSClient(proxy=None)
        client.connected = True
        client._active_tokens = ("tok_dn_1", "tok_dn_2", "tok_up_1", "tok_up_2")
        now_t = time.time()
        for tid in client._active_tokens:
            client._books[tid] = {
                "bids": {0.50: 20.0}, "asks": {0.52: 20.0},
                "last_trade": None, "t": now_t, "ready": True,
            }
        gen_before = client._token_gen

        # 换窗瞬间缩减至新窗口的 2 个 token：绝不能断连或清空已预热好的 tok_up_2 / tok_dn_2
        client.set_tokens(["tok_up_2", "tok_dn_2"])
        self.assertEqual(client._token_gen, gen_before, "Rollover shrink must not force WS reconnect")
        self.assertIn("tok_up_2", client._books)
        self.assertIn("tok_dn_2", client._books)
        self.assertNotIn("tok_up_1", client._books)
        self.assertEqual(client._pending_unsub_tokens, ["tok_dn_1", "tok_up_1"])

    def test_clob_book_schema_guard_recovers_via_ws_ticks(self):
        from runtime.market_feed import MarketFeed

        feed = MarketFeed.__new__(MarketFeed)
        feed._schema_lock = threading.Lock()
        feed._schema_counts = {}
        feed._schema_guard = {"paused": False, "source": "", "reason": "", "detected_at": 0.0}
        feed._net_lock = threading.Lock()
        feed._net_stats = {}
        feed.spot_history = []
        feed._consecutive_spot_fails = 0
        feed._last_binance_px = 0.0
        feed._last_binance_t = 0.0
        feed._last_spot_source = "binance"
        feed._last_ws_book_rec = {}
        feed._record_net = lambda *a, **k: None
        feed.record_raw_event = lambda *a, **k: None

        # 模拟 HTTP fallback 触发 clob_book 暂停
        for _ in range(3):
            feed.record_api_schema_error("clob_book", "HTTP 400", threshold=3)
        self.assertTrue(feed.get_api_schema_guard()["paused"])

        class _FakeSpotWS:
            rtt_ms = 12.0
            def get_latest(self, max_age_s=2.0, now=None):
                return (65000.0, now or time.time(), "binance_ws")

        class _FakeBookWS:
            rtt_ms = 15.0
            def get_order_book(self, tok, max_age_s=3.0, now=None):
                return _book(0.49, 0.51, t=now or time.time())

        feed.attach_ws_clients(spot_ws=_FakeSpotWS(), book_ws=_FakeBookWS())
        feed.fetch_tick_bundle(65000.0, 65000.0, "tok_up", "tok_dn", now=1800000100.0)
        self.assertFalse(feed.get_api_schema_guard()["paused"], "Healthy WS dual books must recover clob_book pause")

    def test_profitable_full_unwind_keeps_nonnegative_total_cost_and_reconcile_identity(self):
        from core.execution import taker_fee_per_share

        tmp = tempfile.mkdtemp(prefix="pm_unwind_profit_")
        try:
            runner = PaperRunner(ledger_dir=tmp)
            t0 = 1800000000.0
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0)
            # 持有 5 股 UP @ $0.40（原始总投入 $2.00）
            runner.pos_up.shares = 5.0
            runner.pos_up.cash = 2.0

            # 盈利全平 @ bid=0.60
            b_up = _book(0.60, 0.62, sz=50.0, t=t0 + 200.0)
            b_dn = _book(0.38, 0.40, sz=50.0, t=t0 + 200.0)
            spot = SpotState(timestamp=t0 + 200.0, price=65100.0, open_price=65000.0, realized_vol_bps=14.0)
            dec = Decision(action="taker_unwind", side="Up", price=0.55, size=5.0, reason="take_profit")
            with patch("runtime.runner.plan_decision", return_value=dec):
                runner.step(t0 + 200.0, spot, b_up, b_dn, replay_mode=True)

            self.assertEqual(runner.pos_up.shares, 0.0)
            self.assertEqual(runner.pos_up.cash, 0.0)
            est_fee = round(5.0 * taker_fee_per_share(0.60, config.PARAMS["TAKER_FEE_RATE"]), 4)
            expected_proc = round(5.0 * 0.60 - est_fee, 4)
            expected_pnl = round(expected_proc - 2.0, 4)

            runner.close_window(65100.0, official_winner="Up", winner_source="spot_fallback")
            rec = json.loads((Path(tmp) / "summary.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertAlmostEqual(rec["total_cost"], 2.0, places=4)
            self.assertGreaterEqual(rec["total_cost"], 0.0)
            self.assertAlmostEqual(rec["proceeds"], expected_proc, places=4)
            self.assertAlmostEqual(rec["net_pnl"], expected_pnl, places=4)

            # 即使官方 Gamma 翻盘为 Down，因仓位已全平，proceeds/net_pnl 仍必须保持完全恒等
            runner.reconcile_record("btc-updown-5m-1800000000", 65000.0, 64900.0, "Down", winner_source="gamma")
            rec2 = json.loads((Path(tmp) / "summary.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertEqual(rec2["winner"], "Down")
            self.assertAlmostEqual(rec2["total_cost"], 2.0, places=4)
            self.assertAlmostEqual(rec2["proceeds"], expected_proc, places=4)
            self.assertAlmostEqual(rec2["net_pnl"], expected_pnl, places=4)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_web_xff_uses_rightmost_ip_cold_start_and_cb_tz(self):
        from core.circuit_breaker import CircuitBreaker
        from web import server as web_server

        # 1. XFF 必须取最右侧真实反代 IP，避免客户端伪造左侧 IP 绕过防爆破
        dummy = object.__new__(QuantHandler)
        dummy.client_address = ("127.0.0.1", 54321)
        dummy.headers = {"X-Forwarded-For": "1.1.1.1, 2.2.2.2, 9.9.9.9"}
        self.assertEqual(dummy._client_ip(), "9.9.9.9")

        # 2. CircuitBreaker 跨日按 PM_TZ_OFFSET_HOURS 与看板一致（2026-01-01 16:30 UTC == UTC+8 的 2026-01-02）
        ts_utc1630 = 1767285000.0  # 2026-01-01T16:30:00Z
        with patch.dict(os.environ, {"PM_TZ_OFFSET_HOURS": "8"}):
            self.assertEqual(CircuitBreaker._get_day_str(ts_utc1630), "2026-01-02")
        with patch.dict(os.environ, {"PM_TZ_OFFSET_HOURS": "0"}):
            self.assertEqual(CircuitBreaker._get_day_str(ts_utc1630), "2026-01-01")

        # 3. 冷启动 LEDGERS_DIR 尚不存在时，/api/batches 和 /api/history 不抛 FileNotFoundError
        tmp = tempfile.mkdtemp(prefix="pm_web_cold_")
        try:
            missing_ledgers = Path(tmp) / "not_created_yet"
            with patch.object(config, "LEDGERS_DIR", missing_ledgers), \
                 patch.object(config, "CURRENT_LEDGER_DIR", missing_ledgers / "v2.8"), \
                 patch.object(config, "BATCH_ID", "v2.8"), \
                 patch.dict(os.environ, {"PM_WEB_PASS": ""}):
                web_server._HISTORY_CACHE.clear()
                web_server._SUMMARY_AGG_CACHE.clear()
                httpd = _HardenedHTTPServer(("127.0.0.1", 0), QuantHandler)
                port = httpd.server_address[1]
                th = threading.Thread(target=httpd.serve_forever, daemon=True)
                th.start()
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/batches", timeout=3) as resp:
                        self.assertEqual(resp.status, 200)
                        self.assertEqual(json.loads(resp.read().decode("utf-8"))["batches"], [])
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/history?batch=v2.8", timeout=3) as resp:
                        self.assertEqual(resp.status, 200)
                        self.assertEqual(json.loads(resp.read().decode("utf-8"))["total_rounds"], 0)
                finally:
                    httpd.shutdown()
                    httpd.server_close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_extreme_one_sided_book_allows_delayed_hedge_and_unwind_and_ms_ts_normalized(self):
        from runtime.trade_stream import TradeStreamClient

        # 1. 13 位毫秒级时间戳必须自动归一化为秒级，不污染回放游标
        ts_client = TradeStreamClient(proxy=None)
        ts_client.set_active_targets(["btc-updown-5m-1800000000"], ["tok_up"])
        ok = ts_client._ingest({
            "slug": "btc-updown-5m-1800000000", "asset": "tok_up", "price": "0.51", "size": "5.0",
            "timestamp": 1800000100500, "side": "SELL", "transactionHash": "0xms1",
        })
        self.assertTrue(ok)
        drained = ts_client.drain_trades("btc-updown-5m-1800000000")
        self.assertAlmostEqual(drained[0]["timestamp"], 1800000100.5, places=3)

        # 2. 极端 96%/4% 盘口（胜方无 ask，败方无 bid）：延迟后的黑天鹅锁利和尾盘防守回收必须放行
        tmp = tempfile.mkdtemp(prefix="pm_extreme_lock_")
        try:
            runner = PaperRunner(ledger_dir=tmp)
            t0 = 1800000000.0
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0)
            runner.pos_up.shares = 5.0
            runner.pos_up.cash = 2.5

            # Up 盘口仅剩买盘 0.96（无卖盘）；Down 盘口仅剩卖盘 0.04（无买盘）
            up_extreme = OrderBook(bids=[(0.96, 50.0)], asks=[], source_t=t0 + 250.0, received_at=t0 + 250.0)
            dn_extreme = OrderBook(bids=[], asks=[(0.04, 50.0)], source_t=t0 + 250.0, received_at=t0 + 250.0)
            spot = SpotState(timestamp=t0 + 250.0, price=65300.0, open_price=65000.0, realized_vol_bps=14.0)

            dec_hedge = Decision(
                action="taker_hedge", side="Down", price=0.04, size=5.0,
                reason="v2.6 极深黑天鹅锁利: Down ask=$0.04",
            )
            with patch("runtime.runner.plan_decision", return_value=dec_hedge):
                runner.step(
                    t0 + 250.0, spot, up_extreme, dn_extreme,
                    delayed_book_fetcher=lambda _s: (up_extreme, dn_extreme),
                    replay_mode=True,
                )
            self.assertAlmostEqual(runner.pos_down.shares, 5.0, places=4, msg="Extreme one-sided black-swan hedge must not be falsely rejected")

            # 同理，尾盘防守回收（卖自己侧 bid=0.10，对侧空盘）也必须放行
            runner2 = PaperRunner(ledger_dir=tmp)
            runner2.start_window("btc-updown-5m-1800000300", t0 + 300.0, t0 + 600.0, 65000.0)
            runner2.pos_up.shares = 5.0
            runner2.pos_up.cash = 2.5
            up_salvage = OrderBook(bids=[(0.10, 50.0)], asks=[(0.12, 50.0)], source_t=t0 + 575.0, received_at=t0 + 575.0)
            dn_empty_ask = OrderBook(bids=[(0.88, 50.0)], asks=[], source_t=t0 + 575.0, received_at=t0 + 575.0)
            dec_unwind = Decision(action="taker_unwind", side="Up", price=0.10, size=5.0, reason="Defensive Salvage")
            with patch("runtime.runner.plan_decision", return_value=dec_unwind):
                runner2.step(
                    t0 + 575.0, spot, up_salvage, dn_empty_ask,
                    delayed_book_fetcher=lambda _s: (up_salvage, dn_empty_ask),
                    replay_mode=True,
                )
            self.assertAlmostEqual(runner2.pos_up.shares, 0.0, places=4, msg="Defensive unwind must not be blocked by opposite empty ask")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
