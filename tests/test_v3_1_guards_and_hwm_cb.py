"""Unit tests for v3.1 defenses:
1. Intraday High-Water-Mark (peak drawdown) CircuitBreaker + restart hydration + day rollover.
2. Pre-trade 5s orderbook knife-drop guard (blocks fresh Leg-1 & cancels resting maker bid).
3. Directional adverse short-horizon spot momentum guard (1.5 bps).
4. Equal-weight single-lot defaults (Mode B & Pyramid disabled; Sub-$1 Flip preserved).
"""
import tempfile
import unittest

import config
from core.circuit_breaker import CircuitBreaker
from core.execution import plan_order_actions
from core.policy import plan_decision
from core.types import Decision, Order, OrderBook, Position, SpotState
from runtime.runner import PaperRunner


def _book(bid: float, ask: float, depth: float = 50.0, t: float = 1200.0) -> OrderBook:
    return OrderBook(
        bids=[(bid, depth)],
        asks=[(ask, depth)],
        last_trade_price=ask,
        source_t=t,
        received_at=t,
    )


def _spot(px: float = 60100.0, open_px: float = 60000.0, vel_bps: float = 0.5, t: float = 1200.0) -> SpotState:
    return SpotState(
        timestamp=t,
        price=px,
        open_price=open_px,
        realized_vol_bps=15.0,
        velocity_bps=vel_bps,
        is_toxic_shock=(abs(vel_bps) >= 3.5),
    )


