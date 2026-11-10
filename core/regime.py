"""Advanced Multi-Dimensional Market Regime Classification Engine.

Classifies BTC 5-minute prediction market conditions into distinct observable regimes:
1. 'range_bound': Mean-reverting range (SNR <= 1.0, |ret| <= 8 bps, odds balanced 44-56%).
   - Optimal market making environment. Primary profit driver.
   - Permits dual-leg maker quoting and scaled size (up to 2 lots).
2. 'normal_oscillation': Standard Brownian oscillation (SNR <= 1.5, odds balanced 42-58%).
   - Baseline market making (1 lot).
3. 'trending_up' / 'trending_down': Strong directional momentum (SNR > 1.8).
   - Counter-trend maker quoting strictly forbidden.
4. 'toxic': Extreme volatility (> 35 bps) or rapid 3-second spot velocity shock (>= 3.5 bps).
   - Instant quote cancellation & trading pause.
5. 'tail_freeze': Late window expiration (tau < 180s).
   - Leg-1 entry strictly forbidden. Zero new risk.
6. 'wide_spread': Orderbook spread > 0.06 or empty top-of-book.
   - Excessive crossing costs; wait for liquidity.
7. 'skewed_drift': Probability outside balanced odds [0.42, 0.58].
   - Blocks asymmetric entry traps.
8. 'transitional': Moderate drift (1.5 < SNR <= 1.8).
   - Defensive wait.
"""
import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List
from .types import OrderBook
from .estimator import estimate_fair_prob


