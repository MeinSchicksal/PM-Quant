"""PM-Quant Configuration & Single Source of Truth.

All model parameters, version identity, and filesystem layout are derived here.
No version logic or branching should exist in downstream modules.
"""
from pathlib import Path

# Semantic Version Identity (v<Major>.<Minor>)
VERSION = "v1.1"
BATCH_ID = VERSION

# Root Directory
ROOT = Path(__file__).resolve().parent

# Core Mathematical & Economic Parameters
PARAMS = {
    # 1. Discrete Physical & Lot Constraints
    "LOT_SIZE": 5.0,                  # Exchange minimum order unit (lots must be integer multiples)
    "MIN_LOTS": 1,                    # Minimum entry lots (1 lot = 5 shares, baseline sizing)
    "MAX_LOTS": 3,                    # Maximum entry lots (3 lots = 15 shares, fractional Kelly sizing cap)
    "LATENCY_S": 0.30,                # Estimated round-trip wire + engine order insertion delay
    "TAKER_FEE_RATE": 0.07,           # Official Polymarket crypto taker fee coefficient: fee = shares * rate * p * (1-p); makers pay zero

    # 2. Probability Estimation, Margin of Safety & Sizing
    "BASE_EDGE": 0.025,               # Minimum price edge demanded over uncertainty band (2.5 ticks)
    "UNCERTAINTY_K": 1.2,             # Scaling multiplier for real-time uncertainty bandwidth
    "BASELINE_VOL_BPS": 15.0,         # Fallback baseline realized volatility in bps / 5m
    "KELLY_FRACTION": 0.25,           # Quarter-Kelly risk fraction scaling factor

    # 3. Inventory & Utility Arbitration
    "RISK_AVERSION_GAMMA": 0.15,      # Avellaneda-Stoikov risk aversion factor for inventory penalty
    "MAX_QUEUE_WAIT_S": 15.0,         # Maximum acceptable passive queue waiting time based on trade rate
    "TOXICITY_SHOCK_BPS": 3.5,        # 3-second rapid price shock threshold indicating informed toxicity

    # 4. Lifecycle & Account-Level Watchdog Boundaries
    "TAIL_FREEZE_S": 60.0,            # Seconds before expiration where new leg-1 entries are disabled
    "MAX_WINDOW_LOSS_USD": 2.00,      # Hard bounded catastrophic drawdown envelope per window
    "MAX_DAILY_DRAWDOWN_USD": 8.00,   # Account-level daily drawdown circuit breaker ($8.00 cumulative loss)
    "LOSS_STREAK_COOLING": 3,         # Consecutive losing rounds triggering cooling mode
    "COOLING_ROUNDS": 3,              # Number of rounds to cool off when loss streak triggers

    # 5. Dual-Regime & Adverse Selection Boundaries
    "SNR_TREND_THRESHOLD": 1.8,       # Signal-to-Noise Ratio threshold to classify momentum trend (p < 0.05)
    "SNR_RANGE_THRESHOLD": 1.2,       # SNR threshold to classify mean-reverting oscillating regime (noise band)
    "MAX_MAKER_ENTRY_PRICE": 0.62,    # Maximum price allowed for passive Leg-1 entry (avoids buying the top)
    "MIN_MAKER_ENTRY_PRICE": 0.38,    # Minimum price allowed for passive Leg-1 entry
    "MAX_PAIR_HEDGE_COST": 1.05,      # Maximum acceptable pair cost (basis + hedge ask + fee) for Taker Hedge
}

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
