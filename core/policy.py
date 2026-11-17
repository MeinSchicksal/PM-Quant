"""Model-Free Dual-Track Policy Engine (v2.4).

Combines two complementary, purely objective tracks (zero Black-Scholes / P*):
1. Dual-Sided Market-Making Track (Rebound & Trend Dip Entry + Trailing Pair Lock):
   - Enters cheap discount legs (<= $0.45) either aligned with objective lead OR
     at a verified rebound turn (e.g. BTC still below strike so Up is cheap <= $0.45,
     but 15s spot momentum and Up orderbook mid are both turning upward).
   - Strictly filters out falling knives (15s adverse book mid drop > 8 cents).
   - Once Leg-1 is filled, lets profit run while opposite ask is still dropping,
     and locks in the risk-free pair as soon as the bounce stalls (+2 ticks off low
     within pair_cost <= 0.97) or hits deep windfall (pair_cost <= 0.90).
2. Single-Side Directional Track (Late-Window High-Conviction & Breakout Run):
   - In late window (15s <= tau <= 120s), enters on strong objective physical lead
     (>= 5.0 bps maker / >= 8.0 bps taker, incorporating locked 60s TWAP average)
     confirmed by market consensus (mid >= 0.52), holding to settlement for full $1.00.
   - If a discount Leg-1 turns into a runaway breakout (lead >= 12.0 bps in our favour),
     exempts from shallow trailing lock to capture full $1.00 single-side convexity.
"""
from typing import Dict, Optional
from .types import Side, OrderBook, Position, Decision, SpotState
from .execution import taker_fee_per_share
from .estimator import compute_objective_lead


def _wait(reason: str, urgent: bool = False) -> Decision:
    """Build a 'wait' decision, declaring whether resting quotes must be pulled."""
    return Decision(action="wait", reason=reason, urgent_cancel=urgent)


def determine_objective_side(
    spot: SpotState,
    tau_s: float,
    params: Dict[str, float],
) -> Optional[Side]:
    """Return the objectively favoured side from lead or verified rebound turn."""
    open_px = float(getattr(spot, "open_price", 0.0) or 0.0)
    lead_bps, _, _ = compute_objective_lead(spot, open_px, tau_s)
    mwde_min_tau = float(params.get("MWDE_MIN_TAU_S", 120.0))
    if tau_s > mwde_min_tau:
        min_lead = float(params.get("MWDE_MIN_LEAD_BPS", 1.5))
        if lead_bps >= min_lead:
            return "Up"
        if lead_bps <= -min_lead:
            return "Down"
        max_dist = float(params.get("MWDE_REBOUND_MAX_DIST_BPS", 8.5))
        min_spot_turn = float(params.get("MWDE_REBOUND_MIN_SPOT_BPS", 1.0))
        min_mid_chg = float(params.get("MWDE_REBOUND_MIN_MID_CHG", 0.015))
        ret_chg = float(getattr(spot, "ret_change_15s_bps", 0.0) or 0.0)
        up_mid_chg = float(getattr(spot, "up_mid_change_15s", 0.0) or 0.0)
        if -max_dist <= lead_bps < min_lead and ret_chg >= min_spot_turn and up_mid_chg >= min_mid_chg:
            return "Up"
        if -min_lead < lead_bps <= max_dist and ret_chg <= -min_spot_turn and (-up_mid_chg) >= min_mid_chg:
            return "Down"
        return None
    else:
        min_lead = float(params.get("LWAA_MIN_LEAD_BPS", 5.0))
        if lead_bps >= min_lead:
            return "Up"
        if lead_bps <= -min_lead:
            return "Down"
        return None


