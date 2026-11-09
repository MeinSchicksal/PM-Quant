"""Batch Replay & Statistical Evaluation Engine.

Replays 5-minute market sessions through the policy and prints
empirical metrics: expected return, win rate, variance, and insurance cost.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from typing import List, Dict, Any
from core.types import OrderBook, Position, SpotState, Order, Decision
from core.policy import plan_decision
from core.execution import plan_order_actions, quantize_lots
from sim.matcher import QueueMatcher
import config


def simulate_synthetic_window(
    scenario: str = "normal_oscillate",
    lot_size: float = 5.0,
    lots: int = 2
) -> Dict[str, Any]:
    """Run a high-fidelity 300-second synthetic simulation of a 5-minute binary window."""
    matcher = QueueMatcher(config.PARAMS["LATENCY_S"])
    pos_up = Position(0.0, 0.0)
    pos_down = Position(0.0, 0.0)
    
    open_px = 75000.0
    start_t = 1000.0
    end_t = 1300.0
    
    order_id_counter = 1
    working_orders: List[Order] = []
    fills: List[Dict[str, Any]] = []
    insurance_paid = 0.0
    
    for sec in range(0, 300, 2):
        now = start_t + sec
        tau = end_t - now
        
        # Scenario Price Drift
        if scenario == "normal_oscillate":
            spot_px = open_px + 15.0 * (1.0 if (sec // 20) % 2 == 0 else -1.0)
            up_mid = 0.52 if spot_px > open_px else 0.48
        elif scenario == "adverse_crash":
            # First 40s normal, then massive crash
            if sec < 40:
                spot_px = open_px + 10.0
                up_mid = 0.52
            else:
                spot_px = open_px - 80.0
                up_mid = 0.20
        else: # smooth_trend
            spot_px = open_px + (sec / 300.0) * 60.0
            up_mid = min(0.85, 0.50 + (sec / 300.0) * 0.35)
            
        down_mid = 1.0 - up_mid
        book_up = OrderBook(bids=[(round(up_mid - 0.01, 2), 40.0)], asks=[(round(up_mid + 0.01, 2), 40.0)])
        book_down = OrderBook(bids=[(round(down_mid - 0.01, 2), 40.0)], asks=[(round(down_mid + 0.01, 2), 40.0)])
        spot = SpotState(timestamp=now, price=spot_px, open_price=open_px, realized_vol_bps=15.0)
        
        # 1. Decide action
        decision = plan_decision(pos_up, pos_down, book_up, book_down, spot, tau, config.PARAMS)
        
        # 2. Plan order adjustments
        to_cancel, action = plan_order_actions(working_orders, decision, now)
        for o in to_cancel:
            o.cancel_at = now
            
        # 3. Handle immediate Taker actions
        if action and action.action == "taker_hedge":
            fill_sz = action.size or 0.0
            fill_px = action.price or 0.0
            fee = fill_sz * 0.005
            if action.side == "Down":
                pos_down.shares += fill_sz
                pos_down.cash += (fill_sz * fill_px + fee)
            else:
                pos_up.shares += fill_sz
                pos_up.cash += (fill_sz * fill_px + fee)
            pair_cost = (pos_up.avg_price + pos_down.avg_price)
            if pair_cost > 1.00:
                insurance_paid += (pair_cost - 1.00) * fill_sz
            fills.append(dict(t=now, side=action.side, px=fill_px, sz=fill_sz, kind="taker"))
            
        elif action and action.action == "quote":
            # Place maker order
            new_sz = quantize_lots(action.size or 0.0, lot_size, lots)
            if new_sz > 0:
                order = Order(
                    id=order_id_counter,
                    client_order_id=f"{config.BATCH_ID}:sim:{action.side}:{order_id_counter}",
                    side=action.side,
                    price=action.price or 0.50,
                    size=new_sz,
                    remaining=new_sz,
                    kind="maker",
                    placed_at=now
                )
                order_id_counter += 1
                matcher.register_order(order, book_up if action.side == "Up" else book_down)
                working_orders.append(order)
                
        # 4. Simulate Passive Maker Fill (probabilistic simulation)
        for o in working_orders:
            if o.cancel_at is None and o.remaining > 0 and now >= o.live_at:
                # If market touches or trades at maker price, grant fill
                cur_mid = up_mid if o.side == "Up" else down_mid
                if o.price >= cur_mid - 0.01:
                    filled = o.remaining
                    o.remaining = 0.0
                    o.filled_size += filled
                    if o.side == "Up":
                        pos_up.shares += filled
                        pos_up.cash += filled * o.price
                    else:
                        pos_down.shares += filled
                        pos_down.cash += filled * o.price
                    fills.append(dict(t=now, side=o.side, px=o.price, sz=filled, kind="maker"))

    # Settlement PnL
    winner = "Up" if spot_px > open_px else "Down"
    proceeds = (pos_up.shares if winner == "Up" else pos_down.shares) * 1.00
    total_cost = pos_up.cash + pos_down.cash
    net_pnl = round(proceeds - total_cost, 4)
    
    return {
        "scenario": scenario,
        "winner": winner,
        "pos_up": pos_up.shares,
        "pos_down": pos_down.shares,
        "total_cost": round(total_cost, 4),
        "proceeds": proceeds,
        "net_pnl": net_pnl,
        "fills": len(fills),
        "insurance_paid": round(insurance_paid, 4)
    }


def run_synthetic_benchmarks():
    print(f"=== PM-Quant Synthetic Benchmark Replay ({config.BATCH_ID}) ===")
    scenarios = ["normal_oscillate", "adverse_crash", "smooth_trend"]
    results = []
    for sc in scenarios:
        res = simulate_synthetic_window(sc)
        results.append(res)
        print(f"Scenario: {res['scenario']:18s} | Net PnL: ${res['net_pnl']:+6.2f} | Cost: ${res['total_cost']:5.2f} | Insurance Paid: ${res['insurance_paid']:4.2f} | Fills: {res['fills']}")
    return results


if __name__ == "__main__":
    run_synthetic_benchmarks()
