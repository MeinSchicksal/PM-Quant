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
        
    # Path 2: Immediate Taker Unwind / Sell of Held Side (only valid if >= 5 shares)
    u_sell = -999.0
    held_bid = book_held.best_bid
    if held_shares >= 5.0 - 1e-6 and held_bid is not None:
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

    # Otherwise, maintain optimal Maker quote on the deficit side to complete round trip
    r_maker_price = max(0.02, min(0.98, round(maker_quote, 2)))
    if book_opp.best_ask is not None and r_maker_price >= book_opp.best_ask:
        return _wait(
            f"Opposite ask ({book_opp.best_ask}) too tight for passive maker quote"
        )
        
    return Decision(
        action="quote",
        side=opp_side,
        price=r_maker_price,
        size=held_shares,
        utility=round(u_hold, 4),
        reason=f"U_hold({u_hold:.2f}) optimal, quote maker @ {r_maker_price}"
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
    entries_count: int = 0
) -> Decision:
    """Master decision cycle called every tick/event.
    
    Evaluates current inventory, circuit breaker state, volatility regime,
    and outputs the singular best action.
    """
    u_shares = pos_up.shares
    d_shares = pos_down.shares
    net_gap = round(u_shares - d_shares, 4)
    
    from .estimator import (
        estimate_fair_prob, 
        estimate_uncertainty_envelope,
        calculate_adaptive_spread_params
    )
    
    # 1. Calculate Fair Prob & Dynamic Volatility Adaptive Bounds
    # Ensure consistent reference basis to avoid exchange spread contamination
    if p_fair is None:
        poly_spot = getattr(spot, "poly_price", None)
        bin_open = getattr(spot, "binance_open_price", None)
        if poly_spot and poly_spot > 0 and spot.open_price > 0:
            p_fair = estimate_fair_prob(poly_spot, spot.open_price, tau_s, spot.realized_vol_bps)
        elif bin_open and bin_open > 0 and spot.price > 0:
            p_fair = estimate_fair_prob(spot.price, bin_open, tau_s, spot.realized_vol_bps)
        else:
            p_fair = estimate_fair_prob(spot.price, spot.open_price, tau_s, spot.realized_vol_bps)
    
    adaptive_edge, adaptive_k, regime = calculate_adaptive_spread_params(
        spot.realized_vol_bps, 
        params.get("BASE_EDGE", 0.025), 
        params.get("UNCERTAINTY_K", 1.2)
    )
    
    epsilon = estimate_uncertainty_envelope(
        spot.realized_vol_bps,
        params["LATENCY_S"],
        adaptive_k,
        tau_s=tau_s
    )

    # 1b. Dynamic Market Micro-Regime Classification (Multi-Dimensional Statistical Metrics)
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

        # 2a. Time Horizon Gates:
        # If in tail freeze window (< 60s), do not open new entries
        if tau_s <= params["TAIL_FREEZE_S"]:
            return _wait("Tail freeze active, no new entries", urgent=True)
            
        # Entry horizon cutoff: only permit initial Leg-1 entry in the first 2 minutes (tau >= 180s)
        # Prevents entering binary gamma traps where opposite hedge liquidity evaporates
        min_entry_tau = float(params.get("MIN_ENTRY_TAU_S", 180.0))
        if tau_s < min_entry_tau:
            return _wait(f"Late window (tau {tau_s:.0f}s < {min_entry_tau:.0f}s): new Leg-1 entry disabled", urgent=True)

        # 2b. Window Lifecycle & Single Entry Constraint:
        # Forbid re-entering after an unwind in the same 5-minute round (prevents revenge trading in chop)
        max_entries = int(params.get("MAX_ENTRIES_PER_WINDOW", 1))
        if entries_count >= max_entries:
            return _wait(f"Max entries per window reached ({entries_count}/{max_entries}), waiting for next window", urgent=True)

        # If toxic spot shock or toxic regime detected, hold off
        if spot.is_toxic_shock or micro_regime == "toxic":
            return _wait(f"Toxic regime detected (vol={spot.realized_vol_bps:.1f}bps, shock={spot.is_toxic_shock})", urgent=True)

        # Check max position limit: if already holding max paired lots, wait for settlement
        max_lots = int(params.get("MAX_LOTS", 3))
        max_shares = params["LOT_SIZE"] * max_lots
        if u_shares >= max_shares - 1e-4:
            return _wait(f"Max position reached ({u_shares:.0f} shares), holding to settlement", urgent=True)
            
        # Check permissions from regime classification:
        if not regime_info.can_quote_up and not regime_info.can_quote_down:
            return _wait(regime_info.reason, urgent=True)

        can_quote_up = regime_info.can_quote_up
        can_quote_down = regime_info.can_quote_down

        # Reservation bids for both sides using adaptive edge
        r_up, r_down = calculate_reservation_bids(
            p_fair, epsilon, adaptive_edge, q=0.0, gamma=params["RISK_AVERSION_GAMMA"]
        )
        
        available_cap = float(params.get("AVAILABLE_CAPITAL", 100.0))
        min_lots = int(params.get("MIN_LOTS", 1))
        lot_size = float(params.get("LOT_SIZE", 5.0))
        
        up_bid = book_up.best_bid or 0.0
        down_bid = book_down.best_bid or 0.0
        max_entry_px = float(params.get("MAX_MAKER_ENTRY_PRICE", 0.62))
        min_entry_px = float(params.get("MIN_MAKER_ENTRY_PRICE", 0.38))
        # Hard pre-trade risk budget: an unhedged Leg-1 premium is the true
        # worst-case loss for the window and must fit inside the envelope.
        loss_budget = float(params.get("WINDOW_RISK_BUDGET_USD", params.get("MAX_WINDOW_LOSS_USD", 2.00)))
        
        # Initial Leg-1 bare risk sizing:
        # In confirmed 'range_bound' regime, use recommended lots (up to 2 lots = 10 shares) to capture edge
        # Otherwise standard 1 lot (5 shares)
        target_lots = regime_info.recommended_lots if regime_info.recommended_lots > 0 else min_lots
        entry_size = float(target_lots * lot_size)
        if available_cap < (entry_size * 0.50):
            entry_size = float(min_lots * lot_size)
        if available_cap < (entry_size * 0.50):
            return _wait(f"Insufficient capital (${available_cap:.2f}) for 1 lot")
        
        if can_quote_up and up_bid >= 0.02 and r_up >= up_bid and r_up >= 0.35:
            quote_px = max(0.02, min(0.98, round(min(r_up, up_bid), 2)))
            if book_up.best_ask is not None:
                quote_px = min(quote_px, round(book_up.best_ask - 0.01, 2))
            if quote_px < min_entry_px or quote_px > max_entry_px:
                return _wait(f"Up quote ({quote_px:.2f}) outside safe entry zone [{min_entry_px:.2f}, {max_entry_px:.2f}]", urgent=True)
            if quote_px < 0.02 or (book_up.best_ask is not None and quote_px >= book_up.best_ask):
                return _wait(f"Up ask ({book_up.best_ask}) too tight for passive maker entry")
            entry_size = risk_adjusted_entry_size(
                entry_size, quote_px, book_down, lot_size, min_lots, max_lots,
                loss_budget, params.get("TAKER_FEE_RATE", 0.07)
            )
            if entry_size <= 0:
                return _wait(
                    f"Up entry @ {quote_px:.2f} would breach ${loss_budget:.2f} worst-case window loss budget (no locked hedge)",
                    urgent=True,
                )
            return Decision(
                action="quote", side="Up", price=quote_px, size=entry_size,
                utility=round(p_fair - quote_px, 4), reason=f"Enter Up maker @ {quote_px} (p_fair={p_fair:.2f}, {micro_regime}, {entry_size:.0f} shares)"
            )
        elif can_quote_down and down_bid >= 0.02 and r_down >= down_bid and r_down >= 0.35:
            quote_px = max(0.02, min(0.98, round(min(r_down, down_bid), 2)))
            if book_down.best_ask is not None:
                quote_px = min(quote_px, round(book_down.best_ask - 0.01, 2))
            if quote_px < min_entry_px or quote_px > max_entry_px:
                return _wait(f"Down quote ({quote_px:.2f}) outside safe entry zone [{min_entry_px:.2f}, {max_entry_px:.2f}]", urgent=True)
            if quote_px < 0.02 or (book_down.best_ask is not None and quote_px >= book_down.best_ask):
                return _wait(f"Down ask ({book_down.best_ask}) too tight for passive maker entry")
            entry_size = risk_adjusted_entry_size(
                entry_size, quote_px, book_up, lot_size, min_lots, max_lots,
                loss_budget, params.get("TAKER_FEE_RATE", 0.07)
            )
            if entry_size <= 0:
                return _wait(
                    f"Down entry @ {quote_px:.2f} would breach ${loss_budget:.2f} worst-case window loss budget (no locked hedge)",
                    urgent=True,
                )
            return Decision(
                action="quote", side="Down", price=quote_px, size=entry_size,
                utility=round((1.0 - p_fair) - quote_px, 4), reason=f"Enter Down maker @ {quote_px} (p_fair={1.0-p_fair:.2f}, {micro_regime}, {entry_size:.0f} shares)"
            )
            
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
        gamma=params["RISK_AVERSION_GAMMA"],
        p_fill_maker=float(params.get("P_FILL_MAKER", 0.60)),
        fee_rate=params.get("TAKER_FEE_RATE", 0.07),
        max_pair_cost=float(params.get("MAX_PAIR_HEDGE_COST", 1.05)),
        tau_s=tau_s,
        regime_name=micro_regime
    )
