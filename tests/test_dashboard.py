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
        self.assertNotIn("open_px", result)
        self.assertNotIn("decisions", result)
        self.assertNotIn("reason", result)

    def test_active_round_never_changes_settled_result(self):
        result = _public_round(
            {"slug": "live", "traded": False, "net_pnl": 0.0},
            {"actions": []}, active=True,
        )
        self.assertEqual(result["result"], "pending")
        self.assertIsNone(result["net_pnl"])


if __name__ == "__main__":
    unittest.main()
