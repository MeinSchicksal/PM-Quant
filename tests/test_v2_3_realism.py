"""v2.3 Realism & Zero-Self-Deception Regression Tests.

Pins the core engineering fixes made in v2.3:
1. Binance 451 failover + stale spot timestamp preservation tripping dead-man switch.
2. Basis-adjusted live spot + Asian 60s TWAP variance in estimate_p_fair (never using lagging TWAP as spot).
3. Strict dual-book consistency (no artificial price shifting when STRICT_BOOK_CONSISTENCY=True).
4. Realistic post-latency Taker IOC check preventing fills when book moves during wire transit.
"""
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.types import OrderBook, Position, SpotState, Decision
from core.estimator import estimate_p_fair, estimate_fair_prob, calculate_dual_horizon_vol
from sim.matcher import is_books_consistent, QueueMatcher
from runtime.runner import PaperRunner
from runtime.market_feed import MarketFeed
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import threading


class TestV23Realism(unittest.TestCase):
    def test_stale_spot_failover_preserves_old_timestamp_and_trips_deadman(self):
        feed = MarketFeed.__new__(MarketFeed)
        feed.session = MagicMock()
        feed.spot_history = []
        feed._last_binance_px = 64000.0
        feed._last_binance_t = 1000.0
        feed._last_spot_source = "binance_vision"
        feed.net_stats = {"last_calc_t": 1000.0, "bytes_window": 0, "msgs_window": 0, "total_bytes": 0}
        feed.record_raw_event = MagicMock()

        # Simulate all spot endpoints failing (e.g. HTTP 451 / network drop)
        bad_resp = MagicMock(status_code=451, content=b"restricted")
        feed.session.get.return_value = bad_resp

        spot = feed.fetch_spot_state(open_price=64000.0, now=1010.0)
        self.assertTrue(spot.is_stale)
        self.assertEqual(spot.timestamp, 1000.0)

        with tempfile.TemporaryDirectory() as tmp:
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-900", 900.0, 1200.0, 64000.0, binance_open_px=64000.0)
            b_up = OrderBook(bids=[(0.48, 20.0)], asks=[(0.50, 20.0)], source_t=1010.0)
            b_dn = OrderBook(bids=[(0.48, 20.0)], asks=[(0.50, 20.0)], source_t=1010.0)
            runner.step(1010.0, spot, b_up, b_dn)
            self.assertIn("Dead-man switch active", runner.decisions[-1]["reason"])
            self.assertEqual(len(runner.fills), 0)

    def test_estimator_never_uses_lagging_twap_as_spot(self):
        # Strike = 73800. Binance jumped to 73920 (+120 above strike), basis_ema = +20 -> cl_est = 73900 (+100 above strike).
        # Meanwhile lagging 60s poly_price is still 73740 (-60 below strike).
        # v2.2 wrongly saw poly_price=73740 and predicted P*(Up) < 0.20; v2.3 MUST predict P*(Up) >> 0.50.
        sp = SpotState(
            timestamp=1200.0, price=73920.0, open_price=73800.0,
            poly_price=73740.0, binance_open_price=73900.0, basis_ema=20.0
        )
        p_up = estimate_p_fair(sp, open_px=73800.0, tau_s=90.0, vol_bps=14.0, poly_price=73740.0)
        self.assertGreater(p_up, 0.70)

    def test_asian_twap_last_60s_locked_average_dominates(self):
        # At tau=20s, 40s of the 60s window already averaged 75100 (+100 above strike 75000),
        # even if live cl_est dipped slightly to 74990, expected_close = (40*75100 + 20*74990)/60 = 75063.3 > 75000!
        sp = SpotState(
            timestamp=1280.0, price=74990.0, open_price=75000.0,
            basis_ema=0.0, twap_realized_60s=75100.0
        )
        p_up = estimate_p_fair(sp, open_px=75000.0, tau_s=20.0, vol_bps=14.0)
        self.assertGreater(p_up, 0.85)

    def test_strict_book_consistency_never_shifts_prices(self):
        b_up = OrderBook(bids=[(0.60, 10.0)], asks=[(0.40, 10.0)])
        b_dn = OrderBook(bids=[(0.50, 10.0)], asks=[(0.50, 10.0)])
        self.assertFalse(is_books_consistent(b_up, b_dn))

    def test_delayed_taker_ioc_misses_when_ask_moves_above_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = PaperRunner(ledger_dir=tmp)
            runner.start_window("btc-updown-5m-1000", 1000.0, 1300.0, 75000.0, binance_open_px=75000.0)
            b_up = OrderBook(bids=[(0.40, 20.0)], asks=[(0.42, 20.0)], source_t=1200.0)
            b_dn = OrderBook(bids=[(0.56, 20.0)], asks=[(0.58, 20.0)], source_t=1200.0)
            sp = SpotState(timestamp=1200.0, price=75100.0, open_price=75000.0, binance_open_price=75000.0, basis_ema=0.0)

            # During 300ms wire transit, market makers reprice Up ask from 0.42 to 0.55
            delayed_up = OrderBook(bids=[(0.52, 20.0)], asks=[(0.55, 20.0)], source_t=1200.3)
            dec = Decision(action="taker_buy", side="Up", price=0.42, size=5.0, utility=0.20, reason="test")
            with patch("runtime.runner.plan_decision", return_value=dec), \
                 patch("runtime.runner.time.sleep"):
                runner.step(1200.0, sp, b_up, b_dn, delayed_book_fetcher=lambda side: delayed_up)

            self.assertEqual(runner.pos_up.shares, 0.0)
            rejects = [a for a in runner.actions if a.get("action") == "taker_reject"]
            self.assertTrue(rejects)
            self.assertIn("IOC 错失防滑点", rejects[-1]["reason"])

    def test_dual_horizon_vol_preserves_macro_vol_during_brief_lull(self):
        # 20 minutes of volatile moves followed by 60s calm pause
        hist = []
        px = 75000.0
        for s in range(0, 1200, 2):
            px += (25.0 if (s // 2) % 2 == 0 else -24.5)
            hist.append((float(s), px))
        for s in range(1200, 1260):
            hist.append((float(s), px))
        vol = calculate_dual_horizon_vol(hist, baseline_vol_bps=14.0)
        self.assertGreater(vol, 25.0)

    def test_concurrent_fetch_tick_bundle_uses_thread_local_sessions(self):
        feed = MarketFeed.__new__(MarketFeed)
        feed.session = MagicMock()
        feed.session.headers = {"User-Agent": "test"}
        feed.session.proxies = {}
        feed.spot_history = deque(maxlen=1800)
        feed._last_binance_px = 0.0
        feed._last_binance_t = 0.0
        feed._last_spot_source = "binance_vision"
        feed._thread_local = threading.local()
        feed._executor = ThreadPoolExecutor(max_workers=3)
        feed.net_stats = {"last_calc_t": 1000.0, "bytes_window": 0, "msgs_window": 0, "total_bytes": 0, "poly_latency_ms": 0.0, "binance_latency_ms": 0.0}
        feed.record_raw_event = MagicMock()

        def _fake_spot(sess=None):
            return 64200.0, "binance_vision", 12.0, 40

        def _fake_book(tok, sess=None):
            b = OrderBook(bids=[(0.48, 10.0)], asks=[(0.50, 10.0)], source_t=1000.0, received_at=1000.0)
            return b, 15.0, 120, {"t": 1000.0, "token_id": tok}

        feed._fetch_live_spot_price = _fake_spot
        feed._fetch_order_book_raw = _fake_book
        try:
            sp, bu, bd = feed.fetch_tick_bundle(64100.0, 64150.0, "tok_u", "tok_d", now=1000.0)
            self.assertEqual(sp.price, 64200.0)
            self.assertFalse(sp.is_stale)
            self.assertEqual(bu.best_bid, 0.48)
            self.assertEqual(bd.best_ask, 0.50)
            self.assertEqual(len(feed.spot_history), 1)
        finally:
            feed.close()


if __name__ == "__main__":
    unittest.main()
