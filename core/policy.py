"""Strict Asian 60s-TWAP Net-Edge Policy Engine.

Single-brain decision cycle verified across 294 recorded windows (+$71.13 / 63.7% WR):
1. Safety guards: circuit breaker, tail freeze (tau < 15s), stale feed/book.
2. Position handling: hold Leg-1 to settlement by default to preserve full $1.00
   upside convexity (no negative-convexity pair_cost<=1.00 profit cap). Only
   allow extreme tail salvage (tau <= 30s, P* < 0.12, bid in [$0.06, $0.15]) or
   opt-in deep arbitrage locks when explicitly enabled.
3. Anti-toxicity guard: pull resting maker quotes on rapid spot shocks (>= 2.5 bps).
4. Unified Net-Edge entry (tau in [15s, 240s], px in [$0.20, $0.75], P* >= 0.52):
   - Taker IOC when net edge after official fee >= MIN_TAKER_EDGE (0.09).
   - Zero-fee Maker quote when edge >= MIN_MAKER_EDGE (0.05) and attempts < 3.
"""
from typing import Dict, Optional
from .types import Side, OrderBook, Position, Decision, SpotState
from .execution import taker_fee_per_share
from .estimator import estimate_p_fair


def _wait(reason: str, urgent: bool = False) -> Decision:
    """Build a 'wait' decision, declaring whether resting quotes must be pulled."""
    return Decision(action="wait", reason=reason, urgent_cancel=urgent)


def _compute_p_fair(spot: SpotState, tau_s: float, params: Dict[str, float]) -> float:
    """Compute Asian 60s-TWAP settlement probability P*(Up) with empirical vol floor."""
    vol_floor = float(params.get("MIN_MODEL_VOL_BPS", 14.0))
    vol_bps = max(float(getattr(spot, "realized_vol_bps", vol_floor) or vol_floor), vol_floor)
    open_px = float(getattr(spot, "open_price", 0.0) or 0.0)
    return estimate_p_fair(spot, open_px, tau_s, vol_bps, poly_price=getattr(spot, "poly_price", None))


