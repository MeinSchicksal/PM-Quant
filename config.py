"""PM-Quant Configuration & Single Source of Truth.

All model parameters, version identity, and filesystem layout are derived here.
No version logic or branching should exist in downstream modules.
"""
from pathlib import Path

# Semantic Version Identity (v<Major>.<Minor>)
VERSION = "v2.2"
BATCH_ID = VERSION

# Root Directory
ROOT = Path(__file__).resolve().parent

# Core Mathematical & Economic Parameters
PARAMS = {
    # 1. Discrete Physical & Lot Constraints
    "LOT_SIZE": 5.0,                  # Exchange minimum order unit (lots must be integer multiples)
    "MIN_LOTS": 1,                    # Minimum entry lots (1 lot = 5 shares, baseline sizing)
    "MAX_LOTS": 2,                    # Maximum entry lots (2 lots = 10 shares, hard risk cap per window)
    "INITIAL_CAPITAL_USD": 100.0,     # Paper account starting bankroll (single source for runner/dashboard/reset)
    "LATENCY_S": 0.30,                # Estimated round-trip wire + engine order insertion delay
    "TAKER_FEE_RATE": 0.07,           # Official Polymarket crypto taker fee coefficient: fee = shares * rate * p * (1-p); makers pay zero

    # 2. Continuous Dynamic Dual Absorption (消除时间断层，动态市价调价)
    # Stage 1: 前半程顺势低吸 (Mid-Window Trend-Biased Discount Entry, 60s ~ 180s)
    "MWDE_ENABLE": True,               # Enable Stage 1: Mid-window trend-biased dip-buying
    "MWDE_MIN_TAU_S": 120.0,           # Seamless handoff at tau=120s (zero gap!)
    "MWDE_MAX_TAU_S": 240.0,           # Active scanning starts after first 60s observation
    "MWDE_MAX_ENTRY_PRICE": 0.45,      # Maximum buying price for dip entry (放宽到 $0.45)
    "MWDE_HEDGE_LOCK_COST": 0.85,      # Leg 2 lock: only buy opposite side if combined cost <= $0.85
    "POST_SHOCK_MIN_MOVE": 40.0,       # 大涨/大跌门槛 ($40.0): 超过此幅度后警惕单边延续，优先买入高价侧

    # Stage 2: 尾盘高确信吸收 (Late-Window Asymmetric Absorption, 180s ~ 285s)
    "LWAA_MAX_TAU_S": 120.0,           # Seamless handoff from Stage 1 (tau <= 120s)
    "LWAA_MIN_TAU_S": 15.0,            # Tail freeze cutoff (tau < 15s)
    "LWAA_MIN_PROB": 0.80,             # High-conviction threshold: 80%
    "LWAA_MIN_EDGE": 0.02,             # Minimum net profit edge demanded (2.0 ticks)
    "LWAA_MAX_ENTRY_PRICE": 0.65,      # Maximum acceptable entry price (最高限价 $0.65)
    "LWAA_HARD_CHASE_CEILING": 0.70,   # Hard chase ceiling (超过 $0.70 绝不追高)
    "LWAA_MIN_ENTRY_PRICE": 0.35,      # Minimum entry price for high-prob outcome
    "LWAA_MAX_QUOTE_ATTEMPTS": 1,      # Maximum maker quote attempts per window
    "LWAA_ENABLE_ARBITRAGE_HEDGE": True, # Always lock both sides when opp ask drops (pair_cost <= 1.00)
    "ALLOW_TAKER_UNWIND": False,       # Strictly forbid panic unwinds / selling (死扛到期，不要卖出)
    "MAX_QUEUE_WAIT_S": 15.0,          # Passive queue waiting timeout (15s) to fill before canceling

    # 3. Lifecycle & Account-Level Watchdog Boundaries
    "MAX_ENTRIES_PER_WINDOW": 1,      # Maximum Leg-1 entry attempts allowed per 5m window
    "MAX_WINDOW_LOSS_USD": 5.00,      # Hard per-window loss envelope. Funds 1 minimum lot at MAX_ENTRY_PRICE (5 * 0.90 = $4.50)
    "MAX_DAILY_DRAWDOWN_USD": 15.00,  # Account-level daily drawdown circuit breaker ($15.00 cumulative loss)
    "LOSS_STREAK_COOLING": 3,         # Consecutive losing rounds triggering cooling mode
    "COOLING_ROUNDS": 3,              # Number of rounds to cool off when loss streak triggers
    "DEADMAN_TIMEOUT_S": 3.0,         # Dead-man switch: cancel resting orders if market feed drops for > 3.0s
    "POLY_PRICE_MAX_AGE_S": 10.0,     # Ignore cached Polymarket TWAP older than this (fall back to Binance spot basis)

    # 4. Market-Regime Classification (attribution / dashboard only; not a v2.2 entry gate)
    "MIN_ENTRY_TAU_S": 180.0,         # Regime classifier horizon reference for can-quote attribution
    "TOXICITY_SHOCK_BPS": 3.5,        # 3-second rapid price shock threshold indicating informed toxicity
    "SNR_TREND_THRESHOLD": 1.8,       # Signal-to-Noise Ratio threshold to classify momentum trend (p < 0.05)
    "SNR_RANGE_THRESHOLD": 1.2,       # SNR threshold to classify normal oscillation regime
    "SNR_RANGE_CONFIDENT": 1.0,       # High-confidence mean-reverting range threshold
    "MAX_RANGE_DISPLACEMENT_BPS": 8.0,# Max displacement from strike to qualify for range-bound regime
    "MAX_ORDERBOOK_SPREAD": 0.06,     # Max book spread (0.06 = 6 cents) before declaring wide-spread regime
    "ALLOW_INCREASED_RANGE_SIZE": False,
    "MIN_FAIR_PROB_ENTRY": 0.42,      # Regime classifier balanced-odds lower bound (attribution)
    "MAX_FAIR_PROB_ENTRY": 0.58,      # Regime classifier balanced-odds upper bound (attribution)
}

def validate_params(params=None):
    """Fail fast when risk parameters contradict each other."""
    p = PARAMS if params is None else params
    errors = []
    max_px = p.get("LWAA_MAX_ENTRY_PRICE", 0.65)
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
