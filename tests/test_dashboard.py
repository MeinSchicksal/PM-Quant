"""Investor dashboard projection tests."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web.server import _legacy_actions, _public_round


class TestInvestorDashboard(unittest.TestCase):
    def test_legacy_journal_excludes_wait_and_duplicate_quotes(self):
        detail = {
            "decisions": [
                {"t": 10.0, "action": "quote", "side": "Up", "price": 0.50, "size": 5.0, "reason": "Enter Up maker"},
                {"t": 11.0, "action": "quote", "side": "Up", "price": 0.50, "size": 5.0, "reason": "Enter Up maker"},
                {"t": 12.0, "action": "wait", "side": None, "price": None, "size": None},
            ],
            "fills": [
                {"t": 13.0, "side": "Up", "price": 0.50, "size": 5.0, "kind": "maker"},
                {"t": 14.0, "side": "Down", "price": 0.46, "size": 5.0, "kind": "maker"},
            ],
        }
        actions = _legacy_actions(detail)
        self.assertEqual(len(actions), 3)
        self.assertEqual([a["action"] for a in actions], ["quote", "fill", "fill"])
        self.assertEqual(actions[-1]["purpose"], "hedge")

    def test_actual_average_is_weighted_by_executed_size(self):
        detail = {
            "actions": [
                {"t": 10.0, "action": "fill", "side": "Up", "price": 0.40, "size": 5.0, "purpose": "entry"},
                {"t": 11.0, "action": "fill", "side": "Up", "price": 0.50, "size": 5.0, "purpose": "entry"},
                {"t": 12.0, "action": "quote", "side": "Down", "price": 0.20, "size": 5.0, "purpose": "hedge"},
            ]
        }
        result = _public_round({"slug": "round", "traded": True, "net_pnl": 0.2}, detail)
        self.assertEqual(result["average_prices"]["Up"], 0.45)
        self.assertIsNone(result["average_prices"]["Down"])

    def test_public_round_contains_no_model_or_market_internals(self):
        record = {
            "slug": "round", "start": 1000.0, "end": 1300.0,
            "traded": True, "net_pnl": 0.2, "winner": "Up",
            "open_px": 75000.0, "close_px": 75100.0,
        }
        detail = {"decisions": [{"reason": "U_hedge > U_hold"}], "fills": []}
        result = _public_round(record, detail)
        self.assertEqual(result["result"], "win")
        self.assertEqual(result["open_px"], 75000.0)
        self.assertEqual(result["close_px"], 75100.0)
        self.assertNotIn("decisions", result)
        self.assertNotIn("reason", result)

    def test_active_round_never_changes_settled_result(self):
        result = _public_round(
            {"slug": "live", "traded": False, "net_pnl": 0.0},
            {"actions": []}, active=True,
        )
        self.assertEqual(result["result"], "pending")
        self.assertIsNone(result["net_pnl"])

    def test_reconcile_record_synchronizes_summary_and_window_gz(self):
        import tempfile
        import shutil
        import gzip
        import json
        from runtime.runner import PaperRunner

        temp_dir = Path(tempfile.mkdtemp())
        try:
            summary_f = temp_dir / "summary.jsonl"
            windows_d = temp_dir / "windows"
            windows_d.mkdir(parents=True, exist_ok=True)

            slug = "btc-updown-5m-9999999"
            init_record = {
                "slug": slug, "start": 9999999.0, "end": 10000299.0,
                "winner": "Down", "open_px": 75000.0, "close_px": 74900.0,
                "pos_up": 10.0, "pos_down": 0.0, "total_cost": 5.0,
                "proceeds": 0.0, "net_pnl": -5.0, "traded": True, "result": "loss"
            }
            summary_f.write_text(json.dumps(init_record) + "\n", encoding="utf-8")
            with gzip.open(windows_d / f"{slug}.json.gz", "wt", encoding="utf-8") as gz:
                json.dump(init_record, gz)

            runner = PaperRunner(ledger_dir=temp_dir)
            # Reconcile with official truth: Up won, open 74800 -> close 74950
            runner.reconcile_record(slug, 74800.0, 74950.0, "Up")

            # Verify summary.jsonl updated
            updated_sum = json.loads(summary_f.read_text(encoding="utf-8").strip())
            self.assertEqual(updated_sum["open_px"], 74800.0)
            self.assertEqual(updated_sum["close_px"], 74950.0)
            self.assertEqual(updated_sum["winner"], "Up")
            self.assertEqual(updated_sum["proceeds"], 10.0)
            self.assertEqual(updated_sum["net_pnl"], 5.0)
            self.assertEqual(updated_sum["result"], "win")

            # Verify windows/<slug>.json.gz updated
            with gzip.open(windows_d / f"{slug}.json.gz", "rt", encoding="utf-8") as gz:
                updated_gz = json.load(gz)
            self.assertEqual(updated_gz["open_px"], 74800.0)
            self.assertEqual(updated_gz["close_px"], 74950.0)
            self.assertEqual(updated_gz["winner"], "Up")
            self.assertEqual(updated_gz["net_pnl"], 5.0)
            self.assertEqual(updated_gz["result"], "win")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_update_open_price_dynamically_calibrates(self):
        import tempfile
        import shutil
        from runtime.runner import PaperRunner

        temp_dir = Path(tempfile.mkdtemp())
        try:
            runner = PaperRunner(ledger_dir=temp_dir)
            runner.start_window("test-slug", 1000.0, 1300.0, open_px=75100.0, binance_open_px=75100.0)
            self.assertTrue(runner.poly_open_is_provisional)
            self.assertEqual(runner.open_price, 75100.0)

            # Official TWAP arrives
            runner.update_open_price(74950.0)
            self.assertFalse(runner.poly_open_is_provisional)
            self.assertEqual(runner.open_price, 74950.0)
            self.assertEqual(runner.price_history[0]["poly_price"], 74950.0)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_retention_cleanup_prunes_expired_market_and_replays_but_keeps_summary(self):
        import os
        import tempfile
        import shutil
        import datetime
        from collections import namedtuple
        from runtime.retention import cleanup_old_data

        Usage = namedtuple("Usage", ["total", "used", "free"])
        healthy_disk = lambda _: Usage(total=100 * (1024 ** 3), used=40 * (1024 ** 3), free=60 * (1024 ** 3))
        tmp = Path(tempfile.mkdtemp(prefix="pm_ret_"))
        try:
            now_dt = datetime.datetime(2026, 11, 22, 12, 0, 0, tzinfo=datetime.timezone.utc)
            now_ts = now_dt.timestamp()

            m_dir = tmp / "1_market"
            l_dir = tmp / "2_ledgers"
            g_dir = tmp / "4_runtime" / "logs"
            (m_dir / "events" / "2026-11-17").mkdir(parents=True)  # 5 days old -> prune
            (m_dir / "events" / "2026-11-20").mkdir(parents=True)  # 2 days old -> keep
            (m_dir / "events" / "2026-11-22").mkdir(parents=True)  # today -> keep
            (m_dir / "settlements" / "2026-11-10").mkdir(parents=True)  # settlements -> keep
            (m_dir / "events" / "2026-11-17" / "ev.jsonl").write_text("x" * 2048, encoding="utf-8")
            (m_dir / "events" / "2026-11-20" / "ev.jsonl").write_text("y" * 1024, encoding="utf-8")
            (m_dir / "events" / "2026-11-22" / "ev.jsonl").write_text("z" * 512, encoding="utf-8")
            (m_dir / "settlements" / "2026-11-10" / "s.jsonl").write_text("keep", encoding="utf-8")

            win_dir = l_dir / "v2.8" / "windows"
            win_dir.mkdir(parents=True)
            summary_f = l_dir / "v2.8" / "summary.jsonl"
            live_f = l_dir / "v2.8" / "live.json"
            summary_f.write_text('{"slug":"old","net_pnl":1.5,"traded":true}\n', encoding="utf-8")
            live_f.write_text('{"slug":"live"}', encoding="utf-8")

            old_epoch = int(now_ts - 10 * 86400)
            new_epoch = int(now_ts - 2 * 86400)
            old_gz = win_dir / f"btc-updown-5m-{old_epoch}.json.gz"
            new_gz = win_dir / f"btc-updown-5m-{new_epoch}.json.gz"
            old_gz.write_bytes(b"old-gz" * 100)
            new_gz.write_bytes(b"new-gz" * 100)

            g_dir.mkdir(parents=True)
            old_log = g_dir / "old.log"
            old_log.write_text("old log", encoding="utf-8")
            os.utime(old_log, (now_ts - 10 * 86400, now_ts - 10 * 86400))

            # Dry run must not delete anything
            dry = cleanup_old_data(
                market_dir=m_dir, ledgers_dir=l_dir, logs_dir=g_dir,
                market_days=3, replay_days=7, log_days=7, now=now_ts, dry_run=True,
                disk_usage_fn=healthy_disk, update_guard_state=False,
            )
            self.assertEqual(dry["removed_dirs"], 1)
            self.assertEqual(dry["removed_files"], 3)
            self.assertTrue((m_dir / "events" / "2026-11-17").exists())
            self.assertTrue(old_gz.exists())

            # Real run deletes expired files only and NEVER touches summary.jsonl / live.json
            res = cleanup_old_data(
                market_dir=m_dir, ledgers_dir=l_dir, logs_dir=g_dir,
                market_days=3, replay_days=7, log_days=7, now=now_ts, dry_run=False,
                disk_usage_fn=healthy_disk, update_guard_state=False,
            )
            self.assertEqual(res["removed_dirs"], 1)
            self.assertEqual(res["removed_files"], 3)
            self.assertGreater(res["freed_bytes"], 2000)
            self.assertFalse((m_dir / "events" / "2026-11-17").exists())
            self.assertTrue((m_dir / "events" / "2026-11-20").exists())
            self.assertTrue((m_dir / "events" / "2026-11-22").exists())
            self.assertTrue((m_dir / "settlements" / "2026-11-10" / "s.jsonl").exists())
            self.assertFalse(old_gz.exists())
            self.assertTrue(new_gz.exists())
            self.assertFalse(old_log.exists())
            self.assertTrue(summary_f.exists(), "summary.jsonl must NEVER be deleted")
            self.assertTrue(live_f.exists(), "live.json must NEVER be deleted")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_unified_dashboard_history_daily_summary_and_basic_auth_guard(self):
        import os
        import json
        import base64
        import tempfile
        import shutil
        import urllib.request
        from unittest.mock import patch
        from http.server import ThreadingHTTPServer
        import threading
        import config
        from web.server import _compute_daily_summary, _check_basic_auth, QuantHandler

        tmp = Path(tempfile.mkdtemp(prefix="pm_web_"))
        try:
            rows = [
                {"slug": "btc-updown-5m-1795200000", "start": 1795200000, "end": 1795200300, "winner": "Up", "winner_source": "gamma", "pos_up": 5.0, "pos_down": 0.0, "total_cost": 2.75, "proceeds": 5.0, "net_pnl": 2.25, "fills_count": 1, "traded": True},
                {"slug": "btc-updown-5m-1795200300", "start": 1795200300, "end": 1795200600, "winner": "Down", "winner_source": "twap", "pos_up": 5.0, "pos_down": 0.0, "total_cost": 2.60, "proceeds": 0.0, "net_pnl": -2.60, "fills_count": 1, "traded": True},
            ]
            _, _, daily = _compute_daily_summary(rows)
            self.assertEqual(len(daily), 1)
            self.assertEqual(daily[0]["traded"], 2)
            self.assertEqual(daily[0]["wins"], 1)
            self.assertEqual(daily[0]["losses"], 1)
            self.assertAlmostEqual(daily[0]["pnl"], -0.35, places=4)

            # Basic Auth checks
            with patch.dict(os.environ, {"PM_WEB_PASS": "", "PM_WEB_USER": "admin"}):
                self.assertTrue(_check_basic_auth(""))
            with patch.dict(os.environ, {"PM_WEB_PASS": "secret123", "PM_WEB_USER": "trader"}):
                self.assertFalse(_check_basic_auth(""))
                bad_tok = base64.b64encode(b"trader:wrong").decode()
                ok_tok = base64.b64encode(b"trader:secret123").decode()
                self.assertFalse(_check_basic_auth(f"Basic {bad_tok}"))
                self.assertTrue(_check_basic_auth(f"Basic {ok_tok}"))

            # Live HTTP check for unified /, /m redirect -> /, /api/system, /api/history
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), QuantHandler)
            port = httpd.server_address[1]
            th = threading.Thread(target=httpd.serve_forever, daemon=True)
            th.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as resp:
                    html = resp.read().decode("utf-8")
                    self.assertEqual(resp.status, 200)
                    self.assertIn("PM-Quant", html)
                    self.assertIn("server-health-strip", html)
                    self.assertIn("settings-modal", html)
                # /m seamlessly redirects to the single unified responsive dashboard /
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/m", timeout=3) as resp:
                    html = resp.read().decode("utf-8")
                    self.assertEqual(resp.status, 200)
                    self.assertIn("server-health-strip", html)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/system", timeout=3) as resp:
                    sys_data = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(resp.status, 200)
                    self.assertIn("service_status", sys_data)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/history", timeout=3) as resp:
                    hist_data = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(resp.status, 200)
                    self.assertIn("today_pnl", hist_data)
                    self.assertIn("daily_summary", hist_data)
            finally:
                httpd.shutdown()
                httpd.server_close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_disk_capacity_watermark_evicts_oldest_and_pauses_raw_recording_when_critical(self):
        import tempfile
        import shutil
        import datetime
        from collections import namedtuple
        from runtime.retention import cleanup_old_data, is_raw_recording_allowed

        Usage = namedtuple("Usage", ["total", "used", "free"])
        tmp = Path(tempfile.mkdtemp(prefix="pm_cap_"))
        try:
            now_dt = datetime.datetime(2026, 11, 22, 12, 0, 0, tzinfo=datetime.timezone.utc)
            now_ts = now_dt.timestamp()
            m_dir = tmp / "1_market"
            l_dir = tmp / "2_ledgers"
            g_dir = tmp / "4_runtime" / "logs"

            # All within 3-day normal retention (1-2 days old), plus today
            for d_str in ("2026-11-20", "2026-11-21", "2026-11-22"):
                d_path = m_dir / "events" / d_str
                d_path.mkdir(parents=True)
                (d_path / "ev.jsonl").write_bytes(b"a" * 20000)

            win_dir = l_dir / "v2.8" / "windows"
            win_dir.mkdir(parents=True)
            summary_f = l_dir / "v2.8" / "summary.jsonl"
            summary_f.write_text('{"slug":"keep","net_pnl":1.0,"traded":true}\n', encoding="utf-8")
            # 35 recent replay gz files (all within 7 days)
            for i in range(35):
                ep = int(now_ts - (35 - i) * 300)
                (win_dir / f"btc-updown-5m-{ep}.json.gz").write_bytes(b"g" * 5000)
            g_dir.mkdir(parents=True)

            # 1. Simulate high (non-critical) disk pressure (88.3% used, 3.5 GB free on 30 GB disk -> >= 85% hard limit, < 93% critical)
            gb = 1024 ** 3
            high_usage = lambda _: Usage(total=30 * gb, used=int(26.5 * gb), free=int(3.5 * gb))
            res = cleanup_old_data(
                market_dir=m_dir, ledgers_dir=l_dir, logs_dir=g_dir,
                market_days=3, replay_days=7, log_days=7, now=now_ts,
                disk_usage_fn=high_usage, update_guard_state=False,
            )
            self.assertTrue(res["pressure_triggered"])
            # Non-today market dirs evicted, today preserved
            self.assertFalse((m_dir / "events" / "2026-11-20").exists())
            self.assertFalse((m_dir / "events" / "2026-11-21").exists())
            self.assertTrue((m_dir / "events" / "2026-11-22").exists(), "Today's UTC folder must never be deleted")
            # At least 30 newest replays preserved under non-critical pressure, summary untouched
            remaining_gz = list(win_dir.glob("*.json.gz"))
            self.assertEqual(len(remaining_gz), 30)
            self.assertTrue(summary_f.exists())
            self.assertFalse(res["disk_guard_paused"])

            # 2. Simulate critical disk full (96% used, 0.3 GB free) -> auto-pauses raw recording
            crit_usage = lambda _: Usage(total=30 * gb, used=int(29.7 * gb), free=int(0.3 * gb))
            res_crit = cleanup_old_data(
                market_dir=m_dir, ledgers_dir=l_dir, logs_dir=g_dir,
                market_days=3, replay_days=7, log_days=7, now=now_ts,
                disk_usage_fn=crit_usage, update_guard_state=True,
            )
            self.assertTrue(res_crit["disk_guard_paused"])
            self.assertFalse(is_raw_recording_allowed())
            self.assertTrue(summary_f.exists(), "summary.jsonl must survive even critical disk-full cleanup")

            # 3. Restore healthy disk -> raw recording resumes automatically
            ok_usage = lambda _: Usage(total=30 * gb, used=10 * gb, free=20 * gb)
            res_ok = cleanup_old_data(
                market_dir=m_dir, ledgers_dir=l_dir, logs_dir=g_dir,
                market_days=3, replay_days=7, log_days=7, now=now_ts,
                disk_usage_fn=ok_usage, update_guard_state=True,
            )
            self.assertFalse(res_ok["disk_guard_paused"])
            self.assertTrue(is_raw_recording_allowed())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
