"""PM-Quant Configuration & Single Source of Truth (v2.10).

All model parameters, version identity, and filesystem layout are derived here.
No version logic or branching should exist in downstream modules.
"""
import os
from pathlib import Path

# Semantic Version Identity (v<Major>.<Minor>)
VERSION = "v2.10"
BATCH_ID = VERSION

# Root Directory
ROOT = Path(__file__).resolve().parent


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        val = float(raw)
        return val if val >= 0.0 else default
    except ValueError:
        return default


# Core Physical & Economic Parameters (v2.10: v2.9 Strategy Baseline + Honest Zero-Lookahead Sim & Scientific Diagnostics)
PARAMS = {
    # 1. Discrete Physical & Lot Constraints
    "LOT_SIZE": 5.0,                  # Exchange minimum order unit (lots must be integer multiples)
    "MIN_ORDER_SHARES": 5.0,          # Hard Polymarket CLOB rule: every placed order must be >= 5.0 shares
    "MIN_LOTS": 1,                    # Minimum entry lots (1 lot = 5 shares)
    "MAX_LOTS": 2,                    # Up to 2 lots (10 shares) max exposure (v2.9 baseline)
    "INITIAL_CAPITAL_USD": 100.0,     # Paper account starting bankroll
    # Calibrated dynamic wire+engine latency: tracks real measured Polymarket RTT/2 +
    # signing/CLOB matching overhead (110ms) with a conservative 150ms floor and +-15%
    # real-world jitter in live paper mode (deterministic 180ms fallback in offline replay).
    # LATENCY_MAX_S=2.50 ensures high-RTT environments are modeled honestly, never capped at 600ms.
    "LATENCY_S": _env_float("PM_LATENCY_S", 0.18),
    "CANCEL_LATENCY_S": _env_float("PM_CANCEL_LATENCY_S", 0.18),
    "LATENCY_DYNAMIC_FROM_RTT": True,
    "LATENCY_ENGINE_OVERHEAD_MS": 110.0,
    "LATENCY_MIN_S": 0.15,
    "LATENCY_MAX_S": 2.50,
    "LATENCY_JITTER_PCT": 0.15,
    "TAKER_FEE_RATE": 0.07,           # Conservative crypto taker fee coefficient: rate * p * (1-p); makers pay zero
    "TAKER_IOC_SLIPPAGE_TICKS": 0.02, # Pre-submission IOC limit cap above signal ask (+$0.02), bounded by exact net edge
    "TAKER_MAX_ADVERSE_DROP": 0.05,   # Legacy compatibility key (post-arrival look-ahead reject is permanently removed)
    "QUEUE_CANCEL_CONSERVATIVE_MULT": 1.03,  # Conservative 3% buffer on physical L2 depth cap (never looser than reality)
    "BOOK_CONSISTENCY_TOLERANCE": 0.015,     # Allow 1-tick async WS frame jitter across Up/Down streams (still blocks arb/cross)

    # 2. Strict Asian TWAP Net-Edge Engine (v2.9 Strategy Baseline Restored)
    "ENABLE_MAKER_ENTRY": True,       # Restored v2.9 baseline: Maker + Taker dual execution
    "DIRECTIONAL_ANTI_TOXICITY": False,  # Restored v2.9 baseline: symmetric spot-shock guard
    "ENTRY_MIN_TAU_S": 15.0,          # Tail freeze cutoff (tau < 15s: cancel resting quotes, hold inventory)
    "ENTRY_MAX_TAU_S": 270.0,         # Entry active window: tau in [15s, 270s]
    "ENTRY_MIN_PROB": 0.52,           # Only bet sides with modelled win probability P* >= 52%
    "ENTRY_MIN_PRICE": 0.20,          # Avoid deep out-of-the-money lottery tickets for fresh Leg-1 entries
    "ENTRY_MAX_PRICE": 0.80,          # Allow high-confidence Leg-1 up to $0.80 when net edge remains strictly positive
    "MIN_MAKER_EDGE": 0.05,           # Minimum base edge for zero-fee Maker bids at flat/cheap prices (px <= $0.54)
    "MIN_HOLD_MAKER_EDGE": 0.015,     # Pull resting Maker quote if live edge decays below 1.5%
    "MIN_TAKER_EDGE": 0.065,          # Minimum base net edge after fee for Taker IOC at flat/cheap prices (px <= $0.54)
    # Odds-Skew & Early-Window Dynamic Edge Premium (v2.9 baseline):
    "PRICE_EDGE_PIVOT": 0.54,         # No extra price premium at or below $0.54 (golden launch zone)
    "PRICE_EDGE_SLOPE": 0.18,         # +1.8% required edge per +$0.10 above $0.54
    "EARLY_TAU_PIVOT_S": 180.0,       # No extra time premium once tau <= 180s
    "EARLY_TAU_MAX_PREMIUM": 0.012,   # Smoothly adds up to +1.2% required edge across tau in (180s, 270s]
    "MAKER_MAX_ASK_IMBALANCE": 4.0,   # Skip passive Maker bid when top-5 ask wall >= 4x bid depth & >= 200 shares
    "MAKER_MIN_WALL_SHARES": 200.0,
    "MAX_ENTRY_VOL_BPS": 75.0,        # Extreme liquidation cascade storm guard (pause new entries when 5m vol > 75 bps)
    "MAX_QUOTE_ATTEMPTS": 99,         # Unrestricted maker quote attempts per window (protected by 2.5s requote cooldown)
    "MIN_REQUOTE_AGE_S": 2.5,         # Minimum order age (s) before allowing price-chase requote
    "MAX_BID_RETREAT_CANCEL": 0.029,  # Cancel resting quote if market best_bid retreats >= 3 ticks ($0.03)
    "MAX_QUEUE_WAIT_S": 15.0,         # Passive queue waiting timeout (s) before cancelling (extended if near queue head)
    "QUEUE_HEAD_PROTECT_SHARES": 25.0,# Protect resting Maker quote past 15s if remaining queue_ahead <= 25 shares at best_bid
    "ANTI_TOXICITY_SHOCK_BPS": 2.5,   # Pull resting maker quotes when 2-3s Binance spot move >= 2.5 bps
    "STRIKE_WAIT_S": 15.0,            # Require confirmed window strike & basis warmup before opening

    # 3. Position Management (v2.9 Baseline Restored: Pyramid + Sub-$1.00 Pair-Lock Flip, Black-Swan Lock OFF)
    "ENABLE_ARBITRAGE_HEDGE": False,  # Generic wide pair-lock stays off (preserves $1.00 convexity)
    "ARBITRAGE_HEDGE_MAX_PAIR_COST": 0.90,
    "ENABLE_BLACKSWAN_LOCK": False,   # Disabled — cross-sample proof (local & London both ~-$0.105/lock drag)
    "BLACKSWAN_LOCK_MAX_OPP_ASK": 0.06,
    "BLACKSWAN_LOCK_MAX_PAIR_COST": 0.85,
    "ENABLE_PYRAMID_ADD": True,       # Add 2nd lot on confirmed winner (Taker-only, P* >= 0.70, tau in [35s, 180s])
    "PYRAMID_ALLOW_MAKER": False,     # Ban passive Maker pyramid bids to eliminate reversal knife-catching
    "SECONDARY_ENTRY_COOLDOWN_S": 8.0,# 8s confirmation cooldown after Leg-1 fill before allowing pyramid add
    "PYRAMID_MAX_TAU_S": 180.0,
    "PYRAMID_MIN_TAU_S": 35.0,
    "PYRAMID_MIN_PROB": 0.70,
    "PYRAMID_MIN_PRICE": 0.35,
    "PYRAMID_MAX_PRICE": 0.82,
    "PYRAMID_MAX_SPREAD": 0.09,
    "PYRAMID_MIN_MAKER_EDGE": 0.06,
    "PYRAMID_MIN_TAKER_EDGE": 0.055,
    "ENABLE_REVERSAL_FLIP": True,     # Sub-$1.00 dynamic pair-lock flip (v2.9 baseline)
    "FLIP_COOLDOWN_S": 5.0,
    "FLIP_MAX_HELD_PROB": 0.42,
    "FLIP_MIN_OPP_PROB": 0.58,
    "FLIP_MAX_PAIR_COST": 0.98,
    "FLIP_MIN_PRICE": 0.01,
    "FLIP_MAX_PRICE": 0.96,
    "FLIP_MAX_SPREAD": 0.12,
    "FLIP_MIN_MAKER_EDGE": 0.045,
    "FLIP_MIN_TAKER_EDGE": 0.040,
    "ALLOW_TAKER_UNWIND": False,      # 严格禁止任何卖出，买入后一律持有至到期结算
    "DEFENSIVE_SALVAGE_ENABLE": False,# 关闭尾盘残值卖出，严守买入即持有至结算原则
    "DEFENSIVE_SALVAGE_TAU_S": 30.0,  # Trigger only when tau <= 30s
    "DEFENSIVE_SALVAGE_MAX_PROB": 0.12,
    "DEFENSIVE_SALVAGE_MAX_BID": 0.15,# Only sell into real bids in [$0.06, $0.15]
    "DEFENSIVE_SALVAGE_MIN_BID": 0.06,

    # 4. Realism & Data Integrity Guards (Zero Self-Deception)
    "STRICT_BOOK_CONSISTENCY": True,  # Reject inconsistent/crossed snapshots instead of shifting prices artificially
    "TAKER_REAL_DELAY_CHECK": True,   # Wait wire latency and match natively on live post-delay book (no post-arrival reject)
    "SPOT_MAX_AGE_S": 3.0,            # Treat spot feed older than 3s as stale -> trips dead-man switch
    "POLY_PRICE_MAX_AGE_S": 10.0,     # Ignore cached Chainlink TWAP older than 10s
    "BASIS_MAX_AGE_S": 120.0,         # Expire Binance-Chainlink basis EMA if TWAP silent > 120s
    # 现货 WebSocket 数据源: binance (默认) / kraken / coinbase / okx / auto (启动时自动测速择优)
    "SPOT_WS_PROVIDER": os.environ.get("PM_SPOT_WS_PROVIDER", "binance").strip().lower() or "binance",
    "MIN_MODEL_VOL_BPS": 14.0,        # Calibrated empirical 5-min BTC std floor (~14 bps); prevents Phi(d) saturation

    # 5. Lifecycle & Account-Level Watchdog Boundaries (Circuit Breaker Disabled per User Request)
    "ENABLE_CIRCUIT_BREAKER": False,
    "MAX_ENTRIES_PER_WINDOW": 2,
    "MAX_WINDOW_LOSS_USD": 100.00,    # Unrestricted per-window cost envelope (v2.9 baseline)
    "MAX_DAILY_DRAWDOWN_USD": 100.00,
    "LOSS_STREAK_COOLING": 999999,
    "COOLING_ROUNDS": 0,
    "DEADMAN_TIMEOUT_S": 3.0,

    # 6. Market-Regime Classification (attribution / dashboard only)
    "MIN_ENTRY_TAU_S": 15.0,
    "TOXICITY_SHOCK_BPS": 3.5,
    "SNR_TREND_THRESHOLD": 1.8,
    "SNR_RANGE_THRESHOLD": 1.2,
    "SNR_RANGE_CONFIDENT": 1.0,
    "MAX_RANGE_DISPLACEMENT_BPS": 8.0,
    "MAX_ORDERBOOK_SPREAD": 0.06,
    "ALLOW_INCREASED_RANGE_SIZE": False,
    "MIN_MARKET_MID_BALANCED": 0.42,
    "MAX_MARKET_MID_BALANCED": 0.58,

    # Compatibility keys for stress/replay overrides
    "LWAA_MIN_TAU_S": 15.0,
    "LWAA_MAX_TAU_S": 270.0,
    "LWAA_MIN_PROB": 0.52,
    "LWAA_MIN_EDGE": 0.05,
    "LWAA_MAX_ENTRY_PRICE": 0.80,

    # 7. Data Retention & Disk-Full Auto-Guard (summary.jsonl is NEVER deleted)
    "RECORD_RAW_MARKET": True,          # Master toggle for raw data/1_market recording (auto-paused if disk critical)
    "MARKET_RETENTION_DAYS": 3,         # Keep recent 3 days of raw data/1_market/{events,oracle,books,spot}
    "WINDOW_REPLAY_RETENTION_DAYS": 7,  # Keep recent 7 days of windows/*.json.gz detail replays
    "MIN_KEEP_REPLAY_FILES": 30,        # Under disk pressure, still try to keep at least 30 newest replay files
    "RUNTIME_LOG_RETENTION_DAYS": 7,    # Keep recent 7 days of runtime logs
    "CLEANUP_INTERVAL_S": 1800.0,       # Check disk capacity & run retention cleanup every 30 minutes
    "DISK_SOFT_LIMIT_PCT": 75.0,        # Target disk usage % to stop pressure eviction
    "DISK_HARD_LIMIT_PCT": 85.0,        # Trigger emergency oldest-first cleanup when disk usage >= 85%
    "DISK_CRITICAL_PAUSE_PCT": 93.0,    # Pause raw 1_market writes if disk usage still >= 93% after cleanup
    "MIN_FREE_DISK_GB": 3.0,            # Trigger emergency cleanup if free disk drops below 3.0 GB
    "CRITICAL_FREE_DISK_GB": 0.8,       # Pause raw 1_market writes if free disk still < 0.8 GB after cleanup
    "MAX_DATA_DIR_MB": 4096.0,          # Rolling quota cap (4 GB) for the entire data/ directory
    "MAX_TODAY_MARKET_MB": 1536.0,      # Same-day raw market volume cap (1.5 GB) before auto-pausing recording
}


