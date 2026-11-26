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

    def test_taker_first_and_calm_wide_spread_maker_fallback(self):
        p = dict(config.PARAMS, AVAILABLE_CAPITAL=100.0, COMMITTED_WINDOW_COST=0.0)
        # Case 1: Taker edge clears post-fee threshold (0.58 - 0.50 - 0.0175 = 0.0625 >= 0.035) -> Taker immediately
        bu_tk = _book(0.47, 0.50)
        bd_tk = _book(0.50, 0.53)
        d_tk = plan_decision(
            Position(), Position(), bu_tk, bd_tk, _spot(vel=0.0), tau_s=160.0,
            params=p, p_fair=0.58,
        )
        self.assertEqual(d_tk.action, "taker_buy")
        self.assertEqual(d_tk.side, "Up")
        self.assertEqual(d_tk.intent, "entry_lead")

        # Case 2: Wide spread where Taker edge is insufficient (0.58 - 0.54 - 0.0174 = 0.0226 < 0.035),
        # but calm Maker bid ($0.50) clears thicker Maker edge (0.58 - 0.50 = 0.08 >= 0.055) -> Maker fallback
        bu_mk = _book(0.49, 0.54)
        bd_mk = _book(0.46, 0.51)
        d_mk = plan_decision(
            Position(), Position(), bu_mk, bd_mk, _spot(vel=0.0), tau_s=160.0,
            params=p, p_fair=0.58,
        )
        self.assertEqual(d_mk.action, "quote")
        self.assertEqual(d_mk.side, "Up")
        self.assertEqual(d_mk.intent, "entry_lead")

        # Case 3: Contested P* < 0.53 (0.49) must NOT catch falling knives even at $0.38
        d_lowp = plan_decision(
            Position(), Position(), _book(0.37, 0.38), _book(0.62, 0.63),
            _spot(vel=0.9), tau_s=150.0, params=p, p_fair=0.49,
        )
        self.assertEqual(d_lowp.action, "wait")

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

        # Early phase (first 60s, tau=260s > 240s): must NOT add to 10 shares
        d_early = plan_decision(
            Position(5.0, 2.60), Position(), bu, bd, _spot(vel=0.5), tau_s=260.0,
            params=p, p_fair=0.65, entries_count=1,
        )
        self.assertEqual(d_early.action, "wait")

        # Mid phase (after 60s, tau=220s <= 240s): trend add is unlocked
        d_mid = plan_decision(
            Position(5.0, 2.60), Position(), bu, bd, _spot(vel=0.5), tau_s=220.0,
            params=p, p_fair=0.65, entries_count=1,
        )
        self.assertEqual(d_mid.action, "taker_buy")

        # Late phase (last 30s, tau=25s < 30s): flat account must NOT open new entry
        d_late = plan_decision(
            Position(), Position(), bu, bd, _spot(vel=0.5), tau_s=25.0,
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

        # 1) Extreme chasing ($0.68 > avg $0.50 + $0.15 = $0.65) -> blocked
        d_chase = plan_decision(
            pos_up, Position(), _book(0.67, 0.68), _book(0.32, 0.33),
            _spot(vel=0.5), tau_s=130.0, params=p, p_fair=0.78, entries_count=1,
        )
        self.assertEqual(d_chase.action, "wait")

        # 2) Cheap price ($0.48), but prob deteriorated from 0.66 to 0.57 (drop 0.09 > 0.06) -> blocked
        d_rev = plan_decision(
            pos_up, Position(), _book(0.47, 0.48), _book(0.52, 0.53),
            _spot(vel=0.0), tau_s=130.0, params=p, p_fair=0.57, entries_count=1,
        )
        self.assertEqual(d_rev.action, "wait")

        # 3) Healthy trend add ($0.58 <= avg $0.50 + $0.15) AND prob healthy (0.70) -> allowed
        d_good = plan_decision(
            pos_up, Position(), _book(0.57, 0.58), _book(0.42, 0.43),
            _spot(vel=0.2), tau_s=130.0, params=p, p_fair=0.70, entries_count=1,
        )
        self.assertEqual(d_good.action, "taker_buy")
        self.assertEqual(d_good.side, "Up")
        self.assertEqual(d_good.size, 5.0)

    def test_payoff_driven_flip_sizes_integer_shares_and_enforces_breakeven(self):
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
        # fee(0.55)=0.017325 -> unit_profit=0.432675 -> ceil(2.60/0.432675) = 7 shares!
        self.assertEqual(d_flip.size, 7.0)
        self.assertEqual(d_flip.intent, "reversal_flip")
        self.assertIn("终局动态不对劲反买", d_flip.reason)

        # Overpriced opposite ask (> FLIP_MAX_PRICE $0.68) must be blocked
        d_expensive = plan_decision(
            Position(5.0, 2.60), Position(), _book(0.28, 0.30), _book(0.70, 0.72),
            _spot(vel=-0.5), tau_s=110.0, params=p, p_fair=0.18, entries_count=1,
        )
        self.assertEqual(d_expensive.action, "wait")

        # Breakeven hard guard: if held cost ($6.80) at ask=$0.64 needs ~20 shares (> FLIP_MAX_SHARES=12),
        # refuse half-way lock instead of buying an unrecoverable partial hedge!
        p_unrecoverable = dict(p, COMMITTED_WINDOW_COST=6.80, HELD_LEG_COST=6.80)
        d_no_half = plan_decision(
            Position(10.0, 6.80), Position(), _book(0.35, 0.36), _book(0.63, 0.64),
            _spot(vel=-0.5), tau_s=110.0, params=p_unrecoverable, p_fair=0.25, entries_count=2,
        )
        self.assertEqual(d_no_half.action, "wait")

        # Recoverable 10-share reversal ($5.20 held cost at ask=$0.50 -> needs 11 shares <= 12, total ~$10.9 <= $14)
        p_recov = dict(p, COMMITTED_WINDOW_COST=5.20, HELD_LEG_COST=5.20)
        d_recov = plan_decision(
            Position(10.0, 5.20), Position(), _book(0.49, 0.50), _book(0.49, 0.50),
            _spot(vel=-0.5), tau_s=110.0, params=p_recov, p_fair=0.35, entries_count=2,
        )
        self.assertEqual(d_recov.action, "taker_buy")
        self.assertEqual(d_recov.size, 11.0)
        self.assertLessEqual(5.20 + d_recov.size * 0.52, float(config.PARAMS["FLIP_MAX_TOTAL_WINDOW_COST"]) + 0.05)

        # After an integer-share flip (e.g. 5 Up + 7 Down -> net_gap = -2 < 5), must hold to settlement
        p_after = dict(p, HAS_WORKING_ORDERS=False, FLIPS_USED=1, COMMITTED_WINDOW_COST=6.5)
        d_hold = plan_decision(
            Position(5.0, 2.60), Position(7.0, 3.90), _book(0.26, 0.28), _book(0.70, 0.72),
            _spot(vel=-0.5), tau_s=85.0, params=p_after, p_fair=0.15, entries_count=2,
        )
        self.assertEqual(d_hold.action, "wait")
        self.assertIn("持有至结算", d_hold.reason)

    def test_flip_shares_steps_down_on_thin_top_level_when_breakeven_still_holds(self):
        """When L1 ask is thin and L2 raises VWAP slightly, step down if breakeven holds or block if not."""
        book_dn = OrderBook(bids=[(0.46, 50.0)], asks=[(0.48, 5.0), (0.60, 20.0)], source_t=1000.0, received_at=1000.0)
        book_up = OrderBook(bids=[(0.51, 50.0)], asks=[(0.53, 50.0)], source_t=1000.0, received_at=1000.0)
        p = dict(
            config.PARAMS,
            AVAILABLE_CAPITAL=100.0,
            COMMITTED_WINDOW_COST=3.00,
            HELD_LEG_COST=3.00,
            SECONDS_SINCE_LAST_ENTRY=20.0,
            FLIPS_USED=0,
        )
        d = plan_decision(
            Position(5.0, 3.00), Position(), book_up, book_dn,
            _spot(vel=-0.5), tau_s=100.0, params=p, p_fair=0.30, entries_count=1,
        )
        self.assertEqual(d.action, "taker_buy")
        self.assertEqual(d.side, "Down")
        self.assertGreaterEqual(d.size, 6.0)

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

    def test_alert_proxy_support_and_clean_reversal_cancel(self):
        """Verify objective reversal cancels resting quotes cleanly and alerts respect configured proxy."""
        from unittest.mock import patch
        from core.execution import plan_order_actions
        from core.types import Decision, Order
        import runtime.alerts as alerts

        # 1) Resting Maker quote cancels cleanly when preferred side flips
        order = Order(
            id=2, client_order_id="cid-intent-1", side="Up", price=0.48, size=5.0, remaining=5.0,
            purpose="entry", placed_at=1000.0, intent="entry_lead",
        )
        to_cancel, _ = plan_order_actions(
            [order], Decision(action="wait", reason="hold"), now=1002.0,
            preferred_side="Down", book_up=_book(0.47, 0.50), book_down=_book(0.50, 0.53),
            p_up=0.45, min_hold_maker_edge=0.020,
        )
        self.assertEqual(len(to_cancel), 1)
        self.assertIn("客观方向反转撤单", to_cancel[0].cancel_reason)

        # 2) Alerts use set_alert_proxy() when delivering webhook
        alerts.set_alert_proxy("http://127.0.0.1:7897")
        try:
            with patch.dict("os.environ", {"PM_ALERT_WEBHOOK_URL": "https://example.invalid/hook"}, clear=False):
                with patch("runtime.alerts.requests.post") as mock_post:
                    mock_post.return_value.status_code = 200
                    alerts._send_all("test", "hello", "info")
                    mock_post.assert_called_once()
                    self.assertEqual(
                        mock_post.call_args.kwargs.get("proxies"),
                        {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"},
                    )
        finally:
            alerts.set_alert_proxy(None)

    def test_price_history_compaction_preserves_window_head_and_bounds_size(self):
        from runtime.runner import PaperRunner

        pts = [{"t": round(1000.0 + i * 0.1, 1), "price": 58000.0 + i * 0.1} for i in range(2200)]
        compacted = PaperRunner._compact_price_history(pts, max_points=1500)
        self.assertLessEqual(len(compacted), 1500)
        self.assertEqual(compacted[0]["t"], 1000.0, "Window 00:00 head must never be truncated")
        self.assertEqual(compacted[-1]["t"], pts[-1]["t"], "Latest tick must be preserved")
        # Recent tail remains high resolution (last 200 ticks untouched)
        self.assertEqual([p["t"] for p in compacted[-200:]], [p["t"] for p in pts[-200:]])

    def test_partial_fill_cannot_overshoot_max_net_lots_or_early_probe_cap(self):
        p = dict(
            config.PARAMS,
            AVAILABLE_CAPITAL=100.0,
            COMMITTED_WINDOW_COST=5.50,
            SECONDS_SINCE_LAST_ENTRY=20.0,
            ENTRY_SIDE_PROB=0.60,
            HAS_WORKING_ORDERS=False,
        )
        bu, bd = _book(0.55, 0.56), _book(0.44, 0.45)

        # Holding 9.85 shares: adding 5.0 would hit 14.85 > 10.0 -> MUST BLOCK
        d_over10 = plan_decision(
            Position(9.85, 5.50), Position(), bu, bd, _spot(vol=15.0), tau_s=160.0,
            params=p, p_fair=0.70, entries_count=2,
        )
        self.assertEqual(d_over10.action, "wait")

        # Holding 4.93 shares in first 60s early-probe phase (tau=250s > 240s) -> MUST BLOCK same-side add
        d_early_dup = plan_decision(
            Position(4.93, 2.80), Position(), bu, bd, _spot(vol=15.0), tau_s=250.0,
            params=dict(p, COMMITTED_WINDOW_COST=2.80), p_fair=0.70, entries_count=1,
        )
        self.assertEqual(d_early_dup.action, "wait")

        # Holding 4.93 shares after 60s (tau=160s <= 240s): 4.93 + 5.0 = 9.93 <= 10.0 -> ALLOWED
        d_mid_ok = plan_decision(
            Position(4.93, 2.80), Position(), bu, bd, _spot(vol=15.0), tau_s=160.0,
            params=dict(p, COMMITTED_WINDOW_COST=2.80), p_fair=0.70, entries_count=1,
        )
        self.assertEqual(d_mid_ok.action, "taker_buy")
        self.assertEqual(d_mid_ok.side, "Up")


if __name__ == "__main__":
    unittest.main()
