import tempfile
import unittest
from unittest.mock import patch

import config
from core.policy import plan_decision, required_entry_edge
from core.types import Decision, Order, OrderBook, Position, SpotState
from runtime.runner import PaperRunner
from sim.matcher import QueueMatcher, is_books_consistent


def _book(bid_px, bid_sz, ask_px, ask_sz, t=1200.0):
    return OrderBook(
        bids=[(bid_px, bid_sz)],
        asks=[(ask_px, ask_sz)],
        source_t=t,
        received_at=t,
    )


class TestV29CalibratedRealismAndDynamicFlip(unittest.TestCase):
    def test_v29_version_and_calibrated_defaults(self):
        self.assertEqual(config.VERSION, "v3.4")
        self.assertEqual(config.BATCH_ID, "v3.4")
        self.assertAlmostEqual(config.PARAMS["LATENCY_S"], 0.18, places=4)
        self.assertAlmostEqual(config.PARAMS["CANCEL_LATENCY_S"], 0.18, places=4)
        self.assertTrue(config.PARAMS["LATENCY_DYNAMIC_FROM_RTT"])
        self.assertAlmostEqual(config.PARAMS["LATENCY_MIN_S"], 0.15, places=4)
        self.assertAlmostEqual(config.PARAMS["LATENCY_JITTER_PCT"], 0.15, places=4)
        self.assertAlmostEqual(config.PARAMS["TAKER_IOC_SLIPPAGE_TICKS"], 0.02, places=4)
        self.assertAlmostEqual(config.PARAMS["QUEUE_CANCEL_CONSERVATIVE_MULT"], 1.03, places=4)
        self.assertTrue(config.validate_params(config.PARAMS))

    def test_v29_dynamic_rtt_latency_with_jitter_and_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = PaperRunner(ledger_dir=tmp)
            # 1. Replay mode must stay strictly deterministic at LATENCY_S=0.18
            self.assertAlmostEqual(runner._effective_wire_latency(replay_mode=True), 0.18, places=4)

            # 2. Low RTT (London ~6ms): base clamped to 150ms floor, live jitter stays in [127.5ms, 172.5ms]
            runner.net_stats = {"poly_latency_ms": 6.0}
            samples_lon = [runner._effective_wire_latency(replay_mode=False) for _ in range(40)]
            self.assertGreater(max(samples_lon) - min(samples_lon), 0.005)
            self.assertGreaterEqual(min(samples_lon), 0.15 * 0.85 - 1e-6)
            self.assertLessEqual(max(samples_lon), 0.15 * 1.15 + 1e-6)

            # 3. High RTT (local China ~240ms): base = (120 + 110)ms = 230ms, jitter stays in [195.5ms, 264.5ms]
            runner.net_stats = {"poly_latency_ms": 240.0}
            samples_cn = [runner._effective_wire_latency(replay_mode=False) for _ in range(40)]
            self.assertGreater(sum(samples_cn) / len(samples_cn), 0.20)
            self.assertGreaterEqual(min(samples_cn), 0.23 * 0.85 - 1e-6)
            self.assertLessEqual(max(samples_cn), 0.23 * 1.15 + 1e-6)

    def test_v29_dynamic_adverse_flip_strictly_below_1_dollar_vs_stop_loss(self):
        from core.execution import plan_order_actions

        pos_up = Position(shares=5.0, cash=2.10)  # Up @ 0.42
        pos_dn = Position(0.0, 0.0)

        # 1. Held Up @ $0.42 looks genuinely wrong (P*_held=0.28, P*_opp=0.72), opposite Down is $0.54
        #    -> pair_cost = 0.42 + 0.54 + 0.0174 = $0.9774 <= $0.98 (< $1.00 positive lock!)
        #    and spot is moving rapidly (4.0 bps velocity + 85 bps storm) -> must execute dynamic flip IOC!
        b_up = _book(0.46, 50.0, 0.48, 50.0)
        b_dn = _book(0.52, 50.0, 0.54, 50.0)
        spot_fast = SpotState(
            timestamp=1210.0, price=64850.0, open_price=65000.0,
            velocity_bps=-4.0, realized_vol_bps=85.0,
        )
        p_fast = dict(
            config.PARAMS,
            ENABLE_REVERSAL_FLIP=True,
            MAX_LOTS=2,
            MAX_ENTRIES_PER_WINDOW=2,
            FLIP_MAX_PAIR_COST=0.98,
            FLIP_MAX_PRICE=0.74,
            FLIP_COOLDOWN_S=5.0,
            SECONDS_SINCE_LAST_ENTRY=6.0,
        )
        dec_flip = plan_decision(
            pos_up, pos_dn, b_up, b_dn, spot_fast,
            tau_s=90.0, params=p_fast, p_fair=0.28, entries_count=1,
        )
        self.assertEqual(dec_flip.action, "taker_buy")
        self.assertEqual(dec_flip.side, "Down")
        self.assertAlmostEqual(dec_flip.price, 0.54, places=4)
        self.assertIn("动态不对劲反买", dec_flip.reason)
        self.assertIn("配对成本=$0.977", dec_flip.reason)

        # 2. The exact 13:50 live mistake (Held Down @ $0.42, opposite Up ask=$0.76 -> pair_cost = $1.21 > $1.00):
        #    Even if P*(Up)=0.88 gives apparent +10.7% edge on Up, pairing above $1.00 locks in a guaranteed loss (-$1.05)
        #    and is a stop-loss operation, NOT a pairing strategy -> MUST BE STRICTLY BLOCKED!
        pos_dn_held = Position(shares=5.0, cash=2.10)  # Down @ 0.42
        b_up_76 = _book(0.75, 50.0, 0.76, 50.0)
        b_dn_24 = _book(0.23, 50.0, 0.25, 50.0)
        dec_1350 = plan_decision(
            Position(0.0, 0.0), pos_dn_held, b_up_76, b_dn_24, spot_fast,
            tau_s=201.0, params=p_fast, p_fair=0.88, entries_count=1,
        )
        self.assertEqual(dec_1350.action, "wait")

        # 3. Same valid sub-$1.00 scenario within 2s cooldown -> blocked against instant double-tap
        p_too_soon = dict(config.PARAMS, SECONDS_SINCE_LAST_ENTRY=2.0)
        dec_soon = plan_decision(
            pos_up, pos_dn, b_up, b_dn, spot_fast,
            tau_s=90.0, params=p_too_soon, p_fair=0.28, entries_count=1,
        )
        self.assertEqual(dec_soon.action, "wait")

        # 4. Held Up is winning strongly (P*_held=0.94), opposite Down ask=$0.06 -> must NEVER bleed black-swan hedge on Down;
        #    in v3.4 with ENABLE_EXTREME_TAKE_PROFIT=True, bid=$0.94 at tau=45s triggers Up take-profit unwind, while bid=$0.91 holds.
        b_up_win = _book(0.94, 50.0, 0.95, 50.0)
        b_dn_bs = _book(0.05, 50.0, 0.06, 50.0)
        spot_calm = SpotState(timestamp=1250.0, price=65080.0, open_price=65000.0, realized_vol_bps=14.0)
        dec_tp = plan_decision(
            pos_up, pos_dn, b_up_win, b_dn_bs, spot_calm,
            tau_s=45.0, params=p_fast, p_fair=0.94, entries_count=1,
        )
        self.assertEqual(dec_tp.action, "taker_unwind")
        self.assertEqual(dec_tp.side, "Up")
        dec_hold = plan_decision(
            pos_up, pos_dn, _book(0.91, 50.0, 0.92, 50.0), _book(0.08, 50.0, 0.09, 50.0), spot_calm,
            tau_s=45.0, params=p_fast, p_fair=0.94, entries_count=1,
        )
        self.assertEqual(dec_hold.action, "wait")

        # 5. Near-head Maker queue protection: order at best_bid with queue_ahead=12 <= 25 shares at age=16s is NOT killed
        o_head = Order(id=99, client_order_id="c99", side="Up", price=0.50, size=5.0, remaining=5.0, placed_at=100.0)
        b_rest = _book(0.50, 20.0, 0.52, 20.0)
        cancels_head, _ = plan_order_actions(
            [o_head], Decision(action="wait", reason="hold"), now=116.0,
            max_queue_wait_s=15.0, preferred_side="Up", book_up=b_rest, book_down=b_rest,
            p_up=0.56, min_hold_maker_edge=0.015, queue_ahead_map={99: 12.0}, queue_head_protect_shares=25.0,
        )
        self.assertEqual(len(cancels_head), 0)

        # Far-behind order (queue_ahead=120 > 25 shares) at age=16s IS cancelled as usual
        cancels_far, _ = plan_order_actions(
            [o_head], Decision(action="wait", reason="hold"), now=116.0,
            max_queue_wait_s=15.0, preferred_side="Up", book_up=b_rest, book_down=b_rest,
            p_up=0.56, min_hold_maker_edge=0.015, queue_ahead_map={99: 120.0}, queue_head_protect_shares=25.0,
        )
        self.assertEqual(len(cancels_far), 1)

    def test_v29_taker_ioc_allows_1c_slippage_if_edge_holds_and_rejects_otherwise(self):
        # Case A: +1c upward slippage (0.45 -> 0.46) with strong remaining edge (0.14 - 0.01 = 0.13 >= 0.065) -> fills at real 0.46
        with tempfile.TemporaryDirectory() as tmp:
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-1000", 1000.0, 1300.0, 75000.0, binance_open_px=75000.0)
            b_up = _book(0.43, 20.0, 0.45, 20.0, t=1200.0)
            b_dn = _book(0.54, 20.0, 0.56, 20.0, t=1200.0)
            sp = SpotState(timestamp=1200.0, price=75080.0, open_price=75000.0, binance_open_price=75000.0, basis_ema=0.0)
            dec = Decision(action="taker_buy", side="Up", price=0.45, size=5.0, utility=0.14, reason="v2.9 净边际吃单 IOC")
            delayed_up = _book(0.44, 20.0, 0.46, 20.0, t=1200.12)
            delayed_dn = _book(0.53, 20.0, 0.55, 20.0, t=1200.12)
            with patch("runtime.runner.plan_decision", return_value=dec), patch("runtime.runner.time.sleep"):
                runner.step(1200.0, sp, b_up, b_dn, delayed_book_fetcher=lambda _: (delayed_up, delayed_dn))
            self.assertAlmostEqual(runner.pos_up.shares, 5.0, places=4)
            self.assertAlmostEqual(runner.fills[-1]["price"], 0.46, places=4)

        # Case B: +1c upward slippage erodes thin edge below MIN_TAKER_EDGE (0.040 -> 0.030 < 0.035) -> rejected
        with tempfile.TemporaryDirectory() as tmp:
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-1000", 1000.0, 1300.0, 75000.0, binance_open_px=75000.0)
            b_up = _book(0.43, 20.0, 0.45, 20.0, t=1200.0)
            b_dn = _book(0.54, 20.0, 0.56, 20.0, t=1200.0)
            sp = SpotState(timestamp=1200.0, price=75080.0, open_price=75000.0, binance_open_price=75000.0, basis_ema=0.0)
            dec_thin = Decision(action="taker_buy", side="Up", price=0.45, size=5.0, utility=0.040, reason="v2.9 净边际吃单 IOC")
            delayed_up = _book(0.44, 20.0, 0.46, 20.0, t=1200.12)
            delayed_dn = _book(0.53, 20.0, 0.55, 20.0, t=1200.12)
            with patch("runtime.runner.plan_decision", return_value=dec_thin), patch("runtime.runner.time.sleep"):
                runner.step(1200.0, sp, b_up, b_dn, delayed_book_fetcher=lambda _: (delayed_up, delayed_dn))
            self.assertAlmostEqual(runner.pos_up.shares, 0.0, places=4)
            self.assertTrue(any("IOC 错失防滑点" in str(a.get("reason", "")) for a in runner.actions))

        # Case C: +3c upward move (0.45 -> 0.48 > +2c IOC protection cap) -> missed/rejected
        with tempfile.TemporaryDirectory() as tmp:
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-1000", 1000.0, 1300.0, 75000.0, binance_open_px=75000.0)
            b_up = _book(0.43, 20.0, 0.45, 20.0, t=1200.0)
            b_dn = _book(0.54, 20.0, 0.56, 20.0, t=1200.0)
            sp = SpotState(timestamp=1200.0, price=75080.0, open_price=75000.0, binance_open_price=75000.0, basis_ema=0.0)
            dec = Decision(action="taker_buy", side="Up", price=0.45, size=5.0, utility=0.14, reason="v2.9 净边际吃单 IOC")
            delayed_up = _book(0.46, 20.0, 0.48, 20.0, t=1200.12)
            delayed_dn = _book(0.51, 20.0, 0.53, 20.0, t=1200.12)
            with patch("runtime.runner.plan_decision", return_value=dec), patch("runtime.runner.time.sleep"):
                runner.step(1200.0, sp, b_up, b_dn, delayed_book_fetcher=lambda _: (delayed_up, delayed_dn))
            self.assertAlmostEqual(runner.pos_up.shares, 0.0, places=4)

    def test_v29_conservative_queue_depth_cap_tracks_cancels_without_optimism(self):
        m = QueueMatcher(latency_s=0.12)
        o = Order(id=1, client_order_id="c1", side="Up", price=0.50, size=5.0, remaining=5.0, placed_at=100.0)
        b_init = OrderBook(bids=[(0.50, 600.0), (0.49, 100.0)], asks=[(0.52, 100.0)])
        m.register_order(o, b_init)
        self.assertTrue(m.activate_order(o, b_init, now=100.12, post_only=True))
        self.assertAlmostEqual(m.queue_ahead[1], 600.0, places=4)

        # Market makers ahead cancel down to 50 shares -> capped at 50 * 1.03 = 51.5 (strictly conservative!)
        b_cancel = OrderBook(bids=[(0.50, 50.0), (0.49, 100.0)], asks=[(0.52, 100.0)])
        m.sync_queue_depth(o, b_cancel, now=101.0, conservative_mult=1.03)
        self.assertAlmostEqual(m.queue_ahead[1], 51.5, places=4)

        # New joiners arrive behind us (depth rises to 400) -> queue_ahead must NEVER increase
        b_refill = OrderBook(bids=[(0.50, 400.0), (0.49, 100.0)], asks=[(0.52, 100.0)])
        m.sync_queue_depth(o, b_refill, now=102.0, conservative_mult=1.03)
        self.assertAlmostEqual(m.queue_ahead[1], 51.5, places=4)

        # Truncated L2 where lowest published bid (0.52) is above our 0.50 order -> must NOT zero out queue_ahead
        b_trunc = OrderBook(bids=[(0.53, 80.0), (0.52, 80.0)], asks=[(0.55, 80.0)])
        m.sync_queue_depth(o, b_trunc, now=103.0, conservative_mult=1.03)
        self.assertAlmostEqual(m.queue_ahead[1], 51.5, places=4)

    def test_v29_book_consistency_tolerates_1tick_async_jitter_but_blocks_arb(self):
        # 1-tick async WS frame jitter (ask_sum = 0.99) -> allowed under default tolerance=0.015
        b_up = _book(0.48, 20.0, 0.50, 20.0)
        b_dn = _book(0.47, 20.0, 0.49, 20.0)
        self.assertTrue(is_books_consistent(b_up, b_dn))

        # Severe inversion (ask_sum = 0.96) -> still strictly blocked
        b_up_bad = _book(0.46, 20.0, 0.48, 20.0)
        b_dn_bad = _book(0.46, 20.0, 0.48, 20.0)
        self.assertFalse(is_books_consistent(b_up_bad, b_dn_bad))

    def test_v29_price_skew_and_early_tau_dynamic_edge_premium(self):
        pos_empty = Position(0.0, 0.0)
        spot = SpotState(timestamp=1100.0, price=65050.0, open_price=65000.0, realized_vol_bps=15.0)

        p_mk = dict(
            config.PARAMS,
            ENABLE_MAKER_ENTRY=True,
            MIN_MAKER_EDGE=0.05,
            PRICE_EDGE_SLOPE=0.18,
            EARLY_TAU_MAX_PREMIUM=0.012,
        )
        # 1. Golden launch zone (px=0.50 <= 0.54, tau=120s <= 180s): exact 5.0% base Maker edge required
        self.assertAlmostEqual(
            required_entry_edge(0.50, 120.0, 0.05, p_mk),
            0.05,
            places=5,
        )
        b_up_flat = _book(0.49, 40.0, 0.51, 40.0)
        b_dn_flat = _book(0.49, 40.0, 0.51, 40.0)
        dec_gold = plan_decision(
            pos_empty, pos_empty, b_up_flat, b_dn_flat, spot,
            tau_s=120.0, params=p_mk, p_fair=0.555,
        )
        self.assertEqual(dec_gold.action, "quote")
        self.assertEqual(dec_gold.side, "Up")
        self.assertAlmostEqual(dec_gold.price, 0.50, places=4)

        # 2. High-price early trap (maker_px=0.61 at tau=234s):
        b_up_high = _book(0.60, 40.0, 0.62, 40.0)
        b_dn_low = _book(0.38, 40.0, 0.40, 40.0)
        dec_trap = plan_decision(
            pos_empty, pos_empty, b_up_high, b_dn_low, spot,
            tau_s=234.0, params=p_mk, p_fair=0.665,
        )
        self.assertEqual(dec_trap.action, "wait")

        dec_strong = plan_decision(
            pos_empty, pos_empty, b_up_high, b_dn_low, spot,
            tau_s=234.0, params=p_mk, p_fair=0.690,
        )
        self.assertIn(dec_strong.action, ("quote", "taker_buy"))
        self.assertEqual(dec_strong.side, "Up")

        # 3. Cheap price in early window (maker_px=0.50 at tau=210s) only adds +0.6% time premium (req=5.6%),
        #    so a genuine early lag (P*=0.56 vs 0.50) still fires immediately!
        dec_early_cheap = plan_decision(
            pos_empty, pos_empty, b_up_flat, b_dn_flat, spot,
            tau_s=210.0, params=p_mk, p_fair=0.560,
        )
        self.assertEqual(dec_early_cheap.action, "quote")
        self.assertAlmostEqual(dec_early_cheap.price, 0.50, places=4)


if __name__ == "__main__":
    unittest.main()
