"""PM-Quant v1.1 Microstructure & Risk Architecture Unit Tests.

Covers the 4 core v1.1 engineering modules:
1. Dynamic Realized Volatility Engine & Adaptive Quoting.
2. Account-Level Daily Drawdown & Loss Streak Circuit Breaker Watchdog.
3. Millisecond Live Trade Prints Matching & Physical Queue Deduction.
4. Fractional Kelly Dynamic Position Sizing (5, 10, 15 shares tiering).
"""
import sys
import unittest
import math
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.types import Order, OrderBook, Position, SpotState, Decision
from core.estimator import (
    calculate_dynamic_realized_vol,
    calculate_adaptive_spread_params,
    calculate_kelly_order_size,
    estimate_fair_prob,
    estimate_uncertainty_envelope
)
from core.circuit_breaker import CircuitBreaker
from core.policy import plan_decision, calculate_reservation_bids, evaluate_unpaired_utility
from sim.matcher import QueueMatcher


class TestDynamicVolatilityEngine(unittest.TestCase):
    def test_insufficient_history_fallback(self):
        """Sparse history or short time span must cleanly return baseline volatility."""
        baseline = 15.0
        # Empty or single point
        self.assertEqual(calculate_dynamic_realized_vol([], baseline), baseline)
        self.assertEqual(calculate_dynamic_realized_vol([(100.0, 75000.0)], baseline), baseline)
        
        # Less than 5 points
        short_pts = [(100.0 + i, 75000.0 + i) for i in range(4)]
        self.assertEqual(calculate_dynamic_realized_vol(short_pts, baseline), baseline)

    def test_flat_prices_calm_regime(self):
        """Completely flat price sequence over 60s should result in bounded calm volatility (<= 5 bps)."""
        now = 1000.0
        # 60 seconds of zero movement
        flat_pts = [(now - 60 + i, 75000.0) for i in range(60)]
        vol = calculate_dynamic_realized_vol(flat_pts)
        self.assertAlmostEqual(vol, 2.0, places=1) # clamped to minimum 2.0 bps
        
        edge, k, regime = calculate_adaptive_spread_params(vol, base_edge=0.025, k_mult=1.2)
        self.assertEqual(regime, "calm")
        self.assertLess(edge, 0.025) # base edge tightened to capture queue priority
        self.assertLess(k, 1.2)

    def test_violent_spot_jump_volatile_regime(self):
        """Rapid jump should register high volatility (> 30 bps) and expand safety pads."""
        now = 1000.0
        # Rapid 50 bps jump within 10 seconds
        jump_pts = [(now - 30 + i, 75000.0) for i in range(20)]
        for i in range(10):
            # 75000 -> 75375 (+50 bps)
            jump_pts.append((now - 10 + i, 75000.0 + i * 37.5))
            
        vol = calculate_dynamic_realized_vol(jump_pts)
        self.assertGreater(vol, 30.0)
        
        edge, k, regime = calculate_adaptive_spread_params(vol, base_edge=0.025, k_mult=1.2)
        self.assertEqual(regime, "volatile")
        self.assertGreater(edge, 0.025) # widened safety edge against toxic flow
        self.assertGreater(k, 1.2)     # widened uncertainty cushion

    def test_volatility_boundedness(self):
        """Extreme artificial movements must remain strictly clamped in [2.0, 80.0] bps."""
        now = 1000.0
        wild_pts = [(now - 10 + i, 75000.0 * (1.1 ** i)) for i in range(10)]
        vol = calculate_dynamic_realized_vol(wild_pts)
        self.assertLessEqual(vol, 80.0)
        self.assertGreaterEqual(vol, 2.0)


