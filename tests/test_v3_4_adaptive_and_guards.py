"""Unit tests for v3.4 Volatility-Adaptive Physics, Anomaly/Desync Guards & Extreme Take-Profit."""
import shutil
import tempfile
import time
import unittest
from pathlib import Path

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


def _spot(price: float = 75000.0, open_px: float = 75000.0, vel: float = 0.0, vol: float = 15.0, t: float = 1000.0) -> SpotState:
    return SpotState(
        timestamp=t,
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

    def test_partial_fill_cannot_overshoot_max_net_lots_or_early_probe_cap(self):
        """Regression for 02:10 (4.93 -> 14.93sh) and 08:45 (9.85 -> 14.85sh) partial-fill cap overshoots."""
        p = dict(
            config.PARAMS,
            AVAILABLE_CAPITAL=100.0,
            COMMITTED_WINDOW_COST=5.50,
            SECONDS_SINCE_LAST_ENTRY=20.0,
            ENTRY_SIDE_PROB=0.60,
            HAS_WORKING_ORDERS=False,
        )
        bu, bd = _book(0.55, 0.56), _book(0.44, 0.45)

        # 1) Holding 9.85 shares (< 10.0) in mid-phase: adding 5.0 would hit 14.85 > 10.0 -> MUST BLOCK!
        d_over10 = plan_decision(
            Position(9.85, 5.50), Position(), bu, bd, _spot(vol=15.0), tau_s=160.0,
            params=p, p_fair=0.70, entries_count=2,
        )
        self.assertEqual(d_over10.action, "wait")

        # 2) Holding 4.93 shares (< 5.0) in first 60s early-probe phase (tau=250s > 240s):
        #    buying another 5.0 same-side would hit 9.93 > 5.0 early cap -> MUST BLOCK!
        d_early_dup = plan_decision(
            Position(4.93, 2.80), Position(), bu, bd, _spot(vol=15.0), tau_s=250.0,
            params=dict(p, COMMITTED_WINDOW_COST=2.80), p_fair=0.70, entries_count=1,
        )
        self.assertEqual(d_early_dup.action, "wait")

        # 3) Same 4.93 shares after 60s (tau=160s <= 240s): buying 5.0 hits 9.93 <= 10.0 -> ALLOWED!
        d_mid_ok = plan_decision(
            Position(4.93, 2.80), Position(), bu, bd, _spot(vol=15.0), tau_s=160.0,
            params=dict(p, COMMITTED_WINDOW_COST=2.80), p_fair=0.70, entries_count=1,
        )
        self.assertEqual(d_mid_ok.action, "taker_buy")
        self.assertEqual(d_mid_ok.side, "Up")

    def test_audit_p0_post_flip_fallthrough_direction_lock_and_pyramid_discipline(self):
        """P0 #1: After flip leaving |net_gap| < 5, NEVER buy back abandoned side or bypass PYRAMID_* rules."""
        base = dict(
            config.PARAMS,
            AVAILABLE_CAPITAL=100.0,
            COMMITTED_WINDOW_COST=6.50,
            SECONDS_SINCE_LAST_ENTRY=15.0,
            HAS_WORKING_ORDERS=False,
            FLIPS_USED=1,
            LAST_FLIP_SIDE="Down",
            ENTRY_SIDE_PROB=0.65,
        )
        # UP=5 (@0.52), DN=7 (@0.557) -> net_gap = -2 < 5
        pos_u, pos_d = Position(5.0, 2.60), Position(7.0, 3.90)

        # 1) Market reverses back to Up (P*(Up)=0.82 vs ask=$0.55): MUST NOT buy back Up (would be 2nd flip!)
        d_back = plan_decision(
            pos_u, pos_d, _book(0.54, 0.55), _book(0.44, 0.45), _spot(vol=15.0), tau_s=100.0,
            params=base, p_fair=0.82, entries_count=2,
        )
        self.assertEqual(d_back.action, "wait")

        # 2) Early phase (tau=250s > 240s): post-flip add MUST be blocked even on locked side Down
        d_early = plan_decision(
            pos_u, pos_d, _book(0.30, 0.32), _book(0.66, 0.68), _spot(vol=15.0), tau_s=250.0,
            params=base, p_fair=0.20, entries_count=2,
        )
        self.assertEqual(d_early.action, "wait")

        # 3) Over PYRAMID_MAX_ABOVE_AVG (Down avg=0.557, max=0.707, ask=0.73): MUST be blocked
        d_over_avg = plan_decision(
            pos_u, pos_d, _book(0.26, 0.27), _book(0.72, 0.73), _spot(vol=15.0), tau_s=100.0,
            params=base, p_fair=0.15, entries_count=2,
        )
        self.assertEqual(d_over_avg.action, "wait")

        # 4) Maker-only wide spread on Down (ask=0.65 not enough taker edge, bid=0.59 has maker edge):
        #    PYRAMID_ALLOW_MAKER=False MUST block passive Maker post-flip add
        d_no_mk = plan_decision(
            pos_u, pos_d, _book(0.35, 0.41), _book(0.59, 0.65), _spot(vol=15.0), tau_s=100.0,
            params=base, p_fair=0.33, entries_count=2,
        )
        self.assertEqual(d_no_mk.action, "wait")

    def test_audit_p0_flip_resets_active_leg_entry_prob(self):
        """P0 #2: Flipping resets _active_leg_entry_prob so PYRAMID_MAX_PROB_DROP checks the NEW leg."""
        tmp = Path(tempfile.mkdtemp(prefix="pm_v34_flip_prob_"))
        try:
            r = PaperRunner(ledger_dir=tmp)
            r.start_window("btc-updown-5m-1800001800", 1800001800.0, 1800002100.0, 60000.0, 60000.0, 60000.0)
            # First entry Up at P*(Up)=0.72
            r.last_prob_up = 0.72
            r.pos_up.shares += 5.0
            r.pos_up.cash += 2.60
            r._record_fill(1800001830.0, "Up", 0.52, 5.0, "taker", "entry")
            self.assertAlmostEqual(r._entry_side_prob, 0.72, places=4)
            self.assertAlmostEqual(r._active_leg_entry_prob, 0.72, places=4)

            # Reversal flip to Down at P*(Up)=0.38 -> P*(Down)=0.62
            r.last_prob_up = 0.38
            r.pos_down.shares += 10.0
            r.pos_down.cash += 5.00
            r._record_fill(1800001880.0, "Down", 0.50, 10.0, "taker", "entry")
            self.assertEqual(r.flips_count, 1)
            self.assertEqual(r.last_flip_side, "Down")
            # Initial attribution preserved, but active-leg probability anchor reset to Down's 0.62
            self.assertAlmostEqual(r._entry_side_prob, 0.72, places=4)
            self.assertAlmostEqual(r._active_leg_entry_prob, 0.62, places=4)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_audit_p1_basis_anomaly_freezes_ema_no_false_recovery(self):
        """P1 #3: Sustained +$150 basis corruption MUST freeze _basis_ema and never falsely recover."""
        tmp = Path(tempfile.mkdtemp(prefix="pm_v34_basis_freeze_"))
        try:
            r = PaperRunner(ledger_dir=tmp)
            t0 = 1800002100.0
            r.start_window("btc-updown-5m-1800002100", t0, t0 + 300.0, 60000.0, 60000.0, 60000.0)
            bu, bd = _book(0.49, 0.50), _book(0.49, 0.50)
            for i in range(25):
                ts = t0 + i * 0.6
                r.step(ts, _spot(vol=15.0, price=60005.0, t=ts), bu, bd, poly_spot=60000.0, poly_spot_ts=ts, replay_mode=True)
            ema_before = float(r._basis_ema)
            self.assertFalse(r._basis_anomaly_active)

            # Feed 60 consecutive corrupted samples (+$150 jump -> 36s of outage):
            # previously _basis_ema drifted toward the bad feed and auto-recovered falsely!
            for j in range(60):
                ts = t0 + 20.0 + j * 0.6
                r.step(ts, _spot(vol=15.0, price=60005.0, t=ts), bu, bd, poly_spot=59850.0, poly_spot_ts=ts, replay_mode=True)
            self.assertTrue(r._basis_anomaly_active)
            self.assertAlmostEqual(float(r._basis_ema), ema_before, places=4)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_audit_p1_flip_blocked_by_extreme_divergence_and_early_net_cap(self):
        """P1 #4 & #6: Flip respects MAX_PLAUSIBLE_EDGE guard and early-phase 5-share net exposure cap."""
        p = dict(
            config.PARAMS,
            AVAILABLE_CAPITAL=100.0,
            COMMITTED_WINDOW_COST=3.50,
            HELD_LEG_COST=3.50,
            SECONDS_SINCE_LAST_ENTRY=20.0,
            FLIPS_USED=0,
            EXTREME_DIV_DOWN_S=4.5,
        )
        # 1) Sustained extreme divergence on Down (P*(Down)=0.85 vs ask=$0.45, gap=0.40 > 0.24 for 4.5s) -> BLOCK flip!
        d_div = plan_decision(
            Position(5.0, 3.50), Position(), _book(0.54, 0.55), _book(0.44, 0.45),
            _spot(vol=15.0), tau_s=110.0, params=p, p_fair=0.15, entries_count=1,
        )
        self.assertEqual(d_div.action, "wait")

        # 2) Early phase (tau=260s > 240s): 5sh Up @$0.70 ($3.50) reversing at Down ask=$0.66
        #    Breakeven needs 11sh Down -> net_gap would be 6sh > 5sh early cap -> MUST BLOCK!
        p_early = dict(p, EXTREME_DIV_DOWN_S=0.0)
        d_cap = plan_decision(
            Position(5.0, 3.50), Position(), _book(0.33, 0.34), _book(0.65, 0.66),
            _spot(vol=15.0), tau_s=260.0, params=p_early, p_fair=0.20, entries_count=1,
        )
        self.assertEqual(d_cap.action, "wait")

    def test_audit_p2_validate_params_ioc_limit_and_resume_state(self):
        """P2 #7, #8, #9 & P3 #10: Param invariants, IOC sweep cap, crash-resume state, and server unwind accounting."""
        from core.policy import _compute_ioc_buy_limit
        from web.server import _public_round

        # #7 validate_params catches FLIP_MAX_SHARES > MAX_LOTS*LOT_SIZE and PYRAMID_MIN_TAU_S > PYRAMID_MAX_TAU_S
        bad_flip = dict(config.PARAMS, FLIP_MAX_SHARES=25.0)
        with self.assertRaises(ValueError):
            config.validate_params(bad_flip)
        bad_pyr = dict(config.PARAMS, PYRAMID_MIN_TAU_S=250.0, PYRAMID_MAX_TAU_S=200.0)
        with self.assertRaises(ValueError):
            config.validate_params(bad_pyr)

        # #9 _compute_ioc_buy_limit rejects sweep exceeding max_cap_px or leaving no positive EV
        self.assertIsNone(
            _compute_ioc_buy_limit(0.73, 0.80, 0.035, 120.0, 0.07, config.PARAMS, 0.74, min_sweep_px=0.76)
        )
        self.assertIsNone(
            _compute_ioc_buy_limit(0.60, 0.60, 0.035, 120.0, 0.07, config.PARAMS, 0.74, min_sweep_px=0.61)
        )

        # #8 _try_resume_from_live restores basis_anomaly_active, last_flip_side, active_leg_entry_prob, quote_attempts
        tmp = Path(tempfile.mkdtemp(prefix="pm_v34_resume_"))
        try:
            r1 = PaperRunner(ledger_dir=tmp)
            t0 = 1800002400.0
            r1.start_window("btc-updown-5m-1800002400", t0, t0 + 300.0, 60000.0, 60000.0, 60000.0)
            r1._basis_ema = 5.0
            r1._basis_updated_t = t0 + 10.0
            r1._basis_sample_count = 15
            r1._basis_anomaly_active = True
            r1._last_basis_dev_usd = 120.0
            r1.last_flip_side = "Down"
            r1._active_leg_entry_prob = 0.64
            r1.quote_attempts = 3
            sp_live = _spot(vol=15.0, price=60005.0, t=time.time())
            sp_live.spot_source = f"{config.PARAMS.get('SPOT_WS_PROVIDER', 'coinbase')}_ws"
            r1._write_live(time.time(), sp_live, _book(0.49, 0.50), _book(0.49, 0.50))

            r2 = PaperRunner(ledger_dir=tmp)
            r2.start_window("btc-updown-5m-1800002400", t0, t0 + 300.0, 60000.0, 60000.0, 60000.0, resume_from_live=True)
            self.assertTrue(r2._basis_anomaly_active)
            self.assertEqual(r2.last_flip_side, "Down")
            self.assertAlmostEqual(r2._active_leg_entry_prob, 0.64, places=4)
            self.assertEqual(r2.quote_attempts, 3)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        # #10 _public_round active mode includes unwind cost and proceeds
        pub = _public_round({
            "slug": "btc-updown-5m-1800002400",
            "start": 1800002400.0,
            "end": 1800002700.0,
            "pos_up": {"shares": 0.0, "cash": 0.0},
            "pos_down": {"shares": 0.0, "cash": 0.0},
            "unwind_cost_basis": 2.60,
            "unwind_proceeds": 4.70,
            "fills_count": 2,
            "traded": True,
        }, active=True)
        self.assertAlmostEqual(pub["total_cost"], 2.60, places=4)
        self.assertAlmostEqual(pub["proceeds"], 4.70, places=4)


if __name__ == "__main__":
    unittest.main()
