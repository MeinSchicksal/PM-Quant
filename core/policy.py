"""Strict Asian 60s-TWAP Multi-Factor Policy Engine (v3.3).

Single-brain decision cycle; all thresholds are defined in config.py PARAMS:
1. Calibrated Student-t(5) pricing (optional unit-variance normalization) +
   overextension edge damping so huge leads are not chased blindly at poor odds.
2. Scene-weighted Maker / Taker execution routing (keeps both channels and
   routes dynamically by spot calmness/momentum, spread, price & tau), plus
   optional contested-zone low-price value entries.
3. Time-paced exposure: light 5-share probe in early phase (first 120s),
   average-protecting quality adds in mid phase (no chasing above avg, no
   buying into reversals), and late-window new-entry freeze (final 45s).
4. Payoff-driven dynamic integer-share reversal flip (5-10 shares in one shot)
   guarded by standalone positive EV, storm vol check, and hard risk caps.
"""
import math
from typing import Dict, Optional
from .types import Side, OrderBook, Position, Decision, SpotState
from .execution import taker_fee_per_share
from .estimator import estimate_p_fair


def _wait(reason: str, urgent: bool = False) -> Decision:
    """Build a 'wait' decision, declaring whether resting quotes must be pulled."""
    return Decision(action="wait", reason=reason, urgent_cancel=urgent)


def required_entry_edge(
    price: float,
    tau_s: float,
    base_edge: float,
    params: Dict[str, float],
    p_side: Optional[float] = None,
) -> float:
    """Compute dynamic required net edge accounting for odds skew & optional early tau.

    - At or below `PRICE_EDGE_PIVOT` ($0.55), no extra price premium.
    - Above the pivot, adds `(price - pivot) * PRICE_EDGE_SLOPE` to protect average entry odds.
    - Above `EARLY_TAU_PIVOT_S`, adds optional smooth early-window premium up to `EARLY_TAU_MAX_PREMIUM`.
    """
    px_pivot = float(params.get("PRICE_EDGE_PIVOT", 0.55))
    px_slope = max(0.0, float(params.get("PRICE_EDGE_SLOPE", 0.05)))
    px_prem = max(0.0, float(price) - px_pivot) * px_slope

    tau_pivot = float(params.get("EARLY_TAU_PIVOT_S", 240.0))
    tau_max_prem = max(0.0, float(params.get("EARLY_TAU_MAX_PREMIUM", 0.0)))
    if tau_s > tau_pivot and tau_max_prem > 0.0:
        span = max(1.0, float(params.get("ENTRY_MAX_TAU_S", 280.0)) - tau_pivot)
        tau_prem = min(1.0, (float(tau_s) - tau_pivot) / span) * tau_max_prem
    else:
        tau_prem = 0.0

    return float(base_edge) + px_prem + tau_prem


def _walk_ask_vwap(book: OrderBook, size: float) -> tuple:
    """Compute visible L2 ask (filled, vwap, worst_px) BEFORE submitting a Taker IOC."""
    filled = 0.0
    notional = 0.0
    worst_px = 0.0
    for px, sz in (getattr(book, "asks", None) or []):
        if filled >= size - 1e-9:
            break
        take = min(size - filled, float(sz))
        if take <= 0.0:
            continue
        filled += take
        notional += take * float(px)
        worst_px = float(px)
    if filled <= 1e-9:
        return 0.0, 0.0, 0.0
    return filled, notional / filled, worst_px


def _compute_ioc_buy_limit(
    sig_px: float,
    p_side: float,
    base_edge: float,
    tau_s: float,
    fee_rate: float,
    params: Dict[str, float],
    max_cap_px: float,
    use_dynamic_edge: bool = True,
    max_pair_cost: Optional[float] = None,
    held_basis: float = 0.0,
    min_sweep_px: Optional[float] = None,
) -> float:
    """Compute exact pre-submission IOC buy limit price (1c tick).

    Guarantees that any fill at or below this limit still clears the required
    post-fee net edge (and optional pair-cost cap) natively on the venue CLOB,
    while never rounding below the visible multi-level sweep worst price.
    """
    ioc_slip = max(0.0, float(params.get("TAKER_IOC_SLIPPAGE_TICKS", 0.02)))
    floor_px = max(sig_px, float(min_sweep_px or 0.0))
    floor_tick = min(0.99, max_cap_px, math.ceil(floor_px * 100.0 - 1e-9) / 100.0)
    cap = min(0.99, max_cap_px, max(floor_tick, round(sig_px + ioc_slip, 2)))
    c_hi = int(round(cap * 100))
    c_lo = max(1, int(round(floor_tick * 100)))
    for c in range(c_hi, c_lo - 1, -1):
        p = c / 100.0
        f_p = taker_fee_per_share(p, fee_rate)
        if max_pair_cost is not None and (held_basis + p + f_p > max_pair_cost + 1e-6):
            continue
        req = required_entry_edge(p, tau_s, base_edge, params, p_side=p_side) if use_dynamic_edge else base_edge
        if (p_side - (p + f_p)) >= req - 1e-6:
            return p
    return round(floor_tick, 2)


