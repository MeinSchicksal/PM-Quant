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