@dataclass(frozen=True)
class MarketRegimeInfo:
    regime: str
    snr: float
    ret_bps: float
    p_fair: float
    vol_bps: float
    spread_ticks: float
    can_quote_up: bool
    can_quote_down: bool
    recommended_lots: int
    reason: str


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
        - SNR <= 1.0: Noise dominated (Mean-reverting / Oscillating).
        - SNR > 1.8: Statistically significant drift (p < 0.05, Momentum trend).
    """
    if spot is None or open_price is None or open_price <= 0 or spot <= 0:
        return 0.0, 0.0
    ret_bps = ((spot - open_price) / open_price) * 10000.0
    tau_norm = max(1.0, tau_s) / 300.0
    sigma_horizon = max(0.5, vol_bps * math.sqrt(tau_norm))
    snr = round(abs(ret_bps) / sigma_horizon, 4)
    return snr, round(ret_bps, 2)


def classify_market_regime(
    spot: Optional[float],
    open_price: Optional[float],
    tau_s: float,
    vol_bps: float,
    book_up: OrderBook,
    book_down: OrderBook,
    is_toxic_shock: bool = False,
    params: Optional[Dict] = None,
    p_fair: Optional[float] = None
) -> MarketRegimeInfo:
    """Classify 5-minute market regime using multi-dimensional microstructure metrics.
    
    Args:
        spot: Current BTC reference or oracle price.
        open_price: Window opening strike price.
        tau_s: Remaining seconds in the 5-minute window.
        vol_bps: Rolling EWMA realized volatility in bps / 5m.
        book_up: Polymarket Up token orderbook.
        book_down: Polymarket Down token orderbook.
        is_toxic_shock: True if rapid adverse velocity shock was detected.
        params: Strategy configuration dictionary.
        p_fair: Precomputed win probability if available.
        
    Returns:
        MarketRegimeInfo with actionable permissions and recommended lot sizing.
    """
    params = params or {}
    
    up_spread = book_up.spread if book_up else 1.0
    dn_spread = book_down.spread if book_down else 1.0
    max_spread = max(up_spread, dn_spread)
    spread_ticks = round(max_spread * 100.0, 1)

    if spot is None or open_price is None or spot <= 0 or open_price <= 0:
        return MarketRegimeInfo(
            regime="transitional", snr=0.0, ret_bps=0.0, p_fair=0.50 if p_fair is None else p_fair,
            vol_bps=vol_bps or 15.0, spread_ticks=spread_ticks,
            can_quote_up=False, can_quote_down=False, recommended_lots=0,
            reason="Reference spot or strike price unavailable"
        )

    if p_fair is None:
        p_fair = estimate_fair_prob(spot, open_price, tau_s, vol_bps)
    snr, ret_bps = calculate_drift_snr(spot, open_price, tau_s, vol_bps)

    # 1. Tail Expiration Regime (tau < min_entry_tau, default 180s)
    min_entry_tau = float(params.get("MIN_ENTRY_TAU_S", 180.0))
    if tau_s < min_entry_tau:
        return MarketRegimeInfo(
            regime="tail_freeze", snr=snr, ret_bps=ret_bps, p_fair=p_fair,
            vol_bps=vol_bps, spread_ticks=spread_ticks,
            can_quote_up=False, can_quote_down=False, recommended_lots=0,
            reason=f"Late window (tau {tau_s:.0f}s < {min_entry_tau:.0f}s)"
        )

    # 2. Toxic Shock Regime (vol > 35 bps or velocity shock)
    if is_toxic_shock or vol_bps > 35.0:
        return MarketRegimeInfo(
            regime="toxic", snr=snr, ret_bps=ret_bps, p_fair=p_fair,
            vol_bps=vol_bps, spread_ticks=spread_ticks,
            can_quote_up=False, can_quote_down=False, recommended_lots=0,
            reason=f"Toxic regime (vol={vol_bps:.1f}bps, shock={is_toxic_shock})"
        )

    # 3. Wide Spread / Illiquid Book Regime
    max_allowed_spread = float(params.get("MAX_ORDERBOOK_SPREAD", 0.06))
    if max_spread > max_allowed_spread or not book_up.bids or not book_down.bids:
        return MarketRegimeInfo(
            regime="wide_spread", snr=snr, ret_bps=ret_bps, p_fair=p_fair,
            vol_bps=vol_bps, spread_ticks=spread_ticks,
            can_quote_up=False, can_quote_down=False, recommended_lots=0,
            reason=f"Book spread too wide ({max_spread:.2f} > {max_allowed_spread:.2f})"
        )

    # 4. Strong Directional Momentum / Trend Regime (SNR > trend_thresh)
    trend_threshold = float(params.get("SNR_TREND_THRESHOLD", 1.8))
    if snr > trend_threshold:
        if ret_bps > 0:
            return MarketRegimeInfo(
                regime="trending_up", snr=snr, ret_bps=ret_bps, p_fair=p_fair,
                vol_bps=vol_bps, spread_ticks=spread_ticks,
                can_quote_up=False, can_quote_down=False, recommended_lots=0,
                reason=f"Strong bullish trend (SNR={snr:.2f} > {trend_threshold:.1f}, +{ret_bps:.1f}bps)"
            )
        else:
            return MarketRegimeInfo(
                regime="trending_down", snr=snr, ret_bps=ret_bps, p_fair=p_fair,
                vol_bps=vol_bps, spread_ticks=spread_ticks,
                can_quote_up=False, can_quote_down=False, recommended_lots=0,
                reason=f"Strong bearish trend (SNR={snr:.2f} > {trend_threshold:.1f}, {ret_bps:.1f}bps)"
            )

    # 5. Skewed Fair Prob Boundary (outside balanced entry zone)
    min_fair_p = float(params.get("MIN_FAIR_PROB_ENTRY", 0.42))
    max_fair_p = float(params.get("MAX_FAIR_PROB_ENTRY", 0.58))
    if p_fair < min_fair_p or p_fair > max_fair_p:
        return MarketRegimeInfo(
            regime="skewed_drift", snr=snr, ret_bps=ret_bps, p_fair=p_fair,
            vol_bps=vol_bps, spread_ticks=spread_ticks,
            can_quote_up=False, can_quote_down=False, recommended_lots=0,
            reason=f"p_fair ({p_fair:.2f}) outside balanced entry zone [{min_fair_p:.2f}, {max_fair_p:.2f}]"
        )

    # 6. Mean-Reverting Range-Bound Regime (High confidence MM Zone)
    # Spot is tightly bound around strike, volatility is calm/normal, odds balanced
    snr_range_conf = float(params.get("SNR_RANGE_CONFIDENT", 1.0))
    max_range_disp = float(params.get("MAX_RANGE_DISPLACEMENT_BPS", 8.0))
    if (snr <= snr_range_conf and 
        abs(ret_bps) <= max_range_disp and 
        vol_bps <= 25.0 and 
        min_fair_p <= p_fair <= max_fair_p):
        allow_boost = bool(params.get("ALLOW_INCREASED_RANGE_SIZE", True))
        rec_lots = 2 if allow_boost else 1
        return MarketRegimeInfo(
            regime="range_bound", snr=snr, ret_bps=ret_bps, p_fair=p_fair,
            vol_bps=vol_bps, spread_ticks=spread_ticks,
            can_quote_up=True, can_quote_down=True, recommended_lots=rec_lots,
            reason=f"Mean-reverting range (SNR={snr:.2f}, disp={ret_bps:+.1f}bps)"
        )

    # 7. Normal Oscillation Regime (Standard MM)
    range_threshold = float(params.get("SNR_RANGE_THRESHOLD", 1.2))
    if snr <= range_threshold:
        return MarketRegimeInfo(
            regime="normal_oscillation", snr=snr, ret_bps=ret_bps, p_fair=p_fair,
            vol_bps=vol_bps, spread_ticks=spread_ticks,
            can_quote_up=True, can_quote_down=True, recommended_lots=1,
            reason=f"Normal oscillation (SNR={snr:.2f})"
        )

    # 8. Transitional Regime (1.2 < SNR <= 1.8)
    return MarketRegimeInfo(
        regime="transitional", snr=snr, ret_bps=ret_bps, p_fair=p_fair,
        vol_bps=vol_bps, spread_ticks=spread_ticks,
        can_quote_up=False, can_quote_down=False, recommended_lots=0,
        reason=f"Transitional drift (SNR={snr:.2f})"
    )
