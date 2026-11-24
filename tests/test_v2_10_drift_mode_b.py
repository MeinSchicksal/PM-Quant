"""Unit tests for v2.10 V1b Mode B (Skewed-Drift High-Certainty Taker IOC)."""
import unittest

import config
from core.policy import plan_decision
from core.types import OrderBook, Position, SpotState


def _book(bid: float, ask: float, depth: float = 50.0) -> OrderBook:
    return OrderBook(
        bids=[(bid, depth)],
        asks=[(ask, depth)],
        last_trade_price=ask,
        source_t=1000.0,
        received_at=1000.0,
    )


def _spot(px: float = 61350.0, open_px: float = 61250.0, vel_bps: float = 0.0, vol_bps: float = 15.0) -> SpotState:
    return SpotState(
        timestamp=1000.0,
        price=px,
        open_price=open_px,
        realized_vol_bps=vol_bps,
        velocity_bps=vel_bps,
        is_toxic_shock=(abs(vel_bps) >= 3.5),
    )


class TestV210DriftModeB(unittest.TestCase):
    def setUp(self):
        self.params = dict(config.PARAMS)
        self.params.update(
            ENABLE_DRIFT_MODE_B=True,
            AVAILABLE_CAPITAL=100.0,
            COMMITTED_WINDOW_COST=0.0,
            BASIS_READY=True,
            REQUIRE_STRIKE_AND_BASIS=True,
        )

    def test_v1b_defaults_disabled_in_config(self):
        self.assertFalse(config.PARAMS["ENABLE_DRIFT_MODE_B"])
        self.assertAlmostEqual(config.PARAMS["DRIFT_MODE_B_MIN_PRICE"], 0.75)
        self.assertAlmostEqual(config.PARAMS["DRIFT_MODE_B_MAX_PRICE"], 0.88)
        self.assertAlmostEqual(config.PARAMS["DRIFT_MODE_B_MIN_PROB"], 0.80)
        self.assertAlmostEqual(config.PARAMS["DRIFT_MODE_B_MIN_EDGE"], 0.05)
        self.assertTrue(config.PARAMS["DRIFT_MODE_B_DISABLE_PYRAMID"])

    def test_mode_b_fires_in_skewed_drift_high_certainty_zone(self):
        # Mode A rejects ask=$0.82 (> ENTRY_MAX_PRICE=0.80 & high price_edge_slope).
        # Mode B triggers clean single-lot IOC when P*=0.90, ask=$0.82 (net edge ~+0.070 >= 0.05).
        dec = plan_decision(
            pos_up=Position(),
            pos_down=Position(),
            book_up=_book(0.81, 0.82),
            book_down=_book(0.18, 0.19),
            spot=_spot(),
            tau_s=120.0,
            params=self.params,
            p_fair=0.90,
            entries_count=0,
        )
        self.assertEqual(dec.action, "taker_buy")
        self.assertEqual(dec.side, "Up")
        self.assertAlmostEqual(dec.price, 0.82, places=2)
        self.assertAlmostEqual(dec.size, 5.0, places=2)
        self.assertIn("模式B", dec.reason)
        self.assertLessEqual(dec.limit_price, 0.88)

    def test_mode_a_takes_strict_precedence_in_range_zone(self):
        # Normal balanced odds ($0.52) must always be handled by Mode A, never Mode B.
        dec = plan_decision(
            pos_up=Position(),
            pos_down=Position(),
            book_up=_book(0.51, 0.52),
            book_down=_book(0.48, 0.49),
            spot=_spot(),
            tau_s=150.0,
            params=self.params,
            p_fair=0.65,
            entries_count=0,
        )
        self.assertEqual(dec.action, "taker_buy")
        self.assertNotIn("模式B", dec.reason)

    def test_mode_b_blocks_locked_above_88c_and_cheap_countertrend(self):
        # Above $0.88 -> Mode C wait
        dec_hi = plan_decision(
            pos_up=Position(),
            pos_down=Position(),
            book_up=_book(0.89, 0.90),
            book_down=_book(0.10, 0.11),
            spot=_spot(),
            tau_s=100.0,
            params=self.params,
            p_fair=0.98,
            entries_count=0,
        )
        self.assertEqual(dec_hi.action, "wait")

        # Cheap counter-trend side ($0.19 with P*=0.10) must never trigger Mode B
        dec_lo = plan_decision(
            pos_up=Position(),
            pos_down=Position(),
            book_up=_book(0.81, 0.82),
            book_down=_book(0.18, 0.19),
            spot=_spot(),
            tau_s=100.0,
            params=self.params,
            p_fair=0.84,  # Up edge only ~+0.01 < 0.05; Down P*=0.16 < 0.80
            entries_count=0,
        )
        self.assertEqual(dec_lo.action, "wait")

    def test_mode_b_position_forbids_pyramid_add(self):
        # Already holding 5 shares of Up bought via Mode B at $0.82 -> pyramid add MUST be blocked
        p = dict(self.params, ENABLE_PYRAMID_ADD=True, SECONDS_SINCE_LAST_ENTRY=30.0)
        dec = plan_decision(
            pos_up=Position(shares=5.0, cash=4.10),
            pos_down=Position(),
            book_up=_book(0.80, 0.81),
            book_down=_book(0.19, 0.20),
            spot=_spot(),
            tau_s=100.0,
            params=p,
            p_fair=0.95,
            entries_count=1,
        )
        self.assertEqual(dec.action, "wait")

    def test_mode_b_blocked_during_spot_shock_or_wide_spread(self):
        dec_shock = plan_decision(
            pos_up=Position(),
            pos_down=Position(),
            book_up=_book(0.81, 0.82),
            book_down=_book(0.18, 0.19),
            spot=_spot(vel_bps=-4.0),
            tau_s=100.0,
            params=self.params,
            p_fair=0.92,
            entries_count=0,
        )
        self.assertEqual(dec_shock.action, "wait")

        dec_spread = plan_decision(
            pos_up=Position(),
            pos_down=Position(),
            book_up=_book(0.74, 0.82),  # spread 0.08 > 0.06
            book_down=_book(0.18, 0.26),
            spot=_spot(),
            tau_s=100.0,
            params=self.params,
            p_fair=0.92,
            entries_count=0,
        )
        self.assertEqual(dec_spread.action, "wait")


if __name__ == "__main__":
    unittest.main()
