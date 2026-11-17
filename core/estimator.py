"""Objective Microstructure Metrics & Physical Lead Arithmetic.

Computes realized volatility (EWMA), drift signal-to-noise ratio (SNR),
and pure arithmetic spot/60s-TWAP displacement from window strike.
No parametric probability estimation (no Black-Scholes / normal CDF) is used.
"""
import math
from typing import Tuple, List, Optional


def compute_objective_lead(spot, open_px: float, tau_s: float) -> Tuple[float, float, float]:
    """Compute pure arithmetic lead (bps, USD, effective price) relative to strike.

    Uses basis-adjusted Chainlink spot estimate when official strike and basis
    are calibrated, and incorporates the already-locked 60s TWAP average during
    the final 60 seconds. Falls back strictly to the single Binance chain when
    basis calibration is unavailable.

    Returns:
        (lead_bps, delta_usd, effective_price)
    """
    spot_px = float(getattr(spot, "price", 0.0) or 0.0)
    basis_ema = getattr(spot, "basis_ema", None)
    realized_60 = getattr(spot, "twap_realized_60s", None)

    if basis_ema is not None and spot_px > 0 and open_px > 0:
        cl_est = max(1e-6, spot_px - float(basis_ema))
        if tau_s <= 60.0 and realized_60 and float(realized_60) > 0:
            u = max(0.0, 60.0 - max(0.0, tau_s))
            rem = max(0.0, min(60.0, tau_s))
            eff_px = (u * float(realized_60) + rem * cl_est) / 60.0
        else:
            eff_px = cl_est
        delta_usd = eff_px - open_px
        lead_bps = (delta_usd / open_px) * 10000.0
        return round(lead_bps, 4), round(delta_usd, 2), round(eff_px, 2)

    bin_open = getattr(spot, "binance_open_price", None)
    ref_open = float(bin_open) if (bin_open and float(bin_open) > 0) else float(open_px or 0.0)
    if ref_open > 0 and spot_px > 0:
        delta_usd = spot_px - ref_open
        lead_bps = (delta_usd / ref_open) * 10000.0
        return round(lead_bps, 4), round(delta_usd, 2), round(spot_px, 2)

    return 0.0, 0.0, spot_px


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


def calculate_dual_horizon_vol(
    spot_history: List[Tuple[float, float]],
    baseline_vol_bps: float = 14.0,
    fast_half_life_s: float = 20.0,
    slow_half_life_s: float = 300.0,
) -> float:
    """Combine fast (20s) and slow (5m over 30m buffer) EWMA realized volatility.

    Takes the conservative maximum so sudden jumps are priced immediately while
    brief 30-60s pauses inside a volatile session do not collapse model vol.
    """
    fast = calculate_dynamic_realized_vol(spot_history, baseline_vol_bps=baseline_vol_bps, half_life_s=fast_half_life_s)
    slow = calculate_dynamic_realized_vol(spot_history, baseline_vol_bps=baseline_vol_bps, half_life_s=slow_half_life_s)
    return round(max(fast, slow), 2)


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


def calculate_drift_snr(
    spot: Optional[float],
    open_price: Optional[float],
    tau_s: float,
    vol_bps: float
) -> Tuple[float, float]:
    """Calculate the statistical Signal-to-Noise Ratio (SNR) of directional drift.
    
    Formula:
        SNR = |ret_bps| / (vol_bps * sqrt(tau_s / 300.0))
        
    Statistical meaning:
        - SNR <= 1.2: Brownian motion noise (Mean-reverting / Oscillating).
        - SNR > 1.8: Statistically significant drift (p < 0.05, Momentum trend).
    """
    if spot is None or open_price is None or open_price <= 0 or spot <= 0:
        return 0.0, 0.0
    ret_bps = ((spot - open_price) / open_price) * 10000.0
    tau_norm = max(1.0, tau_s) / 300.0
    sigma_horizon = max(0.5, vol_bps * math.sqrt(tau_norm))
    snr = round(abs(ret_bps) / sigma_horizon, 4)
    return snr, round(ret_bps, 2)
