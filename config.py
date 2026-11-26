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

    # 2. v3.3 Clean Entry Engine (Taker-First + Calm Maker Fallback + Calibrated Raw t5)
    "MODEL_T5_UNIT_VARIANCE": False,  # 关闭 sqrt(5/3) 放大：避免中段 P* 虚高 +3%~+5.5% 击穿 0.035 边际导致开盘抢跑与卡点早加仓
    "ENABLE_MAKER_ENTRY": True,       # Keep zero-fee Maker fallback when Taker edge isn't met & spot is calm
    "DIRECTIONAL_ANTI_TOXICITY": True,# Only block adverse moves; ALLOW Taker IOC in the direction of spot momentum
    "ENTRY_MIN_TAU_S": 15.0,          # Tail freeze cutoff (tau < 15s: cancel resting quotes, hold inventory)
    "LATE_NO_NEW_ENTRY_TAU_S": 30.0,  # Final 30s: stop opening fresh positions / adds (allow payoff-driven flip rescue only)
    "EARLY_PHASE_MIN_TAU_S": 240.0,   # First 60s (tau > 240s): light probe phase capped at 5 shares (block early noise overload)
    "ENTRY_MAX_TAU_S": 280.0,         # Entry active window: start 20s after open (tau in [15s, 280s])
    "ENTRY_MIN_PROB": 0.53,           # Primary leading-side probability threshold
    "ENTRY_MIN_PRICE": 0.20,          # Avoid deep out-of-the-money lottery tickets
    "ENTRY_MAX_PRICE": 0.74,          # Avoid >$0.74 low-payoff high-price traps (v3.2 baseline)
    "MIN_MAKER_EDGE": 0.055,          # Thick safety cushion for zero-fee Maker bids (strictly > MIN_TAKER_EDGE)
    "MIN_HOLD_MAKER_EDGE": 0.020,     # Pull resting Maker quote if live edge decays below 2.0%
    "MIN_TAKER_EDGE": 0.035,          # Post-fee Taker IOC net-edge threshold (v3.2 baseline)
    # Mild Odds-Skew Protection (v3.2 baseline):
    "PRICE_EDGE_PIVOT": 0.55,         # No extra price premium at or below $0.55
    "PRICE_EDGE_SLOPE": 0.05,         # +0.5% required edge per +$0.10 above $0.55
    "EARLY_TAU_PIVOT_S": 240.0,
    "EARLY_TAU_MAX_PREMIUM": 0.0,     # Disabled: first-60s 5-share cap already handles early-window noise
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

    # 3. Position Management (v3.3: Trend-Confirming Add + Breakeven-Gated Dynamic Flip)
    "ALLOW_REENTRY_AFTER_PAIR": False,# 完成反手后不再同窗反复重开，防锯齿磨损
    "REENTRY_COOLDOWN_S": 10.0,
    "ENABLE_PYRAMID_ADD": True,       # 开启中盘顺势加仓（防下跌摊平 + 前 60s 禁加仓）
    "PYRAMID_ALLOW_AFTER_FLIP": False,# 反手调仓后禁止继续加仓
    "PYRAMID_ALLOW_MAKER": False,     # 加仓仅使用 Taker IOC
    "PYRAMID_USE_DYNAMIC_EDGE": True, # 与开仓使用统一动态净边际标准
    "SECONDARY_ENTRY_COOLDOWN_S": 6.0,# 加仓冷却 6s，避免同帧信号机械翻倍
    "PYRAMID_MAX_TAU_S": 240.0,       # 早盘前 60s 严禁加仓到 10 股，释放 60s 后中盘趋势加仓窗口
    "PYRAMID_MIN_TAU_S": 30.0,        # 尾盘最后 30s 禁止顺势加仓
    "PYRAMID_MIN_PROB": 0.54,         # 己侧概率保持健康（防反转低价送钱）
    "PYRAMID_MAX_PROB_DROP": 0.06,    # 相对首仓开仓 P* 下跌不得超 6%，防止反转下跌途中摊平送钱
    "PYRAMID_MIN_PRICE": 0.20,
    "PYRAMID_MAX_PRICE": 0.74,        # 与 ENTRY_MAX_PRICE 对齐，允许高胜率趋势段顺势加仓
    "PYRAMID_MAX_ABOVE_AVG": 0.15,    # 允许合理顺势加仓（防极端追高但不误杀趋势确认加仓）
    "PYRAMID_MAX_SPREAD": 0.08,
    "PYRAMID_MIN_MAKER_EDGE": 0.06,
    "PYRAMID_MIN_TAKER_EDGE": 0.035,  # 与开仓基础边际一致，消除伪精度碎步微调
    "ENABLE_REVERSAL_FLIP": True,     # 确认反转后按终局保本反算整数股，一步到位 Taker 调仓
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
    "FLIP_MIN_TAKER_EDGE": 0.035,     # 与基础 Taker 边际对齐
    "FLIP_USE_DYNAMIC_EDGE": False,   # 反手不再叠加高价动态边际惩罚，避免错过最佳翻面时机
    "FLIP_PAYOFF_SIZING": True,       # 终局导向动态整数股（支持 5~12 股一次买够，不再死守 5 股）
    "FLIP_REQUIRE_BREAKEVEN": True,   # 硬护栏：上限内若无法达到终局保本则放弃反手，杜绝半吊子锁仓
    "FLIP_TARGET_PROFIT_USD": 0.0,    # 终局目标仅求保本，不强行多买额外敞口
    "FLIP_MAX_SHARES": 12.0,          # 单次反手上限 12 股（可完整覆盖 10 股中盘仓位翻面）
    "FLIP_MAX_TOTAL_WINDOW_COST": 14.0,# 含反手单窗总投入上限 $14.00（与 10 股持仓翻面保本配平）

    # 4. Realism & Data Integrity Guards (Zero Self-Deception)
    "STRICT_BOOK_CONSISTENCY": True,  # Reject inconsistent/crossed snapshots instead of shifting prices artificially
    "TAKER_REAL_DELAY_CHECK": True,   # Wait wire latency and match natively on live post-delay book (no post-arrival reject)
    "POLY_PRICE_MAX_AGE_S": 10.0,     # Ignore cached Chainlink TWAP older than 10s
    "BASIS_MAX_AGE_S": 120.0,         # Expire spot-Chainlink basis EMA if TWAP silent > 120s
    # 现货 WebSocket 数据源: 固定默认 coinbase (BTC/USD，服务器延迟最低且权威)；支持 PM_SPOT_WS_PROVIDER 环境变量覆盖
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
    max_px = p["ENTRY_MAX_PRICE"]
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
