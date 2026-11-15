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
    cb_enabled = bool(params.get("ENABLE_CIRCUIT_BREAKER", True))
    cb_active = cb_enabled and (circuit_breaker_active or bool(params.get("CIRCUIT_BREAKER_ACTIVE", False)))
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

    # Tail freeze applies to EVERY branch, including inventory handling. It used
    # to live only in the flat-inventory path, so a hedge or salvage unwind could
    # still fire at tau=5s while the dashboard claimed the window was frozen.
    min_tau = float(params.get("LWAA_MIN_TAU_S", 15.0))
    if tau_s < min_tau:
        return _wait(f"v2.2: Tail freeze (tau {tau_s:.0f}s < {min_tau:.0f}s), holding to settlement", urgent=True)

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
        if bool(params.get("LWAA_ENABLE_ARBITRAGE_HEDGE", True)):
            max_pair_cost = float(params.get("ARBITRAGE_HEDGE_MAX_PAIR_COST", 1.00))
        else:
            max_pair_cost = float(params.get("MWDE_HEDGE_LOCK_COST", 0.85))
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

        # Check for defensive salvage:
        # When nearing expiry (tau <= 30s) and our held leg probability has collapsed (p_fair_held < 0.12),
        # allow exiting at market bid to salvage residual capital instead of 100% loss.
        salvage_tau = float(params.get("DEFENSIVE_SALVAGE_TAU_S", 30.0))
        salvage_prob = float(params.get("DEFENSIVE_SALVAGE_MAX_PROB", 0.12))
        salvage_min_bid = float(params.get("DEFENSIVE_SALVAGE_MIN_BID", 0.06))
        salvage_enabled = (bool(params.get("DEFENSIVE_SALVAGE_ENABLE", True))
                           and bool(params.get("ALLOW_TAKER_UNWIND", True)))

        if salvage_enabled and tau_s <= salvage_tau and p_fair_held < salvage_prob:
            held_book = book_up if held_side == "Up" else book_down
            salvage_bid = held_book.best_bid
            if salvage_bid is not None and salvage_bid >= salvage_min_bid:
                fee = taker_fee_per_share(salvage_bid, fee_rate)
                salvage_cash = held_shares * (salvage_bid - fee)
                return Decision(
                    action="taker_unwind",
                    side=held_side,
                    price=salvage_bid,
                    size=held_shares,
                    utility=round(salvage_bid - fee - p_fair_held, 4),
                    reason=f"Defensive Salvage: 尾盘({tau_s:.0f}s)胜率坍塌(P*={p_fair_held:.2f} < {salvage_prob:.2f})，市价出清残值 @ {salvage_bid:.2f} (挽回${salvage_cash:.2f})"
                )

        # High-conviction directional mode: NEVER UNWIND outside of extreme defensive salvage!
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

    # Per-window cost envelope. Previously MAX_WINDOW_LOSS_USD was only checked
    # by the stress harness after the fact, so nothing stopped the live engine
    # from stacking cost beyond the declared budget.
    committed = float(params.get("COMMITTED_WINDOW_COST", 0.0))
    window_budget = float(params.get("MAX_WINDOW_LOSS_USD", 5.0))
    worst_entry_cost = size * float(params.get("LWAA_MAX_ENTRY_PRICE", 0.65))
    if committed + worst_entry_cost > window_budget + 1e-9:
        return _wait(
            f"v2.2: 单窗成本包络已用 ${committed:.2f}/${window_budget:.2f}，拒绝继续开仓",
            urgent=True
        )

    if u_shares >= max_shares - 1e-4 or d_shares >= max_shares - 1e-4:
        return _wait(f"Max position reached ({max(u_shares, d_shares):.0f} shares), holding to settlement", urgent=True)

    # Check Micro-Regime Toxicity
    if getattr(spot, "is_toxic_shock", False):
        return _wait("Adverse spot toxic shock detected", urgent=True)

    # -------------------------------------------------------------
    # STAGE 1: 前半程顺势低吸 (Mid-Window Trend-Biased Discount Entry)
    # Window: tau in [120s, 240s] (elapsed 60s ~ 180s)
    # -------------------------------------------------------------
    mwde_min_tau = float(params.get("MWDE_MIN_TAU_S", 120.0))
    mwde_max_tau = float(params.get("MWDE_MAX_TAU_S", 240.0))
    mwde_enabled = bool(params.get("MWDE_ENABLE", True))

    ref_open = spot.open_price
    ref_spot = getattr(spot, "poly_price", None) or spot.price
    delta_s = ref_spot - ref_open if (ref_open > 0 and ref_spot > 0) else 0.0
    p_up = p_fair
    p_down = 1.0 - p_fair

    if mwde_enabled and mwde_min_tau < tau_s <= mwde_max_tau:
        prev_move = float(params.get("PREV_ROUND_ABS_MOVE", 0.0))
        post_shock_min_move = float(params.get("POST_SHOCK_MIN_MOVE", 40.0))
        mwde_max_px = float(params.get("MWDE_MAX_ENTRY_PRICE", 0.45))
        max_pair_precheck = float(params.get("MAX_PAIRING_PRECHECK_COST", 1.05))

        if prev_move >= post_shock_min_move:
            # 历史单边大幅位移兼容逻辑
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
            max_entry_px = float(params.get("LWAA_MAX_ENTRY_PRICE", 0.65))
            hard_chase_ceiling = float(params.get("LWAA_HARD_CHASE_CEILING", 0.70))

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

            if favored_ask is not None and favored_ask > hard_chase_ceiling:
                return _wait(f"v2.2: 大涨大跌后高价侧 {favored_side} ask={favored_ask:.2f} 超过 ${hard_chase_ceiling:.2f}，坚决不追高防守", urgent=True)

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

        # 1. 严格顺势共振判定 (概率过半 + 现货走势一致，绝不买入胜率低于 0.50 的劣势方)
        # 必须同时满足：
        # - 做多 Up: 现货明确上涨 (delta_s > 0) 且 多头公允胜率明确过半 (p_up >= 0.50)
        # - 做空 Down: 现货明确下跌 (delta_s < 0) 且 空头公允胜率明确过半 (p_down >= 0.50，即 p_up <= 0.50)
        # 若两者冲突（如现货微涨但结算概率被压制在0.50以下，或胜率未明），坚决保持观望，绝不抄底劣势方！
        if delta_s > 0 and p_up >= 0.50:
            pref_side: Side = "Up"
            pref_p = p_up
            target_book = book_up
            opp_book = book_down
            trend_label = f"本轮顺势共振偏多 (ΔS={delta_s:+.1f}, P*={p_up:.2f}>=0.50)"
        elif delta_s < 0 and p_down >= 0.50:
            pref_side: Side = "Down"
            pref_p = p_down
            target_book = book_down
            opp_book = book_up
            trend_label = f"本轮顺势共振偏空 (ΔS={delta_s:+.1f}, P*={p_down:.2f}>=0.50)"
        else:
            return _wait(
                f"v2.2: Standby in early/mid window (tau {tau_s:.0f}s). 前半段走势与概率未共振(ΔS={delta_s:+.1f}, P*(Up)={p_up:.2f})，胜率未明确过半，严禁介入劣势方，防守观望"
            )

        if available_cap < size * mwde_max_px:
            return _wait(f"v2.2: Insufficient capital (${available_cap:.2f}) for MWDE entry (${size * mwde_max_px:.2f})")

        best_ask = target_book.best_ask
        best_bid = target_book.best_bid
        opp_ask = opp_book.best_ask

        # 用户核心准则："不要太早吃单，除非两侧加起来少于0.9，先尝试挂单"
        # 1. 尝试挂单优先 (Dynamic Maker Absorption on Preferred Side)
        max_quote_attempts = int(params.get("LWAA_MAX_QUOTE_ATTEMPTS", 1))
        if quote_attempts < max_quote_attempts and best_bid is not None and 0.05 <= best_bid <= mwde_max_px:
            maker_px = round(min(mwde_max_px, best_bid), 2)
            # 若价差充足(best_ask - best_bid >= 0.02)，可提高 1 个 tick (0.01) 优先排队，但绝不超过 mwde_max_px
            if best_ask is not None and best_bid + 0.01 < best_ask and best_bid + 0.01 <= mwde_max_px:
                maker_px = round(best_bid + 0.01, 2)
            maker_px = min(maker_px, mwde_max_px)

            if opp_ask is not None and maker_px + opp_ask > max_pair_precheck:
                return _wait(f"v2.2: 对侧补腿盘口过贵 (本侧挂单={maker_px:.2f} + 对侧ask={opp_ask:.2f} > {max_pair_precheck:.2f})，放弃MWDE挂单")
            if 0.10 <= maker_px <= mwde_max_px:
                return Decision(
                    action="quote",
                    side=pref_side,
                    price=maker_px,
                    size=size,
                    utility=round(pref_p - maker_px, 4),
                    reason=f"MWDE Maker Absorption: 前半程动态挂单({trend_label}) {pref_side} bid={maker_px:.2f} <= ${mwde_max_px:.2f}"
                )

        # 2. 吃单限制：不要太早吃单，除非两边价格加起来 < 0.90 (深度折价无风险套利空间) 且顺势侧 ask <= $0.45
        mwde_max_taker_pair_cost = float(params.get("MWDE_TAKER_PAIR_COST", 1.02))
        if best_ask is not None and 0.10 <= best_ask <= mwde_max_px:
            if opp_ask is not None:
                pair_cost = best_ask + opp_ask
                if pair_cost <= mwde_max_taker_pair_cost:
                    fee = taker_fee_per_share(best_ask, fee_rate)
                    if pref_p - (best_ask + fee) >= 0.01:
                        tag = "两侧合计极度便宜" if pair_cost < 0.90 else "两侧合计成本可接受"
                        return Decision(
                            action="taker_buy",
                            side=pref_side,
                            price=best_ask,
                            size=size,
                            utility=round(pref_p - (best_ask + fee), 4),
                            reason=f"MWDE Taker Absorption: {tag}(本侧ask={best_ask:.2f}+对侧ask={opp_ask:.2f}={pair_cost:.2f} <= {mwde_max_taker_pair_cost:.2f})，果断顺势吃单 {pref_side}"
                        )
                else:
                    return _wait(f"v2.2: 前半程暂不吃单 (两侧ask合计={pair_cost:.2f} > {mwde_max_taker_pair_cost:.2f})，遵循'先挂单、非特价不吃'原则")

        # 3. 严格顺势防护：如果顺势一侧的价格已经过高（> $0.45），宁可等待也不去抄底反向便宜一侧！
        opp_side = "Down" if pref_side == "Up" else "Up"
        return _wait(
            f"v2.2: Standby in early/mid window (tau {tau_s:.0f}s). 上半段严格顺势防守({trend_label})：当前顺势侧 {pref_side} ask/bid 均高于 ${mwde_max_px:.2f}，坚决不追高，且严禁反向抄底便宜侧 {opp_side}，防守等待"
        )

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
            opp_book_for_hedge = book_down if target_side == "Up" else book_up
            opp_ask_precheck = opp_book_for_hedge.best_ask
            max_pair_precheck = float(params.get("MAX_PAIRING_PRECHECK_COST", 1.05))

            if best_ask is not None and min_entry_px <= best_ask <= max_entry_px and best_ask <= hard_chase_ceiling:
                # Pre-check opposite book pairing viability
                if opp_ask_precheck is not None and best_ask + opp_ask_precheck > max_pair_precheck:
                    return _wait(f"v2.2: 对侧补腿盘口过贵 (本侧={best_ask:.2f} + 对侧ask={opp_ask_precheck:.2f} > {max_pair_precheck:.2f})，预期无法锁仓，放弃先手吃单")
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
                # Anti-airball: if market bid is already beyond hard chase ceiling, do not hang deep dead quote
                if best_bid > hard_chase_ceiling:
                    return _wait(f"v2.2: {target_side} bid={best_bid:.2f} 超过追高硬顶 ${hard_chase_ceiling:.2f}，不在深水区悬空挂单")

                # Dynamic pricing: join best_bid or improve inside spread, strictly capped at max_entry_px
                maker_px = round(min(max_entry_px, best_bid), 2)
                # If best_bid is sparse or ridiculously low below ask, anchor dynamically near ask
                if best_ask is not None and maker_px < 0.25 and best_ask >= 0.50:
                    maker_px = round(min(max_entry_px, max(min_entry_px, best_ask - 0.02)), 2)

                maker_px = min(maker_px, max_entry_px)
                if opp_ask_precheck is not None and maker_px + opp_ask_precheck > max_pair_precheck:
                    return _wait(f"v2.2: 对侧补腿盘口过贵 (本侧挂单={maker_px:.2f} + 对侧ask={opp_ask_precheck:.2f} > {max_pair_precheck:.2f})，放弃先手挂单")

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
