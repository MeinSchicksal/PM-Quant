"""PM-Quant Configuration & Single Source of Truth (v3.3).

All model parameters, version identity, and filesystem layout are derived here.
No version logic or branching should exist in downstream modules.
"""
import os
from pathlib import Path

# Semantic Version Identity (v<Major>.<Minor>)
VERSION = "v3.3"
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


# Core Physical & Economic Parameters (v3.3: Weighted Maker/Taker Routing + Paced Inventory + Payoff-Driven Flip)
PARAMS = {
    # 1. Discrete Physical & Lot Constraints
    "LOT_SIZE": 5.0,                  # Base entry unit (5 shares); flips support exact integer shares >= 5
    "MIN_ORDER_SHARES": 5.0,          # Hard Polymarket CLOB rule: every placed order must be >= 5.0 shares
    "MIN_LOTS": 1,                    # Minimum entry lots (1 lot = 5 shares)
    "MAX_LOTS": 4,                    # Per-side cumulative cap (20 shares) to prevent whipsaw fee-churn
    "MAX_NET_LOTS": 2,                # Max net directional exposure |Up - Down| = 10 shares (unlocked only in mid-phase)
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
    "QUEUE_CANCEL_CONSERVATIVE_MULT": 1.03,  # Conservative 3% buffer on physical L2 depth cap (never looser than reality)
    "BOOK_CONSISTENCY_TOLERANCE": 0.015,     # Allow 1-tick async WS frame jitter across Up/Down streams (still blocks arb/cross)

    # 2. v3.3 Multi-Factor Weighted Execution Engine (Maker/Taker Scene Routing + Paced Exposure)
    "MODEL_T5_UNIT_VARIANCE": True,   # Normalize Student-t(5) by sqrt(5/3) to remove mid-range under-confidence
    "ENABLE_MAKER_ENTRY": True,       # Keep Maker & Taker dual channels; select dynamically via scene-weighted score
    "EXECUTION_WEIGHTED_ROUTING": True,  # Score Maker vs Taker by spread, spot calmness/momentum, price & tau
    "DIRECTIONAL_ANTI_TOXICITY": True,# Only block adverse moves; ALLOW Taker IOC in the direction of spot momentum
    "ENTRY_MIN_TAU_S": 15.0,          # Tail freeze cutoff (tau < 15s: cancel resting quotes, hold inventory)
    "LATE_NO_NEW_ENTRY_TAU_S": 45.0,  # Final 45s: stop opening fresh positions / adds (allow payoff-driven flip rescue only)
    "EARLY_PHASE_MIN_TAU_S": 180.0,   # First 120s (tau > 180s): light probe phase capped at 5 shares (no early 10-share overload)
    "ENTRY_MAX_TAU_S": 280.0,         # Entry active window: start 20s after open (tau in [15s, 280s])
    "ENTRY_MIN_PROB": 0.53,           # Primary leading-side probability threshold
    # Low-price mispricing / odds-value entry (not restricted strictly to strong model lead):
    "ENTRY_VALUE_ENABLE": True,       # Allow 5-share value entry on panicky low-price mispricings in contested zone
    "ENTRY_VALUE_MIN_PROB": 0.46,     # Minimum calibrated P* for contested low-price value entry
    "ENTRY_VALUE_MAX_PRICE": 0.42,    # Only fire non-leading value entry when price <= $0.42 (strong payoff asymmetry)
    "ENTRY_VALUE_MIN_EDGE": 0.055,    # Require higher net edge (>= 5.5%) for contested value entries
    "ENTRY_MIN_PRICE": 0.20,          # Avoid deep out-of-the-money lottery tickets
    "ENTRY_MAX_PRICE": 0.72,          # Avoid >$0.72 low-payoff high-price traps
    "ENTRY_OVEREXTENDED_PROB": 0.78,  # Above P* = 78%, add overextension edge penalty (don't chase huge leads blindly)
    "ENTRY_OVEREXTENDED_SLOPE": 0.12, # Extra required edge per +1.0 P* above ENTRY_OVEREXTENDED_PROB
    "MIN_MAKER_EDGE": 0.055,          # Thick safety cushion for zero-fee Maker bids (fixes inverted Maker < Taker threshold)
    "MIN_HOLD_MAKER_EDGE": 0.020,     # Pull resting Maker quote if live edge decays below 2.0%
    "MIN_TAKER_EDGE": 0.038,          # Post-fee Taker IOC net-edge threshold
    "MAKER_CALM_VEL_BPS": 1.0,        # Spot |velocity| < 1.0 bps boosts Maker routing weight
    "MAKER_PREFERRED_SPREAD": 0.02,   # Spread >= $0.02 boosts zero-fee Maker advantage
    "MAKER_MAX_PRICE": 0.62,          # Restrict passive Maker bids to mid/low price zone (avoid high-price toxic fills)
    "TAKER_MOMENTUM_BOOST_BPS": 0.8,  # Tailwind spot momentum >= 0.8 bps boosts Taker routing weight
    "TAKER_URGENT_TAU_S": 90.0,       # Below 90s remaining, bias toward immediate Taker execution
    # Odds-Skew & Early-Window Protection:
    "PRICE_EDGE_PIVOT": 0.54,         # No extra price premium at or below $0.54
    "PRICE_EDGE_SLOPE": 0.10,         # +1.0% required edge per +$0.10 above $0.54 to protect entry average
    "EARLY_TAU_PIVOT_S": 180.0,
    "EARLY_TAU_MAX_PREMIUM": 0.010,   # Moderate +1.0% early-window premium (tau=280s -> 0% at tau=180s)
    "MAKER_MAX_ASK_IMBALANCE": 4.0,   # Skip passive Maker bid when top-5 ask wall >= 4x bid depth & >= 200 shares
    "MAKER_MIN_WALL_SHARES": 200.0,
    "MAX_ENTRY_VOL_BPS": 75.0,        # Extreme liquidation cascade storm guard (pause new entries when 5m vol > 75 bps)
    "MAX_QUOTE_ATTEMPTS": 99,         # Unrestricted maker quote attempts per window (protected by 2.5s requote cooldown)
    "MIN_REQUOTE_AGE_S": 2.5,         # Minimum order age (s) before allowing price-chase requote
    "MAX_BID_RETREAT_CANCEL": 0.029,  # Cancel resting quote if market best_bid retreats >= 3 ticks ($0.03)
    "MAX_QUEUE_WAIT_S": 8.0,          # Shorter Maker queue wait (8s) so stale quotes don't hang
    "QUEUE_HEAD_PROTECT_SHARES": 25.0,# Protect resting Maker quote if remaining queue_ahead <= 25 shares at best_bid
    "ANTI_TOXICITY_SHOCK_BPS": 2.5,   # Pull resting maker quotes when 2-3s spot move >= 2.5 bps
    # Pre-trade knife-drop guard (zero look-ahead):
    "ENABLE_BOOK_DROP_GUARD": False,  # 默认关闭：单看盘口跌3¢不结合现货方向易误杀正常折价好单，保留开关备研
    "BOOK_DROP_GUARD_WINDOW_S": 5.0,  # 盘口急砸回望窗口（秒）
    "BOOK_DROP_GUARD_MAX": 0.03,      # 5s 内盘口跌幅 >= $0.03 触发阈值
    "ENTRY_MAX_ADVERSE_SPOT_BPS": 1.5,# 方向性防接飞刀：现货 2-3s 已反向急跌超 1.5 bps 时禁止逆势开仓

    # 2b. Mode B: Skewed-Drift High-Certainty Taker IOC (disabled)
    "ENABLE_DRIFT_MODE_B": False,
    "DRIFT_MODE_B_MIN_PRICE": 0.75,
    "DRIFT_MODE_B_MAX_PRICE": 0.88,
    "DRIFT_MODE_B_MIN_PROB": 0.80,
    "DRIFT_MODE_B_MIN_EDGE": 0.05,
    "DRIFT_MODE_B_DISABLE_PYRAMID": True,

    # 3. Position Management (v3.3: Average-Protecting Quality Add + Payoff-Driven Dynamic Flip)
    "ENABLE_ARBITRAGE_HEDGE": False,  # 严格关闭锁利阈值，保留赢家腿奔向 $1.00 的完整赔率凸性
    "ARBITRAGE_HEDGE_MAX_PAIR_COST": 0.90,
    "ENABLE_BLACKSWAN_LOCK": False,   # 严格关闭锁利阈值，不把多次小赢拿去换一次亏损
    "BLACKSWAN_LOCK_MAX_OPP_ASK": 0.06,
    "BLACKSWAN_LOCK_MAX_PAIR_COST": 0.85,
    "ALLOW_REENTRY_AFTER_PAIR": False,# 完成反手后不再同窗反复重开，防锯齿磨损
    "REENTRY_COOLDOWN_S": 10.0,
    "ENABLE_PYRAMID_ADD": True,       # 开启“不拉烂均价 + 防反转送钱”的中盘质量加仓
    "PYRAMID_ALLOW_AFTER_FLIP": False,# 反手调仓后禁止继续加仓
    "PYRAMID_ALLOW_MAKER": False,     # 加仓仅使用 Taker IOC
    "PYRAMID_USE_DYNAMIC_EDGE": True, # 与开仓使用统一动态净边际标准
    "SECONDARY_ENTRY_COOLDOWN_S": 12.0,# 加仓冷却 12s，避免同帧信号机械翻倍
    "PYRAMID_MAX_TAU_S": 180.0,       # 早盘前 120s 严禁加仓到 10 股，保留反转补救余地
    "PYRAMID_MIN_TAU_S": 45.0,        # 尾盘 45s 禁止顺势加仓
    "PYRAMID_MIN_PROB": 0.56,         # 己侧概率保持健康（防反转低价送钱）
    "PYRAMID_MAX_PROB_DROP": 0.06,    # 相对首仓开仓 P* 下跌不得超 6%，防止反转下跌途中摊平送钱
    "PYRAMID_MIN_PRICE": 0.20,
    "PYRAMID_MAX_PRICE": 0.65,        # 中盘优质加仓封顶 $0.65，避免高位接盘拉烂均价（B1 实测最优档）
    "PYRAMID_MAX_ABOVE_AVG": 0.05,    # 加仓价 <= 持仓均价 + $0.05（兼顾不拉烂均价与顺势优质加仓）
    "PYRAMID_MAX_SPREAD": 0.08,
    "PYRAMID_MIN_MAKER_EDGE": 0.06,
    "PYRAMID_MIN_TAKER_EDGE": 0.040,  # 中盘优质加仓净边际门槛 4.0%（B1 档：收益与回撤双优）
    "ENABLE_REVERSAL_FLIP": True,     # 确认反转后按终局盈亏反算股数，一步到位 Taker 调仓
    "FLIP_ALLOW_MAKER": False,        # 反转仅使用 Taker IOC
    "FLIP_ALLOW_MULTI": False,        # 单窗最多完整翻面 1 次，防 V 型双杀
    "MAX_FLIPS_PER_WINDOW": 1,
    "FLIP_COOLDOWN_S": 8.0,
    "FLIP_MAX_HELD_PROB": 0.42,
    "FLIP_MIN_OPP_PROB": 0.58,
    "FLIP_MAX_PAIR_COST": 2.00,
    "FLIP_MIN_PRICE": 0.20,
    "FLIP_MAX_PRICE": 0.68,           # 绝不为强行回本去买 >$0.68 的高价负赔率单
    "FLIP_MAX_SPREAD": 0.10,
    "FLIP_MIN_MAKER_EDGE": 0.055,
    "FLIP_MIN_TAKER_EDGE": 0.038,
    "FLIP_USE_DYNAMIC_EDGE": True,    # 反手同样应用高价动态边际惩罚
    "FLIP_PAYOFF_SIZING": True,       # 终局导向动态整数股（支持 5~10 股一次买够，不再死守 5 股）
    "FLIP_TARGET_PROFIT_USD": 0.30,   # 反手目标：对侧胜出时保本并争取微盈 +$0.30
    "FLIP_MAX_SHARES": 10.0,          # 单次反手硬上限 10 股（防马丁格尔翻倍失控）
    "FLIP_MAX_TOTAL_WINDOW_COST": 10.0,# 含反手单窗总投入硬封顶 $10.00
    "ALLOW_TAKER_UNWIND": False,      # 严格禁止卖出，买入即持有至到期结算
    "DEFENSIVE_SALVAGE_ENABLE": False,
    "DEFENSIVE_SALVAGE_TAU_S": 30.0,
    "DEFENSIVE_SALVAGE_MAX_PROB": 0.12,
    "DEFENSIVE_SALVAGE_MAX_BID": 0.15,
    "DEFENSIVE_SALVAGE_MIN_BID": 0.06,

    # 4. Realism & Data Integrity Guards (Zero Self-Deception)
    "STRICT_BOOK_CONSISTENCY": True,  # Reject inconsistent/crossed snapshots instead of shifting prices artificially
    "TAKER_REAL_DELAY_CHECK": True,   # Wait wire latency and match natively on live post-delay book (no post-arrival reject)
    "POLY_PRICE_MAX_AGE_S": 10.0,     # Ignore cached Chainlink TWAP older than 10s
    "BASIS_MAX_AGE_S": 120.0,         # Expire Binance-Chainlink basis EMA if TWAP silent > 120s
    # 现货 WebSocket 数据源: 当前切换为 coinbase (BTC/USD) 进行实测验证；支持环境变量 PM_SPOT_WS_PROVIDER 覆盖 (binance / coinbase / okx / kraken / auto)
    "SPOT_WS_PROVIDER": os.environ.get("PM_SPOT_WS_PROVIDER", "coinbase").strip().lower() or "coinbase",
    "MIN_MODEL_VOL_BPS": 14.0,        # Calibrated empirical 5-min BTC std floor (~14 bps); prevents Phi(d) saturation

    # 5. Lifecycle & Account-Level Watchdog Boundaries (纸面验证阶段关闭熔断，完整暴露策略原生下限)
    "ENABLE_CIRCUIT_BREAKER": False,
    "MAX_ENTRIES_PER_WINDOW": 4,      # 合理控制单窗最多 4 笔（首仓+质量加仓+最多1次终局反手），杜绝锯齿刷单
    "MAX_WINDOW_LOSS_USD": 100.00,    # 纸面验证不设额外预算截断
    "MAX_DAILY_DRAWDOWN_USD": 100.00, # 熔断关闭，不拦截当日回撤
    "LOSS_STREAK_COOLING": 999999,    # 关闭连亏冷却
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
    "MAX_TAKER_SPREAD": 0.10,
    "ALLOW_INCREASED_RANGE_SIZE": False,
    "MIN_MARKET_MID_BALANCED": 0.42,
    "MAX_MARKET_MID_BALANCED": 0.58,

    # Compatibility keys for stress/replay overrides
    "LWAA_MIN_TAU_S": 15.0,
    "LWAA_MAX_TAU_S": 280.0,
    "LWAA_MIN_PROB": 0.53,
    "LWAA_MIN_EDGE": 0.055,
    "LWAA_MAX_ENTRY_PRICE": 0.72,

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
    net_lots = int(p.get("MAX_NET_LOTS", p["MAX_LOTS"]))
    max_pair_cost = lot_size * net_lots * 1.00
    if budget + 1e-9 < min_lot_cost:
        errors.append(
            f"MAX_WINDOW_LOSS_USD={budget:.2f} cannot fund one minimum lot at "
            f"max_entry_price={max_px:.2f} "
            f"({lot_size:.1f} x {max_px:.2f} = {min_lot_cost:.2f})"
        )
    if budget + 1e-9 < max_pair_cost:
        errors.append(
            f"MAX_WINDOW_LOSS_USD={budget:.2f} cannot cover max net directional "
            f"{lot_size * net_lots:.0f}-share exposure (${max_pair_cost:.2f})"
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
    if float(p.get("PYRAMID_MIN_PRICE", 0.20)) > float(p.get("PYRAMID_MAX_PRICE", 0.65)):
        errors.append("PYRAMID_MIN_PRICE exceeds PYRAMID_MAX_PRICE")
    if float(p.get("FLIP_MIN_PRICE", 0.20)) > float(p.get("FLIP_MAX_PRICE", 0.68)):
        errors.append("FLIP_MIN_PRICE exceeds FLIP_MAX_PRICE")
    if float(p.get("FLIP_MAX_SHARES", 10.0)) < float(p.get("MIN_ORDER_SHARES", 5.0)):
        errors.append("FLIP_MAX_SHARES is below MIN_ORDER_SHARES")
    min_tau = float(p.get("ENTRY_MIN_TAU_S", 15.0))
    late_tau = float(p.get("LATE_NO_NEW_ENTRY_TAU_S", 0.0) or 0.0)
    max_tau = float(p.get("ENTRY_MAX_TAU_S", 280.0))
    if late_tau > 0.0 and not (min_tau <= late_tau <= max_tau):
        errors.append(f"LATE_NO_NEW_ENTRY_TAU_S={late_tau} must lie in [{min_tau}, {max_tau}]")
    if p.get("ENABLE_DRIFT_MODE_B", False):
        mb_min = float(p.get("DRIFT_MODE_B_MIN_PRICE", 0.75))
        mb_max = float(p.get("DRIFT_MODE_B_MAX_PRICE", 0.88))
        mb_prob = float(p.get("DRIFT_MODE_B_MIN_PROB", 0.80))
        mb_edge = float(p.get("DRIFT_MODE_B_MIN_EDGE", 0.05))
        if not (0.0 < mb_min < mb_max <= 0.99):
            errors.append(f"Invalid Mode B price range ({mb_min:.2f}, {mb_max:.2f}]")
        if not (0.50 <= mb_prob <= 0.99) or mb_edge < 0.0:
            errors.append(f"Invalid Mode B prob/edge ({mb_prob:.2f}, {mb_edge:.3f})")
        if budget + 1e-9 < lot_size * mb_max:
            errors.append(
                f"MAX_WINDOW_LOSS_USD={budget:.2f} cannot cover 1 Mode B lot at ${mb_max:.2f}"
            )
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