class TestCircuitBreakerWatchdog(unittest.TestCase):
    def test_daily_drawdown_tripping(self):
        """Cumulative daily drawdown exceeding limit must trip circuit breaker."""
        cb = CircuitBreaker(max_daily_drawdown_usd=8.00, loss_streak_cooling=3)
        self.assertFalse(cb.is_active()[0])

        # Suffer consecutive losses totalling -$8.10
        cb.record_round_result(-3.00, traded=True)
        cb.record_round_result(-3.00, traded=True)
        self.assertFalse(cb.is_active()[0]) # 2 losses (-$6.00) not yet exceeding $8.00 or 3 streak
        
        cb.record_round_result(-2.10, traded=True) # Total pnl = -$8.10 (exceeds -$8.00)
        active, reason = cb.is_active()
        self.assertTrue(active)
        self.assertTrue(cb.is_tripped)
        self.assertIn("Daily drawdown", reason)

    def test_loss_streak_cooling(self):
        """3 consecutive losses must trigger cooling lock; winning round resets streak."""
        cb = CircuitBreaker(max_daily_drawdown_usd=15.00, loss_streak_cooling=3, cooling_rounds=3)
        
        cb.record_round_result(-0.50, traded=True)
        cb.record_round_result(-0.40, traded=True)
        self.assertFalse(cb.is_active()[0]) # Only 2 losses, not yet 3
        
        cb.record_round_result(-0.30, traded=True) # 3rd consecutive loss
        active, reason = cb.is_active()
        self.assertTrue(active)
        self.assertIn("Loss streak", reason)
        self.assertEqual(cb.cooling_rounds_left, 3)

        # Winning round resets streak
        cb_win = CircuitBreaker(max_daily_drawdown_usd=15.00, loss_streak_cooling=3)
        cb_win.record_round_result(-0.50, traded=True)
        cb_win.record_round_result(-0.50, traded=True)
        cb_win.record_round_result(0.80, traded=True) # Win breaks streak!
        self.assertEqual(cb_win.consecutive_losses, 0)
        self.assertFalse(cb_win.is_active()[0])

    def test_reduce_only_policy_enforcement(self):
        """When circuit breaker is active, flat positions must not open new Leg-1,
        but unhedged inventory MUST still be allowed to hedge or unwind."""
        cb = CircuitBreaker(max_daily_drawdown_usd=8.00)
        cb.is_tripped = True
        cb.trip_reason = "Daily drawdown limit hit"

        params = dict(config.PARAMS, CIRCUIT_BREAKER_ACTIVE=True, CIRCUIT_BREAKER_REASON=cb.trip_reason, STRATEGY_MODE="legacy_mm")

        # Case 1: Flat inventory (|gap| = 0) -> policy must WAIT and forbid new Leg-1
        pos_flat = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.48, 50.0)], asks=[(0.50, 50.0)])
        book_dn = OrderBook(bids=[(0.48, 50.0)], asks=[(0.50, 50.0)])
        spot = SpotState(timestamp=1000.0, price=75000.0, open_price=75000.0)

        decision_flat = plan_decision(
            pos_flat, pos_flat, book_up, book_dn, spot, tau_s=180.0,
            params=params, circuit_breaker_active=True, circuit_breaker_reason=cb.trip_reason
        )
        self.assertEqual(decision_flat.action, "wait")
        self.assertIn("Circuit breaker active", decision_flat.reason)

        # Case 2: Unhedged inventory (holding 10 Up) -> MUST still execute hedge/unwind!
        pos_up = Position(shares=10.0, cash=4.50)
        decision_unpaired = plan_decision(
            pos_up, pos_flat, book_up, book_dn, spot, tau_s=180.0,
            params=params, circuit_breaker_active=True, circuit_breaker_reason=cb.trip_reason
        )
        # Even with circuit breaker active, cutting unhedged risk is mandatory
        self.assertIn(decision_unpaired.action, ("taker_hedge", "taker_unwind", "quote"))
        if decision_unpaired.action == "quote":
            self.assertEqual(decision_unpaired.side, "Down") # Quoting to hedge


class TestLiveTradePrintsMatching(unittest.TestCase):
    def test_trade_print_exact_queue_deduction(self):
        """Trade prints at order price physically deduct queue ahead by exact executed volume."""
        matcher = QueueMatcher(latency_s=0.30)
        order = Order(id=101, client_order_id="pm_101", side="Up", price=0.45, size=5.0, remaining=5.0, placed_at=100.0)
        book = OrderBook(bids=[(0.45, 12.0), (0.44, 20.0)], asks=[(0.47, 10.0)])
        matcher.register_order(order, book)
        self.assertEqual(matcher.queue_ahead[101], 12.0)

        # Print 1: 5.0 shares trade executed @ 0.45 -> queue ahead reduces from 12.0 to 7.0
        fill1 = matcher.on_trade_print(order, trade_price=0.45, trade_size=5.0, now=100.35)
        self.assertEqual(fill1, 0.0)
        self.assertEqual(matcher.queue_ahead[101], 7.0)

        # Print 2: 7.0 shares trade executed @ 0.45 -> queue ahead reaches 0.0, order remains 5.0
        fill2 = matcher.on_trade_print(order, trade_price=0.45, trade_size=7.0, now=100.36)
        self.assertEqual(fill2, 0.0)
        self.assertEqual(matcher.queue_ahead[101], 0.0)

        # Print 3: 5.0 shares trade executed @ 0.45 -> exact 5.0 shares filled!
        fill3 = matcher.on_trade_print(order, trade_price=0.45, trade_size=5.0, now=100.37)
        self.assertEqual(fill3, 5.0)
        self.assertEqual(order.remaining, 0.0)
        self.assertEqual(order.filled_size, 5.0)

    def test_trade_print_price_levels(self):
        """Trades above bid price do not exhaust bid queue; trades below bid do exhaust."""
        matcher = QueueMatcher(latency_s=0.30)
        order = Order(id=102, client_order_id="pm_102", side="Up", price=0.45, size=5.0, remaining=5.0, placed_at=100.0)
        book = OrderBook(bids=[(0.45, 8.0)], asks=[(0.48, 10.0)])
        matcher.register_order(order, book)

        # Trade executed @ 0.46 (above our 0.45 bid) -> ignored
        fill_above = matcher.on_trade_print(order, trade_price=0.46, trade_size=10.0, now=100.35)
        self.assertEqual(fill_above, 0.0)
        self.assertEqual(matcher.queue_ahead[102], 8.0)

        # Trade executed @ 0.44 (market sell swept down through 0.45 to 0.44) -> exhausts queue & fills
        fill_below = matcher.on_trade_print(order, trade_price=0.44, trade_size=15.0, now=100.36)
        self.assertEqual(fill_below, 5.0)
        self.assertEqual(order.remaining, 0.0)


