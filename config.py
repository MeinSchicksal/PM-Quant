"""PM-Quant Configuration & Single Source of Truth (v3.2).

All model parameters, version identity, and filesystem layout are derived here.
No version logic or branching should exist in downstream modules.
"""
import os
from pathlib import Path

# Semantic Version Identity (v<Major>.<Minor>)
VERSION = "v3.2"
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


# Core Physical & Economic Parameters (v3.1: Equal-Weight Mode A + Sub-$1 Flip + Pre-Trade Knife-Drop Guard + HWM Circuit Breaker)
PARAMS = {
    # 1. Discrete Physical & Lot Constraints
    "LOT_SIZE": 5.0,                  # Exchange minimum order unit (lots must be integer multiples)
    "MIN_ORDER_SHARES": 5.0,          # Hard Polymarket CLOB rule: every placed order must be >= 5.0 shares
    "MIN_LOTS": 1,                    # Minimum entry lots (1 lot = 5 shares)
    "MAX_LOTS": 10,                   # High-frequency window cap per side (50 shares across multi-round trades)
    "MAX_NET_LOTS": 2,                # Max net directional exposure |Up - Down| = 2 lots (10 shares) — cuts MaxDD by 33% while keeping full rolling flexibility
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

    # 2. v3.2 Unlocked Probabilistic Multi-Buy Engine (Taker-First + No Profit-Lock Ceiling)
    "ENABLE_MAKER_ENTRY": True,       # Keep short-lived Maker fallback when Taker edge isn't met & spot is calm
    "DIRECTIONAL_ANTI_TOXICITY": True,# Only block adverse moves; ALLOW Taker IOC in the direction of spot momentum!
    "ENTRY_MIN_TAU_S": 15.0,          # Tail freeze cutoff (tau < 15s: cancel resting quotes, hold inventory)
    "ENTRY_MAX_TAU_S": 285.0,         # Entry active window: start 15s after open (tau in [15s, 285s])
    "ENTRY_MIN_PROB": 0.53,           # Bet leading probability side (P* >= 53%)
    "ENTRY_MIN_PRICE": 0.20,          # Avoid deep out-of-the-money lottery tickets
    "ENTRY_MAX_PRICE": 0.74,          # Avoid >$0.74 low-payoff high-price traps ("概率越高赚的也少")
    "MIN_MAKER_EDGE": 0.045,          # Base edge for zero-fee Maker bids
    "MIN_HOLD_MAKER_EDGE": 0.015,     # Pull resting Maker quote if live edge decays below 1.5%
    "MIN_TAKER_EDGE": 0.035,          # Unlocked Taker IOC net-edge threshold (after fee) for timely execution
    # Mild Odds-Skew Guard (no early-tau penalty):
    "PRICE_EDGE_PIVOT": 0.55,         # No extra price premium at or below $0.55
    "PRICE_EDGE_SLOPE": 0.05,         # Mild +0.5% required edge per +$0.10 above $0.55
    "EARLY_TAU_PIVOT_S": 180.0,
    "EARLY_TAU_MAX_PREMIUM": 0.0,     # Remove early-window time penalty
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
    "STRIKE_WAIT_S": 15.0,            # Require confirmed window strike & basis warmup before opening

    # 2b. Mode B: Skewed-Drift High-Certainty Taker IOC (V1b, zero-impact fallback after Mode A)
    # 已关闭：高价负赔率（$0.75-$0.88 赢小亏大）在低波震荡日易放大尾部回撤，回归等权稳健执行
    "ENABLE_DRIFT_MODE_B": False,
    "DRIFT_MODE_B_MIN_PRICE": 0.75,   # 仅覆盖高于 $0.75 的单边顺势区，与模式 A 零重叠
    "DRIFT_MODE_B_MAX_PRICE": 0.88,   # 高于 $0.88 赔率严重恶化，自动归入模式 C 避险
    "DRIFT_MODE_B_MIN_PROB": 0.80,    # 严格高确定性门槛，彻底封死逆势抄底
    "DRIFT_MODE_B_MIN_EDGE": 0.05,    # V1b 稳健净边际：100ms/500ms/700ms 样本内外 6 象限全胜
    "DRIFT_MODE_B_DISABLE_PYRAMID": True,  # 持有模式 B 高价仓位后严禁二次加仓，锁死单轮尾险

    # 3. Position Management (v3.2: No Profit-Lock Ceiling + High-Freq EV Scaling + Taker-Only Confirmed Flip)
    "ENABLE_ARBITRAGE_HEDGE": False,  # 严格关闭锁利阈值，保留赢家腿奔向 $1.00 的完整赔率凸性
    "ARBITRAGE_HEDGE_MAX_PAIR_COST": 0.90,
    "ENABLE_BLACKSWAN_LOCK": False,   # 严格关闭锁利阈值，不把多次小赢拿去换一次亏损
    "BLACKSWAN_LOCK_MAX_OPP_ASK": 0.06,
    "BLACKSWAN_LOCK_MAX_PAIR_COST": 0.85,
    "ALLOW_REENTRY_AFTER_PAIR": True, # 净敞口归零后允许同窗继续滚动开新轮
    "REENTRY_COOLDOWN_S": 4.0,        # 净敞口归零后再入场冷却 4s
    "ENABLE_PYRAMID_ADD": True,       # 开启同侧概率优势分批加仓（每笔 5 股，受 MAX_NET_LOTS 约束）
    "PYRAMID_ALLOW_AFTER_FLIP": True, # 调仓后重新确立优势仍可继续分批加仓
    "PYRAMID_ALLOW_MAKER": False,     # 加仓仅使用 Taker IOC，不挂被动单
    "PYRAMID_USE_DYNAMIC_EDGE": True, # 与开仓使用统一动态净边际标准
    "SECONDARY_ENTRY_COOLDOWN_S": 4.0,# 高频分批加仓冷却 4s
    "PYRAMID_MAX_TAU_S": 285.0,
    "PYRAMID_MIN_TAU_S": 20.0,
    "PYRAMID_MIN_PROB": 0.54,         # 解开 70% 高价限制：领先侧 P* >= 54% 且跑赢盘口即可分批上车
    "PYRAMID_MIN_PRICE": 0.20,
    "PYRAMID_MAX_PRICE": 0.74,
    "PYRAMID_MAX_SPREAD": 0.10,
    "PYRAMID_MIN_MAKER_EDGE": 0.05,
    "PYRAMID_MIN_TAKER_EDGE": 0.035,
    "ENABLE_REVERSAL_FLIP": True,     # 确认反转后纯 Taker 调仓
    "FLIP_ALLOW_MAKER": False,        # 严禁一反转就挂对面被动 Maker 单！仅确认后吃单调仓
    "FLIP_ALLOW_MULTI": True,
    "MAX_FLIPS_PER_WINDOW": 2,        # 单窗最多允许 2 次确认反转调仓，防锯齿无限横跳
    "FLIP_COOLDOWN_S": 8.0,           # 反转冷却 8s，滤掉瞬间假摔噪音
    "FLIP_MAX_HELD_PROB": 0.42,
    "FLIP_MIN_OPP_PROB": 0.58,        # 对侧确立 58%+ 胜率领先才触发反转调仓
    "FLIP_MAX_PAIR_COST": 2.00,       # 解开 $0.98 配对死锁，按新侧自身独立正 EV 决定是否止血/搬家
    "FLIP_MIN_PRICE": 0.20,
    "FLIP_MAX_PRICE": 0.74,
    "FLIP_MAX_SPREAD": 0.10,
    "FLIP_MIN_MAKER_EDGE": 0.045,
    "FLIP_MIN_TAKER_EDGE": 0.035,
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
    # 现货 WebSocket 数据源: 当前切换为 coinbase (BTC/USD) 进行实测验证；支持环境变量 PM_SPOT_WS_PROVIDER 覆盖 (binance / coinbase / okx / kraken / auto)
    "SPOT_WS_PROVIDER": os.environ.get("PM_SPOT_WS_PROVIDER", "coinbase").strip().lower() or "coinbase",
    "MIN_MODEL_VOL_BPS": 14.0,        # Calibrated empirical 5-min BTC std floor (~14 bps); prevents Phi(d) saturation

    # 5. Lifecycle & Account-Level Watchdog Boundaries (纸面验证阶段关闭熔断，完整暴露策略原生下限)
    "ENABLE_CIRCUIT_BREAKER": False,
    "MAX_ENTRIES_PER_WINDOW": 10,     # 解开同窗 2 次开仓上限，允许高频多轮控仓
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
    "LWAA_MAX_TAU_S": 285.0,
    "LWAA_MIN_PROB": 0.53,
    "LWAA_MIN_EDGE": 0.045,
    "LWAA_MAX_ENTRY_PRICE": 0.74,

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