def validate_params(params=None):
    """Fail fast when risk parameters contradict each other."""
    p = PARAMS if params is None else params
    errors = []
    max_px = p.get("ENTRY_MAX_PRICE", p.get("LWAA_MAX_ENTRY_PRICE", 0.75))
    lot_size = p["LOT_SIZE"]
    budget = p["MAX_WINDOW_LOSS_USD"]
    min_lot_cost = lot_size * max_px * p["MIN_LOTS"]
    max_pair_cost = lot_size * p["MAX_LOTS"] * 1.00
    if budget + 1e-9 < min_lot_cost:
        errors.append(
            f"MAX_WINDOW_LOSS_USD={budget:.2f} cannot fund one minimum lot at "
            f"max_entry_price={max_px:.2f} "
            f"({lot_size:.1f} x {max_px:.2f} = {min_lot_cost:.2f})"
        )
    if budget + 1e-9 < max_pair_cost:
        errors.append(
            f"MAX_WINDOW_LOSS_USD={budget:.2f} cannot cover a fully paired "
            f"{lot_size * p['MAX_LOTS']:.0f}-share position (${max_pair_cost:.2f})"
        )
    if p["MAX_DAILY_DRAWDOWN_USD"] > p["INITIAL_CAPITAL_USD"]:
        errors.append(
            f"MAX_DAILY_DRAWDOWN_USD={p['MAX_DAILY_DRAWDOWN_USD']:.2f} exceeds "
            f"INITIAL_CAPITAL_USD={p['INITIAL_CAPITAL_USD']:.2f}; the breaker can never trip"
        )
    if budget > p["MAX_DAILY_DRAWDOWN_USD"]:
        errors.append(
            f"MAX_WINDOW_LOSS_USD={p['MAX_WINDOW_LOSS_USD']:.2f} exceeds "
            f"MAX_DAILY_DRAWDOWN_USD={p['MAX_DAILY_DRAWDOWN_USD']:.2f}"
        )
    if p["MIN_LOTS"] > p["MAX_LOTS"]:
        errors.append("MIN_LOTS exceeds MAX_LOTS")
    if p["ENTRY_MIN_PRICE"] > p["ENTRY_MAX_PRICE"]:
        errors.append("ENTRY_MIN_PRICE exceeds ENTRY_MAX_PRICE")
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

for _p in (CURRENT_LEDGER_DIR, RUNTIME_DIR, LOGS_DIR, REPORTS_DIR):
    _p.mkdir(parents=True, exist_ok=True)
