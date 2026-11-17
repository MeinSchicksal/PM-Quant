"""Unified Policy Engine (v2.3 Basis-Adjusted Asian TWAP & Net-Edge Engine).

Single-brain decision cycle:
1. Safety guards: circuit breaker, tail freeze (tau < 15s), stale feed/book.
2. Position handling: hold Leg-1 to settlement by default (no negative-convexity
   pair_cost<=1.00 profit cap). Only allow extreme tail salvage (tau <= 30s,
   P* < 0.12) or optional deep-discount pair lock when explicitly enabled.
3. Anti-toxicity guard: pull resting maker quotes on rapid spot shocks (>= 2.5 bps).
4. Unified Net-Edge entry (tau in [15s, 240s], px in [$0.20, $0.75], P* >= 0.50):
   - Taker IOC when net edge after official fee >= MIN_TAKER_EDGE (0.08).
   - Zero-fee Maker quote when edge >= MIN_MAKER_EDGE (0.04) and attempts < 3.
"""
from typing import Dict, Optional
from .types import Side, OrderBook, Position, Decision, SpotState
from .execution import taker_fee_per_share


def _wait(reason: str, urgent: bool = False) -> Decision:
    """Build a 'wait' decision, declaring whether resting quotes must be pulled."""
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
    """Master decision cycle for the v2.3 unified Net-Edge engine."""
    from .estimator import estimate_p_fair

    u_shares = pos_up.shares
    d_shares = pos_down.shares
    net_gap = round(u_shares - d_shares, 4)

    # 1. Watchdog: Circuit breaker
    cb_enabled = bool(params.get("ENABLE_CIRCUIT_BREAKER", True))
    cb_active = cb_enabled and (circuit_breaker_active or bool(params.get("CIRCUIT_BREAKER_ACTIVE", False)))
    if cb_active:
        cb_msg = circuit_breaker_reason or params.get("CIRCUIT_BREAKER_REASON", "Active")
        return _wait(f"Circuit breaker active ({cb_msg}): new Leg-1 disabled", urgent=True)

    # 2. Fair probability evaluation
    if p_fair is None:
        p_fair = estimate_p_fair(
            spot,
            getattr(spot, "open_price", 0.0),
            tau_s,
            max(float(getattr(spot, "realized_vol_bps", 15.0) or 15.0),
                float(params.get("MIN_MODEL_VOL_BPS", 14.0))),
            poly_price=getattr(spot, "poly_price", None),
        )
    p_up = p_fair
    p_down = 1.0 - p_fair

    # 3. Tail freeze applies to EVERY branch (entries, hedges, unwinds)
    min_tau = float(params.get("ENTRY_MIN_TAU_S", params.get("LWAA_MIN_TAU_S", 15.0)))
    if tau_s < min_tau:
        return _wait(f"v2.3: Tail freeze (tau {tau_s:.0f}s < {min_tau:.0f}s), holding to settlement", urgent=True)

    # 4. Inventory State: Holding / Defensive Salvage / Optional Deep Hedge
    fee_rate = float(params.get("TAKER_FEE_RATE", 0.07))
    if abs(net_gap) >= 1e-4:
        held_side: Side = "Up" if net_gap > 0 else "Down"
        held_shares = abs(net_gap)
        held_basis = pos_up.avg_price if held_side == "Up" else pos_down.avg_price
        opp_side: Side = "Down" if held_side == "Up" else "Up"
        opp_book = book_down if held_side == "Up" else book_up
        p_fair_held = p_up if held_side == "Up" else p_down

        # Optional deep arbitrage lock (disabled by default in v2.3 to preserve upside convexity)
        if bool(params.get("ENABLE_ARBITRAGE_HEDGE", False)):
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
                        reason=f"Deep Arbitrage lock: pair_cost={pair_cost:.3f} <= {max_pair_cost:.2f}, take hedge @ {hedge_ask}"
                    )

        # Extreme tail defensive salvage
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

        return _wait(
            f"v2.3: Holding {held_shares:.0f} {held_side} @ {held_basis:.2f} to settlement (p_fair={p_fair_held:.2f})",
            urgent=True
        )

    if u_shares > 1e-4 and d_shares > 1e-4 and abs(net_gap) < 1e-4:
        return _wait(
            f"v2.3: Max position reached & fully hedged pair (UP {u_shares:.0f} / DN {d_shares:.0f}), holding to settlement",
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
            urgent=True
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
                urgent=True
            )

    # 8. Unified Net-Edge Evaluation
    min_prob = float(params.get("ENTRY_MIN_PROB", params.get("LWAA_MIN_PROB", 0.50)))
    min_maker_edge = float(params.get("MIN_MAKER_EDGE", 0.04))
    min_taker_edge = float(params.get("MIN_TAKER_EDGE", 0.08))
    max_quote_attempts = int(params.get("MAX_QUOTE_ATTEMPTS", 3))

    best_taker = None
    best_maker = None

    for side, p_side, tb in (("Up", p_up, book_up), ("Down", p_down, book_down)):
        if p_side < min_prob:
            continue

        ask = tb.best_ask
        if ask is not None and min_entry_px <= ask <= max_entry_px:
            fee = taker_fee_per_share(ask, fee_rate)
            taker_edge = p_side - (ask + fee)
            if taker_edge >= min_taker_edge:
                if best_taker is None or taker_edge > best_taker[0]:
                    best_taker = (taker_edge, side, ask, fee)

        bid = tb.best_bid
        if quote_attempts < max_quote_attempts and bid is not None and min_entry_px <= bid <= max_entry_px:
            maker_px = round(bid, 2)
            if ask is not None and bid + 0.01 < ask and bid + 0.01 <= max_entry_px:
                maker_px = round(bid + 0.01, 2)
            maker_px = min(max_entry_px, max(min_entry_px, maker_px))
            maker_edge = p_side - maker_px
            if maker_edge >= min_maker_edge:
                if best_maker is None or maker_edge > best_maker[0]:
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
            reason=f"v2.3 Taker IOC: {side} ask={ask:.2f}, fee={fee:.4f}, P*={p_side:.2f}, net_edge={edge:.4f}"
        )

    if best_maker is not None:
        edge, side, maker_px, p_side = best_maker
        return Decision(
            action="quote",
            side=side,
            price=maker_px,
            size=size,
            utility=round(edge, 4),
            reason=f"v2.3 Maker Quote: {side} bid={maker_px:.2f}, P*={p_side:.2f}, edge={edge:.4f}"
        )

    # No new action: keep resting order alive (urgent=False) if simply waiting for fill
    return _wait(
        f"v2.3: No executable Net-Edge (P*(Up)={p_up:.2f}, Up={book_up.best_bid}/{book_up.best_ask}, Dn={book_down.best_bid}/{book_down.best_ask})",
        urgent=False
    )
