"""Unit tests for v3.4 Volatility-Adaptive Physics, Anomaly/Desync Guards & Extreme Take-Profit."""
import tempfile
import unittest

import config
from core.policy import plan_decision, required_entry_edge, _effective_adverse_spot_thresh
from core.types import OrderBook, Position, SpotState
from runtime.runner import PaperRunner


def _book(bid: float, ask: float, depth: float = 50.0) -> OrderBook:
    return OrderBook(
        bids=[(bid, depth), (round(max(0.01, bid - 0.01), 2), depth)],
        asks=[(ask, depth), (round(min(0.99, ask + 0.01), 2), depth)],
        source_t=1000.0,
        received_at=1000.0,
    )


def _spot(price: float = 75000.0, open_px: float = 75000.0, vel: float = 0.0, vol: float = 15.0) -> SpotState:
    return SpotState(
        timestamp=1000.0,
        price=price,
        open_price=open_px,
        realized_vol_bps=vol,
        velocity_bps=vel,
        is_toxic_shock=False,
        is_stale=False,
    )


class TestV34AdaptiveAndGuards(unittest.TestCase):
    def test_vol_adaptive_required_edge_zero_in_calm_smooth_in_storm(self):
        p = dict(config.PARAMS)
        # Calm <= 25 bps: identical to base edge
        self.assertAlmostEqual(required_entry_edge(0.50, 150.0, 0.035, p, vol_bps=14.0), 0.035, places=6)
        self.assertAlmostEqual(required_entry_edge(0.50, 150.0, 0.035, p, vol_bps=25.0), 0.035, places=6)
        # Storm 50 bps: +25 * 0.0008 = +0.020 -> 0.055
        self.assertAlmostEqual(required_entry_edge(0.50, 150.0, 0.035, p, vol_bps=50.0), 0.055, places=6)
        # Extreme 90 bps: capped at +0.030 -> 0.065
        self.assertAlmostEqual(required_entry_edge(0.50, 150.0, 0.035, p, vol_bps=90.0), 0.065, places=6)

    def test_high_vol_pauses_maker_urgently_but_allows_thick_taker(self):
        p = dict(config.PARAMS, AVAILABLE_CAPITAL=100.0, COMMITTED_WINDOW_COST=0.0)
        # Wide spread where Taker edge (0.60 - 0.56 - 0.017 = 0.023) fails vol-adjusted threshold,
        # while Maker bid ($0.51) would pass in calm vol, but vol=42 > MAKER_MAX_VOL_BPS=35 blocks Maker urgently!
        d_block = plan_decision(
            Position(), Position(), _book(0.50, 0.56), _book(0.44, 0.50),
            _spot(vel=0.0, vol=42.0), tau_s=150.0, params=p, p_fair=0.60,
        )
        self.assertEqual(d_block.action, "wait")
        self.assertTrue(d_block.urgent_cancel)
        self.assertIn("高波动暂停被动 Maker", d_block.reason)

        # In the same 42 bps storm, a thick Taker IOC (edge ~0.10 >= 0.035 + 0.0136 = 0.0486) is still allowed
        d_take = plan_decision(
            Position(), Position(), _book(0.50, 0.52), _book(0.48, 0.50),
            _spot(vel=0.0, vol=42.0), tau_s=150.0, params=p, p_fair=0.64,
        )
        self.assertEqual(d_take.action, "taker_buy")
        self.assertEqual(d_take.side, "Up")

    def test_dimensionless_adverse_spot_threshold_scales_with_vol(self):
        p = dict(config.PARAMS)
        # Calm 15 bps -> floor stays tight at 1.5 bps
        self.assertAlmostEqual(_effective_adverse_spot_thresh(15.0, p), 1.5, places=3)
        # Volatile 45 bps -> 1.05 * 45 * sqrt(2.5/300) = 4.31 bps (doesn't false-block 2.0 bps noise)
        th45 = _effective_adverse_spot_thresh(45.0, p)
        self.assertTrue(4.0 < th45 <= 4.5)
        # Extreme 70 bps -> capped at 4.5 bps so real crashes still trigger block
        self.assertAlmostEqual(_effective_adverse_spot_thresh(70.0, p), 4.5, places=3)

    def test_persistent_extreme_divergence_allows_burst_blocks_sustained(self):
        p = dict(config.PARAMS, AVAILABLE_CAPITAL=100.0, COMMITTED_WINDOW_COST=0.0)
        bu, bd = _book(0.49, 0.50), _book(0.50, 0.51)

        # 1) 18% real lag alpha (<= 24%) allowed even if sustained 10s
        d_ok_lag = plan_decision(
            Position(), Position(), bu, bd, _spot(vol=15.0), tau_s=150.0,
            params=dict(p, EXTREME_DIV_UP_S=10.0), p_fair=0.68,
        )
        self.assertEqual(d_ok_lag.action, "taker_buy")

        # 2) 28% extreme divergence for 1.5s (< 3.0s) -> genuine rapid spot breakout, ALLOWED
        d_burst = plan_decision(
            Position(), Position(), bu, bd, _spot(vol=15.0), tau_s=150.0,
            params=dict(p, EXTREME_DIV_UP_S=1.5), p_fair=0.78,
        )
        self.assertEqual(d_burst.action, "taker_buy")

        # 3) 28% extreme divergence persisting >= 3.0s -> anomalous/desync, BLOCKED urgently
        d_anom = plan_decision(
            Position(), Position(), bu, bd, _spot(vol=15.0), tau_s=150.0,
            params=dict(p, EXTREME_DIV_UP_S=3.5), p_fair=0.78,
        )
        self.assertEqual(d_anom.action, "wait")
        self.assertTrue(d_anom.urgent_cancel)
        self.assertIn("持续极端分歧", d_anom.reason)

    def test_basis_anomaly_pauses_entries_and_recovers_with_hysteresis(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = PaperRunner(ledger_dir=tmp)
            t0 = 1800000000.0
            runner.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0, binance_open_px=65000.0)
            # Warm up basis EMA cleanly around $0
            runner._basis_ema = 0.0
            runner._basis_sample_count = 15
            runner._basis_updated_t = t0 + 60.0
            runner._spot_buf = [(t0 + i, 65000.0) for i in range(0, 62)]

            bu, bd = _book(0.49, 0.51), _book(0.49, 0.51)
            # Sudden $120 basis desync (poly_spot jumps down to 64880 while spot 60s avg = 65000)
            st_jump = _spot(price=65000.0, open_px=65000.0, vol=15.0)
            runner.step(t0 + 65.0, st_jump, bu, bd, poly_spot=64880.0, poly_spot_ts=t0 + 65.0, replay_mode=True)
            self.assertTrue(runner._basis_anomaly_active)
            self.assertIn("基差瞬变脱锚", runner.decisions[-1]["reason"])

            # Partial convergence ($65 > $50 recover line) stays paused
            runner.step(t0 + 67.0, st_jump, bu, bd, poly_spot=64935.0, poly_spot_ts=t0 + 67.0, replay_mode=True)
            self.assertTrue(runner._basis_anomaly_active)

            # Converged within <= $50 for < 5s stays paused, then clears at >= 5s
            runner.step(t0 + 69.0, st_jump, bu, bd, poly_spot=64990.0, poly_spot_ts=t0 + 69.0, replay_mode=True)
            self.assertTrue(runner._basis_anomaly_active)
            runner.step(t0 + 75.0, st_jump, bu, bd, poly_spot=64990.0, poly_spot_ts=t0 + 75.0, replay_mode=True)
            self.assertFalse(runner._basis_anomaly_active)

    def test_extreme_take_profit_policy_rules_and_runner_no_reentry(self):
        p_tp = dict(
            config.PARAMS,
            ENABLE_EXTREME_TAKE_PROFIT=True,
            TAKE_PROFIT_MIN_BID=0.94,
            TAKE_PROFIT_MIN_TAU_S=30.0,
            TAKE_PROFIT_MIN_PROB=0.90,
            AVAILABLE_CAPITAL=100.0,
            COMMITTED_WINDOW_COST=5.20,
        )
        pos_up = Position(10.0, 5.20)  # 10 shares @ $0.52

        # 1) Full 10-share bid depth at >= $0.94, tau=90s >= 30s, P*=0.95 -> triggers taker_unwind even under CB!
        bu_rich = OrderBook(bids=[(0.96, 5.0), (0.94, 10.0)], asks=[(0.97, 20.0)], source_t=1000.0, received_at=1000.0)
        bd_rich = _book(0.03, 0.04)
        d_tp = plan_decision(
            pos_up, Position(), bu_rich, bd_rich, _spot(vol=15.0), tau_s=90.0,
            params=p_tp, circuit_breaker_active=True, p_fair=0.95, entries_count=2,
        )
        self.assertEqual(d_tp.action, "taker_unwind")
        self.assertEqual(d_tp.side, "Up")
        self.assertEqual(d_tp.size, 10.0)
        self.assertAlmostEqual(d_tp.limit_price, 0.94, places=2)
        self.assertEqual(d_tp.intent, "take_profit_unwind")

        # 2) Thin bid above $0.94 (only 4 shares at $0.95, rest at $0.90) -> must NOT unwind
        bu_thin = OrderBook(bids=[(0.95, 4.0), (0.90, 50.0)], asks=[(0.96, 20.0)], source_t=1000.0, received_at=1000.0)
        d_thin = plan_decision(
            pos_up, Position(), bu_thin, bd_rich, _spot(vol=15.0), tau_s=90.0,
            params=p_tp, p_fair=0.95, entries_count=2,
        )
        self.assertEqual(d_thin.action, "wait")

        # 3) Final 30s (tau=20s < 30s) -> hold to settlement instead of paying sell spread
        d_late = plan_decision(
            pos_up, Position(), bu_rich, bd_rich, _spot(vol=15.0), tau_s=20.0,
            params=p_tp, p_fair=0.95, entries_count=2,
        )
        self.assertEqual(d_late.action, "wait")

        # 4) Once WINDOW_UNWOUND=True, flat account must refuse same-window re-entry
        d_locked = plan_decision(
            Position(), Position(), _book(0.50, 0.52), _book(0.48, 0.50),
            _spot(vol=15.0), tau_s=80.0,
            params=dict(p_tp, WINDOW_UNWOUND=True, COMMITTED_WINDOW_COST=0.0),
            p_fair=0.65, entries_count=1,
        )
        self.assertEqual(d_locked.action, "wait")
        self.assertIn("极端浮盈落袋", d_locked.reason)

        # 5) End-to-end PaperRunner accounting + crash resume persistence
        with tempfile.TemporaryDirectory() as tmp:
            r1 = PaperRunner(ledger_dir=tmp)
            t0 = 1800000000.0
            r1.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0, binance_open_px=65000.0)
            r1.pos_up = Position(10.0, 5.20)
            r1.entries_count = 2
            r1._apply_taker_outcome(
                fill_ts=t0 + 180.0, now=t0 + 180.0, action=d_tp, want=10.0,
                is_buy=False, filled=10.0, vwap=0.95, reject_reason=None,
            )
            self.assertTrue(r1.window_unwound)
            self.assertAlmostEqual(r1.pos_up.shares, 0.0, places=6)
            r1._write_live(t0 + 181.0, _spot(price=65200.0, open_px=65000.0), bu_rich, bd_rich)
            import json as _json
            live_snap = _json.loads(r1.live_file.read_text(encoding="utf-8"))
            # Regression: profitable unwind must NEVER produce negative committed_cost in live.json
            self.assertGreaterEqual(live_snap["committed_cost"], 0.0)
            self.assertAlmostEqual(live_snap["committed_cost"], 0.0, places=4)
            self.assertGreater(live_snap["available_capital"], 104.0)

            # Crash-resume restores window_unwound and exact unwind accounting
            r2 = PaperRunner(ledger_dir=tmp)
            r2.start_window("btc-updown-5m-1800000000", t0, t0 + 300.0, 65000.0, resume_from_live=True)
            self.assertTrue(r2.window_unwound)
            r2.close_window(64800.0, official_winner="Down")  # Even if market later reverses to Down!
            # Realized profit locked in: 10 * 0.95 - fee(~0.0332) - $5.20 cost ≈ +$4.2668
            self.assertGreater(r2.total_pnl, 4.20)

    def test_audit_regressions_crossed_book_unwind_and_partial_unwind_no_pyramid(self):
        p_tp = dict(
            config.PARAMS,
            ENABLE_EXTREME_TAKE_PROFIT=True,
            TAKE_PROFIT_MIN_BID=0.94,
            TAKE_PROFIT_MIN_TAU_S=30.0,
            TAKE_PROFIT_MIN_PROB=0.90,
            AVAILABLE_CAPITAL=100.0,
            COMMITTED_WINDOW_COST=3.00,
            SECONDS_SINCE_LAST_ENTRY=20.0,
            ENTRY_SIDE_PROB=0.65,
        )
        # 1) Crossed / inconsistent book (bid=0.96 >= ask=0.95) must NEVER trigger bogus unwind
        bu_crossed = OrderBook(bids=[(0.96, 50.0)], asks=[(0.95, 50.0)], source_t=1000.0, received_at=1000.0)
        bd_ok = _book(0.03, 0.04)
        d_cross = plan_decision(
            Position(5.0, 2.60), Position(), bu_crossed, bd_ok, _spot(vol=15.0), tau_s=90.0,
            params=p_tp, p_fair=0.95, entries_count=1,
        )
        self.assertEqual(d_cross.action, "wait")

        # 2) Partial unwind leaves 6 shares (>= 5) with WINDOW_UNWOUND=True:
        #    Must block pyramid_add when bid drops to $0.60, yet still allow finishing unwind at >= $0.94
        pos_rem = Position(6.0, 3.12)
        d_no_add = plan_decision(
            pos_rem, Position(), _book(0.59, 0.60), _book(0.40, 0.41), _spot(vol=15.0), tau_s=120.0,
            params=dict(p_tp, WINDOW_UNWOUND=True), p_fair=0.72, entries_count=1,
        )
        self.assertEqual(d_no_add.action, "wait")

        bu_rich = OrderBook(bids=[(0.95, 20.0)], asks=[(0.96, 20.0)], source_t=1000.0, received_at=1000.0)
        d_finish = plan_decision(
            pos_rem, Position(), bu_rich, bd_ok, _spot(vol=15.0), tau_s=90.0,
            params=dict(p_tp, WINDOW_UNWOUND=True), p_fair=0.95, entries_count=1,
        )
        self.assertEqual(d_finish.action, "taker_unwind")
        self.assertEqual(d_finish.size, 6.0)

        # 3) Dead opposite book (opp asks/bids completely empty at $0.98) must NOT block selling winning leg
        bu_98 = OrderBook(bids=[(0.98, 50.0)], asks=[], source_t=1000.0, received_at=1000.0)
        bd_empty = OrderBook(bids=[], asks=[], source_t=1000.0, received_at=1000.0)
        d_dead_opp = plan_decision(
            Position(5.0, 2.60), Position(), bu_98, bd_empty, _spot(vol=15.0), tau_s=60.0,
            params=p_tp, p_fair=0.98, entries_count=1,
        )
        self.assertEqual(d_dead_opp.action, "taker_unwind")
        self.assertEqual(d_dead_opp.size, 5.0)


if __name__ == "__main__":
    unittest.main()
