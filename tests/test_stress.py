"""Tests for the synthetic stress harness: determinism, no crashes, and the declared risk envelope."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from sim.stress import SCENARIOS, MarketModel, _new_runner, run_scenario, build_prefix
import random


class TestStressHarness(unittest.TestCase):
    def test_paths_start_at_open_and_are_deterministic(self):
        for name, meta in SCENARIOS.items():
            a = meta["gen"](random.Random(f"{name}|1")).x
            b = meta["gen"](random.Random(f"{name}|1")).x
            self.assertEqual(len(a), 301, name)
            self.assertEqual(a[0], 0.0, name)
            self.assertEqual(a, b, name)
        self.assertEqual(len(build_prefix(random.Random(1))), 60)

    def test_every_scenario_runs_without_crash_and_respects_loss_envelope(self):
        envelope = config.PARAMS["MAX_WINDOW_LOSS_USD"]
        with tempfile.TemporaryDirectory() as tmp:
            runner = _new_runner(tmp)
            for name in SCENARIOS:
                s = run_scenario(name, 6, 11, MarketModel(), runner)
                self.assertEqual(s["errors"], 0, f"{name}: {s.get('traceback')}")
                self.assertGreater(s["worst"], -envelope, f"{name} breached per-window loss envelope")

    def test_one_sided_book_never_raises(self):
        """Liquidity-evaporation regime (loser has no bids, winner no asks) must not crash the policy."""
        mm = MarketModel(evap_p_2m=1.0, evap_p_1m=1.0)
        with tempfile.TemporaryDirectory() as tmp:
            runner = _new_runner(tmp)
            s = run_scenario("flash_perm", 10, 5, mm, runner)
            self.assertEqual(s["errors"], 0, s.get("traceback"))


if __name__ == "__main__":
    unittest.main()