def _compute_flip_shares(
    held_leg_cost: float,
    committed: float,
    opp_shares: float,
    opp_book: OrderBook,
    fee_rate: float,
    min_order_sh: float,
    available_cap: float,
    window_budget: float,
    params: Dict[str, float],
) -> float:
    """Compute payoff-driven integer flip shares (5..FLIP_MAX_SHARES) in one shot.

    Steps down integer shares against actual visible L2 ask VWAP so a thin top
    level never causes a valid rescue flip to be dropped entirely when q-1 fits.
    When `FLIP_REQUIRE_BREAKEVEN` is True, refuses any partial/half-way flip that
    cannot reach settlement breakeven on the flipped side (eliminating guaranteed-loss locks).
    """
    if not bool(params.get("FLIP_PAYOFF_SIZING", False)):
        return float(min_order_sh)
    ask0 = opp_book.best_ask
    if ask0 is None or ask0 <= 0.0 or ask0 >= 0.99:
        return float(min_order_sh)
    p0 = float(ask0) + taker_fee_per_share(float(ask0), fee_rate)
    unit_profit = max(0.05, 1.0 - p0)
    target_profit = max(0.0, float(params.get("FLIP_TARGET_PROFIT_USD", 0.0)))
    base_cost = max(float(held_leg_cost), float(committed))
    need_payout = max(0.0, base_cost + target_profit - float(opp_shares))
    min_sh_int = int(math.ceil(min_order_sh - 1e-9))
    q_target = max(min_sh_int, int(math.ceil(need_payout / unit_profit - 1e-9)))
    max_sh = max(min_sh_int, int(float(params.get("FLIP_MAX_SHARES", 12.0))))
    q_cap = min(q_target, max_sh)

    # Cap by hard per-window flip total cost, available bankroll, and visible L2 depth
    max_flip_total_cost = float(params.get("FLIP_MAX_TOTAL_WINDOW_COST", window_budget))
    eff_budget = min(window_budget, max_flip_total_cost)
    rem_cash = max(0.0, min(available_cap, eff_budget - committed))
    f_min_px = float(params.get("FLIP_MIN_PRICE", 0.01))
    f_max_px = float(params.get("FLIP_MAX_PRICE", 0.68))
    req_breakeven = bool(params.get("FLIP_REQUIRE_BREAKEVEN", True))
    vis_depth = sum(float(sz) for _, sz in (getattr(opp_book, "asks", None) or []))
    max_depth_int = int(math.floor(vis_depth + 1e-6))
    q_upper = min(max_sh, max_depth_int)

    if req_breakeven:
        # Search upward from L1-estimated q_target to find the smallest integer q
        # whose real L2-swept VWAP clears settlement breakeven within budget/caps.
        for q in range(max(min_sh_int, q_target), q_upper + 1):
            vis_filled, vis_vwap, _ = _walk_ask_vwap(opp_book, float(q))
            if vis_filled < float(q) - 1e-6:
                continue
            if not (f_min_px - 1e-6 <= vis_vwap <= f_max_px + 1e-6):
                continue
            need_cash = float(q) * (vis_vwap + taker_fee_per_share(vis_vwap, fee_rate))
            if need_cash > rem_cash + 1e-9:
                break
            if (float(opp_shares) + float(q)) >= (base_cost + need_cash - 1e-6):
                return float(q)
        return 0.0

    q_start = min(q_cap, max_depth_int)
    for q in range(q_start, min_sh_int - 1, -1):
        vis_filled, vis_vwap, _ = _walk_ask_vwap(opp_book, float(q))
        if vis_filled < float(q) - 1e-6:
            continue
        if not (f_min_px - 1e-6 <= vis_vwap <= f_max_px + 1e-6):
            continue
        need_cash = float(q) * (vis_vwap + taker_fee_per_share(vis_vwap, fee_rate))
        if need_cash <= rem_cash + 1e-9:
            return float(q)
    return 0.0


def _has_heavy_ask_wall(book: OrderBook, params: Dict[str, float]) -> bool:
    """Detect overwhelming ask-side selling pressure on top-5 book depth.

    Prevents posting a passive Maker bid directly in front of a massive selling
    wall (ask_d5 >= 200 shares AND ask_d5 / bid_d5 >= 4.0).
    """
    max_imbalance = float(params.get("MAKER_MAX_ASK_IMBALANCE", 4.0))
    min_wall = float(params.get("MAKER_MIN_WALL_SHARES", 200.0))
    if max_imbalance <= 0 or min_wall <= 0:
        return False
    bids = getattr(book, "bids", None) or []
    asks = getattr(book, "asks", None) or []
    if not bids or not asks:
        return False
    bid_d5 = sum(float(sz) for _, sz in bids[:5])
    ask_d5 = sum(float(sz) for _, sz in asks[:5])
    return ask_d5 >= min_wall and (ask_d5 / max(1.0, bid_d5)) >= max_imbalance


def _compute_p_fair(spot: SpotState, tau_s: float, params: Dict[str, float]) -> float:
    """Compute Asian 60s-TWAP settlement probability P*(Up) with empirical vol floor."""
    vol_floor = float(params.get("MIN_MODEL_VOL_BPS", 14.0))
    vol_bps = max(float(getattr(spot, "realized_vol_bps", vol_floor) or vol_floor), vol_floor)
    open_px = float(getattr(spot, "open_price", 0.0) or 0.0)
    unit_var = bool(params.get("MODEL_T5_UNIT_VARIANCE", False))
    return estimate_p_fair(
        spot, open_px, tau_s, vol_bps,
        poly_price=getattr(spot, "poly_price", None),
        unit_variance=unit_var,
    )


def determine_objective_side(
    spot: SpotState,
    tau_s: float,
    params: Dict[str, float],
    p_fair: Optional[float] = None,
) -> Optional[Side]:
    """Return the model-favoured side when P* exceeds ENTRY_MIN_PROB."""
    min_prob = float(params.get("ENTRY_MIN_PROB", 0.53))
    p_up = float(p_fair) if p_fair is not None else _compute_p_fair(spot, tau_s, params)
    if p_up >= min_prob:
        return "Up"
    if (1.0 - p_up) >= min_prob:
        return "Down"
    return None


