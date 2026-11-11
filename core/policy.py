"""Unified Policy Engine (Avellaneda-Stoikov Inventory Pricing & Utility Arbitration).

Replaces hundreds of discrete rule branches with continuous inventory-adjusted
reservation prices and risk-adjusted utility comparisons.
"""
from typing import Dict, Optional, Tuple
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



def _hedgeable_shares(
    quote_px: float,
    book_opp: OrderBook,
    fee_rate: float,
    max_locked_pair_cost: float = 1.00
) -> float:
    """Shares the opposite book can absorb while keeping the pair <= max cost.

    Walks the ask ladder (ascending) and returns the largest cumulative size
    whose VWAP still locks `quote_px + vwap + fee(vwap) <= max_locked_pair_cost`.
    A locked pair is only risk-free if the hedge can actually be executed, so
    the *visible size*, not just the top-of-book price, has to cover it.
    """
    if book_opp is None or not getattr(book_opp, "asks", None):
        return 0.0
    shares = 0.0
    notional = 0.0
    for level in book_opp.asks:
        try:
            px = float(level[0])
            sz = float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        if px <= 0 or sz <= 0:
            continue
        candidate_shares = shares + sz
        candidate_notional = notional + px * sz
        vwap = candidate_notional / candidate_shares
        if quote_px + vwap + taker_fee_per_share(vwap, fee_rate) > max_locked_pair_cost + 1e-9:
            # Ladder is ascending: deeper levels only get worse.
            break
        shares, notional = candidate_shares, candidate_notional
    return shares


