"""PM-Quant Configuration & Single Source of Truth.

All model parameters, version identity, and filesystem layout are derived here.
No version logic or branching should exist in downstream modules.
"""
from pathlib import Path

# Semantic Version Identity (v<Major>.<Minor>)
VERSION = "v2.1"
BATCH_ID = VERSION

# Root Directory
ROOT = Path(__file__).resolve().parent

# Core Mathematical & Economic Parameters
PARAMS = {
    # 0. Active Strategy Engine
    "STRATEGY_MODE": "LWAA",          # Active Strategy: "LWAA" v2.1 Dual-Stage (MWDE + LWAA)

    # 1. Discrete Physical & Lot Constraints
    "LOT_SIZE": 5.0,                  # Exchange minimum order unit (lots must be integer multiples)
    "MIN_LOTS": 1,                    # Minimum entry lots (1 lot = 5 shares, baseline sizing)
    "MAX_LOTS": 2,                    # Maximum entry lots (2 lots = 10 shares, hard risk cap per window)
    "INITIAL_CAPITAL_USD": 100.0,     # Paper account starting bankroll (single source for runner/dashboard/reset)
    "LATENCY_S": 0.30,                # Estimated round-trip wire + engine order insertion delay
    "TAKER_FEE_RATE": 0.07,           # Official Polymarket crypto taker fee coefficient: fee = shares * rate * p * (1-p); makers pay zero

    # 2. Dual-Stage Core Strategy: MWDE (Mid-Window Discount Entry) + LWAA (Late-Window Asymmetric Absorption)
    # Stage 1: 前半程顺势低吸 (Mid-Window Trend-Biased Discount Entry, 60s ~ 150s)
    "MWDE_ENABLE": True,               # Enable Stage 1: Mid-window trend-biased dip-buying
    "MWDE_MIN_TAU_S": 150.0,           # Stage 1 horizon cutoff (tau in [150s, 240s], elapsed 60s ~ 150s)
    "MWDE_MAX_TAU_S": 240.0,
    "MWDE_MAX_ENTRY_PRICE": 0.45,      # Maximum buying price for dip entry (用户要求: 放宽到 $0.45)
    "MWDE_PREV_ROUND_MIN_MOVE": 20.0,  # Filter: previous round move >= $20 (前一盘刚刚跌/涨过比较多的价格)
    "MWDE_FIRST_60S_MAX_RANGE": 15.0,  # Filter: first 60s range <= $15 (60秒只涨/跌几美金，涨跌幅度没有之前那么大)
    "MWDE_MID_PROB_LOW": 0.38,         # Filter: probability in middle consolidation corridor [0.38, 0.62]
    "MWDE_MID_PROB_HIGH": 0.62,
    "MWDE_HEDGE_LOCK_COST": 0.85,      # Leg 2 lock: only buy opposite side if combined cost <= $0.85 (不着急买入另一侧止损)

    # Stage 2: 尾盘高确信吸收 (Late-Window Asymmetric Absorption, 120s ~ 15s)
    "LWAA_MAX_TAU_S": 120.0,           # Scan in late window (tau <= 120s, 2 minutes)
    "LWAA_MIN_TAU_S": 15.0,            # Tail freeze cutoff (tau < 15s)
    "LWAA_MIN_PROB": 0.80,             # High-conviction threshold: 80% (robust against 50/50 chop)
    "LWAA_MIN_EDGE": 0.02,             # Minimum net profit edge demanded over ask + taker fee (2.0 ticks)
    "LWAA_MAX_ENTRY_PRICE": 0.65,      # Maximum acceptable entry price (用户要求: 买入最高限价设为 $0.65)
    "LWAA_HARD_CHASE_CEILING": 0.70,   # Hard chase ceiling (超过 $0.70 绝不追高)
    "LWAA_MIN_ENTRY_PRICE": 0.35,      # Minimum entry price for high-prob outcome
    "LWAA_MAX_QUOTE_ATTEMPTS": 1,      # Maximum maker quote attempts per window (挂不上不再重挂，等待到期)
    "LWAA_ENABLE_ARBITRAGE_HEDGE": True, # Always lock both sides when opp ask drops (pair_cost <= 1.00) for risk-free profit
    "ALLOW_TAKER_UNWIND": False,       # Strictly forbid panic unwinds / selling (死扛到期，不要卖出)

    # 3. Lifecycle & Account-Level Watchdog Boundaries
    "MAX_ENTRIES_PER_WINDOW": 1,      # Maximum Leg-1 entry attempts allowed per 5m window
    "MAX_WINDOW_LOSS_USD": 5.00,      # Hard per-window loss envelope. Funds 1 minimum lot at MAX_ENTRY_PRICE (5 * 0.90 = $4.50)
    "MAX_DAILY_DRAWDOWN_USD": 15.00,  # Account-level daily drawdown circuit breaker ($15.00 cumulative loss)
    "LOSS_STREAK_COOLING": 3,         # Consecutive losing rounds triggering cooling mode
    "COOLING_ROUNDS": 3,              # Number of rounds to cool off when loss streak triggers
    "DEADMAN_TIMEOUT_S": 3.0,         # Dead-man switch: cancel resting orders if market feed drops for > 3.0s
    "POLY_PRICE_MAX_AGE_S": 10.0,     # Ignore cached Polymarket TWAP older than this (fall back to Binance spot basis)

    # 4. Legacy Avellaneda-Stoikov & Dual-Regime Compatibility Parameters
    "TAIL_FREEZE_S": 60.0,            # Seconds before expiration where all actions are frozen
    "MIN_ENTRY_TAU_S": 180.0,         # Minimum remaining seconds (tau >= 180s) to permit initial Leg-1 entry in legacy mode
    "BASE_EDGE": 0.025,               # Minimum price edge demanded over uncertainty band (2.5 ticks)
    "UNCERTAINTY_K": 1.2,             # Scaling multiplier for real-time uncertainty bandwidth
    "BASELINE_VOL_BPS": 15.0,         # Fallback baseline realized volatility in bps / 5m
    "KELLY_FRACTION": 0.25,           # Quarter-Kelly risk fraction scaling factor
    "RISK_AVERSION_GAMMA": 0.15,      # Avellaneda-Stoikov risk aversion factor for inventory penalty
    "MAX_QUEUE_WAIT_S": 15.0,         # Maximum acceptable passive queue waiting time based on trade rate
    "TOXICITY_SHOCK_BPS": 3.5,        # 3-second rapid price shock threshold indicating informed toxicity
    "SNR_TREND_THRESHOLD": 1.8,       # Signal-to-Noise Ratio threshold to classify momentum trend (p < 0.05)
    "SNR_RANGE_THRESHOLD": 1.2,       # SNR threshold to classify normal oscillation regime
    "SNR_RANGE_CONFIDENT": 1.0,       # High-confidence mean-reverting range threshold
    "MAX_RANGE_DISPLACEMENT_BPS": 8.0,# Max displacement from strike to qualify for range-bound regime
    "MAX_ORDERBOOK_SPREAD": 0.06,     # Max book spread (0.06 = 6 cents) before declaring wide-spread regime
    "ALLOW_INCREASED_RANGE_SIZE": False,
    "MAX_MAKER_ENTRY_PRICE": 0.90,    # Maximum price for passive entry
    "MIN_MAKER_ENTRY_PRICE": 0.38,    # Minimum price allowed for passive entry
    "MIN_FAIR_PROB_ENTRY": 0.42,      # Minimum fair probability for balanced Leg-1 entry
    "MAX_FAIR_PROB_ENTRY": 0.58,      # Maximum fair probability for balanced Leg-1 entry
    "MAX_PAIR_HEDGE_COST": 1.05,      # Maximum acceptable pair cost for Taker Hedge
    "P_FILL_MAKER": 0.60,             # Assumed probability a resting Leg-2 maker quote fills before expiry
}

