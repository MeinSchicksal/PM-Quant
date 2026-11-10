"""Unified Policy Engine (Avellaneda-Stoikov Inventory Pricing & Utility Arbitration).

Replaces hundreds of discrete rule branches with continuous inventory-adjusted
reservation prices and risk-adjusted utility comparisons.
"""
from typing import Dict, Optional, Tuple
from .types import Side, OrderBook, Position, Decision, SpotState
from .execution import taker_fee_per_share


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
    max_pair_cost: float = 1.05
) -> Decision:
    """Evaluate 3-way utility arbitration when holding an unhedged inventory.
    
    Compares:
    1. U_hedge: Immediate Taker hedge on opposite side (zero variance, lock-in cost).
       STRICT GUARD: Disqualified if pair_cost > max_pair_cost ($1.05) to prevent
       catastrophic whipsaw where trader pays $1.50+ for a $1.00 payout.
    2. U_sell: Immediate Taker sell of held side (zero variance, cut loss if >= 5 shares).
       Dominates when hedge is exorbitant.
    3. U_hold: Passive quoting on Maker / waiting to settle (penalized by inventory variance).
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
        
    # Path 2: Immediate Taker Unwind / Sell of Held Side (only valid if >= 5 shares)
    u_sell = -999.0
    held_bid = book_held.best_bid
    if held_shares >= 5.0 - 1e-6 and held_bid is not None:
        u_sell = held_shares * (held_bid - taker_fee_per_share(held_bid, fee_rate) - held_basis_price)
        
    # Path 3: Passive Quoting / Holding to Expiration
    # EV of holding = shares * (p_fair - cost)
    # Variance penalty = gamma * shares * Var
    ev_holding = held_shares * (p_fair_held - held_basis_price)
    variance_penalty = gamma * (held_shares ** 1.2) * var
    u_hold = ev_holding - variance_penalty
    
    # If Maker order has a chance of filling, add expected edge
    opp_bid = book_opp.best_bid
    if opp_bid is not None:
        maker_gain = held_shares * (1.00 - held_basis_price - opp_bid)
        if maker_gain > 0:
            u_hold += p_fill_maker * maker_gain

    # === ARBITRATION DECISION ===
    # Best immediate guaranteed exit (hedge vs sell)
    best_immediate = max(u_hedge, u_sell)
    
    # If immediate execution provides superior utility than holding with variance:
    if best_immediate > u_hold:
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

    # Otherwise, maintain optimal Maker quote on the deficit side
    # Reservation price determines optimal bid
    r_maker_price = round(min(0.98, max(0.02, 1.00 - held_basis_price)), 2)
    if opp_bid is not None and opp_bid >= 0.02:
        r_maker_price = min(r_maker_price, opp_bid)
    
    # Guard: Maker quote must never cross the opposite ask into taker territory
    if book_opp.best_ask is not None:
        r_maker_price = min(r_maker_price, round(book_opp.best_ask - 0.01, 2))
        
    r_maker_price = max(0.02, min(0.98, round(r_maker_price, 2)))
    if book_opp.best_ask is not None and r_maker_price >= book_opp.best_ask:
        return Decision(
            action="wait",
            reason=f"Opposite ask ({book_opp.best_ask}) too tight for passive maker quote"
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
    p_fair: Optional[float] = None
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
        calculate_adaptive_spread_params,
        calculate_kelly_order_size,
        classify_market_micro_regime
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
        adaptive_k
    )

    # 1b. Dynamic Market Micro-Regime Classification (Statistical Drift SNR)
    ref_spot = getattr(spot, "poly_price", None) or spot.price
    ref_open = getattr(spot, "open_price", 0.0)
    if not (getattr(spot, "poly_price", None) and spot.poly_price > 0):
        ref_open = getattr(spot, "binance_open_price", spot.open_price)
    trend_threshold = float(params.get("SNR_TREND_THRESHOLD", 1.8))
    range_threshold = float(params.get("SNR_RANGE_THRESHOLD", 1.2))
    micro_regime, snr, ret_bps = classify_market_micro_regime(
        ref_spot, ref_open, tau_s, spot.realized_vol_bps,
        is_toxic_shock=spot.is_toxic_shock,
        trend_threshold=trend_threshold,
        range_threshold=range_threshold
    )
    
    # 2. Case: Balanced or Flat Inventory (|net_gap| < 1e-4)
    if abs(net_gap) < 1e-4:
        # Check Circuit Breaker Watchdog: forbid opening new Leg-1 when tripped or cooling
        cb_active = circuit_breaker_active or bool(params.get("CIRCUIT_BREAKER_ACTIVE", False))
        if cb_active:
            cb_msg = circuit_breaker_reason or params.get("CIRCUIT_BREAKER_REASON", "Active")
            return Decision(action="wait", reason=f"Circuit breaker active ({cb_msg}): new Leg-1 disabled")

        # If in tail freeze window (< 60s), do not open new windows
        if tau_s <= params["TAIL_FREEZE_S"]:
            return Decision(action="wait", reason="Tail freeze active, no new entries")
            
        # If toxic spot shock or toxic regime detected, hold off
        if spot.is_toxic_shock or micro_regime == "toxic":
            return Decision(action="wait", reason=f"Toxic regime detected (vol={spot.realized_vol_bps:.1f}bps, shock={spot.is_toxic_shock})")

        # Check max position limit: if already holding max paired lots, wait for settlement
        max_lots = int(params.get("MAX_LOTS", 3))
        max_shares = params["LOT_SIZE"] * max_lots
        if u_shares >= max_shares - 1e-4:
            return Decision(action="wait", reason=f"Max position reached ({u_shares:.0f} shares), holding to settlement")
            
        # Reservation bids for both sides using adaptive edge
        r_up, r_down = calculate_reservation_bids(
            p_fair, epsilon, adaptive_edge, q=0.0, gamma=params["RISK_AVERSION_GAMMA"]
        )
        
        available_cap = float(params.get("AVAILABLE_CAPITAL", 100.0))
        min_lots = int(params.get("MIN_LOTS", 1))
        k_frac = float(params.get("KELLY_FRACTION", 0.25))
        lot_size = float(params.get("LOT_SIZE", 5.0))
        
        up_bid = book_up.best_bid or 0.0
        down_bid = book_down.best_bid or 0.0
        max_entry_px = float(params.get("MAX_MAKER_ENTRY_PRICE", 0.62))
        min_entry_px = float(params.get("MIN_MAKER_ENTRY_PRICE", 0.38))
        
        # Dual-Regime Quoting Rules:
        # - If trending_down: strictly forbid quoting Up (prevents catching falling knives)
        # - If trending_up: strictly forbid quoting Down (prevents standing in front of surges)
        # - Maker entry quotes must stay within [min_entry_px, max_entry_px] to prevent buying at the top
        can_quote_up = (micro_regime != "trending_down")
        can_quote_down = (micro_regime != "trending_up")
        
        if can_quote_up and up_bid >= 0.02 and r_up >= up_bid and r_up >= 0.35 and p_fair >= 0.48:
            quote_px = max(0.02, min(0.98, round(min(r_up, up_bid), 2)))
            if book_up.best_ask is not None:
                quote_px = min(quote_px, round(book_up.best_ask - 0.01, 2))
            if quote_px < min_entry_px or quote_px > max_entry_px:
                return Decision(action="wait", reason=f"Up quote ({quote_px:.2f}) outside safe entry zone [{min_entry_px:.2f}, {max_entry_px:.2f}]")
            if quote_px < 0.02 or (book_up.best_ask is not None and quote_px >= book_up.best_ask):
                return Decision(action="wait", reason=f"Up ask ({book_up.best_ask}) too tight for passive maker entry")
            size = calculate_kelly_order_size(
                available_cap, p_fair, quote_px, lot_size, min_lots, max_lots, k_frac
            )
            if size < lot_size:
                return Decision(action="wait", reason=f"Insufficient capital (${available_cap:.2f}) for 1 lot")
            return Decision(
                action="quote", side="Up", price=quote_px, size=size,
                utility=round(p_fair - quote_px, 4), reason=f"Enter Up maker @ {quote_px} (p_fair={p_fair:.2f}, {micro_regime}, {size:.0f}s)"
            )
        elif can_quote_down and down_bid >= 0.02 and r_down >= down_bid and r_down >= 0.35 and (1.0 - p_fair) >= 0.48:
            quote_px = max(0.02, min(0.98, round(min(r_down, down_bid), 2)))
            if book_down.best_ask is not None:
                quote_px = min(quote_px, round(book_down.best_ask - 0.01, 2))
            if quote_px < min_entry_px or quote_px > max_entry_px:
                return Decision(action="wait", reason=f"Down quote ({quote_px:.2f}) outside safe entry zone [{min_entry_px:.2f}, {max_entry_px:.2f}]")
            if quote_px < 0.02 or (book_down.best_ask is not None and quote_px >= book_down.best_ask):
                return Decision(action="wait", reason=f"Down ask ({book_down.best_ask}) too tight for passive maker entry")
            size = calculate_kelly_order_size(
                available_cap, 1.0 - p_fair, quote_px, lot_size, min_lots, max_lots, k_frac
            )
            if size < lot_size:
                return Decision(action="wait", reason=f"Insufficient capital (${available_cap:.2f}) for 1 lot")
            return Decision(
                action="quote", side="Down", price=quote_px, size=size,
                utility=round((1.0 - p_fair) - quote_px, 4), reason=f"Enter Down maker @ {quote_px} (p_fair={1.0-p_fair:.2f}, {micro_regime}, {size:.0f}s)"
            )
            
        return Decision(action="wait", reason=f"No edge outside uncertainty band (+/-{epsilon:.2f}, {micro_regime})")

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
        fee_rate=params.get("TAKER_FEE_RATE", 0.07),
        max_pair_cost=float(params.get("MAX_PAIR_HEDGE_COST", 1.05))
    )
