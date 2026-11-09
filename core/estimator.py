"""Mathematical Estimation & Microstructure Metrics.

Handles continuous probability estimation, uncertainty envelope calculation,
queue fill probability, and order flow toxicity detection.
"""
import math
from typing import Tuple, List
from .types import SpotState, OrderBook


def normal_cdf(x: float) -> float:
    """Standard Normal Cumulative Distribution Function using error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def estimate_fair_prob(spot: float, open_price: float, tau_s: float, vol_bps: float) -> float:
    """Estimate risk-neutral settlement probability p_fair for Up winning (S_T > S_0).
    
    Args:
        spot: Current BTC/USD spot or oracle reference price.
        open_price: Price of BTC/USD at window opening (strike price).
        tau_s: Seconds remaining until settlement (tau = T - t).
        vol_bps: Realized volatility in basis points per 5-minute window.
    """
    if tau_s <= 0.5:
        # Near instant settlement: binary step with small slack
        return 0.99 if spot > open_price else 0.01
    
    if open_price <= 0 or spot <= 0:
        return 0.50

    # Annualized volatility converted to tau scaling (5-min base: 300s)
    # sigma_tau = (vol_bps / 10000.0) * sqrt(tau / 300)
    sigma_tau = max(1e-5, (vol_bps / 10000.0) * math.sqrt(tau_s / 300.0))
    d = math.log(spot / open_price) / sigma_tau
    prob = normal_cdf(d)
    
    # Mathematical realistic boundary: prediction markets are never 0% or 100% until closed
    return max(0.01, min(0.99, round(prob, 4)))


def estimate_uncertainty_envelope(
    vol_bps: float, 
    latency_s: float = 0.30, 
    k_mult: float = 1.2,
    basis_error: float = 0.008
) -> float:
    """Calculate the uncertainty band epsilon_t representing estimation error and latency risk.
    
    Epsilon incorporates:
    1. Information latency (300ms wire delay during which spot moves).
    2. Oracle basis error between Binance Spot and Chainlink TWAP.
    
    The trading system only quotes when market price deviates from p_fair by > epsilon + edge.
    """
    # 300ms spot dispersion
    dispersion_lat = (vol_bps / 10000.0) * math.sqrt(latency_s / 300.0)
    # Probability sensitivity near 0.50 (normal pdf max is ~0.4)
    prob_uncertainty = 0.40 * dispersion_lat * 100.0 # converted to probability units
    
    epsilon = k_mult * (prob_uncertainty + basis_error)
    # Reasonable mathematical bounds: between 1.5 cents and 8 cents
    return max(0.015, min(0.08, round(epsilon, 4)))


def estimate_queue_wait_time(queue_ahead: float, trade_rate_per_sec: float) -> float:
    """Estimate passive fill waiting time in seconds based on recent order flow velocity."""
    rate = max(0.5, trade_rate_per_sec) # minimum 0.5 shares/sec baseline
    return queue_ahead / rate


def detect_toxicity_shock(
    spot_history_3s: List[Tuple[float, float]], 
    shock_threshold_bps: float = 3.5
) -> bool:
    """Check if BTC experienced an adverse jump of >= shock_threshold_bps in past 3 seconds.
    
    Informed toxic flow manifests as rapid multi-tick directional spot moves.
    """
    if len(spot_history_3s) < 2:
        return False
    t_now, px_now = spot_history_3s[-1]
    for t, px in spot_history_3s[:-1]:
        if t_now - t <= 3.5 and px > 0:
            diff_bps = abs(px_now - px) / px * 10000.0
            if diff_bps >= shock_threshold_bps:
                return True
    return False


def calculate_dynamic_realized_vol(
    spot_history: List[Tuple[float, float]], 
    baseline_vol_bps: float = 15.0,
    half_life_s: float = 15.0
) -> float:
    """Calculate real-time EWMA realized volatility of per-second log returns over rolling window.
    
    Args:
        spot_history: Time-ordered list of (timestamp, spot_price) pairs (e.g. past 60s).
        baseline_vol_bps: Fallback baseline volatility in bps / 5m if history insufficient.
        half_life_s: Decay half-life in seconds for exponential weighting.
        
    Returns:
        vol_bps: Realized volatility in basis points per 5-minute window (300s).
                 Clamped to [2.0 bps, 80.0 bps].
    """
    valid = [(t, px) for t, px in spot_history if px > 0]
    if len(valid) < 5:
        return baseline_vol_bps
        
    t_end = valid[-1][0]
    t_start = valid[0][0]
    if (t_end - t_start) < 5.0:
        return baseline_vol_bps

    # Exponential decay constant: lambda_decay = ln(2) / half_life
    decay = math.log(2.0) / max(1.0, half_life_s)
    
    weighted_var_sum = 0.0
    weight_sum = 0.0
    
    for i in range(1, len(valid)):
        t_prev, px_prev = valid[i - 1]
        t_curr, px_curr = valid[i]
        dt = t_curr - t_prev
        if dt <= 0.0 or px_prev <= 0 or px_curr <= 0:
            continue
            
        # Log return
        r = math.log(px_curr / px_prev)
        # Normalized 1-second variance
        var_1s = (r * r) / dt
        
        # Age relative to t_end
        age = max(0.0, t_end - t_curr)
        w = math.exp(-decay * age)
        
        weighted_var_sum += w * var_1s
        weight_sum += w
        
    if weight_sum < 1e-6:
        return baseline_vol_bps
        
    ewma_var_1s = weighted_var_sum / weight_sum
    ewma_vol_1s = math.sqrt(max(1e-12, ewma_var_1s))
    
    # Scale 1-second vol to 5-minute window (300 seconds) in basis points (10,000)
    vol_5m = ewma_vol_1s * math.sqrt(300.0)
    vol_bps = vol_5m * 10000.0
    
    # Bounded between 2.0 bps and 80.0 bps
    return max(2.0, min(80.0, round(vol_bps, 2)))


def calculate_adaptive_spread_params(
    vol_bps: float,
    base_edge: float = 0.025,
    k_mult: float = 1.2
) -> Tuple[float, float, str]:
    """Calculate adaptive base edge and uncertainty multiplier based on realized volatility regime.
    
    Regimes:
    - Calm (vol_bps < 5.0): Narrow spread to seize top-of-book maker priority.
    - Normal (5.0 <= vol_bps <= 30.0): Standard baseline edge and uncertainty band.
    - Volatile (vol_bps > 30.0): Widen safety pad to prevent toxic taker adverse selection.
    
    Returns:
        (adaptive_edge, adaptive_k, regime)
    """
    if vol_bps < 5.0:
        # Scale down edge proportionally (down to 60% of base_edge at <= 3 bps)
        scale = max(0.60, vol_bps / 5.0)
        edge = max(0.015, round(base_edge * scale, 4))
        k = round(k_mult * 0.85, 2)
        return edge, k, "calm"
    elif vol_bps > 30.0:
        # Scale up edge to widen safety cushion against toxic jumps
        scale = min(2.0, 1.0 + (vol_bps - 30.0) / 25.0)
        edge = min(0.05, round(base_edge * scale, 4))
        k = round(k_mult * scale, 2)
        return edge, k, "volatile"
    else:
        return base_edge, k_mult, "normal"


def calculate_kelly_order_size(
    available_capital: float,
    p_win: float,
    quote_price: float,
    lot_size: float = 5.0,
    min_lots: int = 1,
    max_lots: int = 3,
    kelly_fraction: float = 0.25
) -> float:
    """Calculate fractional Kelly order size in discrete exchange lots.
    
    Args:
        available_capital: Current available balance (USD).
        p_win: Estimated model win probability for this side.
        quote_price: Planned execution or maker quote price.
        lot_size: Minimum discrete lot increment (default 5.0 shares).
        min_lots: Minimum entry lots when positive edge exists (default 1 lot = 5 shares).
        max_lots: Maximum entry lots when edge is large (default 3 lots = 15 shares).
        kelly_fraction: Fractional Kelly scaling factor (0.25 = Quarter-Kelly).
        
    Returns:
        Order size in shares: discrete multiple in {5.0, 10.0, 15.0}.
    """
    min_cost = quote_price * lot_size
    if available_capital < min_cost or quote_price <= 0.01 or quote_price >= 0.99:
        return 0.0 if available_capital < min_cost else float(min_lots * lot_size)
        
    edge = p_win - quote_price
    if edge <= 1e-4:
        return float(min_lots * lot_size)
        
    # Binary payoff odds: b = (1 - quote_price) / quote_price
    # Full Kelly: f* = (p_win * (b + 1) - 1) / b = (p_win - quote_price) / (1 - quote_price)
    full_kelly = edge / (1.0 - quote_price)
    quarter_kelly = max(0.0, kelly_fraction * full_kelly)
    
    # Capital risked
    risk_usd = available_capital * quarter_kelly
    raw_shares = risk_usd / quote_price
    raw_lots = raw_shares / lot_size
    
    if raw_lots < 1.5:
        target_lots = min_lots
    elif raw_lots < 2.5:
        target_lots = min(2, max_lots)
    else:
        target_lots = max_lots
        
    return float(target_lots * lot_size)