def plan_decision(
    pos_up: Position,
    pos_down: Position,
    book_up: OrderBook,
    book_down: OrderBook,
    spot: SpotState,
    tau_s: float,
    params: Dict[str, float],
    circuit_breaker_active: bool = False,
    circuit_breaker_reason: str = "",
    entries_count: int = 0,
    regime_info=None,
    quote_attempts: int = 0,
) -> Decision:
    """Master decision cycle for the v2.4 dual-track engine."""
    u_shares = pos_up.shares
    d_shares = pos_down.shares
    net_gap = round(u_shares - d_shares, 4)

    # 1. Watchdog: Circuit breaker
    cb_enabled = bool(params.get("ENABLE_CIRCUIT_BREAKER", True))
    cb_active = cb_enabled and (circuit_breaker_active or bool(params.get("CIRCUIT_BREAKER_ACTIVE", False)))
    if cb_active:
        cb_msg = circuit_breaker_reason or params.get("CIRCUIT_BREAKER_REASON", "Active")
        return _wait(f"Circuit breaker active ({cb_msg}): new Leg-1 disabled", urgent=True)

    # 2. Tail freeze applies to EVERY branch (entries, hedges, unwinds)
    min_tau = float(params.get("ENTRY_MIN_TAU_S", params.get("LWAA_MIN_TAU_S", 15.0)))
    if tau_s < min_tau:
        return _wait(f"v2.4: Tail freeze (tau {tau_s:.0f}s < {min_tau:.0f}s), holding to settlement", urgent=True)

    # 3. Objective Physical Lead (pure arithmetic vs strike / 60s TWAP)
    open_px = float(getattr(spot, "open_price", 0.0) or 0.0)
    lead_bps, delta_usd, _ = compute_objective_lead(spot, open_px, tau_s)

    # 4. Inventory State: Deep Pair Lock / Trailing Pair Lock / Single-Side Run / Salvage
    fee_rate = float(params.get("TAKER_FEE_RATE", 0.07))
    if abs(net_gap) >= 1e-4:
        held_side: Side = "Up" if net_gap > 0 else "Down"
        held_shares = abs(net_gap)
        held_basis = pos_up.avg_price if held_side == "Up" else pos_down.avg_price
        opp_side: Side = "Down" if held_side == "Up" else "Up"
        opp_book = book_down if held_side == "Up" else book_up
        held_book = book_up if held_side == "Up" else book_down
        held_lead_bps = lead_bps if held_side == "Up" else -lead_bps

        hedge_ask = opp_book.best_ask
        if hedge_ask is not None:
            fee = taker_fee_per_share(hedge_ask, fee_rate)
            pair_cost = held_basis + hedge_ask + fee

            # 4a. Deep Arbitrage Windfall Lock (locks >= 10% risk-free spread immediately)
            if bool(params.get("ENABLE_ARBITRAGE_HEDGE", True)):
                max_pair_cost = float(params.get("ARBITRAGE_HEDGE_MAX_PAIR_COST", 0.90))
                if pair_cost <= max_pair_cost:
                    return Decision(
                        action="taker_hedge",
                        side=opp_side,
                        price=hedge_ask,
                        size=held_shares,
                        utility=round(held_shares * (1.00 - pair_cost), 4),
                        reason=f"Deep Arbitrage lock: pair_cost={pair_cost:.3f} <= {max_pair_cost:.2f}, take hedge @ {hedge_ask:.2f}"
                    )

            # 4b. Dynamic Trailing Pair Lock for discount market-making entries
            # Waits while rebound is still pushing opp_ask down; locks when opp_ask
            # bounces >= 2 ticks from its post-entry low, unless single-side breakout is running.
            if bool(params.get("ENABLE_TRAILING_HEDGE", True)):
                max_basis = float(params.get("TRAILING_HEDGE_MAX_BASIS", 0.48))
                arm_cost = float(params.get("TRAILING_HEDGE_ARM_COST", 0.95))
                max_trail_pair = float(params.get("TRAILING_HEDGE_MAX_PAIR_COST", 0.97))
                bounce_ticks = float(params.get("TRAILING_HEDGE_BOUNCE_TICKS", 0.02))
                single_run_bps = float(params.get("SINGLE_SIDE_RUN_BPS", 12.0))

                min_opp = getattr(spot, "min_opp_ask_seen", None)
                if min_opp is not None and held_basis <= max_basis and held_lead_bps < single_run_bps:
                    min_fee = taker_fee_per_share(float(min_opp), fee_rate)
                    best_pair_seen = held_basis + float(min_opp) + min_fee
                    if (best_pair_seen <= arm_cost + 1e-6
                            and (hedge_ask - float(min_opp)) >= bounce_ticks - 1e-6
                            and pair_cost <= max_trail_pair + 1e-6):
                        return Decision(
                            action="taker_hedge",
                            side=opp_side,
                            price=hedge_ask,
                            size=held_shares,
                            utility=round(held_shares * (1.00 - pair_cost), 4),
                            reason=f"Trailing Pair Lock: 势头停顿动态锁利 (最低对侧={min_opp:.2f}->现价={hedge_ask:.2f}, 总成本={pair_cost:.3f} <= {max_trail_pair:.2f})"
                        )

        # 4c. Extreme tail defensive salvage (pure objective bid collapse + adverse spot)
        salvage_tau = float(params.get("DEFENSIVE_SALVAGE_TAU_S", 30.0))
        salvage_max_bid = float(params.get("DEFENSIVE_SALVAGE_MAX_BID", 0.15))
        salvage_min_bid = float(params.get("DEFENSIVE_SALVAGE_MIN_BID", 0.06))
        salvage_enabled = (
            bool(params.get("DEFENSIVE_SALVAGE_ENABLE", True))
            and bool(params.get("ALLOW_TAKER_UNWIND", True))
        )
        spot_adverse = held_lead_bps < 0

        if salvage_enabled and tau_s <= salvage_tau and spot_adverse:
            salvage_bid = held_book.best_bid
            if salvage_bid is not None and salvage_min_bid <= salvage_bid <= salvage_max_bid:
                fee = taker_fee_per_share(salvage_bid, fee_rate)
                salvage_cash = held_shares * (salvage_bid - fee)
                return Decision(
                    action="taker_unwind",
                    side=held_side,
                    price=salvage_bid,
                    size=held_shares,
                    utility=round(salvage_bid - fee, 4),
                    reason=f"Defensive Salvage: 尾盘({tau_s:.0f}s)盘口坍塌(bid={salvage_bid:.2f} <= {salvage_max_bid:.2f}, 领先={lead_bps:+.1f}bps)，市价出清残值 (挽回${salvage_cash:.2f})"
                )

        max_shares_cap = float(params.get("LOT_SIZE", 5.0)) * int(params.get("MAX_LOTS", 1))
        anti_toxic_bps = float(params.get("ANTI_TOXICITY_SHOCK_BPS", 2.5))
        vel_bps = abs(float(getattr(spot, "velocity_bps", 0.0) or 0.0))
        is_toxic_or_stale = (
            bool(getattr(spot, "is_toxic_shock", False))
            or vel_bps >= anti_toxic_bps
            or bool(getattr(spot, "is_stale", False))
        )
        should_pull = (held_shares >= max_shares_cap - 1e-4) or is_toxic_or_stale
        return _wait(
            f"v2.4: Holding {held_shares:.0f} {held_side} @ {held_basis:.2f} to settlement (lead={lead_bps:+.1f}bps)",
            urgent=should_pull
        )

    if u_shares > 1e-4 and d_shares > 1e-4 and abs(net_gap) < 1e-4:
        return _wait(
            f"v2.4: Max position reached & fully hedged pair (UP {u_shares:.0f} / DN {d_shares:.0f}), holding to settlement",
            urgent=True
        )

    # 5. Flat Inventory Caps & Risk Envelopes
    max_entries = int(params.get("MAX_ENTRIES_PER_WINDOW", 1))
    if entries_count >= max_entries:
        return _wait(f"Max entries per window reached ({entries_count}/{max_entries}), waiting for next window", urgent=True)

    max_lots = int(params.get("MAX_LOTS", 1))
    lot_size = float(params.get("LOT_SIZE", 5.0))
    size = float(lot_size)
    max_shares = lot_size * max_lots
    if u_shares >= max_shares - 1e-4 or d_shares >= max_shares - 1e-4:
        return _wait(f"Max position reached ({max(u_shares, d_shares):.0f} shares), holding to settlement", urgent=True)

    max_entry_px = float(params.get("ENTRY_MAX_PRICE", params.get("LWAA_HARD_CHASE_CEILING", 0.70)))
    available_cap = float(params.get("AVAILABLE_CAPITAL", 100.0))
    if available_cap < size * max_entry_px:
        return _wait(f"v2.4: Insufficient capital (${available_cap:.2f}) for entry (${size * max_entry_px:.2f})", urgent=True)

    committed = float(params.get("COMMITTED_WINDOW_COST", 0.0))
    window_budget = float(params.get("MAX_WINDOW_LOSS_USD", 5.0))
    if committed + size * max_entry_px > window_budget + 1e-9:
        return _wait(
            f"v2.4: 单窗成本包络已用 ${committed:.2f}/${window_budget:.2f}，拒绝继续开仓",
            urgent=True
        )

    # 6. Anti-Toxicity Guard: cancel resting quotes on rapid Binance shock
    anti_toxic_bps = float(params.get("ANTI_TOXICITY_SHOCK_BPS", 2.5))
    vel_bps = abs(float(getattr(spot, "velocity_bps", 0.0) or 0.0))
    if getattr(spot, "is_toxic_shock", False) or vel_bps >= anti_toxic_bps:
        return _wait(f"v2.4: Anti-toxicity spot shock ({vel_bps:.1f} bps), pulling quotes", urgent=True)

    # 7. Active Time Window & Readiness Guards
    max_tau = float(params.get("ENTRY_MAX_TAU_S", params.get("MWDE_MAX_TAU_S", 240.0)))
    if tau_s > max_tau:
        return _wait(f"v2.4: Observation period (tau {tau_s:.0f}s > {max_tau:.0f}s)", urgent=True)

    if params.get("REQUIRE_STRIKE_AND_BASIS", False):
        if not params.get("BASIS_READY", True) or open_px <= 0:
            return _wait("v2.4: Waiting for official strike & basis calibration", urgent=True)

    if getattr(spot, "is_stale", False):
        return _wait("v2.4: Spot price stale, holding off entry", urgent=True)

    # Strict book consistency check (no fake shifting)
    au, ad = book_up.best_ask, book_down.best_ask
    bu, bd = book_up.best_bid, book_down.best_bid
    if au is None or ad is None or bu is None or bd is None:
        return _wait("v2.4: Incomplete orderbook snapshot, waiting", urgent=True)
    if bool(params.get("STRICT_BOOK_CONSISTENCY", True)):
        if (au + ad < 0.995) or (bu + bd > 1.005) or (bu >= au) or (bd >= ad):
            return _wait(
                f"v2.4: Inconsistent book snapshot (ask_sum={au+ad:.3f}, bid_sum={bu+bd:.3f}), skipping",
                urgent=True
            )

    max_quote_attempts = int(params.get("MAX_QUOTE_ATTEMPTS", 3))
    max_book_spread = float(params.get("MAX_ORDERBOOK_SPREAD", 0.06))
    min_entry_px = float(params.get("ENTRY_MIN_PRICE", 0.20))
    ret_chg_15s = float(getattr(spot, "ret_change_15s_bps", 0.0) or 0.0)
    up_mid_chg_15s = float(getattr(spot, "up_mid_change_15s", 0.0) or 0.0)
    max_mid_drop = float(params.get("MWDE_MAX_ADVERSE_MID_DROP", 0.08))

    # 8. Track 1: Mid-Window Trend & Rebound Dip Entry (MWDE, 120s < tau <= 240s)
    mwde_min_tau = float(params.get("MWDE_MIN_TAU_S", 120.0))
    if bool(params.get("MWDE_ENABLE", True)) and tau_s > mwde_min_tau:
        prev_move = float(params.get("PREV_ROUND_ABS_MOVE", 0.0))
        post_shock_min = float(params.get("POST_SHOCK_MIN_MOVE", 40.0))
        mwde_max_px = float(params.get("MWDE_MAX_ENTRY_PRICE", 0.45))

        if prev_move >= post_shock_min:
            up_ref = book_up.mid or 0.50
            dn_ref = book_down.mid or 0.50
            fav_side: Side = "Down" if dn_ref > up_ref else "Up"
            fav_book = book_down if fav_side == "Down" else book_up
            fav_ask, fav_bid = fav_book.best_ask, fav_book.best_bid
            lwaa_max_px = float(params.get("LWAA_MAX_ENTRY_PRICE", 0.65))
            fav_lead_bps = lead_bps if fav_side == "Up" else -lead_bps
            if fav_lead_bps > -2.0:
                if quote_attempts < max_quote_attempts and fav_bid is not None and mwde_max_px <= fav_bid <= lwaa_max_px and fav_book.spread <= max_book_spread + 1e-6:
                    return Decision(
                        action="quote",
                        side=fav_side,
                        price=round(fav_bid, 2),
                        size=size,
                        utility=round(abs(lead_bps), 2),
                        reason=f"Post-Shock Consensus: 前盘大波动(${prev_move:.1f})后挂单高价侧 {fav_side} bid={fav_bid:.2f}"
                    )
                taker_min_lead = float(params.get("POST_SHOCK_TAKER_MIN_LEAD_BPS", 3.0))
                if fav_lead_bps >= taker_min_lead and fav_ask is not None and mwde_max_px <= fav_ask <= lwaa_max_px and fav_book.spread <= max_book_spread + 1e-6:
                    return Decision(
                        action="taker_buy",
                        side=fav_side,
                        price=fav_ask,
                        size=size,
                        utility=round(abs(lead_bps), 2),
                        reason=f"Post-Shock Consensus: 前盘大波动(${prev_move:.1f})后顺高价共识侧吃单 {fav_side} @ {fav_ask:.2f}"
                    )
            # Fall through to normal MWDE discount market-making if Post-Shock has no safe entry

        min_lead_bps = float(params.get("MWDE_MIN_LEAD_BPS", 1.5))
        max_rebound_dist = float(params.get("MWDE_REBOUND_MAX_DIST_BPS", 8.5))
        min_rebound_spot = float(params.get("MWDE_REBOUND_MIN_SPOT_BPS", 1.0))
        min_rebound_mid = float(params.get("MWDE_REBOUND_MIN_MID_CHG", 0.015))

        pref_side: Optional[Side] = None
        entry_mode = ""

        # Mode A: Rebound Turn Entry (price below/above open so token is cheap <= $0.45,
        # while both short-term BTC spot and orderbook probability are turning back toward open!)
        if (-max_rebound_dist <= lead_bps < min_lead_bps
                and ret_chg_15s >= min_rebound_spot
                and up_mid_chg_15s >= min_rebound_mid
                and bu <= mwde_max_px):
            pref_side = "Up"
            entry_mode = f"水下止跌抬头低吸(15s现价{ret_chg_15s:+.1f}bps, 盘口+{up_mid_chg_15s*100:.1f}%)"
        elif (-min_lead_bps < lead_bps <= max_rebound_dist
                and ret_chg_15s <= -min_rebound_spot
                and (-up_mid_chg_15s) >= min_rebound_mid
                and bd <= mwde_max_px):
            pref_side = "Down"
            entry_mode = f"高位回调抬头低吸(15s现价{ret_chg_15s:+.1f}bps, 盘口+{(-up_mid_chg_15s)*100:.1f}%)"
        # Mode B: Trend-Aligned Dip Entry (spot already leading >= 1.5 bps while token still <= $0.45)
        elif abs(lead_bps) >= min_lead_bps:
            pref_side = "Up" if lead_bps > 0 else "Down"
            entry_mode = f"顺势低吸(lead={lead_bps:+.1f}bps, ΔS=${delta_usd:+.1f})"

        if pref_side is None:
            return _wait(
                f"v2.4 MWDE: 现货偏离不足 ({lead_bps:+.1f} bps < ±{min_lead_bps:.1f} bps) 且无止跌抬头信号，过滤零附近震荡噪音",
                urgent=False
            )

        # Falling-knife guard: block entry if target side orderbook mid is crashing AND spot is moving against it
        my_mid_chg = up_mid_chg_15s if pref_side == "Up" else -up_mid_chg_15s
        spot_against = (pref_side == "Up" and ret_chg_15s < -1.0) or (pref_side == "Down" and ret_chg_15s > 1.0)
        if my_mid_chg < -max_mid_drop and spot_against:
            return _wait(
                f"v2.4 MWDE: 防飞刀保护拦截 ({pref_side} 15s盘口暴跌{my_mid_chg*100:.1f}% 且现货反向{ret_chg_15s:+.1f}bps)，拒绝接飞刀",
                urgent=True
            )

        tb = book_up if pref_side == "Up" else book_down
        ask, bid = tb.best_ask, tb.best_bid

        # Deep discount pair taker (Up ask + Down ask < 0.90)
        taker_pair_thresh = float(params.get("MWDE_TAKER_PAIR_COST", 0.90))
        if ask is not None and min_entry_px <= ask <= mwde_max_px and (au + ad) < taker_pair_thresh:
            return Decision(
                action="taker_buy",
                side=pref_side,
                price=ask,
                size=size,
                utility=round(abs(lead_bps), 2),
                reason=f"v2.4 MWDE Taker: 双边ask合计={au+ad:.2f} < {taker_pair_thresh:.2f} ({entry_mode}) {pref_side} @ {ask:.2f}"
            )

        # Discount Maker Quote (bid <= $0.45)
        if quote_attempts < max_quote_attempts and bid is not None and min_entry_px <= bid <= mwde_max_px and tb.spread <= max_book_spread + 1e-6:
            maker_px = round(bid, 2)
            if ask is not None and bid + 0.01 < ask and bid + 0.01 <= mwde_max_px:
                maker_px = round(bid + 0.01, 2)
            return Decision(
                action="quote",
                side=pref_side,
                price=maker_px,
                size=size,
                utility=round(abs(lead_bps), 2),
                reason=f"v2.4 MWDE Maker: {entry_mode} {pref_side} bid={maker_px:.2f} <= ${mwde_max_px:.2f}"
            )

        return _wait(
            f"v2.4 MWDE: 目标侧 {pref_side} ({entry_mode}) 盘口 {bid}/{ask} 高于折价上限 ${mwde_max_px:.2f}，等待",
            urgent=False
        )

    # 9. Track 2: Late-Window Single-Side Prediction (LWAA, 15s <= tau <= 120s)
    lwaa_min_lead = float(params.get("LWAA_MIN_LEAD_BPS", 5.0))
    lwaa_taker_lead = float(params.get("LWAA_TAKER_LEAD_BPS", 8.0))
    lwaa_min_px = float(params.get("LWAA_MIN_ENTRY_PRICE", 0.20))
    lwaa_max_px = float(params.get("LWAA_MAX_ENTRY_PRICE", 0.65))
    hard_ceiling = float(params.get("LWAA_HARD_CHASE_CEILING", 0.70))
    min_implied = float(params.get("LWAA_MIN_MARKET_IMPLIED", 0.52))

    if abs(lead_bps) < lwaa_min_lead:
        return _wait(
            f"v2.4 LWAA: 尾盘客观领先不足 ({lead_bps:+.1f} bps < ±{lwaa_min_lead:.1f} bps)，放弃博弈",
            urgent=False
        )

    pref_side = "Up" if lead_bps > 0 else "Down"
    tb = book_up if pref_side == "Up" else book_down
    ask, bid = tb.best_ask, tb.best_bid
    mkt_mid = tb.mid or bid or ask or 0.50

    if mkt_mid < min_implied:
        return _wait(
            f"v2.4 LWAA: 盘口共识({mkt_mid:.2f})未站上 {min_implied:.2f}，不逆市场真金白银方向 ({pref_side}, lead={lead_bps:+.1f}bps)",
            urgent=False
        )

    if abs(lead_bps) >= lwaa_taker_lead and ask is not None and lwaa_min_px <= ask <= min(lwaa_max_px, hard_ceiling):
        return Decision(
            action="taker_buy",
            side=pref_side,
            price=ask,
            size=size,
            utility=round(abs(lead_bps), 2),
            reason=f"v2.4 LWAA Taker IOC: 单侧高确信预测 {pref_side} ask={ask:.2f} (客观领先={lead_bps:+.1f}bps, ΔS=${delta_usd:+.1f}, 盘口={mkt_mid:.2f})"
        )

    if quote_attempts < max_quote_attempts and bid is not None and lwaa_min_px <= bid <= min(lwaa_max_px, hard_ceiling) and tb.spread <= max_book_spread + 1e-6:
        maker_px = round(bid, 2)
        if ask is not None and bid + 0.01 < ask and bid + 0.01 <= lwaa_max_px:
            maker_px = round(bid + 0.01, 2)
        return Decision(
            action="quote",
            side=pref_side,
            price=maker_px,
            size=size,
            utility=round(abs(lead_bps), 2),
            reason=f"v2.4 LWAA Maker: 单侧高确信预测 {pref_side} bid={maker_px:.2f} (客观领先={lead_bps:+.1f}bps, ΔS=${delta_usd:+.1f}, 盘口={mkt_mid:.2f})"
        )

    return _wait(
        f"v2.4 LWAA: {pref_side} (lead={lead_bps:+.1f}bps) 盘口 {bid}/{ask} 超出安全区间 [${lwaa_min_px:.2f}, ${lwaa_max_px:.2f}]",
        urgent=False
    )