class TestFractionalKellyDynamicSizing(unittest.TestCase):
    def test_kelly_sizing_tiers(self):
        """Quarter-Kelly scales order size across discrete {5.0, 10.0, 15.0} tiers based on edge."""
        capital = 100.0
        quote_px = 0.50

        # Tier 1: Marginal edge (p_win = 0.53, edge = 0.03) -> 1 lot (5.0 shares)
        sz_marginal = calculate_kelly_order_size(capital, p_win=0.53, quote_price=quote_px, lot_size=5.0, min_lots=1, max_lots=3)
        self.assertEqual(sz_marginal, 5.0)

        # Tier 2: Moderate edge (p_win = 0.62, edge = 0.12) -> 2 lots (10.0 shares)
        sz_moderate = calculate_kelly_order_size(capital, p_win=0.62, quote_price=quote_px, lot_size=5.0, min_lots=1, max_lots=3)
        self.assertEqual(sz_moderate, 10.0)

        # Tier 3: Strong edge (p_win = 0.72, edge = 0.22) -> 3 lots (15.0 shares)
        sz_strong = calculate_kelly_order_size(capital, p_win=0.72, quote_price=quote_px, lot_size=5.0, min_lots=1, max_lots=3)
        self.assertEqual(sz_strong, 15.0)

    def test_hedge_sizing_matches_held_inventory(self):
        """When Leg-1 entry filled 15 shares, Leg-2 hedge MUST size exactly 15 shares for exact pairing."""
        book_up = OrderBook(bids=[(0.55, 50.0)], asks=[(0.58, 50.0)])
        book_dn = OrderBook(bids=[(0.42, 50.0)], asks=[(0.45, 50.0)])

        decision = evaluate_unpaired_utility(
            held_side="Up",
            held_shares=15.0, # 3 lots entry was filled
            held_basis_price=0.52,
            p_fair_held=0.55,
            book_held=book_up,
            book_opp=book_dn,
            gamma=0.15
        )
        # Decision must size hedge to exactly 15.0 shares
        self.assertEqual(decision.size, 15.0)
        self.assertEqual(decision.side, "Down")

    def test_insufficient_capital_boundary(self):
        """When available capital cannot purchase 1 minimum lot, Kelly size returns 0.0 and policy waits."""
        capital_tiny = 1.50 # Less than 5 shares * $0.50 = $2.50
        sz = calculate_kelly_order_size(capital_tiny, p_win=0.70, quote_price=0.50, lot_size=5.0)
        self.assertEqual(sz, 0.0)

        pos_flat = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.48, 50.0)], asks=[(0.50, 50.0)])
        book_dn = OrderBook(bids=[(0.48, 50.0)], asks=[(0.50, 50.0)])
        spot = SpotState(timestamp=1000.0, price=75010.0, open_price=75000.0) # Balanced Up edge within [0.42, 0.58]
        params_tiny = dict(config.PARAMS, AVAILABLE_CAPITAL=capital_tiny, STRATEGY_MODE="legacy_mm")

        dec = plan_decision(pos_flat, pos_flat, book_up, book_dn, spot, tau_s=180.0, params=params_tiny)
        self.assertEqual(dec.action, "wait")
        self.assertIn("Insufficient capital", dec.reason)

    def test_loss_streak_persists_across_day_rollover(self):
        """Loss streak from before midnight must not be erased when hydrating from summary."""
        cb = CircuitBreaker(max_daily_drawdown_usd=10.00, loss_streak_cooling=3)
        # 2 losses yesterday, 1 loss today
        records = [
            {"end": 1000.0, "net_pnl": -1.0, "traded": True},
            {"end": 1300.0, "net_pnl": -1.0, "traded": True},
            {"end": 100000.0, "net_pnl": -1.0, "traded": True}, # across midnight
        ]
        cb.load_from_summary(records)
        active, reason = cb.is_active(ts=100000.0)
        self.assertTrue(active)
        self.assertIn("Loss streak", reason)


if __name__ == "__main__":
    unittest.main()
