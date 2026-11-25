"""Unit tests for v3.3 Multi-Factor Weighted Execution, Paced Exposure & Payoff-Driven Flip."""
import math
import unittest

import config
from core.estimator import student_t5_cdf, normal_cdf
from core.policy import plan_decision, required_entry_edge
from core.types import OrderBook, Position, SpotState


def _book(bid: float, ask: float, depth: float = 50.0) -> OrderBook:
    return OrderBook(
        bids=[(bid, depth), (round(max(0.01, bid - 0.01), 2), depth)],
        asks=[(ask, depth), (round(min(0.99, ask + 0.01), 2), depth)],
        source_t=1000.0,
        received_at=1000.0,
    )


def _spot(vel: float = 0.0, vol: float = 15.0) -> SpotState:
    return SpotState(
        timestamp=1000.0,
        price=75000.0,
        open_price=75000.0,
        realized_vol_bps=vol,
        velocity_bps=vel,
        is_toxic_shock=False,
        is_stale=False,
    )


class TestV33PayoffWeightedEngine(unittest.TestCase):
    def test_unit_variance_t5_fixes_midrange_underconfidence(self):
        raw_p = student_t5_cdf(1.0, unit_variance=False)
        norm_p = student_t5_cdf(1.0, unit_variance=True)
        # Unit-variance t5 lifts mid-range probability above unscaled t5 while keeping fatter extreme tails
        self.assertGreater(norm_p, raw_p)
        self.assertTrue(0.82 < norm_p < 0.90)
        self.assertLess(student_t5_cdf(3.5, unit_variance=True), normal_cdf(3.5))

    def test_overextended_prob_adds_edge_damping(self):
        p = dict(config.PARAMS)
        e_norm = required_entry_edge(0.60, 120.0, 0.038, p, p_side=0.72)
        e_over = required_entry_edge(0.60, 120.0, 0.038, p, p_side=0.90)
        self.assertGreater(e_over, e_norm + 0.01)

    def test_weighted_routing_picks_maker_in_calm_wide_spread_and_taker_in_momentum(self):
        p = dict(config.PARAMS, AVAILABLE_CAPITAL=100.0, COMMITTED_WINDOW_COST=0.0)
        # Wide spread ($0.03), calm spot (0.0 bps), mid/low price, ample time -> Maker wins by weighted score
        bu = _book(0.47, 0.50)
        bd = _book(0.50, 0.53)
        d_calm = plan_decision(
            Position(), Position(), bu, bd, _spot(vel=0.0), tau_s=160.0,
            params=p, p_fair=0.58,
        )
        self.assertEqual(d_calm.action, "quote")
        self.assertEqual(d_calm.side, "Up")

        # Same book & probability, but tailwind spot momentum (+1.6 bps) -> Taker wins by weighted score
        d_mom = plan_decision(
            Position(), Position(), bu, bd, _spot(vel=1.6), tau_s=160.0,
            params=p, p_fair=0.58,
        )
        self.assertEqual(d_mom.action, "taker_buy")
        self.assertEqual(d_mom.side, "Up")

    def test_low_price_value_mispricing_entry_in_contested_zone(self):
        p = dict(config.PARAMS, AVAILABLE_CAPITAL=100.0, COMMITTED_WINDOW_COST=0.0)
        # Contested P*(Up)=0.49 (below 0.53), but panicked Up ask=$0.38 -> strong payoff asymmetry
        bu = _book(0.37, 0.38)
        bd = _book(0.62, 0.63)
        d = plan_decision(
            Position(), Position(), bu, bd, _spot(vel=0.9), tau_s=150.0,
            params=p, p_fair=0.49,
        )
        self.assertIn(d.action, ("taker_buy", "quote"))
        self.assertEqual(d.side, "Up")
        self.assertIn("低价赔率错杀", d.reason)

    def test_early_phase_blocks_add_and_late_phase_blocks_new_entry(self):
        p = dict(
            config.PARAMS,
            AVAILABLE_CAPITAL=100.0,
            COMMITTED_WINDOW_COST=2.60,
            HELD_LEG_COST=2.60,
            ENTRY_SIDE_PROB=0.60,
            SECONDS_SINCE_LAST_ENTRY=30.0,
        )
        bu = _book(0.50, 0.51)
        bd = _book(0.49, 0.50)

        # Early phase (tau=220s > 180s): must NOT add to 10 shares
        d_early = plan_decision(
            Position(5.0, 2.60), Position(), bu, bd, _spot(vel=0.5), tau_s=220.0,
            params=p, p_fair=0.65, entries_count=1,
        )
        self.assertEqual(d_early.action, "wait")

        # Late phase (tau=35s < 45s): flat account must NOT open new entry
        d_late = plan_decision(
            Position(), Position(), bu, bd, _spot(vel=0.5), tau_s=35.0,
            params=dict(p, COMMITTED_WINDOW_COST=0.0), p_fair=0.65, entries_count=0,
        )
        self.assertEqual(d_late.action, "wait")
        self.assertIn("尾盘收官期", d_late.reason)

    def test_quality_add_protects_average_and_blocks_reversing_prob(self):
        p = dict(
            config.PARAMS,
            AVAILABLE_CAPITAL=100.0,
            COMMITTED_WINDOW_COST=2.50,
            HELD_LEG_COST=2.50,
            ENTRY_SIDE_PROB=0.66,
            SECONDS_SINCE_LAST_ENTRY=20.0,
        )
        pos_up = Position(5.0, 2.50)  # avg $0.50

        # 1) Chasing higher price ($0.58 > avg $0.50 + $0.05) -> blocked
        d_chase = plan_decision(
            pos_up, Position(), _book(0.57, 0.58), _book(0.42, 0.43),
            _spot(vel=0.5), tau_s=130.0, params=p, p_fair=0.70, entries_count=1,
        )
        self.assertEqual(d_chase.action, "wait")

        # 2) Cheap price ($0.48), but prob deteriorated from 0.66 to 0.57 (drop 0.09 > 0.06) -> blocked
        d_rev = plan_decision(
            pos_up, Position(), _book(0.47, 0.48), _book(0.52, 0.53),
            _spot(vel=0.0), tau_s=130.0, params=p, p_fair=0.57, entries_count=1,
        )
        self.assertEqual(d_rev.action, "wait")

        # 3) Better/similar price ($0.49 <= $0.55) AND prob healthy (0.64, drop 0.02 <= 0.06) -> allowed
        d_good = plan_decision(
            pos_up, Position(), _book(0.48, 0.49), _book(0.51, 0.52),
            _spot(vel=0.2), tau_s=130.0, params=p, p_fair=0.64, entries_count=1,
        )
        self.assertEqual(d_good.action, "taker_buy")
        self.assertEqual(d_good.side, "Up")
        self.assertEqual(d_good.size, 5.0)

    def test_payoff_driven_flip_sizes_integer_shares_and_caps_martingale(self):
        # Held 5 Up costing $2.60; reversal confirmed with Down ask=$0.55, P*(Down)=0.65
        p = dict(
            config.PARAMS,
            AVAILABLE_CAPITAL=100.0,
            COMMITTED_WINDOW_COST=2.60,
            HELD_LEG_COST=2.60,
            SECONDS_SINCE_LAST_ENTRY=20.0,
            FLIPS_USED=0,
        )
        d_flip = plan_decision(
            Position(5.0, 2.60), Position(), _book(0.44, 0.45), _book(0.54, 0.55),
            _spot(vel=-0.5), tau_s=110.0, params=p, p_fair=0.35, entries_count=1,
        )
        self.assertEqual(d_flip.action, "taker_buy")
        self.assertEqual(d_flip.side, "Down")
        # fee(0.55)=0.017325 -> unit_profit=0.432675 -> ceil((2.60+0.30)/0.432675) = 7 shares!
        self.assertEqual(d_flip.size, 7.0)
        self.assertIn("终局动态不对劲反买", d_flip.reason)

        # Overpriced opposite ask (> FLIP_MAX_PRICE $0.68) must be blocked even if trying to break even
        d_expensive = plan_decision(
            Position(5.0, 2.60), Position(), _book(0.28, 0.30), _book(0.70, 0.72),
            _spot(vel=-0.5), tau_s=110.0, params=p, p_fair=0.18, entries_count=1,
        )
        self.assertEqual(d_expensive.action, "wait")

        # Hard cap: even if held cost is huge ($5.80), flip shares never exceed FLIP_MAX_SHARES and total cost <= $10
        p_big = dict(p, COMMITTED_WINDOW_COST=5.80, HELD_LEG_COST=5.80)
        d_cap = plan_decision(
            Position(10.0, 5.80), Position(), _book(0.44, 0.45), _book(0.54, 0.55),
            _spot(vel=-0.5), tau_s=110.0, params=p_big, p_fair=0.35, entries_count=2,
        )
        self.assertEqual(d_cap.action, "taker_buy")
        self.assertLessEqual(d_cap.size, float(config.PARAMS["FLIP_MAX_SHARES"]))

        # After an integer-share flip (e.g. 5 Up + 8 Down -> net_gap = -3 < 5), must hold to settlement
        # and NEVER fall through to fresh-entry logic to chase another lot!
        p_after = dict(p, HAS_WORKING_ORDERS=False, FLIPS_USED=1, COMMITTED_WINDOW_COST=7.0)
        d_hold = plan_decision(
            Position(5.0, 2.60), Position(8.0, 4.40), _book(0.26, 0.28), _book(0.70, 0.72),
            _spot(vel=-0.5), tau_s=85.0, params=p_after, p_fair=0.15, entries_count=2,
        )
        self.assertEqual(d_hold.action, "wait")
        self.assertIn("持有至结算", d_hold.reason)
        self.assertLessEqual(5.80 + d_cap.size * 0.57, float(config.PARAMS["FLIP_MAX_TOTAL_WINDOW_COST"]) + 0.05)

    def test_flip_shares_steps_down_on_thin_top_level_vwap_budget(self):
        """When L1 ask is thin and sweeping L2 pushes q shares slightly over $10, step down to q-1."""
        book_dn = OrderBook(bids=[(0.46, 50.0)], asks=[(0.48, 5.0), (0.60, 20.0)], source_t=1000.0, received_at=1000.0)
        book_up = OrderBook(bids=[(0.51, 50.0)], asks=[(0.53, 50.0)], source_t=1000.0, received_at=1000.0)
        p = dict(
            config.PARAMS,
            AVAILABLE_CAPITAL=100.0,
            COMMITTED_WINDOW_COST=6.00,
            HELD_LEG_COST=6.00,
            SECONDS_SINCE_LAST_ENTRY=20.0,
            FLIPS_USED=0,
        )
        d = plan_decision(
            Position(10.0, 6.00), Position(), book_up, book_dn,
            _spot(vel=-0.5), tau_s=100.0, params=p, p_fair=0.30, entries_count=2,
        )
        self.assertEqual(d.action, "taker_buy")
        self.assertEqual(d.side, "Down")
        self.assertEqual(d.size, 7.0)

    def test_value_maker_quote_survives_narrow_preferred_side_flip_until_prob_breaks(self):
        """Contested low-price value Maker quote is not cancelled just because opp hits 0.53."""
        from core.execution import plan_order_actions
        from core.types import Decision, Order

        order = Order(
            id=1, client_order_id="cid-val-1", side="Up", price=0.38, size=5.0, remaining=5.0,
            purpose="entry", placed_at=1000.0,
        )
        order.entry_reason = "v3.3 低价赔率错杀·净边际挂单 Maker: Up bid=$0.38 (P*=0.48, 零费边际=+0.100)"
        book_up = _book(0.37, 0.40)
        book_dn = _book(0.60, 0.63)

        # P*(Up)=0.47 >= 0.46 while opp P*(Down)=0.53 triggers preferred_side="Down": keep alive!
        to_cancel_ok, _ = plan_order_actions(
            [order], Decision(action="wait", reason="hold"), now=1002.0,
            preferred_side="Down", book_up=book_up, book_down=book_dn,
            p_up=0.47, min_hold_maker_edge=0.015, value_min_prob=0.46,
        )
        self.assertEqual(to_cancel_ok, [])

        # Once P*(Up)=0.43 < 0.46: must cancel cleanly!
        to_cancel_brk, _ = plan_order_actions(
            [order], Decision(action="wait", reason="hold"), now=1003.0,
            preferred_side="Down", book_up=book_up, book_down=book_dn,
            p_up=0.43, min_hold_maker_edge=0.015, value_min_prob=0.46,
        )
        self.assertEqual(len(to_cancel_brk), 1)
        self.assertIn("低价错杀胜率跌破下限撤单", to_cancel_brk[0].cancel_reason)

    def test_audit_bugfixes_spot_velocity_outage_and_resume_brier_and_close_pending(self):
        """Verify spot velocity >5s outage guard, Brier live resume, and pending_takers cancel on close."""
        import tempfile
        from unittest.mock import MagicMock
        from runtime.market_feed import MarketFeed
        from runtime.runner import PaperRunner
        from core.types import Decision

        # 1) Spot velocity must NOT treat a 30s outage gap as a 2-3s momentum shock
        mf = MarketFeed.__new__(MarketFeed)
        mf.spot_history = [(1000.0, 65000.0)]
        mf._spot_continuity_t = 0.0
        mf._last_spot_currency = "USD"
        mf._last_binance_px = 65000.0
        mf._last_binance_t = 1000.0
        mf._last_spot_source = "coinbase_ws"
        mf._consecutive_spot_fails = 0
        mf._last_vol_calc_t = 0.0
        mf._cached_vol_bps = 14.0
        mf._last_spot_rec_t = 1030.0
        mf._last_spot_rec_px = 65200.0
        mf._net_lock = None
        mf.net_stats = {
            "poly_latency_ms": 0.0, "binance_latency_ms": 0.0, "chainlink_latency_ms": 0.0,
            "downstream_kbps": 0.0, "total_bytes": 0, "bytes_window": 0,
            "last_calc_t": 1030.0, "msg_rate": 0.0, "msgs_window": 0,
        }
        mf.record_raw_event = lambda *a, **kw: None
        st = mf._build_spot_state(65000.0, 1030.0, 65200.0, "coinbase_ws", 10.0, 64)
        self.assertAlmostEqual(st.velocity_bps, 0.0, places=4)
        self.assertFalse(st.is_toxic_shock)

        # 2) Live resume restores Brier snapshots + close_window cancels pending_takers future
        with tempfile.TemporaryDirectory() as tmp:
            r1 = PaperRunner(ledger_dir=tmp)
            r1.start_window("btc-updown-5m-1800000000", 1800000000.0, 1800000300.0, 65000.0)
            r1._mid_p_up = 0.62
            r1._mid_mkt_up = 0.55
            r1._entry_p_up = 0.61
            r1._entry_mkt_up = 0.54
            r1._entry_side_prob = 0.61
            r1._write_live(1800000160.0, _spot(), _book(0.54, 0.56), _book(0.44, 0.46))

            r2 = PaperRunner(ledger_dir=tmp)
            r2.start_window("btc-updown-5m-1800000000", 1800000000.0, 1800000300.0, 65000.0, resume_from_live=True)
            self.assertAlmostEqual(r2._mid_p_up, 0.62)
            self.assertAlmostEqual(r2._mid_mkt_up, 0.55)
            self.assertAlmostEqual(r2._entry_p_up, 0.61)
            self.assertAlmostEqual(r2._entry_mkt_up, 0.54)

            mock_fut = MagicMock()
            r2.pending_takers.append({
                "action": Decision(action="taker_buy", side="Up", price=0.55, size=5.0),
                "want": 5.0,
                "book_future": mock_fut,
            })
            r2.close_window(65100.0, official_winner="Up")
            mock_fut.cancel.assert_called_once()
            self.assertTrue(any(a.get("action") == "taker_reject" for a in r2.actions))


if __name__ == "__main__":
    unittest.main()
