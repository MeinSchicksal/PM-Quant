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
    """
    if raw_size < lot_size - 1e-4:
        return 0.0
    lots = int(raw_size // lot_size)
    lots = min(lots, max_lots)
    return float(lots * lot_size)


def is_order_diff_significant(
    existing: Order, 
    desired_side: Side, 
    desired_price: float, 
    desired_size: float
) -> bool:
    """Check if working order needs replacement.
    
    Avoids cancel/replace churn if price and size are practically identical.
    """
    if existing.side != desired_side:
        return True
    if abs(existing.price - desired_price) >= 0.009: # 1 tick difference
        return True
    if abs(existing.remaining - desired_size) >= 0.5:
        return True
    return False


def plan_order_actions(
    working_orders: List[Order],
    decision: Decision,
    now: float
) -> Tuple[List[Order], Optional[Decision]]:
    """Determine which orders to cancel and which new order to place.
    
    Returns:
        (orders_to_cancel, action_to_execute)
    """
    to_cancel: List[Order] = []
    
    if decision.action == "wait":
        # Cancel any active unfulfilled entries when policy commands wait
        for o in working_orders:
            if o.cancel_at is None and o.remaining > 1e-6:
                to_cancel.append(o)
        return to_cancel, None

    if decision.action in ("taker_hedge", "taker_unwind"):
        # Immediate aggressive order: cancel any working makers first
        for o in working_orders:
            if o.cancel_at is None and o.remaining > 1e-6:
                to_cancel.append(o)
        return to_cancel, decision

    if decision.action == "quote":
        # Maker quoting: check if an identical order is already active
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
            # Order is already optimal in book; no new order needed
            return to_cancel, None
            
        return to_cancel, decision

    return to_cancel, None