def risk_adjusted_entry_size(
    requested_size: float,
    quote_px: float,
    book_opp: OrderBook,
    lot_size: float,
    min_lots: int,
    max_lots: int,
    budget_usd: float,
    fee_rate: float = 0.07
) -> float:
    """Cap a new Leg-1 entry so its worst-case window loss is bounded.

    Two cases:
    1. The opposite ask ladder can absorb the size at a VWAP that locks
       pair_cost <= 1.00. That portion of the position is (at entry) risk-free,
       so it is allowed up to the hard lot cap.
    2. Otherwise the entry is naked until hedged: its entire premium can be
       lost if the contract expires worthless. Size is capped so that
       shares * quote_px stays inside the window loss budget.

    Returns 0.0 when even one lot would breach the budget.
    """
    if requested_size <= 0 or quote_px <= 0:
        return 0.0

    requested_lots = int(round(requested_size / lot_size))
    requested_lots = max(min_lots, min(max_lots, requested_lots))

    hedgeable = _hedgeable_shares(quote_px, book_opp, fee_rate)
    if hedgeable >= lot_size - 1e-9:
        locked_lots = int(hedgeable // lot_size)
        return float(min(requested_lots, locked_lots) * lot_size)

    if budget_usd <= 0:
        return 0.0
    affordable_lots = int((budget_usd / quote_px) // lot_size)
    lots = min(affordable_lots, requested_lots, max_lots)
    return float(lots * lot_size) if lots >= min_lots else 0.0


def calculate_reservation_bids(
    p_fair: float,
    epsilon: float,
    base_edge: float,
    q: float,
    gamma: float = 0.15
) -> Tuple[float, float]:
    """Calculate Avellaneda-Stoikov reservation prices for Up and Down.
    
    Args:
        p_fair: Up win probability [0.01, 0.99]
        epsilon: Uncertainty envelope bandwidth
        base_edge: Required profit margin over fair value
        q: Net inventory exposure: q = shares_Up - shares_Down
        gamma: Risk aversion coefficient penalizing inventory variance
        
    Returns:
        (r_bid_Up, r_bid_Down): Willing buy prices for both legs.
    """
    var = p_fair * (1.0 - p_fair)
    inventory_penalty = gamma * q * var
    
    # Up reservation bid decreases as q increases (holding excess Up)
    r_up = (p_fair - epsilon) - base_edge - inventory_penalty
    
    # Down reservation bid increases as q increases (hungry to buy Down to hedge Up)
    p_down_fair = 1.0 - p_fair
    r_down = (p_down_fair - epsilon) - base_edge + inventory_penalty
    
    # Physical tick constraints [0.02, 0.98]
    r_up = max(0.02, min(0.98, round(r_up, 2)))
    r_down = max(0.02, min(0.98, round(r_down, 2)))
    return r_up, r_down


def evaluate_unpaired_utility(
    held_side: Side,
    held_shares: float,
    held_basis_price: float,
    p_fair_held: float,
    book_held: OrderBook,
    book_opp: OrderBook,
    gamma: float = 0.15,
    p_fill_maker: float = 0.60,
    fee_rate: float = 0.07,
    max_pair_cost: float = 1.05,
    tau_s: float = 200.0,
    regime_name: str = "normal_oscillation"
) -> Decision:
    """Evaluate 3-way utility arbitration when holding an unhedged inventory.
    
    Compares:
    1. U_hedge: Immediate Taker hedge on opposite side (zero variance, lock-in cost).
       - STRICT GUARD: Disqualified if pair_cost > max_pair_cost ($1.05) to prevent
         catastrophic whipsaw where trader overpays for a $1.00 payout.
       - ARBITRAGE LOCK: If pair_cost <= 1.00, executes immediately to lock guaranteed non-negative return.
    2. U_sell: Immediate Taker sell of held side (zero variance, cut loss if >= 5 shares).
       Dominates when hedge is exorbitant.
    3. U_hold: Passive quoting on Maker / waiting to settle.
       - Under normal oscillation / range-bound market without adverse drift, works Leg-2 Maker quote
         at top-of-book to capture spread at 0 fee.
       - Emergency exit (taker hedge or unwind) triggers if adverse drift > 5 ticks, regime adverse, or tau < 90s.
    """
    opp_side: Side = "Down" if held_side == "Up" else "Up"
    var = p_fair_held * (1.0 - p_fair_held)
    
    # Path 1: Immediate Taker Hedge on Opposite Side
    u_hedge = -999.0
    hedge_ask = book_opp.best_ask
    if hedge_ask is not None:
        pair_cost = held_basis_price + hedge_ask + taker_fee_per_share(hedge_ask, fee_rate)
        # Payoff is 1.00 per paired set. Disqualify if pair_cost exceeds hard insurance ceiling
        if pair_cost <= max_pair_cost:
            u_hedge = held_shares * (1.00 - pair_cost)
            # Immediate risk-free arbitrage lock (pair_cost <= 1.00)
            if pair_cost <= 1.00:
                return Decision(
                    action="taker_hedge",
                    side=opp_side,
                    price=hedge_ask,
                    size=held_shares,
                    utility=round(u_hedge, 4),
                    reason=f"U_hedge({u_hedge:.2f}) arbitrage (pair_cost={pair_cost:.3f} <= 1.00), take hedge @ {hedge_ask}"
                )
        
    # Path 2: Immediate Taker Unwind / Sell of Held Side.
    # Any positive size is sellable: depth-limited matching produces partial
    # fills (e.g. 1.93 shares), and the old ">= 5 shares" floor stranded those
    # legs with no exit other than holding to expiry.
    u_sell = -999.0
    held_bid = book_held.best_bid
    if held_shares > 1e-9 and held_bid is not None and held_bid > 0:
        u_sell = held_shares * (held_bid - taker_fee_per_share(held_bid, fee_rate) - held_basis_price)
        
    # Path 3: Passive Quoting / Holding to Expiration
    # Utility of quoting the opposite side: with probability p_fill the hedge
    # fills and the pair settles for $1.00 (payoff 1 - basis - quote per share);
    # otherwise the position is carried to settlement with EV (p_fair - basis)
    # minus the inventory variance penalty. The earlier version added a
    # p_fill * maker_gain term on top of the full hold-to-expiry EV, which
    # double-counted the same shares and made holding look artificially cheap.
    opp_bid = book_opp.best_bid
    maker_quote = round(min(0.98, max(0.02, 1.00 - held_basis_price)), 2)
    if opp_bid is not None and opp_bid >= 0.02:
        maker_quote = min(maker_quote, opp_bid)
    # Guard: Maker quote must never cross the opposite ask into taker territory
    if book_opp.best_ask is not None:
        maker_quote = min(maker_quote, round(book_opp.best_ask - 0.01, 2))

    p_fill = min(1.0, max(0.0, p_fill_maker))
    hedged_gain = held_shares * (1.00 - held_basis_price - maker_quote)
    ev_holding = held_shares * (p_fair_held - held_basis_price)
    variance_penalty = gamma * (held_shares ** 1.2) * var
    u_hold = p_fill * hedged_gain + (1.0 - p_fill) * (ev_holding - variance_penalty)

    # === ARBITRATION DECISION ===
    # Check if position is in distress (adverse price drift, adverse regime, or late window)
    adverse_drift = (held_basis_price - p_fair_held) > 0.05
    regime_adverse = (held_side == "Down" and regime_name in ("trending_up", "toxic")) or \
                     (held_side == "Up" and regime_name in ("trending_down", "toxic"))
    late_window = (tau_s < 90.0)
    in_distress = adverse_drift or regime_adverse or late_window

    best_immediate = max(u_hedge, u_sell)

    # If in distress and the immediate exit is actually better than holding,
    # take it. The comparison against u_hold is the whole point of the
    # arbitration: emergency exits are a risk trade-off, not an unconditional
    # command, and the reason string must not claim a comparison that never ran.
    if in_distress and best_immediate > -900.0 and best_immediate > u_hold:
        if u_hedge > -990.0 and u_hedge >= u_sell and hedge_ask is not None:
            return Decision(
                action="taker_hedge",
                side=opp_side,
                price=hedge_ask,
                size=held_shares,
                utility=round(u_hedge, 4),
                reason=f"U_hedge({u_hedge:.2f}) > U_hold({u_hold:.2f}), take hedge @ {hedge_ask}"
            )
        elif u_sell > -990.0 and held_bid is not None:
            return Decision(
                action="taker_unwind",
                side=held_side,
                price=held_bid,
                size=held_shares,
                utility=round(u_sell, 4),
                reason=f"U_sell({u_sell:.2f}) > U_hold({u_hold:.2f}), unwind position @ {held_bid}"
            )

    # Otherwise, maintain the optimal Maker quote on the deficit side to
    # complete the round trip. `maker_quote` is already clamped inside
    # [0.02, 0.98] and below the opposite ask, so no second guard is needed.
    return Decision(
        action="quote",
        side=opp_side,
        price=round(maker_quote, 2),
        size=held_shares,
        utility=round(u_hold, 4),
        reason=f"U_hold({u_hold:.2f}) optimal, quote maker @ {round(maker_quote, 2)}"
    )


def plan_decision_lwaa(
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
    """Master decision cycle for LWAA (Late-Window Asymmetric Absorption).
    
    1. Early/Mid Window (tau > LWAA_MAX_TAU_S, e.g. 60s):
       - Standby. Random-walk noise carries adverse selection; quoting is disabled.
    2. Late Window Horizon (LWAA_MIN_TAU_S <= tau <= LWAA_MAX_TAU_S, [15s, 60s]):
       - High-certainty window. Evaluate p_fair.
       - If p_fair(Up) >= LWAA_MIN_PROB (e.g. 0.88):
         Evaluate Taker absorption on Up asks (p_fair - ask - fee >= LWAA_MIN_EDGE).
         If taker edge exists: execute taker_buy Up!
         Else evaluate Maker bid on Up.
       - Symmetrically for Down if p_fair(Down) >= LWAA_MIN_PROB.
    3. Position Handling:
       - Single-leg position is held to settlement (1.00 payout).
       - Taker unwinds (selling at 0.10) are STRICTLY FORBIDDEN.
       - Taker hedge is only permitted if pair_cost <= 1.00 (risk-free arbitrage lock).
    4. Tail Freeze (tau < LWAA_MIN_TAU_S, e.g. 15s):
       - Freeze all new entries, pull resting maker quotes, hold inventory to settlement.
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
            f"v2.1: Holding {held_shares:.0f} {held_side} @ {held_basis:.2f} to settlement (死扛让子弹飞，不急止损, p_fair={p_fair_held:.2f})",
            urgent=True
        )

    # 2b. Fully Hedged Arbitrage Pair (Both sides held and balanced)
    if u_shares > 1e-4 and d_shares > 1e-4 and abs(net_gap) < 1e-4:
        return _wait(
            f"v2.1: Max position reached & fully hedged arbitrage pair (UP {u_shares:.0f} / DN {d_shares:.0f}), holding to settlement for guaranteed $1.00 payout",
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
        return _wait(f"v2.1: Tail freeze (tau {tau_s:.0f}s < {min_tau:.0f}s), holding to settlement", urgent=True)

    # -------------------------------------------------------------
    # STAGE 1: 前半程顺势低吸 (Mid-Window Trend-Biased Discount Entry)
    # Window: tau in [150s, 240s] (elapsed 60s ~ 150s)
    # -------------------------------------------------------------
    mwde_min_tau = float(params.get("MWDE_MIN_TAU_S", 150.0))
    mwde_max_tau = float(params.get("MWDE_MAX_TAU_S", 240.0))
    mwde_enabled = bool(params.get("MWDE_ENABLE", True))

    if mwde_enabled and mwde_min_tau <= tau_s <= mwde_max_tau:
        prev_move = float(params.get("PREV_ROUND_ABS_MOVE", 30.0))
        min_prev_move = float(params.get("MWDE_PREV_ROUND_MIN_MOVE", 20.0))
        range_60s = float(params.get("FIRST_60S_RANGE", 5.0))
        max_range_60s = float(params.get("MWDE_FIRST_60S_MAX_RANGE", 15.0))

        # Check consolidation corridor and probability around middle
        is_consolidation = (prev_move >= min_prev_move) and (range_60s <= max_range_60s)
        p_in_mid = float(params.get("MWDE_MID_PROB_LOW", 0.38)) <= p_fair <= float(params.get("MWDE_MID_PROB_HIGH", 0.62))

        if is_consolidation and p_in_mid:
            # 优先选边: 同步看当前距离开盘价的涨跌大小，结合胜率优先选边
            ref_open = getattr(spot, "binance_open_price", None) or spot.open_price
            delta_s = spot.price - ref_open if ref_open > 0 else 0.0

            if delta_s > 0 or p_fair >= 0.50:
                pref_side: Side = "Up"
                pref_p = p_fair
                target_book = book_up
            else:
                pref_side: Side = "Down"
                pref_p = 1.0 - p_fair
                target_book = book_down

            mwde_max_px = float(params.get("MWDE_MAX_ENTRY_PRICE", 0.45))
            if available_cap < size * mwde_max_px:
                return _wait(f"v2.1: Insufficient capital (${available_cap:.2f}) for MWDE entry (${size * mwde_max_px:.2f})")
            best_ask = target_book.best_ask
            best_bid = target_book.best_bid

            # 1. Taker Absorption on Preferred Side if ask <= $0.45
            if best_ask is not None and best_ask <= mwde_max_px and best_ask >= 0.10:
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

            # 2. Maker Absorption on Preferred Side if bid <= $0.45
            max_quote_attempts = int(params.get("LWAA_MAX_QUOTE_ATTEMPTS", 1))
            if quote_attempts < max_quote_attempts and best_bid is not None and best_bid >= 0.05:
                maker_px = round(min(mwde_max_px, best_bid), 2)
                if best_ask is not None:
                    maker_px = min(maker_px, round(best_ask - 0.01, 2))
                if 0.10 <= maker_px <= mwde_max_px:
                    return Decision(
                        action="quote",
                        side=pref_side,
                        price=maker_px,
                        size=size,
                        utility=round(pref_p - maker_px, 4),
                        reason=f"MWDE Maker Absorption: 顺势低吸挂单 {pref_side} bid={maker_px:.2f} <= ${mwde_max_px:.2f}"
                    )

    # -------------------------------------------------------------
    # STAGE 2: 尾盘高确信吸收 (Late-Window Asymmetric Absorption)
    # Window: tau in [15s, 120s]
    # -------------------------------------------------------------
    lwaa_max_tau = float(params.get("LWAA_MAX_TAU_S", 120.0))
    if tau_s <= lwaa_max_tau:
        min_prob = float(params.get("LWAA_MIN_PROB", 0.80))
        min_edge = float(params.get("LWAA_MIN_EDGE", 0.02))
        max_entry_px = float(params.get("LWAA_MAX_ENTRY_PRICE", 0.65))
        hard_chase_ceiling = float(params.get("LWAA_HARD_CHASE_CEILING", 0.70))
        min_entry_px = float(params.get("LWAA_MIN_ENTRY_PRICE", 0.35))
        max_quote_attempts = int(params.get("LWAA_MAX_QUOTE_ATTEMPTS", 1))

        p_up = p_fair
        p_down = 1.0 - p_fair

        target_side: Optional[Side] = None
        if p_up >= min_prob:
            target_side = "Up"
        elif p_down >= min_prob:
            target_side = "Down"

        if target_side is not None:
            target_p = p_up if target_side == "Up" else p_down
            target_book = book_up if target_side == "Up" else book_down
            if available_cap < size * max_entry_px:
                return _wait(f"v2.1: Insufficient capital (${available_cap:.2f}) for LWAA entry (${size * max_entry_px:.2f})")
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

            # Path B: Maker Absorption (Passive Queue on Bid with Zero Fee)
            if quote_attempts >= max_quote_attempts:
                return _wait(f"v2.1: 已达最大挂单次数 ({quote_attempts}/{max_quote_attempts})，挂不上不再追挂，死扛等待到期", urgent=True)

            if best_bid is not None and best_bid >= 0.02:
                maker_px = round(min(max_entry_px, max(min_entry_px, min(target_p - min_edge, best_bid))), 2)
                if best_ask is not None:
                    maker_px = min(maker_px, round(best_ask - 0.01, 2))
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
                            reason=f"LWAA Maker Absorption: {target_side} bid={maker_px:.2f}, p_fair={target_p:.2f}, edge={maker_edge:.4f}"
                        )

            if best_ask is not None and (best_ask > max_entry_px or best_ask > hard_chase_ceiling):
                return _wait(f"v2.1: {target_side} 卖盘 ask={best_ask:.2f} 高于限价 ${max_entry_px:.2f} (超过${hard_chase_ceiling:.2f}绝不追高)")

            return _wait(f"v2.1: {target_side} p_fair={target_p:.2f} but book asks/bids offer no executable edge (>={min_edge:.2f})")

        return _wait(f"v2.1: Outcome uncertain (Up={p_up:.2f}, Down={p_down:.2f} < {min_prob:.2f}), no high-probability edge")

    # Early/mid standby when outside Stage 1 and Stage 2 windows
    return _wait(f"v2.1: Standby in early/mid window (tau {tau_s:.0f}s), waiting for MWDE discount dip or LWAA late window")


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
    """Master decision dispatcher routing to active strategy engine."""
    mode = params.get("STRATEGY_MODE", "LWAA")
    if mode == "LWAA":
        return plan_decision_lwaa(
            pos_up=pos_up,
            pos_down=pos_down,
            book_up=book_up,
            book_down=book_down,
            spot=spot,
            tau_s=tau_s,
            params=params,
            circuit_breaker_active=circuit_breaker_active,
            circuit_breaker_reason=circuit_breaker_reason,
            p_fair=p_fair,
            entries_count=entries_count,
            regime_info=regime_info,
            quote_attempts=quote_attempts
        )
    return plan_decision_legacy(
        pos_up=pos_up,
        pos_down=pos_down,
        book_up=book_up,
        book_down=book_down,
        spot=spot,
        tau_s=tau_s,
        params=params,
        circuit_breaker_active=circuit_breaker_active,
        circuit_breaker_reason=circuit_breaker_reason,
        p_fair=p_fair,
        entries_count=entries_count,
        regime_info=regime_info
    )


def plan_decision_legacy(
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
    """Legacy v1.1 Master decision cycle (Avellaneda-Stoikov + Dual Regime).
    
    Evaluates current inventory, circuit breaker state, volatility regime,
    and outputs the singular best action.
    """
    u_shares = pos_up.shares
    d_shares = pos_down.shares
    net_gap = round(u_shares - d_shares, 4)
    
    from .estimator import (
        estimate_p_fair,
        estimate_uncertainty_envelope,
        calculate_adaptive_spread_params
    )
    
    # 1. Calculate Fair Prob & Dynamic Volatility Adaptive Bounds
    # The runner computes this every tick and passes it in; the fallback exists
    # for direct callers (tests, research scripts) and uses the same helper so
    # the reference chain cannot drift between the two paths.
    if p_fair is None:
        p_fair = estimate_p_fair(
            spot,
            getattr(spot, "open_price", 0.0),
            tau_s,
            spot.realized_vol_bps,
            poly_price=getattr(spot, "poly_price", None),
        )
    
    adaptive_edge, adaptive_k, regime = calculate_adaptive_spread_params(
        spot.realized_vol_bps, 
        params.get("BASE_EDGE", 0.025), 
        params.get("UNCERTAINTY_K", 1.2)
    )
    
    epsilon = estimate_uncertainty_envelope(
        spot.realized_vol_bps,
        float(params.get("LATENCY_S", 0.30)),
        adaptive_k,
        tau_s=tau_s
    )

    # 1b. Dynamic Market Micro-Regime Classification (Multi-Dimensional Statistical Metrics)
    if regime_info is None:
        from .regime import classify_market_regime
        ref_spot = getattr(spot, "poly_price", None) or spot.price
        ref_open = getattr(spot, "open_price", 0.0)
        if not (getattr(spot, "poly_price", None) and spot.poly_price > 0):
            ref_open = getattr(spot, "binance_open_price", None) or spot.open_price
        if not ref_open or ref_open <= 0:
            ref_open = getattr(spot, "open_price", 0.0) or 0.0
        regime_info = classify_market_regime(
            ref_spot, ref_open, tau_s, spot.realized_vol_bps,
            book_up, book_down,
            is_toxic_shock=spot.is_toxic_shock,
            params=params,
            p_fair=p_fair
        )
    micro_regime = regime_info.regime
    snr = regime_info.snr
    ret_bps = regime_info.ret_bps
    
    # 2. Case: Balanced or Flat Inventory (|net_gap| < 1e-4)
    if abs(net_gap) < 1e-4:
        # Check Circuit Breaker Watchdog: forbid opening new Leg-1 when tripped or cooling
        cb_active = circuit_breaker_active or bool(params.get("CIRCUIT_BREAKER_ACTIVE", False))
        if cb_active:
            cb_msg = circuit_breaker_reason or params.get("CIRCUIT_BREAKER_REASON", "Active")
            return _wait(f"Circuit breaker active ({cb_msg}): new Leg-1 disabled", urgent=True)

        # 2a. Entry horizon: no new Leg-1 once the window is late (tau < 180s).
        # The tail-freeze band (tau <= 60s) is a strict subset of this cutoff, so
        # it is a label on the same gate rather than a second, unreachable gate.
        min_entry_tau = float(params.get("MIN_ENTRY_TAU_S", 180.0))
        if tau_s < min_entry_tau:
            label = "Tail freeze" if tau_s <= float(params.get("TAIL_FREEZE_S", 60.0)) else "Late window"
            return _wait(f"{label} (tau {tau_s:.0f}s < {min_entry_tau:.0f}s): new Leg-1 entry disabled", urgent=True)

        # 2b. Window Lifecycle & Single Entry Constraint:
        # Forbid re-entering after an unwind in the same 5-minute round (prevents revenge trading in chop)
        max_entries = int(params.get("MAX_ENTRIES_PER_WINDOW", 1))
        if entries_count >= max_entries:
            return _wait(f"Max entries per window reached ({entries_count}/{max_entries}), waiting for next window", urgent=True)

        # Toxic shock is one of the regimes the classifier already detects, so
        # the decision only needs to consult the regime, not the raw flag.
        if micro_regime == "toxic":
            return _wait(f"Toxic regime detected: {regime_info.reason}", urgent=True)

        # Check max position limit: if already holding max paired lots, wait for settlement
        max_lots = int(params.get("MAX_LOTS", 3))
        max_shares = float(params.get("LOT_SIZE", 5.0)) * max_lots
        if u_shares >= max_shares - 1e-4:
            return _wait(f"Max position reached ({u_shares:.0f} shares), holding to settlement", urgent=True)
            
        # Check permissions from regime classification:
        if not regime_info.can_quote_up and not regime_info.can_quote_down:
            return _wait(regime_info.reason, urgent=True)

        can_quote_up = regime_info.can_quote_up
        can_quote_down = regime_info.can_quote_down

            # Reservation bids for both sides using adaptive edge
        r_up, r_down = calculate_reservation_bids(
            p_fair, epsilon, adaptive_edge, q=0.0, gamma=float(params.get("RISK_AVERSION_GAMMA", 0.15))
        )

        available_cap = float(params.get("AVAILABLE_CAPITAL", 100.0))
        min_lots = int(params.get("MIN_LOTS", 1))
        lot_size = float(params.get("LOT_SIZE", 5.0))

        max_entry_px = float(params.get("MAX_MAKER_ENTRY_PRICE", 0.62))
        min_entry_px = float(params.get("MIN_MAKER_ENTRY_PRICE", 0.38))
        # Hard pre-trade risk budget: an unhedged Leg-1 premium is the true
        # worst-case loss for the window and must fit inside the envelope.
        loss_budget = float(params.get("WINDOW_RISK_BUDGET_USD", params.get("MAX_WINDOW_LOSS_USD", 2.00)))
        fee_rate = params.get("TAKER_FEE_RATE", 0.07)

        # Initial Leg-1 bare risk sizing: range_bound may recommend more than one
        # lot (only if the config opts in), everything else is one lot.
        target_lots = regime_info.recommended_lots if regime_info.recommended_lots > 0 else min_lots
        entry_size = float(target_lots * lot_size)
        # Capital gate: a 50% buffer is kept on top of the premium at risk so a
        # hedge or unwind can still be paid for after the first leg is filled.
        hedge_reserve = 0.50
        if available_cap < (entry_size * hedge_reserve):
            entry_size = float(min_lots * lot_size)
        if available_cap < (entry_size * hedge_reserve):
            return _wait(f"Insufficient capital (${available_cap:.2f}) for 1 lot")

        def _plan_entry(
            side: Side,
            can_quote: bool,
            reservation: float,
            book_side: OrderBook,
            book_opp: OrderBook,
            p_side: float,
        ) -> Optional[Decision]:
            """One Leg-1 maker quote; None means this side simply has no edge."""
            if not can_quote or not book_side.bids:
                return None
            best_bid = book_side.best_bid
            if best_bid is None or best_bid < 0.02:
                return None
            # We only join the bid if the reservation price is at least the bid and
            # inside the safe corridor; otherwise there is nothing worth quoting.
            if reservation < min_entry_px or reservation < best_bid:
                return None
            quote_px = max(0.02, min(0.98, round(min(reservation, best_bid), 2)))
            if book_side.best_ask is not None:
                quote_px = min(quote_px, round(book_side.best_ask - 0.01, 2))
            if quote_px < min_entry_px or quote_px > max_entry_px:
                return _wait(
                    f"{side} quote ({quote_px:.2f}) outside safe entry zone "
                    f"[{min_entry_px:.2f}, {max_entry_px:.2f}]",
                    urgent=True,
                )
            size = risk_adjusted_entry_size(
                entry_size, quote_px, book_opp, lot_size, min_lots, max_lots, loss_budget, fee_rate
            )
            if size <= 0:
                return _wait(
                    f"{side} entry @ {quote_px:.2f} would breach ${loss_budget:.2f} "
                    "worst-case window loss budget (no locked hedge)",
                    urgent=True,
                )
            return Decision(
                action="quote",
                side=side,
                price=quote_px,
                size=size,
                utility=round(p_side - quote_px, 4),
                reason=(
                    f"Enter {side} maker @ {quote_px} "
                    f"(p_fair={p_side:.2f}, {micro_regime}, {size:.0f} shares)"
                ),
            )

        for side, can_quote_side, reservation, book_side, book_opp, p_side in (
            ("Up", regime_info.can_quote_up, r_up, book_up, book_down, p_fair),
            ("Down", regime_info.can_quote_down, r_down, book_down, book_up, 1.0 - p_fair),
        ):
            entry = _plan_entry(side, can_quote_side, reservation, book_side, book_opp, p_side)
            if entry is not None:
                return entry

        return _wait(f"No edge outside uncertainty band (+/-{epsilon:.2f}, {micro_regime})")

    # 3. Case: Unpaired Inventory (Holding excess Up or Down)
    held_side: Side = "Up" if net_gap > 0 else "Down"
    held_shares = abs(net_gap)
    held_basis = pos_up.avg_price if held_side == "Up" else pos_down.avg_price
    p_fair_held = p_fair if held_side == "Up" else (1.0 - p_fair)
    
    b_held = book_up if held_side == "Up" else book_down
    b_opp = book_down if held_side == "Up" else book_up
    
    return evaluate_unpaired_utility(
        held_side=held_side,
        held_shares=held_shares,
        held_basis_price=held_basis,
        p_fair_held=p_fair_held,
        book_held=b_held,
        book_opp=b_opp,
        gamma=float(params.get("RISK_AVERSION_GAMMA", 0.15)),
        p_fill_maker=float(params.get("P_FILL_MAKER", 0.60)),
        fee_rate=params.get("TAKER_FEE_RATE", 0.07),
        max_pair_cost=float(params.get("MAX_PAIR_HEDGE_COST", 1.05)),
        tau_s=tau_s,
        regime_name=micro_regime
    )