def _plan_inventory_management(
    held_side: Side,
    held_shares: float,
    held_total_shares: float,
    held_basis: float,
    opp_side: Side,
    opp_shares: float,
    opp_book: OrderBook,
    held_book: OrderBook,
    p_fair_held: float,
    spot: SpotState,
    tau_s: float,
    params: Dict[str, float],
    entries_count: int = 1,
    quote_attempts: int = 0,
    circuit_breaker_active: bool = False,
) -> Decision:
    """Manage open directional inventory: optional locks/salvage (disabled by default), leading-side multi-buy add, or confirmed-reversal flip."""
    fee_rate = float(params.get("TAKER_FEE_RATE", 0.07))
    lot_size = float(params.get("LOT_SIZE", 5.0))
    min_order_sh = max(lot_size, float(params.get("MIN_ORDER_SHARES", 5.0)))
    available_cap = float(params.get("AVAILABLE_CAPITAL", 100.0))

    au, ad = held_book.best_ask, opp_book.best_ask
    bu, bd = held_book.best_bid, opp_book.best_bid
    book_tol = max(0.0, float(params.get("BOOK_CONSISTENCY_TOLERANCE", 0.015)))
    opp_book_valid = (
        opp_book.best_ask is not None
        and (opp_book.best_bid is None or opp_book.best_bid < opp_book.best_ask)
    )
    dual_books_consistent = opp_book_valid
    if bool(params.get("STRICT_BOOK_CONSISTENCY", True)):
        if au is not None and ad is not None and bu is not None and bd is not None:
            dual_books_consistent = (
                (au + ad >= 1.0 - book_tol - 1e-9)
                and (bu + bd <= 1.0 + book_tol + 1e-9)
                and (bu < au)
                and (bd < ad)
            )
    spot_is_stale = bool(getattr(spot, "is_stale", False))

    # Under reduce-only circuit breaker, never open secondary risk (pyramid add / reversal flip)
    if circuit_breaker_active:
        return _wait(
            f"Circuit breaker reduce-only: 持有 {held_shares:.0f} 股 {held_side} @ ${held_basis:.2f}，禁止加仓/反手",
            urgent=True,
        )

    max_lots_total = int(params.get("MAX_LOTS", 2))
    max_shares_cap = lot_size * max_lots_total
    max_net_lots = int(params.get("MAX_NET_LOTS", max_lots_total))
    max_net_shares_cap = lot_size * max_net_lots
    max_entries = int(params.get("MAX_ENTRIES_PER_WINDOW", 1))
    anti_toxic_bps = float(params.get("ANTI_TOXICITY_SHOCK_BPS", 2.5))
    vel_bps = abs(float(getattr(spot, "velocity_bps", 0.0) or 0.0))
    max_entry_vol = float(params.get("MAX_ENTRY_VOL_BPS", 75.0))
    live_vol = float(getattr(spot, "realized_vol_bps", 15.0) or 15.0)
    is_storm_vol = max_entry_vol > 0 and live_vol > max_entry_vol
    is_toxic_or_stale = (
        bool(getattr(spot, "is_toxic_shock", False))
        or vel_bps >= anti_toxic_bps
        or is_storm_vol
        or bool(getattr(spot, "is_stale", False))
    )

    # Secondary Entry (v3.2 unified probabilistic inventory control):
    #   (1) Leading-Side Multi-Buy Add: scale in 1-lot increments on the winning/leading side
    #       without waiting for expensive 70%+ odds traps (Taker IOC).
    #   (2) Confirmed Reversal Rebalance (Flip): cut/shift wrong exposure via Taker IOC ONLY when
    #       opposite side confirms clear probability leadership + positive standalone EV (never passive Maker).
    max_tau = float(params.get("ENTRY_MAX_TAU_S", 280.0))
    sec_cooldown = float(params.get("SECONDARY_ENTRY_COOLDOWN_S", 15.0))
    flip_cooldown = float(params.get("FLIP_COOLDOWN_S", 5.0))
    elapsed_since_entry = params.get("SECONDS_SINCE_LAST_ENTRY")
    pyr_cooldown_ok = (elapsed_since_entry is None) or (float(elapsed_since_entry) >= sec_cooldown - 1e-6)
    flip_cooldown_ok = (elapsed_since_entry is None) or (float(elapsed_since_entry) >= flip_cooldown - 1e-6)

    committed = float(params.get("COMMITTED_WINDOW_COST", 0.0))
    window_budget = float(params.get("MAX_WINDOW_LOSS_USD", 10.0))
    max_book_spread = float(params.get("MAX_ORDERBOOK_SPREAD", 0.06))
    max_taker_spread = float(params.get("MAX_TAKER_SPREAD", max_book_spread))
    max_quote_attempts = int(params.get("MAX_QUOTE_ATTEMPTS", 3))
    vel_signed_inv = float(getattr(spot, "velocity_bps", 0.0) or 0.0)
    max_adverse_spot = float(params.get("ENTRY_MAX_ADVERSE_SPOT_BPS", 0.0) or 0.0)
    adverse_thresh = min(anti_toxic_bps, max_adverse_spot) if max_adverse_spot > 0.0 else anti_toxic_bps

    if (
        held_shares >= lot_size - 1e-4
        and entries_count < max_entries
        and tau_s <= max_tau
        and not spot_is_stale
    ):
        books_ok = (au is not None and ad is not None and bu is not None and bd is not None)
        if books_ok and bool(params.get("STRICT_BOOK_CONSISTENCY", True)):
            books_ok = (
                (au + ad >= 1.0 - book_tol - 1e-9)
                and (bu + bd <= 1.0 + book_tol + 1e-9)
                and (bu < au)
                and (bd < ad)
            )

        # 1) Quality Average-Protecting Add (Taker-only by default):
        #    - Blocked in early probe phase (tau > PYRAMID_MAX_TAU_S) and late phase (tau < PYRAMID_MIN_TAU_S)
        #    - Protects average entry price (eff_ask <= held_basis + PYRAMID_MAX_ABOVE_AVG)
        #    - Blocks adding into deteriorating/reversing probabilities (prob drop <= PYRAMID_MAX_PROB_DROP)
        pyr_min_tau = float(params.get("PYRAMID_MIN_TAU_S", 45.0))
        pyr_max_tau = float(params.get("PYRAMID_MAX_TAU_S", 180.0))
        early_phase_min_tau = float(params.get("EARLY_PHASE_MIN_TAU_S", 0.0) or 0.0)
        if early_phase_min_tau > 0.0:
            pyr_max_tau = min(pyr_max_tau, early_phase_min_tau)
        pyr_max_spread = float(params.get("PYRAMID_MAX_SPREAD", max_taker_spread))
        pyr_allow_maker = bool(params.get("PYRAMID_ALLOW_MAKER", False))
        adverse_held_shock = (
            (held_side == "Up" and vel_signed_inv <= -adverse_thresh)
            or (held_side == "Down" and vel_signed_inv >= adverse_thresh)
            or (bool(getattr(spot, "is_toxic_shock", False)) and abs(vel_signed_inv) < 1e-9)
        )
        pyr_taker_ok = (
            (not is_storm_vol)
            and (not spot_is_stale)
            and (not adverse_held_shock)
        ) if bool(params.get("DIRECTIONAL_ANTI_TOXICITY", True)) else (not is_toxic_or_stale)
        allow_add_after_flip = bool(params.get("PYRAMID_ALLOW_AFTER_FLIP", False))
        entry_side_prob = params.get("ENTRY_SIDE_PROB")
        max_prob_drop = float(params.get("PYRAMID_MAX_PROB_DROP", 0.0) or 0.0)
        prob_not_deteriorated = (
            entry_side_prob is None
            or max_prob_drop <= 0.0
            or (float(entry_side_prob) - p_fair_held) <= max_prob_drop + 1e-6
        )
        if (
            books_ok
            and pyr_taker_ok
            and pyr_cooldown_ok
            and prob_not_deteriorated
            and bool(params.get("ENABLE_PYRAMID_ADD", False))
            and (allow_add_after_flip or opp_shares < 1e-4)
            and (held_shares + lot_size) <= max_net_shares_cap + 1e-4
            and (held_total_shares + lot_size) <= max_shares_cap + 1e-4
            and pyr_min_tau <= tau_s <= pyr_max_tau
            and p_fair_held >= float(params.get("PYRAMID_MIN_PROB", 0.56))
            and held_book.spread <= pyr_max_spread + 1e-6
        ):
            p_min_px = float(params.get("PYRAMID_MIN_PRICE", 0.20))
            p_max_px = float(params.get("PYRAMID_MAX_PRICE", 0.62))
            max_above_avg = params.get("PYRAMID_MAX_ABOVE_AVG")
            if max_above_avg is not None and held_basis > 1e-6:
                p_max_px = min(p_max_px, held_basis + float(max_above_avg))
            p_tk_edge = float(params.get("PYRAMID_MIN_TAKER_EDGE", 0.048))
            p_mk_edge = float(params.get("PYRAMID_MIN_MAKER_EDGE", 0.06))
            use_dyn_pyr_edge = bool(params.get("PYRAMID_USE_DYNAMIC_EDGE", True))
            ask = held_book.best_ask
            if ask is not None and p_min_px <= ask <= p_max_px + 1e-6:
                vis_filled, vis_vwap, vis_worst = _walk_ask_vwap(held_book, lot_size)
                eff_ask = vis_vwap if vis_filled >= lot_size - 1e-6 else ask
                if vis_filled >= lot_size - 1e-6 and p_min_px <= eff_ask <= p_max_px + 1e-6:
                    fee = taker_fee_per_share(eff_ask, fee_rate)
                    need_cash = lot_size * (eff_ask + fee)
                    tk_edge = p_fair_held - (eff_ask + fee)
                    req_pyr_edge = (
                        required_entry_edge(eff_ask, tau_s, p_tk_edge, params, p_side=p_fair_held)
                        if use_dyn_pyr_edge
                        else p_tk_edge
                    )
                    if (
                        tk_edge >= req_pyr_edge - 1e-9
                        and available_cap + 1e-9 >= need_cash
                        and committed + need_cash <= window_budget + 1e-9
                    ):
                        ioc_lim = _compute_ioc_buy_limit(
                            eff_ask, p_fair_held, p_tk_edge, tau_s, fee_rate, params,
                            p_max_px, use_dynamic_edge=use_dyn_pyr_edge, min_sweep_px=vis_worst,
                        )
                        return Decision(
                            action="taker_buy",
                            side=held_side,
                            price=round(eff_ask, 4),
                            size=lot_size,
                            utility=round(tk_edge, 4),
                            limit_price=ioc_lim,
                            reason=f"优质控均价加仓 IOC: {held_side} ask=${eff_ask:.2f} (均价=${held_basis:.2f}, 限价=${ioc_lim:.2f}, P*={p_fair_held:.2f}, 净边际={tk_edge:+.3f})",
                            intent="pyramid_add",
                        )
            bid = held_book.best_bid
            mk_cap = min(p_max_px, 0.78)
            if (
                pyr_allow_maker
                and not is_toxic_or_stale
                and not _has_heavy_ask_wall(held_book, params)
                and quote_attempts < max_quote_attempts
                and bid is not None
                and p_min_px <= bid <= mk_cap
            ):
                mk_px = round(bid + 0.01, 2) if (ask is not None and bid + 0.01 < ask and bid + 0.01 <= mk_cap) else round(bid, 2)
                need_cash = lot_size * mk_px
                mk_edge = p_fair_held - mk_px
                if (
                    mk_px <= mk_cap
                    and mk_edge >= p_mk_edge
                    and available_cap + 1e-9 >= need_cash
                    and committed + need_cash <= window_budget + 1e-9
                ):
                    return Decision(
                        action="quote",
                        side=held_side,
                        price=mk_px,
                        size=lot_size,
                        utility=round(mk_edge, 4),
                        reason=f"v2.9 顺势确认加仓 Maker: {held_side} bid=${mk_px:.2f} (P*={p_fair_held:.2f}, 零费边际={mk_edge:+.3f})",
                        intent="pyramid_add",
                    )

        # 2) Payoff-Driven Confirmed Reversal Rebalance / Flip:
        #    When held leg weakens (P*_held <= FLIP_MAX_HELD_PROB) and opposite leg confirms clear
        #    leadership (P*_opp >= FLIP_MIN_OPP_PROB) with standalone positive post-fee EV, execute
        #    one-shot Taker IOC sized to break-even/profit on settlement (5..FLIP_MAX_SHARES, hard-capped).
        p_opp = 1.0 - p_fair_held
        flip_max_spread = float(params.get("FLIP_MAX_SPREAD", 0.12))
        flip_max_pair = float(params.get("FLIP_MAX_PAIR_COST", 2.00))
        flips_used = int(params.get("FLIPS_USED", 0) or 0)
        max_flips = int(params.get("MAX_FLIPS_PER_WINDOW", 1))
        flip_allow_maker = bool(params.get("FLIP_ALLOW_MAKER", False))
        flip_allow_multi = bool(params.get("FLIP_ALLOW_MULTI", False))
        flip_use_dyn_edge = bool(params.get("FLIP_USE_DYNAMIC_EDGE", False))
        adverse_opp_shock = (
            (opp_side == "Up" and vel_signed_inv <= -adverse_thresh)
            or (opp_side == "Down" and vel_signed_inv >= adverse_thresh)
        )
        flip_spot_ok = (
            (not spot_is_stale)
            and (not adverse_opp_shock)
        ) if bool(params.get("DIRECTIONAL_ANTI_TOXICITY", True)) else (not spot_is_stale)
        if (
            bool(params.get("ENABLE_REVERSAL_FLIP", False))
            and flip_cooldown_ok
            and flip_spot_ok
            and flips_used < max_flips
            and (flip_allow_multi or opp_shares < 1e-4)
            and opp_book_valid
            and dual_books_consistent
            and p_fair_held <= float(params.get("FLIP_MAX_HELD_PROB", 0.42))
            and p_opp >= float(params.get("FLIP_MIN_OPP_PROB", 0.58))
            and opp_book.spread <= flip_max_spread + 1e-6
        ):
            f_min_px = float(params.get("FLIP_MIN_PRICE", 0.01))
            f_max_px = float(params.get("FLIP_MAX_PRICE", 0.68))
            f_tk_edge = float(params.get("FLIP_MIN_TAKER_EDGE", 0.038))
            f_mk_edge = float(params.get("FLIP_MIN_MAKER_EDGE", 0.055))
            ask = opp_book.best_ask
            if ask is not None and f_min_px - 1e-6 <= ask <= f_max_px + 1e-6:
                held_leg_cost = float(params.get("HELD_LEG_COST", held_shares * held_basis) or (held_shares * held_basis))
                flip_sz = _compute_flip_shares(
                    held_leg_cost=held_leg_cost,
                    committed=committed,
                    opp_shares=opp_shares,
                    opp_book=opp_book,
                    fee_rate=fee_rate,
                    min_order_sh=min_order_sh,
                    available_cap=available_cap,
                    window_budget=window_budget,
                    params=params,
                )
                if flip_sz >= min_order_sh - 1e-4:
                    vis_filled, vis_vwap, vis_worst = _walk_ask_vwap(opp_book, flip_sz)
                    eff_ask = vis_vwap if vis_filled >= flip_sz - 1e-6 else ask
                    if vis_filled >= flip_sz - 1e-6 and f_min_px - 1e-6 <= eff_ask <= f_max_px + 1e-6:
                        fee = taker_fee_per_share(eff_ask, fee_rate)
                        pair_cost = held_basis + eff_ask + fee
                        need_cash = flip_sz * (eff_ask + fee)
                        tk_edge = p_opp - (eff_ask + fee)
                        req_flip_edge = (
                            required_entry_edge(eff_ask, tau_s, f_tk_edge, params, p_side=p_opp)
                            if flip_use_dyn_edge
                            else f_tk_edge
                        )
                        max_flip_total = float(params.get("FLIP_MAX_TOTAL_WINDOW_COST", window_budget))
                        eff_flip_budget = min(window_budget, max_flip_total) if bool(params.get("FLIP_PAYOFF_SIZING", False)) else window_budget
                        if (
                            pair_cost <= flip_max_pair + 1e-6
                            and tk_edge >= req_flip_edge - 1e-9
                            and available_cap + 1e-9 >= need_cash
                            and committed + need_cash <= eff_flip_budget + 1e-9
                        ):
                            ioc_lim = _compute_ioc_buy_limit(
                                eff_ask, p_opp, f_tk_edge, tau_s, fee_rate, params,
                                f_max_px, use_dynamic_edge=flip_use_dyn_edge,
                                max_pair_cost=flip_max_pair if flip_max_pair < 1.5 else None,
                                held_basis=held_basis,
                                min_sweep_px=vis_worst,
                            )
                            return Decision(
                                action="taker_buy",
                                side=opp_side,
                                price=round(eff_ask, 4),
                                size=flip_sz,
                                utility=round(tk_edge, 4),
                                limit_price=ioc_lim,
                                reason=f"终局动态不对劲反买 IOC ({flip_sz:.0f}股): {opp_side} ask=${eff_ask:.2f} (配对成本=${pair_cost:.3f}, 对侧P*={p_opp:.2f}, 净边际={tk_edge:+.3f})",
                                intent="reversal_flip",
                            )
            bid = opp_book.best_bid
            if (
                flip_allow_maker
                and not is_toxic_or_stale
                and not _has_heavy_ask_wall(opp_book, params)
                and quote_attempts < max_quote_attempts
                and bid is not None
                and f_min_px - 1e-6 <= bid <= f_max_px + 1e-6
            ):
                mk_px = round(bid + 0.01, 2) if (ask is not None and bid + 0.01 < ask and bid + 0.01 <= f_max_px) else round(bid, 2)
                pair_cost_mk = held_basis + mk_px
                need_cash = lot_size * mk_px
                mk_edge = p_opp - mk_px
                if (
                    pair_cost_mk <= flip_max_pair + 1e-6
                    and mk_edge >= f_mk_edge
                    and available_cap + 1e-9 >= need_cash
                    and committed + need_cash <= window_budget + 1e-9
                ):
                    return Decision(
                        action="quote",
                        side=opp_side,
                        price=mk_px,
                        size=lot_size,
                        utility=round(mk_edge, 4),
                        reason=f"动态不对劲反买 Maker: {opp_side} bid=${mk_px:.2f} (配对成本=${pair_cost_mk:.3f}, 对侧P*={p_opp:.2f}, 零费边际={mk_edge:+.3f})",
                        intent="reversal_flip",
                    )

    should_pull = (
        (held_shares >= lot_size - 1e-4)
        or (held_shares >= max_net_shares_cap - 1e-4)
        or (entries_count >= max_entries and held_shares >= min_order_sh - 1e-4)
        or is_toxic_or_stale
    )
    return _wait(
        f"v2.6: 持有 {held_shares:.0f} 股 {held_side} @ ${held_basis:.2f} 至结算 (P*={p_fair_held:.2f})",
        urgent=should_pull,
    )


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
    quote_attempts: int = 0,
) -> Decision:
    """Master decision cycle for the Strict Asian TWAP Net-Edge engine."""
    u_shares = pos_up.shares
    d_shares = pos_down.shares
    net_gap = round(u_shares - d_shares, 4)

    # 1. Watchdog: Circuit breaker (True reduce-only: permits inventory-reducing
    #    hedges / tail salvage on open positions, blocks all new entries)
    cb_enabled = bool(params.get("ENABLE_CIRCUIT_BREAKER", True))
    cb_active = cb_enabled and (circuit_breaker_active or bool(params.get("CIRCUIT_BREAKER_ACTIVE", False)))

    # 2. Fair probability evaluation (Asian 60s TWAP Brownian bridge)
    if p_fair is None:
        p_fair = _compute_p_fair(spot, tau_s, params)
    p_up = float(p_fair)
    p_down = 1.0 - p_up

    # 3. Tail freeze applies to EVERY branch (entries, hedges, unwinds)
    min_tau = float(params.get("ENTRY_MIN_TAU_S", 15.0))
    if tau_s < min_tau:
        return _wait(f"v2.3: Tail freeze (tau {tau_s:.0f}s < {min_tau:.0f}s), holding to settlement", urgent=True)

    # 4. Inventory State: manage open position >= 1 full lot, OR protect active partial Maker order
    #    (if remaining < 1 lot with NO active working order, do NOT deadlock the rest of the window).
    min_order_sh_top = max(float(params.get("LOT_SIZE", 5.0)), float(params.get("MIN_ORDER_SHARES", 5.0)))
    has_working_orders = bool(params.get("HAS_WORKING_ORDERS", True))
    if abs(net_gap) >= min_order_sh_top - 1e-4 or (abs(net_gap) >= 1e-4 and has_working_orders):
        held_side: Side = "Up" if net_gap > 0 else "Down"
        opp_side: Side = "Down" if held_side == "Up" else "Up"
        return _plan_inventory_management(
            held_side=held_side,
            held_shares=abs(net_gap),
            held_total_shares=u_shares if held_side == "Up" else d_shares,
            held_basis=pos_up.avg_price if held_side == "Up" else pos_down.avg_price,
            opp_side=opp_side,
            opp_shares=d_shares if held_side == "Up" else u_shares,
            opp_book=book_down if held_side == "Up" else book_up,
            held_book=book_up if held_side == "Up" else book_down,
            p_fair_held=p_up if held_side == "Up" else p_down,
            spot=spot,
            tau_s=tau_s,
            params=params,
            entries_count=entries_count,
            quote_attempts=quote_attempts,
            circuit_breaker_active=cb_active,
        )

    if cb_active:
        cb_msg = circuit_breaker_reason or params.get("CIRCUIT_BREAKER_REASON", "Active")
        return _wait(f"Circuit breaker active ({cb_msg}): new Leg-1 disabled", urgent=True)

    if (
        not bool(params.get("ALLOW_REENTRY_AFTER_PAIR", True))
        and u_shares > 1e-4
        and d_shares > 1e-4
        and (abs(net_gap) < 1e-4 or (u_shares >= min_order_sh_top - 1e-4 and d_shares >= min_order_sh_top - 1e-4))
    ):
        return _wait(
            f"v3.3: Max position reached & fully hedged pair / 终局调仓 (UP {u_shares:.0f} / DN {d_shares:.0f})，持有至结算",
            urgent=True,
        )

    # 5. Flat Inventory Caps & Risk Envelopes
    max_entries = int(params.get("MAX_ENTRIES_PER_WINDOW", 1))
    if entries_count >= max_entries:
        return _wait(f"Max entries per window reached ({entries_count}/{max_entries}), waiting for next window", urgent=True)

    reentry_cd = float(params.get("REENTRY_COOLDOWN_S", 0.0) or 0.0)
    elapsed_flat = params.get("SECONDS_SINCE_LAST_ENTRY")
    if entries_count > 0 and reentry_cd > 0.0 and elapsed_flat is not None and float(elapsed_flat) < reentry_cd - 1e-6:
        return _wait(f"波段再入场冷却 ({float(elapsed_flat):.1f}s < {reentry_cd:.1f}s)", urgent=False)

    max_lots = int(params.get("MAX_LOTS", 1))
    max_net_lots_top = int(params.get("MAX_NET_LOTS", max_lots))
    lot_size = float(params.get("LOT_SIZE", 5.0))
    size = float(lot_size)
    max_shares = lot_size * max_lots
    max_net_shares_top = lot_size * max_net_lots_top
    early_phase_min_tau_top = float(params.get("EARLY_PHASE_MIN_TAU_S", 0.0) or 0.0)
    if u_shares >= max_shares - 1e-4 or d_shares >= max_shares - 1e-4:
        return _wait(f"Max position reached ({max(u_shares, d_shares):.0f} shares), holding to settlement", urgent=True)

    max_entry_px = float(params.get("ENTRY_MAX_PRICE", 0.72))
    min_entry_px = float(params.get("ENTRY_MIN_PRICE", 0.20))

    available_cap = float(params.get("AVAILABLE_CAPITAL", 100.0))
    if available_cap + 1e-9 < size * min_entry_px:
        return _wait(f"v2.9: Insufficient capital (${available_cap:.2f}) for entry", urgent=True)

    committed = float(params.get("COMMITTED_WINDOW_COST", 0.0))
    window_budget = float(params.get("MAX_WINDOW_LOSS_USD", 5.0))
    if committed + size * min_entry_px > window_budget + 1e-9:
        return _wait(
            f"v2.9: 单窗成本包络已用 ${committed:.2f}/${window_budget:.2f}，拒绝继续开仓",
            urgent=True,
        )

    # 6. Anti-Toxicity & Extreme Volatility Storm Guard
    anti_toxic_bps = float(params.get("ANTI_TOXICITY_SHOCK_BPS", 2.5))
    vel_signed = float(getattr(spot, "velocity_bps", 0.0) or 0.0)
    vel_bps = abs(vel_signed)
    is_shock_any = bool(getattr(spot, "is_toxic_shock", False)) or vel_bps >= anti_toxic_bps
    directional_anti_toxic = bool(params.get("DIRECTIONAL_ANTI_TOXICITY", True))
    if is_shock_any and (not directional_anti_toxic or vel_bps < 1e-9):
        return _wait(f"v2.9: Anti-toxicity spot shock ({vel_bps:.1f} bps), pulling quotes", urgent=True)
    max_entry_vol = float(params.get("MAX_ENTRY_VOL_BPS", 75.0))
    live_vol = float(getattr(spot, "realized_vol_bps", 15.0) or 15.0)
    if max_entry_vol > 0 and live_vol > max_entry_vol:
        return _wait(f"v2.9: 极端巨震风暴避险 (5m波动率 {live_vol:.1f} > {max_entry_vol:.0f} bps)", urgent=True)

    # 7. Active Time Window & Readiness Guards
    max_tau = float(params.get("ENTRY_MAX_TAU_S", 280.0))
    if tau_s > max_tau:
        return _wait(f"v3.3: Observation period (tau {tau_s:.0f}s > {max_tau:.0f}s)", urgent=True)

    if params.get("REQUIRE_STRIKE_AND_BASIS", False):
        if not params.get("BASIS_READY", True) or float(getattr(spot, "open_price", 0.0) or 0.0) <= 0:
            return _wait("v2.3: Waiting for official strike & basis calibration", urgent=True)

    if getattr(spot, "is_stale", False):
        return _wait("v2.3: Spot price stale, holding off entry", urgent=True)

    # Book consistency check (no fake shifting; tolerates 1-tick async WS jitter)
    au, ad = book_up.best_ask, book_down.best_ask
    bu, bd = book_up.best_bid, book_down.best_bid
    if au is None or ad is None or bu is None or bd is None:
        return _wait("v2.3: Incomplete orderbook snapshot, waiting", urgent=True)
    book_tol = max(0.0, float(params.get("BOOK_CONSISTENCY_TOLERANCE", 0.015)))
    if bool(params.get("STRICT_BOOK_CONSISTENCY", True)):
        if (
            (au + ad < 1.0 - book_tol - 1e-9)
            or (bu + bd > 1.0 + book_tol + 1e-9)
            or (bu >= au)
            or (bd >= ad)
        ):
            return _wait(
                f"v2.9: Inconsistent book snapshot (ask_sum={au+ad:.3f}, bid_sum={bu+bd:.3f}), skipping",
                urgent=True,
            )

    late_no_new_tau = float(params.get("LATE_NO_NEW_ENTRY_TAU_S", 0.0) or 0.0)
    if late_no_new_tau > min_tau and tau_s < late_no_new_tau:
        return _wait(
            f"v3.3: 尾盘收官期停止新开仓 (tau {tau_s:.0f}s < {late_no_new_tau:.0f}s)",
            urgent=True,
        )

    # 8. Clean Taker-First Net-Edge & Calm Maker Fallback Evaluation
    fee_rate = float(params.get("TAKER_FEE_RATE", 0.07))
    min_prob = float(params.get("ENTRY_MIN_PROB", 0.53))
    min_maker_edge = float(params.get("MIN_MAKER_EDGE", 0.055))
    min_taker_edge = float(params.get("MIN_TAKER_EDGE", 0.035))
    max_book_spread = float(params.get("MAX_ORDERBOOK_SPREAD", 0.06))
    max_taker_spread = float(params.get("MAX_TAKER_SPREAD", max_book_spread))
    max_quote_attempts = int(params.get("MAX_QUOTE_ATTEMPTS", 3))
    enable_maker = bool(params.get("ENABLE_MAKER_ENTRY", True))

    best_taker = None  # (edge, side, eff_ask, fee, ioc_lim)
    best_maker = None  # (edge, side, maker_px, p_side)
    wall_blocked_msg = None
    budget_blocked_msg = None
    knife_blocked_msg = None
    knife_urgent = False

    enable_book_drop = bool(params.get("ENABLE_BOOK_DROP_GUARD", False))
    book_drop_max = float(params.get("BOOK_DROP_GUARD_MAX", 0.03))
    max_adverse_spot_bps = float(params.get("ENTRY_MAX_ADVERSE_SPOT_BPS", 0.0) or 0.0)

    for side, p_side, tb in (("Up", p_up, book_up), ("Down", p_down, book_down)):
        if p_side < min_prob:
            continue

        # Guard against partial-fill cap overshoot (e.g. 4.93sh partial fill in first 60s or 9.85sh -> 14.85sh)
        side_after = (u_shares + size) if side == "Up" else (d_shares + size)
        net_after = abs((u_shares + size - d_shares) if side == "Up" else (u_shares - (d_shares + size)))
        eff_net_cap = (
            lot_size
            if (early_phase_min_tau_top > 0.0 and tau_s > early_phase_min_tau_top)
            else max_net_shares_top
        )
        if side_after > max_shares + 1e-4 or net_after > eff_net_cap + 1e-4:
            continue

        # Pre-trade knife-drop guard (zero look-ahead)
        if enable_book_drop and book_drop_max > 0.0:
            mom_key = "BOOK_MOM_UP" if side == "Up" else "BOOK_MOM_DOWN"
            side_mkt_mom = params.get(mom_key)
            if side_mkt_mom is not None and float(side_mkt_mom) <= -book_drop_max - 1e-9:
                knife_blocked_msg = (
                    f"v3.3: 盘口急砸防接飞刀 ({side} 近5s变动{float(side_mkt_mom):+.3f} <= -${book_drop_max:.2f})"
                )
                knife_urgent = True
                continue

        # Directional toxicity: block entry if spot is moving sharply AGAINST this side
        adverse_spot_thresh = (
            min(anti_toxic_bps, max_adverse_spot_bps)
            if max_adverse_spot_bps > 0.0
            else anti_toxic_bps
        )
        adverse_shock = (
            (side == "Up" and vel_signed <= -adverse_spot_thresh)
            or (side == "Down" and vel_signed >= adverse_spot_thresh)
        )
        if adverse_shock:
            knife_blocked_msg = (
                f"v3.3: 现货逆向急跌防接飞刀 ({side}, 现货速度={vel_signed:+.1f} bps)"
            )
            continue

        ask = tb.best_ask
        if ask is not None and min_entry_px <= ask <= max_entry_px + 1e-6 and tb.spread <= max_taker_spread + 1e-6:
            # Pre-submission visible L2 VWAP check (no blind best_ask assumption)
            vis_filled, vis_vwap, vis_worst = _walk_ask_vwap(tb, size)
            eff_ask = vis_vwap if vis_filled >= size - 1e-6 else ask
            if min_entry_px <= eff_ask <= max_entry_px + 1e-6 and vis_filled >= size - 1e-6:
                fee = taker_fee_per_share(eff_ask, fee_rate)
                need_cash = size * (eff_ask + fee)
                taker_edge = p_side - (eff_ask + fee)
                req_tk_edge = required_entry_edge(eff_ask, tau_s, min_taker_edge, params, p_side=p_side)
                if taker_edge >= req_tk_edge - 1e-9:
                    if available_cap + 1e-9 < need_cash:
                        budget_blocked_msg = f"v3.3: Insufficient capital (${available_cap:.2f}) for entry (${need_cash:.2f})"
                    elif committed + need_cash > window_budget + 1e-9:
                        budget_blocked_msg = f"v3.3: 单窗成本包络已用 ${committed:.2f}/${window_budget:.2f}，拒绝继续开仓"
                    else:
                        if best_taker is None or taker_edge > best_taker[0]:
                            ioc_lim = _compute_ioc_buy_limit(
                                eff_ask, p_side, min_taker_edge, tau_s, fee_rate, params, max_entry_px,
                                min_sweep_px=vis_worst,
                            )
                            best_taker = (taker_edge, side, eff_ask, fee, ioc_lim)

        # Passive Maker fallback: strictly forbidden during ANY spot shock (adverse selection)
        bid = tb.best_bid
        if (
            enable_maker
            and not is_shock_any
            and tb.spread <= max_book_spread + 1e-6
            and quote_attempts < max_quote_attempts
            and bid is not None
            and min_entry_px <= bid <= max_entry_px + 1e-6
        ):
            maker_px = round(bid + 0.01, 2) if (ask is not None and bid + 0.01 < ask and bid + 0.01 <= max_entry_px) else round(bid, 2)
            maker_px = min(max_entry_px, max(min_entry_px, maker_px))
            need_cash = size * maker_px
            maker_edge = p_side - maker_px
            req_mk_edge = required_entry_edge(maker_px, tau_s, min_maker_edge, params, p_side=p_side)
            if maker_edge >= req_mk_edge - 1e-9:
                if available_cap + 1e-9 < need_cash:
                    budget_blocked_msg = f"v3.3: Insufficient capital (${available_cap:.2f}) for entry (${need_cash:.2f})"
                elif committed + need_cash > window_budget + 1e-9:
                    budget_blocked_msg = f"v3.3: 单窗成本包络已用 ${committed:.2f}/${window_budget:.2f}，拒绝继续开仓"
                elif _has_heavy_ask_wall(tb, params):
                    wall_blocked_msg = f"v3.3: 盘口卖墙压盘拦截 Maker 挂单 ({side} bid=${maker_px:.2f}, P*={p_side:.2f})"
                else:
                    if best_maker is None or maker_edge > best_maker[0]:
                        best_maker = (maker_edge, side, maker_px, p_side)

    # Clean Taker-First routing: take immediately when post-fee edge clears; fall back to Maker in calm wide spreads
    if best_taker is not None:
        edge, side, ask, fee, ioc_lim = best_taker
        p_side = p_up if side == "Up" else p_down
        return Decision(
            action="taker_buy",
            side=side,
            price=round(ask, 4),
            size=size,
            utility=round(edge, 4),
            limit_price=ioc_lim,
            reason=f"v3.3 净边际吃单 IOC: {side} ask=${ask:.2f} (限价=${ioc_lim:.2f}, P*={p_side:.2f}, 扣费净边际={edge:+.3f})",
            intent="entry_lead",
        )

    if best_maker is not None:
        edge, side, maker_px, p_side = best_maker
        return Decision(
            action="quote",
            side=side,
            price=maker_px,
            size=size,
            utility=round(edge, 4),
            reason=f"v3.3 净边际挂单 Maker: {side} bid=${maker_px:.2f} (P*={p_side:.2f}, 零费边际={edge:+.3f})",
            intent="entry_lead",
        )

    if is_shock_any:
        return _wait(f"v2.9: Anti-toxicity spot shock ({vel_bps:.1f} bps), pulling quotes", urgent=True)

    if knife_blocked_msg:
        return _wait(knife_blocked_msg, urgent=knife_urgent)

    if budget_blocked_msg:
        return _wait(budget_blocked_msg, urgent=True)

    if wall_blocked_msg:
        # Urgent cancel so any already-resting passive bid directly in front of
        # a newly formed massive ask wall is pulled immediately instead of taking toxic flow.
        return _wait(wall_blocked_msg, urgent=True)

    return _wait(
        f"v2.9: 盘口无足够净边际 (P*(Up)={p_up:.2f}, Up={bu}/{au}, Dn={bd}/{ad})",
        urgent=False,
    )