def validate_params(params=None):
    """Fail fast when risk parameters contradict each other."""
    p = PARAMS if params is None else params
    errors = []
    max_px = p.get("LWAA_MAX_ENTRY_PRICE", p.get("MAX_MAKER_ENTRY_PRICE", 0.90))
    min_lot_cost = p["LOT_SIZE"] * max_px * p["MIN_LOTS"]
    if p["MAX_WINDOW_LOSS_USD"] + 1e-9 < min_lot_cost:
        errors.append(
            f"MAX_WINDOW_LOSS_USD={p['MAX_WINDOW_LOSS_USD']:.2f} cannot fund one minimum lot at "
            f"max_entry_price={max_px:.2f} "
            f"({p['LOT_SIZE']:.1f} x {max_px:.2f} = {min_lot_cost:.2f})"
        )
    if p["MAX_WINDOW_LOSS_USD"] > p["MAX_DAILY_DRAWDOWN_USD"]:
        errors.append(
            f"MAX_WINDOW_LOSS_USD={p['MAX_WINDOW_LOSS_USD']:.2f} exceeds "
            f"MAX_DAILY_DRAWDOWN_USD={p['MAX_DAILY_DRAWDOWN_USD']:.2f}"
        )
    if p.get("MIN_MAKER_ENTRY_PRICE", 0.38) > p.get("MAX_MAKER_ENTRY_PRICE", 0.90):
        errors.append("MIN_MAKER_ENTRY_PRICE exceeds MAX_MAKER_ENTRY_PRICE")
    if p["MIN_LOTS"] > p["MAX_LOTS"]:
        errors.append("MIN_LOTS exceeds MAX_LOTS")
    if errors:
        raise ValueError("Inconsistent risk parameters: " + "; ".join(errors))
    return True


# Derived Filesystem Paths
DATA_DIR = ROOT / "data"
MARKET_DIR = DATA_DIR / "1_market"
LEDGERS_DIR = DATA_DIR / "2_ledgers"
CURRENT_LEDGER_DIR = LEDGERS_DIR / VERSION
REPORTS_DIR = DATA_DIR / "3_reports"
RUNTIME_DIR = DATA_DIR / "4_runtime"
LOGS_DIR = RUNTIME_DIR / "logs"

# Ensure runtime directories exist
for _p in (CURRENT_LEDGER_DIR, RUNTIME_DIR, LOGS_DIR, REPORTS_DIR):
    _p.mkdir(parents=True, exist_ok=True)
