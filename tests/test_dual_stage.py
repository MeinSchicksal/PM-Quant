"""Unit tests for PM-Quant v2.4 Model-Free Objective Dual-Stage Engine invariants."""
import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.types import OrderBook, Position, SpotState
from core.policy import plan_decision


class TestV24ObjectivePolicy(unittest.TestCase):
    def setUp(self):
        self.params = dict(
            config.PARAMS,
            ENTRY_MIN_TAU_S=15.0,
            ENTRY_MAX_TAU_S=240.0,
            ENTRY_MIN_PRICE=0.20,
            ENTRY_MAX_PRICE=0.70,
            MAX_QUOTE_ATTEMPTS=3,
            ENABLE_ARBITRAGE_HEDGE=False,
        )

    def test_mwde_discount_maker_quote_when_spot_leads(self):
        """Mid-window (120 < tau <= 240) quotes Maker when spot leads >= 1.5 bps and bid <= $0.45."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.40, 20.0)], asks=[(0.44, 20.0)])
        book_dn = OrderBook(bids=[(0.56, 20.0)], asks=[(0.60, 20.0)])
        # +2.0 bps lead (+15 on 75000)
        spot = SpotState(timestamp=1100.0, price=75015.0, open_price=75000.0)

        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=200.0, params=self.params)
        self.assertEqual(dec.action, "quote")
        self.assertEqual(dec.side, "Up")
        self.assertAlmostEqual(dec.price, 0.41)
        self.assertEqual(dec.size, 5.0)
        self.assertIn("MWDE Maker", dec.reason)

    def test_lwaa_taker_ioc_when_objective_lead_exceeds_taker_threshold(self):
        """Late window (tau <= 120s) executes Taker IOC when lead >= 8.0 bps and market confirms."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.58, 20.0)], asks=[(0.60, 20.0)])
        book_dn = OrderBook(bids=[(0.39, 20.0)], asks=[(0.42, 20.0)])
        # +10.0 bps lead (+75 on 75000)
        spot = SpotState(timestamp=1240.0, price=75075.0, open_price=75000.0)

        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=60.0, params=self.params)
        self.assertEqual(dec.action, "taker_buy")
        self.assertEqual(dec.side, "Up")
        self.assertAlmostEqual(dec.price, 0.60)
        self.assertEqual(dec.size, 5.0)

    def test_rejects_expensive_ask_beyond_entry_ceiling(self):
        """Prices above LWAA_MAX_ENTRY_PRICE / HARD_CHASE_CEILING are strictly skipped."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.78, 20.0)], asks=[(0.80, 20.0)])
        book_dn = OrderBook(bids=[(0.19, 20.0)], asks=[(0.21, 20.0)])
        spot = SpotState(timestamp=1200.0, price=75150.0, open_price=75000.0)

        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=100.0, params=self.params)
        self.assertEqual(dec.action, "wait")

    def test_holding_preserves_convexity_when_hedge_disabled(self):
        """Holds Leg-1 to settlement without capping winners via pair_cost <= 1.00."""
        pos_up = Position(shares=5.0, cash=2.10)
        pos_dn = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.60, 50.0)], asks=[(0.65, 50.0)])
        book_dn = OrderBook(bids=[(0.33, 50.0)], asks=[(0.35, 50.0)])
        spot = SpotState(timestamp=1200.0, price=75020.0, open_price=75000.0)

        dec = plan_decision(pos_up, pos_dn, book_up, book_dn, spot, tau_s=100.0, params=self.params)
        self.assertEqual(dec.action, "wait")
        self.assertIn("Holding 5 Up", dec.reason)

    def test_deep_arbitrage_hedge_locks_risk_free_pair(self):
        """When ENABLE_ARBITRAGE_HEDGE=True, deep pair_cost <= 0.90 executes hedge."""
        pos_up = Position(shares=5.0, cash=2.10)  # @ 0.42
        pos_dn = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.60, 50.0)], asks=[(0.65, 50.0)])
        book_dn = OrderBook(bids=[(0.30, 50.0)], asks=[(0.35, 50.0)])
        spot = SpotState(timestamp=1200.0, price=75020.0, open_price=75000.0)

        p = dict(self.params, ENABLE_ARBITRAGE_HEDGE=True, ARBITRAGE_HEDGE_MAX_PAIR_COST=0.90)
        dec = plan_decision(pos_up, pos_dn, book_up, book_dn, spot, tau_s=100.0, params=p)
        self.assertEqual(dec.action, "taker_hedge")
        self.assertEqual(dec.side, "Down")

    def test_fully_hedged_pair_holds_to_settlement(self):
        """Balanced Up/Down pair holds cleanly for $1.00 redemption."""
        pos_up = Position(shares=5.0, cash=2.10)
        pos_dn = Position(shares=5.0, cash=1.80)
        book_up = OrderBook(bids=[(0.60, 50.0)], asks=[(0.65, 50.0)])
        book_dn = OrderBook(bids=[(0.30, 50.0)], asks=[(0.35, 50.0)])
        spot = SpotState(timestamp=1200.0, price=75000.0, open_price=75000.0)

        dec = plan_decision(pos_up, pos_dn, book_up, book_dn, spot, tau_s=100.0, params=self.params)
        self.assertEqual(dec.action, "wait")
        self.assertIn("fully hedged pair", dec.reason.lower())

    def test_forbids_late_entry_without_objective_lead(self):
        """Late window blocks entry if objective lead < LWAA_MIN_LEAD_BPS."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.52, 20.0)], asks=[(0.54, 20.0)])
        book_dn = OrderBook(bids=[(0.46, 20.0)], asks=[(0.48, 20.0)])
        # Only +1.33 bps lead (< 5.0 bps)
        spot = SpotState(timestamp=1240.0, price=75010.0, open_price=75000.0)

        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=60.0, params=self.params)
        self.assertEqual(dec.action, "wait")
        self.assertIn("尾盘客观领先不足", dec.reason)

    def test_rebound_turn_entry_below_strike_when_spot_and_book_turn_up(self):
        """Even when BTC is still below strike (-3.3 bps), if 15s spot & Up mid turn upward, enter Up <= $0.45."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.41, 20.0)], asks=[(0.43, 20.0)])
        book_dn = OrderBook(bids=[(0.56, 20.0)], asks=[(0.58, 20.0)])
        # Spot is 74975 (-3.33 bps below 75000), but rebounding (+2.0 bps in 15s, Up mid +0.03 in 15s)
        spot = SpotState(
            timestamp=1100.0, price=74975.0, open_price=75000.0,
            ret_change_15s_bps=2.0, up_mid_change_15s=0.03
        )
        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=200.0, params=self.params)
        self.assertEqual(dec.action, "quote")
        self.assertEqual(dec.side, "Up")
        self.assertAlmostEqual(dec.price, 0.42)
        self.assertIn("水下止跌抬头低吸", dec.reason)

    def test_falling_knife_guard_blocks_entry_during_book_crash(self):
        """Blocks discount entry if target side book mid crashed > 8 cents in 15s while spot moves against it."""
        pos = Position(0.0, 0.0)
        book_up = OrderBook(bids=[(0.41, 20.0)], asks=[(0.43, 20.0)])
        book_dn = OrderBook(bids=[(0.56, 20.0)], asks=[(0.58, 20.0)])
        spot = SpotState(
            timestamp=1100.0, price=75015.0, open_price=75000.0,
            ret_change_15s_bps=-2.5, up_mid_change_15s=-0.12
        )
        dec = plan_decision(pos, pos, book_up, book_dn, spot, tau_s=200.0, params=self.params)
        self.assertEqual(dec.action, "wait")
        self.assertTrue(dec.urgent_cancel)
        self.assertIn("防飞刀保护拦截", dec.reason)

    def test_trailing_pair_lock_and_single_side_breakout_exemption(self):
        """Trailing pair lock waits at new low, locks on +2 tick bounce, and exempts >= 12 bps single-side breakout."""
        pos_up = Position(shares=5.0, cash=2.10)  # Held Up @ $0.42
        pos_dn = Position(0.0, 0.0)
        p = dict(self.params, ENABLE_ARBITRAGE_HEDGE=True, ENABLE_TRAILING_HEDGE=True)

        # 1. Opposite ask at new low 0.49 (min_opp_ask_seen=0.49, bounce=0.00): let profit run!
        b_up1 = OrderBook(bids=[(0.50, 20.0)], asks=[(0.52, 20.0)])
        b_dn1 = OrderBook(bids=[(0.47, 20.0)], asks=[(0.49, 20.0)])
        sp1 = SpotState(timestamp=1150.0, price=75010.0, open_price=75000.0, min_opp_ask_seen=0.49)
        dec1 = plan_decision(pos_up, pos_dn, b_up1, b_dn1, sp1, tau_s=150.0, params=p)
        self.assertEqual(dec1.action, "wait")

        # 2. Rebound stalls: opposite ask bounces +2 ticks from 0.49 to 0.51 (pair_cost ~ 0.947 <= 0.97) -> lock pair!
        b_up2 = OrderBook(bids=[(0.48, 20.0)], asks=[(0.50, 20.0)])
        b_dn2 = OrderBook(bids=[(0.49, 20.0)], asks=[(0.51, 20.0)])
        sp2 = SpotState(timestamp=1155.0, price=75005.0, open_price=75000.0, min_opp_ask_seen=0.49)
        dec2 = plan_decision(pos_up, pos_dn, b_up2, b_dn2, sp2, tau_s=145.0, params=p)
        self.assertEqual(dec2.action, "taker_hedge")
        self.assertEqual(dec2.side, "Down")
        self.assertAlmostEqual(dec2.price, 0.51)
        self.assertIn("Trailing Pair Lock", dec2.reason)

        # 3. Single-side breakout exemption: if lead >= 12 bps (+15 bps), hold single side for full $1.00!
        sp_breakout = SpotState(timestamp=1155.0, price=75115.0, open_price=75000.0, min_opp_ask_seen=0.49)
        dec3 = plan_decision(pos_up, pos_dn, b_up2, b_dn2, sp_breakout, tau_s=145.0, params=p)
        self.assertEqual(dec3.action, "wait")

    def test_post_shock_consensus_directional_guard_and_maker_preference(self):
        """Post-Shock Consensus refuses counter-spot chasing and falls back to MWDE, prefers Maker quote, and uses Taker only on strong lead."""
        p = dict(self.params, PREV_ROUND_ABS_MOVE=100.0, POST_SHOCK_MIN_MOVE=90.0)
        b_up = OrderBook(bids=[(0.38, 20.0)], asks=[(0.40, 20.0)])
        b_dn = OrderBook(bids=[(0.60, 20.0)], asks=[(0.62, 20.0)])

        # 1. Spot opposes Down consensus by +4.0 bps -> refuses Down @ 0.62 and falls back to MWDE (Up @ 0.38)
        sp_oppose = SpotState(timestamp=1070.0, price=75030.0, open_price=75000.0)
        dec_fallback = plan_decision(Position(0, 0), Position(0, 0), b_up, b_dn, sp_oppose, tau_s=230.0, params=p)
        self.assertEqual(dec_fallback.action, "quote")
        self.assertEqual(dec_fallback.side, "Up")
        self.assertAlmostEqual(dec_fallback.price, 0.39)

        # 2. Spot slightly confirms Down (-1.0 bps) and spread is tight -> prefers Maker quote @ 0.60
        sp_confirm = SpotState(timestamp=1070.0, price=74992.5, open_price=75000.0)
        dec_quote = plan_decision(Position(0, 0), Position(0, 0), b_up, b_dn, sp_confirm, tau_s=230.0, params=p)
        self.assertEqual(dec_quote.action, "quote")
        self.assertEqual(dec_quote.side, "Down")
        self.assertAlmostEqual(dec_quote.price, 0.60)

        # 3. With quote attempts exhausted and strong spot lead (-4.0 bps -> fav_lead +4.0), Taker IOC is allowed
        sp_strong = SpotState(timestamp=1070.0, price=74970.0, open_price=75000.0)
        dec_taker = plan_decision(
            Position(0, 0), Position(0, 0), b_up, b_dn, sp_strong, tau_s=230.0,
            params=p, quote_attempts=int(p.get("MAX_QUOTE_ATTEMPTS", 5))
        )
        self.assertEqual(dec_taker.action, "taker_buy")
        self.assertEqual(dec_taker.side, "Down")


if __name__ == "__main__":
    unittest.main()
