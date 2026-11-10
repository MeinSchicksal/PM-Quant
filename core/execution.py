"""Execution, Lot Quantization, and Order Lifecycle Engine.

Guarantees discrete lot conservation, throttles redundant order cancellation churn,
and prevents overshooting residual inventory traps.
"""
from typing import Dict, List, Optional, Tuple
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
    max_queue_wait_s: float = 15.0
) -> Tuple[List[Order], Optional[Decision]]:
    """Determine which orders to cancel and which new order to place.
    
    Applies quoting hysteresis and avoids canceling resting safe quotes on
    transient 1-second 'no edge' blips unless toxic shock or tail freeze occurs.
    
    Returns:
        (orders_to_cancel, action_to_execute)
    """
    to_cancel: List[Order] = []
    
    if decision.action == "wait":
        reason_lower = (decision.reason or "").lower()
        # Urgent cancel conditions: toxic spot shock, tail freeze, trend regime, or watchdog locks
        is_urgent_cancel = (
            "toxic" in reason_lower
            or "freeze" in reason_lower
            or "max position" in reason_lower
            or "drawdown" in reason_lower
            or "cooling" in reason_lower
            or "circuit breaker" in reason_lower
            or "trend" in reason_lower
            or "safe entry" in reason_lower
            or "late window" in reason_lower
            or "max entries" in reason_lower
            or "balanced entry" in reason_lower
            or "wide spread" in reason_lower
            or "skewed" in reason_lower
            or "transitional" in reason_lower
        )
        
        for o in working_orders:
            if o.cancel_at is None and o.remaining > 1e-6:
                # Cancel immediately if urgent, or if order has rested past max queue wait time
                order_age = now - (o.placed_at or now)
                if is_urgent_cancel or order_age >= max_queue_wait_s:
                    to_cancel.append(o)
        return to_cancel, None

    if decision.action in ("taker_hedge", "taker_unwind"):
        # Immediate aggressive order: cancel any working makers first
        for o in working_orders:
            if o.cancel_at is None and o.remaining > 1e-6:
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
                if not is_order_diff_significant(o, target_side, target_px, target_sz):
                    reusable_order = o
                else:
                    to_cancel.append(o)
                    
        if reusable_order is not None:
            # Order is already optimal in book; protect queue position
            return to_cancel, None
            
        return to_cancel, decision

    return to_cancel, None
