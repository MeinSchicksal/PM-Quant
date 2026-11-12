"""Unified Policy Engine (v2.2 Continuous Dynamic Dual Absorption).

A single decision cycle combining Mid-Window Trend-Biased Discount Entry (MWDE)
with Late-Window Asymmetric Absorption (LWAA). Inventory is held to settlement;
the opposite leg is only bought when it locks a risk-free pair (pair_cost <= 1.00).
"""
from typing import Dict, Optional
from .types import Side, OrderBook, Position, Decision, SpotState
from .execution import taker_fee_per_share


def _wait(reason: str, urgent: bool = False) -> Decision:
    """Build a 'wait' decision, explicitly declaring whether resting quotes
    must be pulled immediately.

    Structural reasons (toxic shock, tail freeze, regime lockout, risk
    envelope breach, lifecycle caps) set urgent=True; soft reasons (thin
    opposite ask, no edge, insufficient capital) keep resting quotes so the
    queue position is not lost to noise.
    """
    return Decision(action="wait", reason=reason, urgent_cancel=urgent)


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
    p_fair: Optional[float] = None,
    entries_count: int = 0,
    regime_info=None,
    quote_attempts: int = 0
) -> Decision:
    """Master decision cycle for the v2.2 continuous dual-stage absorption engine.

    1. Early Window (tau > MWDE_MAX_TAU_S): standby, no quoting.
    2. Stage 1 - MWDE (MWDE_MIN_TAU_S < tau <= MWDE_MAX_TAU_S): trend-biased
       discount entry. After a large prior-round move, favour the higher-priced
       (market-consensus) side; otherwise follow the current drift, buying the
       preferred side only at a discount (ask/bid <= MWDE_MAX_ENTRY_PRICE).
    3. Stage 2 - LWAA (LWAA_MIN_TAU_S <= tau <= LWAA_MAX_TAU_S): high-certainty
       absorption. When p_fair(side) >= LWAA_MIN_PROB, take the ask if it leaves
       net edge within the entry corridor, else work a maker bid.
    4. Position handling: a single leg is held to settlement (never unwound);
       the opposite leg is only bought when it locks pair_cost <= 1.00.
    5. Tail Freeze (tau < LWAA_MIN_TAU_S): freeze new entries, pull resting
       quotes, hold inventory to settlement.
    """
    from .estimator import estimate_p_fair

    u_shares = pos_up.shares
    d_shares = pos_down.shares
    net_gap = round(u_shares - d_shares, 4)

    # Watchdog 1: Circuit breaker check
    cb_active = circuit_breaker_active or bool(params.get("CIRCUIT_BREAKER_ACTIVE", False))
    if cb_active:
        cb_msg = circuit_breaker_reason or params.get("CIRCUIT_BREAKER_REASON", "Active")
        return _wait(f"Circuit breaker active ({cb_msg}): new Leg-1 disabled", urgent=True)

    # 1. Evaluate Probability
    if p_fair is None:
        p_fair = estimate_p_fair(
            spot,
            getattr(spot, "open_price", 0.0),
            tau_s,
            getattr(spot, "realized_vol_bps", 15.0),
            poly_price=getattr(spot, "poly_price", None),
        )

    # 2. Check Inventory State: Unpaired / Holding
    if abs(net_gap) >= 1e-4:
        held_side: Side = "Up" if net_gap > 0 else "Down"
        held_shares = abs(net_gap)
        held_basis = pos_up.avg_price if held_side == "Up" else pos_down.avg_price
        opp_side: Side = "Down" if held_side == "Up" else "Up"
        opp_book = book_down if held_side == "Up" else book_up
        p_fair_held = p_fair if held_side == "Up" else (1.0 - p_fair)
        fee_rate = float(params.get("TAKER_FEE_RATE", 0.07))

        # Check for arbitrage hedge:
        # User constraint: "不着急买入另外一侧止损，就死扛到期"
        # Only take hedge if opposite side is cheap enough to lock guaranteed profit (pair_cost <= max_lock_cost)
        max_pair_cost = float(params.get("MWDE_HEDGE_LOCK_COST", 0.85))
        if bool(params.get("LWAA_ENABLE_ARBITRAGE_HEDGE", True)):
            max_pair_cost = max(max_pair_cost, 1.00)
        hedge_ask = opp_book.best_ask
        if hedge_ask is not None:
            fee = taker_fee_per_share(hedge_ask, fee_rate)
            pair_cost = held_basis + hedge_ask + fee
            if pair_cost <= max_pair_cost:
                u_hedge = held_shares * (1.00 - pair_cost)
                return Decision(
                    action="taker_hedge",
                    side=opp_side,
                    price=hedge_ask,
                    size=held_shares,
                    utility=round(u_hedge, 4),
                    reason=f"Arbitrage lock: pair_cost={pair_cost:.3f} <= {max_pair_cost:.2f}, take hedge @ {hedge_ask}"
                )

        # High-conviction directional mode: NEVER UNWIND!
        # "不着急买入另外一侧止损，死扛让子弹飞至到期结算"
        return _wait(
            f"v2.2: Holding {held_shares:.0f} {held_side} @ {held_basis:.2f} to settlement (死扛让子弹飞，不急止损, p_fair={p_fair_held:.2f})",
            urgent=True
        )

    # 2b. Fully Hedged Arbitrage Pair (Both sides held and balanced)
    if u_shares > 1e-4 and d_shares > 1e-4 and abs(net_gap) < 1e-4:
        return _wait(
            f"v2.2: Max position reached & fully hedged arbitrage pair (UP {u_shares:.0f} / DN {d_shares:.0f}), holding to settlement for guaranteed $1.00 payout",
            urgent=True
        )

    # 3. Flat Inventory: Sizing, Horizon, and Entry Gate
    # Max entries per window check
    max_entries = int(params.get("MAX_ENTRIES_PER_WINDOW", 1))
    if entries_count >= max_entries:
        return _wait(f"Max entries per window reached ({entries_count}/{max_entries}), waiting for next window", urgent=True)

    # Max position reached check
    max_lots = int(params.get("MAX_LOTS", 2))
    lot_size = float(params.get("LOT_SIZE", 5.0))
    size = float(lot_size)
    available_cap = float(params.get("AVAILABLE_CAPITAL", 100.0))
    fee_rate = float(params.get("TAKER_FEE_RATE", 0.07))
    max_shares = lot_size * max_lots
    if u_shares >= max_shares - 1e-4 or d_shares >= max_shares - 1e-4:
        return _wait(f"Max position reached ({max(u_shares, d_shares):.0f} shares), holding to settlement", urgent=True)

    # Check Micro-Regime Toxicity
    if getattr(spot, "is_toxic_shock", False):
        return _wait("Adverse spot toxic shock detected", urgent=True)

    min_tau = float(params.get("LWAA_MIN_TAU_S", 15.0))
    if tau_s < min_tau:
        return _wait(f"v2.2: Tail freeze (tau {tau_s:.0f}s < {min_tau:.0f}s), holding to settlement", urgent=True)

    # -------------------------------------------------------------
    # STAGE 1: 前半程顺势低吸 (Mid-Window Trend-Biased Discount Entry)
    # Window: tau in [120s, 240s] (elapsed 60s ~ 180s)
    # -------------------------------------------------------------
    mwde_min_tau = float(params.get("MWDE_MIN_TAU_S", 120.0))
    mwde_max_tau = float(params.get("MWDE_MAX_TAU_S", 240.0))
    mwde_enabled = bool(params.get("MWDE_ENABLE", True))

    ref_open = getattr(spot, "binance_open_price", None) or spot.open_price
    delta_s = spot.price - ref_open if ref_open > 0 else 0.0
    p_up = p_fair
    p_down = 1.0 - p_fair

    if mwde_enabled and mwde_min_tau < tau_s <= mwde_max_tau:
        # 大涨/大跌警惕机制:
        # 用户原则: "大涨/跌之后要警惕，可能不会继续涨/跌，可以先买价格高的一侧"
        prev_move = float(params.get("PREV_ROUND_ABS_MOVE", 0.0))
        post_shock_min_move = float(params.get("POST_SHOCK_MIN_MOVE", 40.0))
        max_entry_px = float(params.get("LWAA_MAX_ENTRY_PRICE", 0.65))
        hard_chase_ceiling = float(params.get("LWAA_HARD_CHASE_CEILING", 0.70))

        if prev_move >= post_shock_min_move:
            # 识别盘口高价侧 (胜率更高、市场共识侧)
            up_ask = book_up.best_ask or 0.50
            dn_ask = book_down.best_ask or 0.50
            up_bid = book_up.best_bid or 0.50
            dn_bid = book_down.best_bid or 0.50
            up_ref = (up_ask + up_bid) / 2.0
            dn_ref = (dn_ask + dn_bid) / 2.0

            if dn_ref > up_ref:
                favored_side: Side = "Down"
                favored_p = p_down
                favored_book = book_down
            else:
                favored_side: Side = "Up"
                favored_p = p_up
                favored_book = book_up

            favored_ask = favored_book.best_ask
            favored_bid = favored_book.best_bid

            # 1. 高价侧如果在安全成本内 (<= 0.65)，优先吃单高价侧
            if favored_ask is not None and 0.45 <= favored_ask <= max_entry_px:
                fee = taker_fee_per_share(favored_ask, fee_rate)
                return Decision(
                    action="taker_buy",
                    side=favored_side,
                    price=favored_ask,
                    size=size,
                    utility=round(favored_p - (favored_ask + fee), 4),
                    reason=f"Post-Shock Consensus: 大涨大跌后警惕延续(前盘${prev_move:.1f})，优先吃单高价侧 {favored_side} ask={favored_ask:.2f} <= ${max_entry_px:.2f}"
                )

            # 2. 如果高价侧超过 0.70，坚决不追高
            if favored_ask is not None and favored_ask > hard_chase_ceiling:
                return _wait(f"v2.2: 大涨大跌后高价侧 {favored_side} ask={favored_ask:.2f} 超过 ${hard_chase_ceiling:.2f}，坚决不追高防守", urgent=True)

            # 3. 如果高价侧买一价合适，挂单高价侧
            max_quote_attempts = int(params.get("LWAA_MAX_QUOTE_ATTEMPTS", 1))
            if quote_attempts < max_quote_attempts and favored_bid is not None and 0.45 <= favored_bid <= max_entry_px:
                maker_px = round(min(max_entry_px, favored_bid), 2)
                return Decision(
                    action="quote",
                    side=favored_side,
                    price=maker_px,
                    size=size,
                    utility=round(favored_p - maker_px, 4),
                    reason=f"Post-Shock Consensus: 大涨大跌后警惕延续(前盘${prev_move:.1f})，挂单高价侧 {favored_side} bid={maker_px:.2f}"
                )

            return _wait(f"v2.2: 大涨大跌后警惕(前盘${prev_move:.1f})，高价侧 {favored_side} 盘口无安全成本，防守等待")

        # 正常波动区间: 顺势低吸侧选择 (同步看当前距离开盘价的涨跌大小，结合胜率优先选边)
        if delta_s > 0 or p_up >= 0.50:
            pref_side: Side = "Up"
            pref_p = p_up
            target_book = book_up
        else:
            pref_side: Side = "Down"
            pref_p = p_down
            target_book = book_down

        mwde_max_px = float(params.get("MWDE_MAX_ENTRY_PRICE", 0.45))
        if available_cap < size * mwde_max_px:
            return _wait(f"v2.2: Insufficient capital (${available_cap:.2f}) for MWDE entry (${size * mwde_max_px:.2f})")

        best_ask = target_book.best_ask
        best_bid = target_book.best_bid

        # 1. Taker Absorption on Preferred Side if ask <= $0.45
        if best_ask is not None and 0.10 <= best_ask <= mwde_max_px:
            fee = taker_fee_per_share(best_ask, fee_rate)
            if pref_p - (best_ask + fee) >= 0.01:
                return Decision(
                    action="taker_buy",
                    side=pref_side,
                    price=best_ask,
                    size=size,
                    utility=round(pref_p - (best_ask + fee), 4),
                    reason=f"MWDE Taker Absorption: 顺势低吸 {pref_side} ask={best_ask:.2f} <= ${mwde_max_px:.2f} (ΔS={delta_s:+.1f}, P*={pref_p:.2f})"
                )

        # 2. Dynamic Maker Absorption on Preferred Side if bid <= $0.45
        max_quote_attempts = int(params.get("LWAA_MAX_QUOTE_ATTEMPTS", 1))
        if quote_attempts < max_quote_attempts and best_bid is not None and 0.05 <= best_bid <= mwde_max_px:
            # Dynamic pricing: join best_bid or improve inside spread, bounded by $0.45
            maker_px = round(min(mwde_max_px, best_bid), 2)
            if best_ask is not None and best_bid + 0.01 < best_ask and best_bid + 0.01 <= mwde_max_px:
                maker_px = round(best_bid + 0.01, 2)
            if 0.10 <= maker_px <= mwde_max_px:
                return Decision(
                    action="quote",
                    side=pref_side,
                    price=maker_px,
                    size=size,
                    utility=round(pref_p - maker_px, 4),
                    reason=f"MWDE Maker Absorption: 顺势低吸挂单 {pref_side} bid={maker_px:.2f} <= ${mwde_max_px:.2f}"
                )

        return _wait(f"v2.2: Standby in early/mid window (tau {tau_s:.0f}s), waiting for MWDE discount dip <= ${mwde_max_px:.2f} or LWAA late window")

    # -------------------------------------------------------------
    # STAGE 2: 尾盘高确信吸收 (Late-Window Asymmetric Absorption)
    # Window: tau in [15s, 120s] (elapsed 180s ~ 285s)
    # -------------------------------------------------------------
    lwaa_max_tau = float(params.get("LWAA_MAX_TAU_S", 120.0))
    if tau_s <= lwaa_max_tau:
        min_prob = float(params.get("LWAA_MIN_PROB", 0.80))
        min_edge = float(params.get("LWAA_MIN_EDGE", 0.02))
        max_entry_px = float(params.get("LWAA_MAX_ENTRY_PRICE", 0.65))
        hard_chase_ceiling = float(params.get("LWAA_HARD_CHASE_CEILING", 0.70))
        min_entry_px = float(params.get("LWAA_MIN_ENTRY_PRICE", 0.35))
        max_quote_attempts = int(params.get("LWAA_MAX_QUOTE_ATTEMPTS", 1))

        target_side: Optional[Side] = None
        if p_up >= min_prob:
            target_side = "Up"
        elif p_down >= min_prob:
            target_side = "Down"

        if target_side is not None:
            target_p = p_up if target_side == "Up" else p_down
            target_book = book_up if target_side == "Up" else book_down
            if available_cap < size * max_entry_px:
                return _wait(f"v2.2: Insufficient capital (${available_cap:.2f}) for LWAA entry (${size * max_entry_px:.2f})")

            best_ask = target_book.best_ask
            best_bid = target_book.best_bid

            # Path A: Taker Absorption (Immediate Edge Capture, strictly capped at max_entry_px and hard_chase_ceiling)
            if best_ask is not None and min_entry_px <= best_ask <= max_entry_px and best_ask <= hard_chase_ceiling:
                fee = taker_fee_per_share(best_ask, fee_rate)
                effective_cost = best_ask + fee
                taker_edge = target_p - effective_cost
                if taker_edge >= min_edge:
                    return Decision(
                        action="taker_buy",
                        side=target_side,
                        price=best_ask,
                        size=size,
                        utility=round(taker_edge, 4),
                        reason=f"LWAA Taker Absorption: {target_side} ask={best_ask:.2f}, fee={fee:.4f}, p_fair={target_p:.2f}, edge={taker_edge:.4f}"
                    )

            # Path B: Dynamic Maker Absorption (Passive Queue on Bid with Zero Fee)
            # If quote attempts exceeded, wait with urgent=False so resting quote remains active in queue
            if quote_attempts >= max_quote_attempts:
                return _wait(f"v2.2: 已达最大挂单次数 ({quote_attempts}/{max_quote_attempts})，挂不上不再追挂，死扛等待到期", urgent=False)

            if best_bid is not None and best_bid >= 0.02:
                # Dynamic pricing: join best_bid or improve inside spread, strictly capped at max_entry_px
                maker_px = round(min(max_entry_px, best_bid), 2)
                # If best_bid is sparse or ridiculously low below ask, anchor dynamically near ask
                if best_ask is not None and maker_px < 0.25 and best_ask >= 0.50:
                    maker_px = round(min(max_entry_px, max(min_entry_px, best_ask - 0.02)), 2)

                maker_px = min(maker_px, max_entry_px)
                if min_entry_px <= maker_px <= hard_chase_ceiling:
                    maker_edge = target_p - maker_px
                    if maker_edge >= min_edge:
                        return Decision(
                            action="quote",
                            side=target_side,
                            price=maker_px,
                            size=size,
                            utility=round(maker_edge, 4),
                            reason=f"LWAA Maker Absorption: 结合市价挂单 {target_side} bid={maker_px:.2f}, p_fair={target_p:.2f}, edge={maker_edge:.4f}"
                        )
            elif best_ask is not None:
                # Empty bid book: anchor dynamically near market ask, bounded by max_entry_px
                maker_px = round(min(max_entry_px, max(min_entry_px, best_ask - 0.02)), 2)
                maker_edge = target_p - maker_px
                if maker_edge >= min_edge:
                    return Decision(
                        action="quote",
                        side=target_side,
                        price=maker_px,
                        size=size,
                        utility=round(maker_edge, 4),
                        reason=f"LWAA Maker Absorption: 结合市价挂单 {target_side} bid={maker_px:.2f}, p_fair={target_p:.2f}, edge={maker_edge:.4f}"
                    )

            if best_ask is not None and (best_ask > max_entry_px or best_ask > hard_chase_ceiling):
                return _wait(f"v2.2: {target_side} 卖盘 ask={best_ask:.2f} 高于限价 ${max_entry_px:.2f} (超过${hard_chase_ceiling:.2f}绝不追高)")

            return _wait(f"v2.2: {target_side} p_fair={target_p:.2f} but book asks/bids offer no executable edge (>={min_edge:.2f})")

        return _wait(f"v2.2: Outcome uncertain (Up={p_up:.2f}, Down={p_down:.2f} < {min_prob:.2f}), no high-probability edge")

    # Early standby during first 60s (tau > 240s)
    return _wait(f"v2.2: Standby in early/mid window (tau {tau_s:.0f}s), waiting for MWDE discount dip or LWAA late window")
