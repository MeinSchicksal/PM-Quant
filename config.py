"""PM-Quant Configuration & Single Source of Truth (v2.4).

All model parameters, version identity, and filesystem layout are derived here.
No version logic or branching should exist in downstream modules.
"""
from pathlib import Path

# Semantic Version Identity (v<Major>.<Minor>)
VERSION = "v2.4"
BATCH_ID = VERSION

# Root Directory
ROOT = Path(__file__).resolve().parent

# Core Physical & Economic Parameters (Model-Free Objective Architecture)
PARAMS = {
    # 1. Discrete Physical & Lot Constraints
    "LOT_SIZE": 5.0,                  # Exchange minimum order unit (lots must be integer multiples)
    "MIN_LOTS": 1,                    # Minimum entry lots (1 lot = 5 shares)
    "MAX_LOTS": 1,                    # Fixed 1-lot sizing per window
    "INITIAL_CAPITAL_USD": 100.0,     # Paper account starting bankroll
    "LATENCY_S": 0.30,                # Round-trip wire + engine order insertion delay (s)
    "TAKER_FEE_RATE": 0.07,           # Official Polymarket crypto taker fee coefficient: rate * p * (1-p); makers pay zero

    # 2. v2.4 Model-Free Objective Dual-Stage Engine (Zero Probability Estimation)
    "ENTRY_MIN_TAU_S": 15.0,          # Tail freeze cutoff (tau < 15s: cancel resting quotes, hold inventory)
    "ENTRY_MAX_TAU_S": 240.0,         # Entry active window: tau in [15s, 240s] (elapsed 60s ~ 285s)
    "ENTRY_MIN_PRICE": 0.20,          # Avoid deep out-of-the-money lottery tickets
    "ENTRY_MAX_PRICE": 0.70,          # Hard chase ceiling (never pay above $0.70)
    "MAX_QUOTE_ATTEMPTS": 5,          # Max maker quote attempts per window (protected by 2.5s requote cooldown)
    "MIN_REQUOTE_AGE_S": 2.5,         # Minimum order age (s) before allowing price-chase requote
    "MAX_BID_RETREAT_CANCEL": 0.029,  # Cancel resting quote if market best_bid retreats >= 3 ticks ($0.03)
    "MAX_QUEUE_WAIT_S": 15.0,         # Passive queue waiting timeout (s) before cancelling
    "ANTI_TOXICITY_SHOCK_BPS": 2.5,   # Pull resting maker quotes when 2-3s Binance spot move >= 2.5 bps
    "STRIKE_WAIT_S": 15.0,            # Require confirmed window strike & basis warmup before opening

    # Stage 1: Mid-Window Trend & Rebound Discount Entry (MWDE, tau in (120s, 240s])
    "MWDE_ENABLE": True,
    "MWDE_MIN_TAU_S": 120.0,
    "MWDE_MAX_TAU_S": 240.0,
    "MWDE_MAX_ENTRY_PRICE": 0.45,     # Discount entry cap in mid-window
    "MWDE_MIN_LEAD_BPS": 1.5,         # Minimum spot displacement from open (bps) for trend-aligned dip
    "MWDE_REBOUND_MAX_DIST_BPS": 8.5, # Max distance below/above open (bps) to allow rebound turn entry
    "MWDE_REBOUND_MIN_SPOT_BPS": 1.0, # Minimum 15s spot turn (bps) towards strike for rebound entry
    "MWDE_REBOUND_MIN_MID_CHG": 0.015,# Minimum 15s orderbook probability rise (+1.5%) confirming rebound
    "MWDE_MAX_ADVERSE_MID_DROP": 0.08,# Falling-knife guard: block entry if 15s book mid dropped > 8 cents
    "MWDE_TAKER_PAIR_COST": 0.90,     # Only take ask early if Up+Down ask sum < 0.90
    "POST_SHOCK_MIN_MOVE": 90.0,      # Prior-round large move threshold ($90, ~14 bps) for consensus priority
    "POST_SHOCK_TAKER_MIN_LEAD_BPS": 3.0, # Minimum same-direction spot lead (bps) to cross spread post-shock

    # Stage 2: Late-Window Single-Side Prediction (LWAA, tau in [15s, 120s])
    "LWAA_MIN_TAU_S": 15.0,
    "LWAA_MAX_TAU_S": 120.0,
    "LWAA_MIN_LEAD_BPS": 5.0,         # Minimum objective spot / 60s-TWAP lead (bps) for late entry
    "LWAA_TAKER_LEAD_BPS": 8.0,       # Minimum lead (bps) required to cross spread as Taker IOC
    "LWAA_MIN_ENTRY_PRICE": 0.20,
    "LWAA_MAX_ENTRY_PRICE": 0.65,
    "LWAA_HARD_CHASE_CEILING": 0.70,
    "LWAA_MIN_MARKET_IMPLIED": 0.52,  # Target side market mid/bid must reflect majority consensus (>= 0.52)

    # 3. Dual-Sided Trailing Pair Lock & Single-Side Convexity Holding
    "ENABLE_ARBITRAGE_HEDGE": True,   # Immediate deep risk-free pair lock (pair_cost <= 0.90)
    "ARBITRAGE_HEDGE_MAX_PAIR_COST": 0.90,
    "ENABLE_TRAILING_HEDGE": True,    # Dynamic trailing pair lock for discount Leg-1 entries
    "TRAILING_HEDGE_MAX_BASIS": 0.48, # Apply trailing pair lock to discount entries (basis <= $0.48)
    "TRAILING_HEDGE_ARM_COST": 0.95,  # Arm trailing lock once best seen pair_cost <= $0.95
    "TRAILING_HEDGE_MAX_PAIR_COST": 0.97, # Execute trailing lock when opp_ask bounces >= $0.02 and pair_cost <= $0.97
    "TRAILING_HEDGE_BOUNCE_TICKS": 0.02,  # Opp ask bounce from post-entry low that triggers lock
    "SINGLE_SIDE_RUN_BPS": 12.0,      # Exemption: if objective lead >= 12 bps in our favor, let single side run to $1.00!
    "ALLOW_TAKER_UNWIND": True,       # Only extreme tail defensive salvage is allowed
    "DEFENSIVE_SALVAGE_ENABLE": True, # Exit collapsed leg near expiry to recover residual cash
    "DEFENSIVE_SALVAGE_TAU_S": 30.0,  # Trigger only when tau <= 30s
    "DEFENSIVE_SALVAGE_MAX_BID": 0.15,# Trigger only when market best_bid <= $0.15 (collapsed leg)
    "DEFENSIVE_SALVAGE_MIN_BID": 0.06,

    # 4. Realism & Data Integrity Guards (Zero Self-Deception)
    "STRICT_BOOK_CONSISTENCY": True,  # Reject inconsistent/crossed snapshots instead of shifting prices artificially
    "TAKER_REAL_DELAY_CHECK": True,   # Wait LATENCY_S and re-fetch live book before IOC taker fill in live paper
    "SPOT_MAX_AGE_S": 3.0,            # Treat spot feed older than 3s as stale -> trips dead-man switch
    "POLY_PRICE_MAX_AGE_S": 10.0,     # Ignore cached Chainlink TWAP older than 10s
    "BASIS_MAX_AGE_S": 120.0,         # Expire Binance-Chainlink basis EMA if TWAP silent > 120s

    # 5. Lifecycle & Account-Level Watchdog Boundaries
    "ENABLE_CIRCUIT_BREAKER": True,
    "MAX_ENTRIES_PER_WINDOW": 1,
    "MAX_WINDOW_LOSS_USD": 5.00,      # Hard per-window cost envelope (5 shares x $1.00)
    "MAX_DAILY_DRAWDOWN_USD": 15.00,
    "LOSS_STREAK_COOLING": 3,
    "COOLING_ROUNDS": 3,
    "DEADMAN_TIMEOUT_S": 3.0,

    # 6. Market-Regime Classification (attribution / dashboard only; not an entry gate)
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
