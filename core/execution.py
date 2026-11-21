"""Execution, Lot Quantization, and Order Lifecycle Engine.

Guarantees discrete lot conservation, throttles redundant order cancellation churn,
and prevents overshooting residual inventory traps.
"""
from typing import List, Optional, Tuple
from .types import Order, Decision, Side


def quantize_lots(raw_size: float, lot_size: float = 5.0, max_lots: int = 2) -> float:
    """Quantize desired shares into discrete exchange lots.
    
    Guarantees:
    - Never generates odd share counts (e.g., 6.0, 7.0, 2.5).
    - Hard bounds output to {0.0, 5.0, 10.0} shares when lot_size=5.0 and max_lots=2.
    - Immune to floating point imprecision (e.g. 4.999999).
    """
    if raw_size < lot_size - 1e-4:
        return 0.0
    lots = int((raw_size + 1e-4) // lot_size)
    lots = min(lots, max_lots)
    return float(lots * lot_size)


def taker_fee_per_share(price: float, fee_rate: float = 0.07) -> float:
    """Official Polymarket crypto taker fee per share: feeRate * p * (1 - p).

    Peaks at p=0.5 and vanishes at certain outcomes. Maker orders pay zero.
    """
    return fee_rate * price * (1.0 - price)


def is_order_diff_significant(
    existing: Order, 
    desired_side: Side, 
    desired_price: float, 
    desired_size: float,
    hysteresis_ticks: float = 0.019
) -> bool:
    """Check if working order needs replacement using a 2-tick hysteresis band.
    
    Protects queue priority against 1-tick micro-fluctuations (< $0.02).
    """
    if existing.side != desired_side:
        return True
    if abs(existing.price - desired_price) >= hysteresis_ticks: # 2 ticks ($0.02)
        return True
    if abs(existing.remaining - desired_size) >= 0.5:
        return True
    return False


def plan_order_actions(
    working_orders: List[Order],
    decision: Decision,
    now: float,
    max_queue_wait_s: float = 15.0,
    preferred_side: Optional[Side] = None,
    book_up: Optional[object] = None,
    book_down: Optional[object] = None,
    min_requote_age_s: float = 2.5,
    bid_retreat_ticks: float = 0.029,
    min_order_shares: float = 5.0,
) -> Tuple[List[Order], Optional[Decision]]:
    """Determine which orders to cancel and which new order to place.
    
    Applies quoting hysteresis and protects resting safe quotes from 1-second
    noise, while immediately pulling quotes if toxic shock, tail freeze,
    objective direction reversal, or >= 3-tick bid retreat occurs.
    Enforces the exchange rule that every submitted order must be >= min_order_shares (5.0).
    
    Returns:
        (orders_to_cancel, action_to_execute)
    """
    to_cancel: List[Order] = []
    if decision.action in ("quote", "taker_buy", "taker_hedge", "taker_unwind"):
        if float(decision.size or 0.0) < min_order_shares - 1e-4:
            return to_cancel, None
    
    if decision.action == "wait":
        # Structural cancel signal comes from the policy, not from the wording
        # of `reason`. Policy sets urgent_cancel=True for toxic shocks, tail
        # freeze, regime lockouts, lifecycle caps and risk-budget breaches.
        is_urgent_cancel = bool(getattr(decision, "urgent_cancel", False))
        
        for o in working_orders:
            if o.cancel_at is None and o.remaining > 1e-6:
                order_age = now - (o.placed_at or now)
                if is_urgent_cancel:
                    o.cancel_reason = "急剧变盘/紧急避险撤单"
                    to_cancel.append(o)
                    continue
                if o.purpose != "hedge" and preferred_side is not None and o.side != preferred_side:
                    o.cancel_reason = f"客观方向反转撤单(挂单={o.side}, 现势={preferred_side})"
                    to_cancel.append(o)
                    continue
                tb = book_up if o.side == "Up" else book_down
                tb_bid = getattr(tb, "best_bid", None) if tb is not None else None
                if order_age >= 1.0 and tb_bid is not None and (o.price - tb_bid) >= bid_retreat_ticks:
                    o.cancel_reason = f"盘口买一退守撤单(挂单=${o.price:.2f}, 现买一=${tb_bid:.2f})"
                    to_cancel.append(o)
                    continue
                if order_age >= max_queue_wait_s:
                    o.cancel_reason = f"排队超时撤单(已排队{order_age:.1f}s)"
                    to_cancel.append(o)
        return to_cancel, None

    if decision.action in ("taker_buy", "taker_hedge", "taker_unwind"):
        # Immediate aggressive order: cancel any working makers first
        for o in working_orders:
            if o.cancel_at is None and o.remaining > 1e-6:
                o.cancel_reason = "转为Taker吃单撤回未成挂单"
                to_cancel.append(o)
        return to_cancel, decision

    if decision.action == "quote":
        # Maker quoting: check if an identical or near-identical order is already active
        target_side = decision.side
        target_px = decision.price or 0.0
        target_sz = decision.size or 0.0
        
        reusable_order = None
        for o in working_orders:
            if o.cancel_at is None and o.remaining > 1e-6:
                order_age = now - (o.placed_at or now)
                if not is_order_diff_significant(o, target_side, target_px, target_sz):
                    reusable_order = o
                elif o.side == target_side and abs(o.price - target_px) < 0.035 and order_age < min_requote_age_s:
                    # Protect resting quote from <3-tick ping-pong within cooldown window
                    reusable_order = o
                else:
                    o.cancel_reason = f"盘口调价重挂 (${o.price:.2f} → ${target_px:.2f})"
                    to_cancel.append(o)
                    
        if reusable_order is not None:
            # Order is already optimal in book; protect queue position
            return to_cancel, None
            
        return to_cancel, decision

    return to_cancel, None
