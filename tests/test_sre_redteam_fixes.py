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

        # Existing winning position -> still allowed to execute Black-Swan Lock under breaker
        dec_lock = plan_decision(
            Position(5.0, 2.80), Position(0, 0), _book(0.94, 0.96), _book(0.04, 0.05),
            spot, tau_s=120.0, params=params, circuit_breaker_active=True,
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


if __name__ == "__main__":
    unittest.main()
