"""PM-Quant Configuration & Single Source of Truth (v2.5).

All model parameters, version identity, and filesystem layout are derived here.
No version logic or branching should exist in downstream modules.
"""
from pathlib import Path

# Semantic Version Identity (v<Major>.<Minor>)
VERSION = "v2.6"
BATCH_ID = VERSION

# Root Directory
ROOT = Path(__file__).resolve().parent

# Core Physical & Economic Parameters (v2.6: WS Low-Latency + Pyramid Confirm + Reversal Flip + Black-Swan Lock)
PARAMS = {
    # 1. Discrete Physical & Lot Constraints
    "LOT_SIZE": 5.0,                  # Exchange minimum order unit (lots must be integer multiples)
    "MIN_ORDER_SHARES": 5.0,          # Hard Polymarket CLOB rule: every placed order must be >= 5.0 shares
    "MIN_LOTS": 1,                    # Minimum entry lots (1 lot = 5 shares)
    "MAX_LOTS": 2,                    # Up to 2 lots (10 shares) via confirmed pyramid add
    "INITIAL_CAPITAL_USD": 100.0,     # Paper account starting bankroll
    "LATENCY_S": 0.30,                # Round-trip wire + engine order insertion delay (s)
    "CANCEL_LATENCY_S": 0.30,         # In-flight wire delay for order cancellation requests (s)
    "TAKER_FEE_RATE": 0.07,           # Official Polymarket crypto taker fee coefficient: rate * p * (1-p); makers pay zero
    "TAKER_MAX_ADVERSE_DROP": 0.05,   # Reject delayed taker IOC if ask collapses >= $0.05 (knife-drop protection)

    # 2. Strict Asian TWAP Net-Edge Engine (verified +$79.26 / 60.7% WR / t=+2.46 across 359 windows)
    "ENTRY_MIN_TAU_S": 15.0,          # Tail freeze cutoff (tau < 15s: cancel resting quotes, hold inventory)
    "ENTRY_MAX_TAU_S": 240.0,         # Entry active window: tau in [15s, 240s] (elapsed 60s ~ 285s)
    "ENTRY_MIN_PROB": 0.52,           # Only bet sides with modelled win probability P* >= 52%
    "ENTRY_MIN_PRICE": 0.20,          # Avoid deep out-of-the-money lottery tickets
    "ENTRY_MAX_PRICE": 0.75,          # Avoid extreme negative-skew high-price bets
    "MIN_MAKER_EDGE": 0.05,           # Minimum modelled edge for zero-fee Maker bids (P* - maker_px >= 0.05)
    "MIN_TAKER_EDGE": 0.09,           # Minimum net edge after fee for Taker IOC (P* - ask - fee >= 0.09)
    "MAX_QUOTE_ATTEMPTS": 99,         # Unrestricted maker quote attempts per window (protected by 2.5s requote cooldown)
    "MIN_REQUOTE_AGE_S": 2.5,         # Minimum order age (s) before allowing price-chase requote
    "MAX_BID_RETREAT_CANCEL": 0.029,  # Cancel resting quote if market best_bid retreats >= 3 ticks ($0.03)
    "MAX_QUEUE_WAIT_S": 15.0,         # Passive queue waiting timeout (s) before cancelling
    "ANTI_TOXICITY_SHOCK_BPS": 2.5,   # Pull resting maker quotes when 2-3s Binance spot move >= 2.5 bps
    "STRIKE_WAIT_S": 15.0,            # Require confirmed window strike & basis warmup before opening

    # 3. Position Management: Pyramid Confirm Add + Reversal Flip + Ultra-Deep Black-Swan Lock
    "ENABLE_ARBITRAGE_HEDGE": False,  # Generic wide pair-lock stays off (preserves $1.00 convexity)
    "ARBITRAGE_HEDGE_MAX_PAIR_COST": 0.90,
    "ENABLE_BLACKSWAN_LOCK": True,    # Lock risk-free profit when opp_ask <= $0.06 & pair_cost <= $0.85 (94%+ winners)
    "BLACKSWAN_LOCK_MAX_OPP_ASK": 0.06,
    "BLACKSWAN_LOCK_MAX_PAIR_COST": 0.85,
    "ENABLE_PYRAMID_ADD": True,       # Add 2nd lot on confirmed winner (tau <= 180s, P* >= 0.65, px in [$0.35,$0.72])
    "SECONDARY_ENTRY_COOLDOWN_S": 15.0,  # Min seconds after Leg-1 fill before allowing pyramid add or reversal flip
    "PYRAMID_MAX_TAU_S": 180.0,
    "PYRAMID_MIN_PROB": 0.65,
    "PYRAMID_MIN_PRICE": 0.35,
    "PYRAMID_MAX_PRICE": 0.72,
    "PYRAMID_MIN_MAKER_EDGE": 0.06,
    "PYRAMID_MIN_TAKER_EDGE": 0.09,
    "ENABLE_REVERSAL_FLIP": True,     # Independent opposite-side entry when Leg-1 collapses (P* <= 0.30, P_opp >= 0.65)
    "FLIP_MAX_HELD_PROB": 0.30,
    "FLIP_MIN_OPP_PROB": 0.65,
    "FLIP_MIN_MAKER_EDGE": 0.06,
    "FLIP_MIN_TAKER_EDGE": 0.09,
    "ALLOW_TAKER_UNWIND": True,       # Only extreme tail defensive salvage is allowed
    "DEFENSIVE_SALVAGE_ENABLE": True, # Exit collapsed leg near expiry when P* < 0.12
    "DEFENSIVE_SALVAGE_TAU_S": 30.0,  # Trigger only when tau <= 30s
    "DEFENSIVE_SALVAGE_MAX_PROB": 0.12,
    "DEFENSIVE_SALVAGE_MAX_BID": 0.15,# Only sell into real bids in [$0.06, $0.15]
    "DEFENSIVE_SALVAGE_MIN_BID": 0.06,

    # 4. Realism & Data Integrity Guards (Zero Self-Deception)
    "STRICT_BOOK_CONSISTENCY": True,  # Reject inconsistent/crossed snapshots instead of shifting prices artificially
    "TAKER_REAL_DELAY_CHECK": True,   # Wait LATENCY_S and re-fetch live book before IOC taker fill in live paper
    "SPOT_MAX_AGE_S": 3.0,            # Treat spot feed older than 3s as stale -> trips dead-man switch
    "POLY_PRICE_MAX_AGE_S": 10.0,     # Ignore cached Chainlink TWAP older than 10s
    "BASIS_MAX_AGE_S": 120.0,         # Expire Binance-Chainlink basis EMA if TWAP silent > 120s
    "MIN_MODEL_VOL_BPS": 14.0,        # Calibrated empirical 5-min BTC std floor (~14 bps); prevents Phi(d) saturation

    # 5. Lifecycle & Account-Level Watchdog Boundaries (Circuit Breaker & Cooling Disabled)
    "ENABLE_CIRCUIT_BREAKER": False,
    "MAX_ENTRIES_PER_WINDOW": 2,
    "MAX_WINDOW_LOSS_USD": 100.00,    # Unrestricted per-window cost envelope
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
    "LWAA_MAX_TAU_S": 240.0,
    "LWAA_MIN_PROB": 0.52,
    "LWAA_MIN_EDGE": 0.05,
    "LWAA_MAX_ENTRY_PRICE": 0.75,
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
