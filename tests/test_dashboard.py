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


if __name__ == "__main__":
    unittest.main()