class TestV31GuardsAndHwmCircuitBreaker(unittest.TestCase):
    def test_hwm_peak_drawdown_trips_even_when_daily_pnl_still_positive(self):
        """Morning +$15 profit followed by -$12.50 drop (net +$2.50) must trip $12 HWM breaker."""
        cb = CircuitBreaker(max_daily_drawdown_usd=12.0, loss_streak_cooling=5, cooling_rounds=3, enabled=True)
        ts = 1795500000.0
        cb.record_round_result(+8.0, traded=True, ts=ts)
        cb.record_round_result(+7.0, traded=True, ts=ts + 300)
        self.assertAlmostEqual(cb.daily_peak, 15.0, places=4)
        self.assertFalse(cb.is_active(ts + 300)[0])

        cb.record_round_result(-6.0, traded=True, ts=ts + 600)
        cb.record_round_result(-6.5, traded=True, ts=ts + 900)
        # daily_pnl = +2.50, but peak drawdown = 15.0 - 2.5 = 12.5 >= 12.0
        active, reason = cb.is_active(ts + 900)
        self.assertTrue(active)
        self.assertTrue(cb.is_tripped)
        self.assertIn("Daily drawdown", reason)
        st = cb.get_status(ts + 900)
        self.assertAlmostEqual(st["daily_peak"], 15.0, places=4)
        self.assertAlmostEqual(st["daily_drawdown"], 12.5, places=4)

        # Restart hydration must reconstruct daily_peak and tripped state identically
        cb2 = CircuitBreaker(max_daily_drawdown_usd=12.0, loss_streak_cooling=5, cooling_rounds=3, enabled=True)
        cb2.load_from_summary([
            {"end": ts, "net_pnl": 8.0, "traded": True},
            {"end": ts + 300, "net_pnl": 7.0, "traded": True},
            {"end": ts + 600, "net_pnl": -6.0, "traded": True},
            {"end": ts + 900, "net_pnl": -6.5, "traded": True},
        ])
        self.assertTrue(cb2.is_active(ts + 900)[0])
        self.assertAlmostEqual(cb2.daily_peak, 15.0, places=4)

        # Next natural day resets HWM drawdown and tripped flag
        next_day_ts = ts + 86400.0
        self.assertFalse(cb2.is_active(next_day_ts)[0])
        self.assertAlmostEqual(cb2.daily_peak, 0.0, places=4)

    def test_book_drop_guard_blocks_fresh_entry_and_allows_when_book_stable(self):
        params = dict(
            config.PARAMS,
            AVAILABLE_CAPITAL=100.0,
            COMMITTED_WINDOW_COST=0.0,
            BASIS_READY=True,
            REQUIRE_STRIKE_AND_BASIS=True,
            ENABLE_BOOK_DROP_GUARD=True,
            BOOK_DROP_GUARD_MAX=0.03,
            BOOK_MOM_UP=-0.045,
            BOOK_MOM_DOWN=+0.045,
        )
        # Up side has huge model edge (P*=0.68 vs ask=0.50), but Up book just dropped -$0.045 in 5s -> blocked!
        dec_blocked = plan_decision(
            pos_up=Position(),
            pos_down=Position(),
            book_up=_book(0.49, 0.50),
            book_down=_book(0.50, 0.51),
            spot=_spot(vel_bps=0.5),
            tau_s=150.0,
            params=params,
            p_fair=0.68,
            entries_count=0,
        )
        self.assertEqual(dec_blocked.action, "wait")
        self.assertIn("盘口急砸", dec_blocked.reason)
        self.assertTrue(dec_blocked.urgent_cancel)

        # Same book without recent drop -> enters cleanly
        params_ok = dict(params, BOOK_MOM_UP=+0.01, BOOK_MOM_DOWN=-0.01)
        dec_ok = plan_decision(
            pos_up=Position(),
            pos_down=Position(),
            book_up=_book(0.49, 0.50),
            book_down=_book(0.50, 0.51),
            spot=_spot(vel_bps=0.5),
            tau_s=150.0,
            params=params_ok,
            p_fair=0.68,
            entries_count=0,
        )
        self.assertEqual(dec_ok.action, "taker_buy")
        self.assertEqual(dec_ok.side, "Up")

    def test_adverse_spot_momentum_1_5bp_blocks_entry_but_allows_tailwind(self):
        params = dict(
            config.PARAMS,
            AVAILABLE_CAPITAL=100.0,
            COMMITTED_WINDOW_COST=0.0,
            BASIS_READY=True,
            REQUIRE_STRIKE_AND_BASIS=True,
            ENTRY_MAX_ADVERSE_SPOT_BPS=1.5,
            BOOK_MOM_UP=0.0,
            BOOK_MOM_DOWN=0.0,
        )
        # Buying Up while spot moved -1.8 bps against us -> blocked
        dec_adverse = plan_decision(
            pos_up=Position(),
            pos_down=Position(),
            book_up=_book(0.49, 0.50),
            book_down=_book(0.50, 0.51),
            spot=_spot(vel_bps=-1.8),
            tau_s=150.0,
            params=params,
            p_fair=0.68,
            entries_count=0,
        )
        self.assertEqual(dec_adverse.action, "wait")
        self.assertIn("现货逆向", dec_adverse.reason)

        # Buying Up while spot moved +1.8 bps in our favor -> allowed
        dec_tailwind = plan_decision(
            pos_up=Position(),
            pos_down=Position(),
            book_up=_book(0.49, 0.50),
            book_down=_book(0.50, 0.51),
            spot=_spot(vel_bps=+1.8),
            tau_s=150.0,
            params=params,
            p_fair=0.68,
            entries_count=0,
        )
        self.assertEqual(dec_tailwind.action, "taker_buy")
        self.assertEqual(dec_tailwind.side, "Up")

    def test_execution_cancels_resting_maker_when_target_book_drops(self):
        order_up = Order(id=1, client_order_id="pm_1", side="Up", price=0.52, size=5.0, remaining=5.0, placed_at=100.0)
        order_hedge = Order(id=2, client_order_id="pm_2", side="Up", price=0.40, size=5.0, remaining=5.0, placed_at=100.0, purpose="hedge")
        wait_dec = Decision(action="wait", reason="waiting", urgent_cancel=False)
        to_cancel, act = plan_order_actions(
            [order_up, order_hedge],
            wait_dec,
            now=103.0,
            book_up=_book(0.51, 0.52),
            book_down=_book(0.48, 0.49),
            mkt_mom_up=-0.04,
            mkt_mom_down=+0.04,
            book_drop_guard_max=0.03,
        )
        self.assertIsNone(act)
        self.assertEqual([o.id for o in to_cancel], [1])
        self.assertIn("盘口急砸防毒撤单", order_up.cancel_reason)

    def test_runner_computes_5s_book_momentum_buffer(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-1800000000", 1800000000.0, 1800000300.0, 60000.0)
            m_u0, m_d0 = runner._update_and_get_book_momentum(
                1800000020.0, _book(0.55, 0.57), _book(0.43, 0.45), window_s=5.0
            )
            self.assertIsNone(m_u0)
            m_u1, m_d1 = runner._update_and_get_book_momentum(
                1800000025.0, _book(0.50, 0.52), _book(0.48, 0.50), window_s=5.0
            )
            self.assertAlmostEqual(m_u1, -0.05, places=4)
            self.assertAlmostEqual(m_d1, +0.05, places=4)


if __name__ == "__main__":
    unittest.main()