def determine_objective_side(
    spot: SpotState,
    tau_s: float,
    params: Dict[str, float],
) -> Optional[Side]:
    """Return the model-favoured side when P* exceeds ENTRY_MIN_PROB."""
    min_prob = float(params.get("ENTRY_MIN_PROB", params.get("LWAA_MIN_PROB", 0.52)))
    p_up = _compute_p_fair(spot, tau_s, params)
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
) -> Decision:
    """Manage open directional inventory: black-swan lock, optional arb, tail salvage, or v2.6 secondary entry."""
    fee_rate = float(params.get("TAKER_FEE_RATE", 0.07))
    lot_size = float(params.get("LOT_SIZE", 5.0))
    min_order_sh = max(lot_size, float(params.get("MIN_ORDER_SHARES", 5.0)))

    if bool(params.get("ENABLE_ARBITRAGE_HEDGE", False)) and held_shares >= min_order_sh - 1e-4:
        max_pair_cost = float(params.get("ARBITRAGE_HEDGE_MAX_PAIR_COST", 0.90))
        hedge_ask = opp_book.best_ask
        if hedge_ask is not None:
            fee = taker_fee_per_share(hedge_ask, fee_rate)
            pair_cost = held_basis + hedge_ask + fee
            if pair_cost <= max_pair_cost:
                return Decision(
                    action="taker_hedge",
                    side=opp_side,
                    price=hedge_ask,
                    size=held_shares,
                    utility=round(held_shares * (1.00 - pair_cost), 4),
                    reason=f"Deep Arbitrage lock: pair_cost={pair_cost:.3f} <= {max_pair_cost:.2f}, take hedge @ {hedge_ask:.2f}",
                )

    # Ultra-deep black-swan lock: protect 94%+ winners (opp_ask <= $0.06 & pair_cost <= $0.85, requires >= 5.0 shares)
    if bool(params.get("ENABLE_BLACKSWAN_LOCK", False)) and held_shares >= min_order_sh - 1e-4:
        bs_max_opp = float(params.get("BLACKSWAN_LOCK_MAX_OPP_ASK", 0.06))
        bs_max_pair = float(params.get("BLACKSWAN_LOCK_MAX_PAIR_COST", 0.85))
        hedge_ask = opp_book.best_ask
        if hedge_ask is not None and hedge_ask <= bs_max_opp + 1e-6:
            fee = taker_fee_per_share(hedge_ask, fee_rate)
            pair_cost = held_basis + hedge_ask + fee
            if pair_cost <= bs_max_pair + 1e-6:
                return Decision(
                    action="taker_hedge",
                    side=opp_side,
                    price=hedge_ask,
                    size=held_shares,
                    utility=round(held_shares * (1.00 - pair_cost), 4),
                    reason=f"v2.6 极深黑天鹅锁利: {opp_side} ask=${hedge_ask:.2f}, pair_cost={pair_cost:.3f} <= {bs_max_pair:.2f}",
                )

    salvage_tau = float(params.get("DEFENSIVE_SALVAGE_TAU_S", 30.0))
    salvage_prob = float(params.get("DEFENSIVE_SALVAGE_MAX_PROB", 0.12))
    salvage_max_bid = float(params.get("DEFENSIVE_SALVAGE_MAX_BID", 0.15))
    salvage_min_bid = float(params.get("DEFENSIVE_SALVAGE_MIN_BID", 0.06))
    salvage_enabled = (
        bool(params.get("DEFENSIVE_SALVAGE_ENABLE", True))
        and bool(params.get("ALLOW_TAKER_UNWIND", True))
    )

    if salvage_enabled and held_shares >= min_order_sh - 1e-4 and tau_s <= salvage_tau and p_fair_held < salvage_prob:
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
                reason=f"Defensive Salvage: 尾盘({tau_s:.0f}s) P*={p_fair_held:.2f} 坍塌，市价出清残值 bid={salvage_bid:.2f} (挽回${salvage_cash:.2f})",
            )

    max_shares_cap = lot_size * int(params.get("MAX_LOTS", 1))
    max_entries = int(params.get("MAX_ENTRIES_PER_WINDOW", 1))
    anti_toxic_bps = float(params.get("ANTI_TOXICITY_SHOCK_BPS", 2.5))
    vel_bps = abs(float(getattr(spot, "velocity_bps", 0.0) or 0.0))
    is_toxic_or_stale = (
        bool(getattr(spot, "is_toxic_shock", False))
        or vel_bps >= anti_toxic_bps
        or bool(getattr(spot, "is_stale", False))
    )

    # v2.6 Secondary Entry: Confirmed Pyramid Add or Collapsed Reversal Flip
    # Only evaluate secondary entries once Leg-1 has filled at least one complete lot
    # AND at least SECONDARY_ENTRY_COOLDOWN_S seconds have passed since the prior entry fill
    # (prevents 0.5s double-tap into an unconfirmed 2x position).
    max_tau = float(params.get("ENTRY_MAX_TAU_S", params.get("LWAA_MAX_TAU_S", 240.0)))
    sec_cooldown = float(params.get("SECONDARY_ENTRY_COOLDOWN_S", 15.0))
    elapsed_since_entry = params.get("SECONDS_SINCE_LAST_ENTRY")
    cooldown_ok = (elapsed_since_entry is None) or (float(elapsed_since_entry) >= sec_cooldown - 1e-6)
    if (
        held_shares >= lot_size - 1e-4
        and entries_count < max_entries
        and tau_s <= max_tau
        and not is_toxic_or_stale
        and cooldown_ok
    ):
        au, ad = held_book.best_ask, opp_book.best_ask
        bu, bd = held_book.best_bid, opp_book.best_bid
        books_ok = (au is not None and ad is not None and bu is not None and bd is not None)
        if books_ok and bool(params.get("STRICT_BOOK_CONSISTENCY", True)):
            books_ok = (au + ad >= 0.995) and (bu + bd <= 1.005) and (bu < au) and (bd < ad)

        available_cap = float(params.get("AVAILABLE_CAPITAL", 100.0))
        committed = float(params.get("COMMITTED_WINDOW_COST", 0.0))
        window_budget = float(params.get("MAX_WINDOW_LOSS_USD", 10.0))
        max_book_spread = float(params.get("MAX_ORDERBOOK_SPREAD", 0.06))
        max_quote_attempts = int(params.get("MAX_QUOTE_ATTEMPTS", 3))

        if books_ok:
            # 1) Confirmed Pyramid Add on winning unhedged Leg-1 (check gross side shares, never net gap)
            if (
                bool(params.get("ENABLE_PYRAMID_ADD", False))
                and opp_shares < 1e-4
                and held_total_shares < max_shares_cap - 1e-4
                and tau_s <= float(params.get("PYRAMID_MAX_TAU_S", 180.0))
                and p_fair_held >= float(params.get("PYRAMID_MIN_PROB", 0.65))
                and held_book.spread <= max_book_spread + 1e-6
            ):
                p_min_px = float(params.get("PYRAMID_MIN_PRICE", 0.35))
                p_max_px = float(params.get("PYRAMID_MAX_PRICE", 0.72))
                p_tk_edge = float(params.get("PYRAMID_MIN_TAKER_EDGE", 0.09))
                p_mk_edge = float(params.get("PYRAMID_MIN_MAKER_EDGE", 0.06))
                if available_cap >= lot_size * p_max_px and committed + lot_size * p_max_px <= window_budget + 1e-9:
                    ask = held_book.best_ask
                    if ask is not None and p_min_px <= ask <= p_max_px:
                        fee = taker_fee_per_share(ask, fee_rate)
                        tk_edge = p_fair_held - (ask + fee)
                        if tk_edge >= p_tk_edge:
                            return Decision(
                                action="taker_buy",
                                side=held_side,
                                price=ask,
                                size=lot_size,
                                utility=round(tk_edge, 4),
                                reason=f"v2.6 顺势确认加仓 IOC: {held_side} ask=${ask:.2f} (P*={p_fair_held:.2f}, 净边际={tk_edge:+.3f})",
                            )
                    bid = held_book.best_bid
                    mk_cap = min(p_max_px, 0.70)
                    if quote_attempts < max_quote_attempts and bid is not None and p_min_px <= bid <= mk_cap:
                        mk_px = round(bid + 0.01, 2) if (ask is not None and bid + 0.01 < ask and bid + 0.01 <= mk_cap) else round(bid, 2)
                        mk_edge = p_fair_held - mk_px
                        if mk_px <= mk_cap and mk_edge >= p_mk_edge:
                            return Decision(
                                action="quote",
                                side=held_side,
                                price=mk_px,
                                size=lot_size,
                                utility=round(mk_edge, 4),
                                reason=f"v2.6 顺势确认加仓 Maker: {held_side} bid=${mk_px:.2f} (P*={p_fair_held:.2f}, 零费边际={mk_edge:+.3f})",
                            )

            # 2) Independent Opposite-Side Flip when Leg-1 collapsed
            p_opp = 1.0 - p_fair_held
            if (
                bool(params.get("ENABLE_REVERSAL_FLIP", False))
                and opp_shares < 1e-4
                and p_fair_held <= float(params.get("FLIP_MAX_HELD_PROB", 0.30))
                and p_opp >= float(params.get("FLIP_MIN_OPP_PROB", 0.65))
                and opp_book.spread <= max_book_spread + 1e-6
            ):
                f_min_px = float(params.get("ENTRY_MIN_PRICE", 0.20))
                f_max_px = float(params.get("ENTRY_MAX_PRICE", 0.75))
                f_tk_edge = float(params.get("FLIP_MIN_TAKER_EDGE", 0.09))
                f_mk_edge = float(params.get("FLIP_MIN_MAKER_EDGE", 0.06))
                if available_cap >= lot_size * f_max_px and committed + lot_size * f_max_px <= window_budget + 1e-9:
                    ask = opp_book.best_ask
                    if ask is not None and f_min_px <= ask <= f_max_px:
                        fee = taker_fee_per_share(ask, fee_rate)
                        tk_edge = p_opp - (ask + fee)
                        if tk_edge >= f_tk_edge:
                            return Decision(
                                action="taker_buy",
                                side=opp_side,
                                price=ask,
                                size=lot_size,
                                utility=round(tk_edge, 4),
                                reason=f"v2.6 坍塌反手吃单 IOC: {opp_side} ask=${ask:.2f} (P*={p_opp:.2f}, 净边际={tk_edge:+.3f})",
                            )
                    bid = opp_book.best_bid
                    if quote_attempts < max_quote_attempts and bid is not None and f_min_px <= bid <= f_max_px:
                        mk_px = round(bid + 0.01, 2) if (ask is not None and bid + 0.01 < ask and bid + 0.01 <= f_max_px) else round(bid, 2)
                        mk_edge = p_opp - mk_px
                        if mk_edge >= f_mk_edge:
                            return Decision(
                                action="quote",
                                side=opp_side,
                                price=mk_px,
                                size=lot_size,
                                utility=round(mk_edge, 4),
                                reason=f"v2.6 坍塌反手挂单 Maker: {opp_side} bid=${mk_px:.2f} (P*={p_opp:.2f}, 零费边际={mk_edge:+.3f})",
                            )

    should_pull = (
        (held_shares >= max_shares_cap - 1e-4)
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

    # 1. Watchdog: Circuit breaker
    cb_enabled = bool(params.get("ENABLE_CIRCUIT_BREAKER", True))
    cb_active = cb_enabled and (circuit_breaker_active or bool(params.get("CIRCUIT_BREAKER_ACTIVE", False)))
    if cb_active:
        cb_msg = circuit_breaker_reason or params.get("CIRCUIT_BREAKER_REASON", "Active")
        return _wait(f"Circuit breaker active ({cb_msg}): new Leg-1 disabled", urgent=True)

    # 2. Fair probability evaluation (Asian 60s TWAP Brownian bridge)
    if p_fair is None:
        p_fair = _compute_p_fair(spot, tau_s, params)
    p_up = float(p_fair)
    p_down = 1.0 - p_up

    # 3. Tail freeze applies to EVERY branch (entries, hedges, unwinds)
    min_tau = float(params.get("ENTRY_MIN_TAU_S", params.get("LWAA_MIN_TAU_S", 15.0)))
    if tau_s < min_tau:
        return _wait(f"v2.3: Tail freeze (tau {tau_s:.0f}s < {min_tau:.0f}s), holding to settlement", urgent=True)

    # 4. Inventory State: Black-Swan Lock / Optional Deep Arb / Tail Salvage / v2.6 Secondary Entry
    if abs(net_gap) >= 1e-4:
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
        )

    if u_shares > 1e-4 and d_shares > 1e-4 and abs(net_gap) < 1e-4:
        return _wait(
            f"v2.3: Max position reached & fully hedged pair (UP {u_shares:.0f} / DN {d_shares:.0f}), holding to settlement",
            urgent=True,
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

    max_entry_px = float(params.get("ENTRY_MAX_PRICE", params.get("LWAA_MAX_ENTRY_PRICE", 0.75)))
    min_entry_px = float(params.get("ENTRY_MIN_PRICE", 0.20))

    available_cap = float(params.get("AVAILABLE_CAPITAL", 100.0))
    if available_cap < size * max_entry_px:
        return _wait(f"v2.3: Insufficient capital (${available_cap:.2f}) for entry (${size * max_entry_px:.2f})", urgent=True)

    committed = float(params.get("COMMITTED_WINDOW_COST", 0.0))
    window_budget = float(params.get("MAX_WINDOW_LOSS_USD", 5.0))
    if committed + size * max_entry_px > window_budget + 1e-9:
        return _wait(
            f"v2.3: 单窗成本包络已用 ${committed:.2f}/${window_budget:.2f}，拒绝继续开仓",
            urgent=True,
        )

    # 6. Anti-Toxicity Guard: cancel resting quotes on rapid Binance shock
    anti_toxic_bps = float(params.get("ANTI_TOXICITY_SHOCK_BPS", 2.5))
    vel_bps = abs(float(getattr(spot, "velocity_bps", 0.0) or 0.0))
    if getattr(spot, "is_toxic_shock", False) or vel_bps >= anti_toxic_bps:
        return _wait(f"v2.3: Anti-toxicity spot shock ({vel_bps:.1f} bps), pulling quotes", urgent=True)

    # 7. Active Time Window & Readiness Guards
    max_tau = float(params.get("ENTRY_MAX_TAU_S", params.get("LWAA_MAX_TAU_S", 240.0)))
    if tau_s > max_tau:
        return _wait(f"v2.3: Observation period (tau {tau_s:.0f}s > {max_tau:.0f}s)", urgent=True)

    if params.get("REQUIRE_STRIKE_AND_BASIS", False):
        if not params.get("BASIS_READY", True) or float(getattr(spot, "open_price", 0.0) or 0.0) <= 0:
            return _wait("v2.3: Waiting for official strike & basis calibration", urgent=True)

    if getattr(spot, "is_stale", False):
        return _wait("v2.3: Spot price stale, holding off entry", urgent=True)

    # Strict book consistency check (no fake shifting)
    au, ad = book_up.best_ask, book_down.best_ask
    bu, bd = book_up.best_bid, book_down.best_bid
    if au is None or ad is None or bu is None or bd is None:
        return _wait("v2.3: Incomplete orderbook snapshot, waiting", urgent=True)
    if bool(params.get("STRICT_BOOK_CONSISTENCY", True)):
        if (au + ad < 0.995) or (bu + bd > 1.005) or (bu >= au) or (bd >= ad):
            return _wait(
                f"v2.3: Inconsistent book snapshot (ask_sum={au+ad:.3f}, bid_sum={bu+bd:.3f}), skipping",
                urgent=True,
            )

    # 8. Strict Net-Edge Evaluation
    fee_rate = float(params.get("TAKER_FEE_RATE", 0.07))
    min_prob = float(params.get("ENTRY_MIN_PROB", params.get("LWAA_MIN_PROB", 0.52)))
    min_maker_edge = float(params.get("MIN_MAKER_EDGE", params.get("LWAA_MIN_EDGE", 0.05)))
    min_taker_edge = float(params.get("MIN_TAKER_EDGE", 0.09))
    max_book_spread = float(params.get("MAX_ORDERBOOK_SPREAD", 0.06))
    max_quote_attempts = int(params.get("MAX_QUOTE_ATTEMPTS", 3))

    best_taker = None
    best_maker = None

    for side, p_side, tb in (("Up", p_up, book_up), ("Down", p_down, book_down)):
        if p_side < min_prob or tb.spread > max_book_spread + 1e-6:
            continue

        ask = tb.best_ask
        if ask is not None and min_entry_px <= ask <= max_entry_px:
            fee = taker_fee_per_share(ask, fee_rate)
            taker_edge = p_side - (ask + fee)
            if taker_edge >= min_taker_edge and (best_taker is None or taker_edge > best_taker[0]):
                best_taker = (taker_edge, side, ask, fee)

        bid = tb.best_bid
        if quote_attempts < max_quote_attempts and bid is not None and min_entry_px <= bid <= max_entry_px:
            maker_px = round(bid + 0.01, 2) if (ask is not None and bid + 0.01 < ask and bid + 0.01 <= max_entry_px) else round(bid, 2)
            maker_px = min(max_entry_px, max(min_entry_px, maker_px))
            maker_edge = p_side - maker_px
            if maker_edge >= min_maker_edge and (best_maker is None or maker_edge > best_maker[0]):
                best_maker = (maker_edge, side, maker_px, p_side)

    if best_taker is not None:
        edge, side, ask, fee = best_taker
        p_side = p_up if side == "Up" else p_down
        return Decision(
            action="taker_buy",
            side=side,
            price=ask,
            size=size,
            utility=round(edge, 4),
            reason=f"v2.3 净边际吃单 IOC: {side} ask=${ask:.2f} (P*={p_side:.2f}, 扣费净边际={edge:+.3f})",
        )

    if best_maker is not None:
        edge, side, maker_px, p_side = best_maker
        return Decision(
            action="quote",
            side=side,
            price=maker_px,
            size=size,
            utility=round(edge, 4),
            reason=f"v2.3 净边际挂单 Maker: {side} bid=${maker_px:.2f} (P*={p_side:.2f}, 零费边际={edge:+.3f})",
        )

    return _wait(
        f"v2.3: 盘口无足够净边际 (P*(Up)={p_up:.2f}, Up={bu}/{au}, Dn={bd}/{ad})",
        urgent=False,
    )
