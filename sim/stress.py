"""Synthetic Stress-Test Harness (scenario generator + microstructure market simulator).

Purpose
-------
Live paper-trading needs weeks to cover enough market regimes. This harness
compresses that by generating thousands of *synthetic* 5-minute BTC paths across
many regimes and replaying each through the REAL decision/execution code path
(`PaperRunner.step` -> `plan_decision` -> `plan_order_actions` -> `QueueMatcher`).

What it is / is not
-------------------
* It IS a robustness / risk-envelope test: crashes, naked-exposure-at-expiry,
  per-window loss envelope breaches, behaviour under liquidity evaporation,
  stale feeds, basis dislocations, jumps and regime shifts.
* It is NOT evidence of live profitability. PnL here depends on assumptions about
  how the Polymarket book reacts to spot (`MarketModel`). Those assumptions are
  calibrated from recorded data where possible (see constants) and are exposed as
  parameters, with a sensitivity sweep to show how conclusions depend on them.

Calibration sources (recorded data, data/1_market + data/2_ledgers)
-------------------------------------------------------------------
* 5m log-return std  : 12.9 bps over 198 settled windows (95th pct |ret| 26 bps, max 75 bps)
* Book spread        : median 1 tick ($0.01)
* Trade flow         : ~11 prints/s per market, size median 5.5, mean 20 shares
* Tail liquidity     : one-sided books in 27% of snapshots at 60-120s left, 87% under 60s;
                       at extremes the loser token has no bids and the winner token no asks.
"""
import sys
import math
import time
import random
import tempfile
import traceback
from collections import deque, Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.types import OrderBook, SpotState
from core.estimator import (
    calculate_dynamic_realized_vol,
    calculate_adaptive_spread_params,
    normal_cdf,
)
from core.execution import taker_fee_per_share
from core.circuit_breaker import CircuitBreaker
from runtime.runner import PaperRunner

WINDOW_S = 300
PRE_S = 60
OPEN_BIN = 75000.0
BASIS_USD = 100.0                 # Binance - Chainlink basis (constant; cancels in returns)
REAL_SIGMA_5M_BPS = 12.9          # calibrated from 198 settled windows
POLY_LAG_S = 3                    # Chainlink reference lags Binance by ~3s
SENTINEL_PRINT = {"outcome": "Up", "price": 0.999, "size": 0.0}  # forces strict fills (no phantom volume)


# --------------------------------------------------------------------------- #
# Price path generators (log-return of the true underlying vs. open, in bps)
# --------------------------------------------------------------------------- #
@dataclass
class Path:
    x: List[float]                          # 301 values, x[0] = 0
    bin_offset: Optional[List[float]] = None  # Binance-only dislocation (bps)
    stale_s: int = 1                        # Binance feed refresh period


def _walk(rng: random.Random, sigma5m: float, sig_fn: Optional[Callable[[int], float]] = None) -> List[float]:
    out = [0.0]
    for t in range(1, WINDOW_S + 1):
        s = (sig_fn(t) if sig_fn else sigma5m) / math.sqrt(WINDOW_S)
        out.append(out[-1] + rng.gauss(0.0, s))
    return out


def _ou(rng: random.Random, sig_stat: float, tau_rev: float) -> List[float]:
    a = math.exp(-1.0 / tau_rev)
    b = sig_stat * math.sqrt(1.0 - a * a)
    out = [0.0]
    for _ in range(WINDOW_S):
        out.append(out[-1] * a + rng.gauss(0.0, b))
    return out


def _sign(rng: random.Random) -> float:
    return 1.0 if rng.random() < 0.5 else -1.0


def g_brownian(sig: float):
    return lambda rng: Path(_walk(rng, sig))


def g_range(rng):
    return Path(_ou(rng, 8.0, 40.0))


def g_pin(rng):
    return Path(_ou(rng, 3.0, 15.0))


def g_trend(rng):
    d = rng.uniform(15, 35) * _sign(rng)
    w = _walk(rng, 6.0)
    return Path([d * t / WINDOW_S + w[t] for t in range(WINDOW_S + 1)])


def g_late_breakout(rng):
    d = rng.uniform(25, 45) * _sign(rng)
    w = _walk(rng, 4.0)
    return Path([w[t] + (d * (t - 180) / 120.0 if t > 180 else 0.0) for t in range(WINDOW_S + 1)])


def g_fake_breakout(rng):
    a = rng.uniform(12, 22) * _sign(rng)
    n, out = 0.0, [0.0]
    for t in range(1, WINDOW_S + 1):
        if t <= 40:
            base = a * t / 40.0
        elif t <= 60:
            base = a
        elif t <= 150:
            base = a * (1.0 - (t - 60) / 90.0)
        else:
            base = 0.0
        n = 0.8 * n + rng.gauss(0.0, 0.6)
        out.append(base + n)
    return Path(out)


def _flash(rng, retrace: bool):
    w = _walk(rng, 8.0)
    tj = rng.randint(40, 240)
    size = rng.uniform(25, 45) * _sign(rng)
    out = []
    for t in range(WINDOW_S + 1):
        v = w[t]
        if t > tj:
            v += size * min(1.0, (t - tj) / 3.0)
            if retrace:
                v -= 0.6 * size * max(0.0, min(1.0, (t - tj - 10) / 60.0))
        out.append(v)
    return Path(out)


def g_flash_perm(rng):
    return _flash(rng, False)


def g_flash_retrace(rng):
    return _flash(rng, True)


def g_jump_diffusion(rng):
    w = _walk(rng, 9.0)
    jump, out = 0.0, []
    for t in range(WINDOW_S + 1):
        if t > 0 and rng.random() < 2.0 / WINDOW_S:
            jump += rng.gauss(0.0, 12.0)
        out.append(w[t] + jump)
    return Path(out)


def g_vol_shift(rng):
    return Path(_walk(rng, 0.0, sig_fn=lambda t: 4.0 if t < 120 else 35.0))


def g_whipsaw(rng):
    phi = rng.uniform(0, 2 * math.pi)
    n = _ou(rng, 3.0, 10.0)
    return Path([12.0 * (math.sin(2 * math.pi * t / 70.0 + phi) - math.sin(phi)) + n[t] for t in range(WINDOW_S + 1)])


def g_basis_dislocation(rng):
    x = _walk(rng, 10.0)
    t0 = rng.randint(50, 180)
    amp = 20.0 * _sign(rng)
    off = []
    for t in range(WINDOW_S + 1):
        if t < t0 or t > t0 + 46:
            off.append(0.0)
        else:
            off.append(amp * min(1.0, (t - t0) / 3.0, (t0 + 46 - t) / 3.0))
    return Path(x, bin_offset=off)


def g_stale_feed(rng):
    return Path(_walk(rng, REAL_SIGMA_5M_BPS), stale_s=5)


SCENARIOS: Dict[str, Dict] = {
    "calm":               {"gen": g_brownian(6.0),  "desc": "平静布朗 σ=6bps"},
    "normal":             {"gen": g_brownian(REAL_SIGMA_5M_BPS), "desc": "实测常态 σ=12.9bps"},
    "volatile":           {"gen": g_brownian(30.0), "desc": "高波动 σ=30bps"},
    "range":              {"gen": g_range,          "desc": "区间震荡(OU 均值回归)"},
    "pin_at_strike":      {"gen": g_pin,            "desc": "死钉行权价(σ≈3bps 抖动)"},
    "trend":              {"gen": g_trend,          "desc": "稳定单边趋势 15~35bps"},
    "late_breakout":      {"gen": g_late_breakout,  "desc": "尾盘突破(前180s横盘)"},
    "fake_breakout":      {"gen": g_fake_breakout,  "desc": "假突破后全部回吐"},
    "flash_perm":         {"gen": g_flash_perm,     "desc": "闪崩/闪涨 25~45bps 不回头"},
    "flash_retrace":      {"gen": g_flash_retrace,  "desc": "闪崩后回补60%"},
    "jump_diffusion":     {"gen": g_jump_diffusion, "desc": "跳跃扩散(泊松跳跃)"},
    "vol_shift":          {"gen": g_vol_shift,      "desc": "波动率突变 4→35bps"},
    "whipsaw":            {"gen": g_whipsaw,        "desc": "反复穿越行权价(正弦+噪声)"},
    "basis_dislocation":  {"gen": g_basis_dislocation, "desc": "Binance 单所basis错位 ±20bps"},
    "stale_feed":         {"gen": g_stale_feed,     "desc": "行情每5秒才更新一次"},
}

SEQUENCE_WEIGHTS = {
    "normal": 30, "range": 20, "calm": 10, "trend": 10, "jump_diffusion": 8,
    "flash_perm": 5, "fake_breakout": 5, "late_breakout": 4, "whipsaw": 4,
    "vol_shift": 2, "pin_at_strike": 2,
}


def build_prefix(rng: random.Random) -> List[float]:
    """60s pre-window history (t=-60..-1) so the volatility estimator is warm at t=0."""
    s = REAL_SIGMA_5M_BPS / math.sqrt(WINDOW_S)
    vals = [0.0]
    for _ in range(PRE_S):
        vals.append(vals[-1] + rng.gauss(0.0, s))
    return vals[1:][::-1]


# --------------------------------------------------------------------------- #
# Polymarket book / flow simulator
# --------------------------------------------------------------------------- #
@dataclass
class MarketModel:
    lag_s: int = 2                 # how many seconds the book lags the underlying
    sigma_bps: float = REAL_SIGMA_5M_BPS   # volatility the market prices with
    noise_sd: float = 0.006
    noise_ar: float = 0.9
    flow_per_token_s: float = 5.5
    flow_scale: float = 1.0
    size_median: float = 5.5
    size_sigma: float = 1.6
    depth_median: float = 100.0
    depth_sigma: float = 0.9
    informed_k: float = 6.0        # order-flow imbalance following recent mid moves
    evap_hi: float = 0.88
    evap_p_2m: float = 0.60
    evap_p_1m: float = 0.92


def _poisson(rng: random.Random, lam: float) -> int:
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


class MarketSim:
    def __init__(self, mm: MarketModel, rng: random.Random):
        self.mm, self.rng = mm, rng
        self.noise = 0.0
        self.mid_hist = [0.5] * 4
        self.evap = False
        self.last_px = {"Up": 0.5, "Down": 0.5}

    def _depth(self) -> float:
        return self.rng.lognormvariate(math.log(self.mm.depth_median), self.mm.depth_sigma)

    @staticmethod
    def _lvl(levels):
        return [(round(p, 2), s) for p, s in levels if 0.0 < p < 1.0]

    def tick(self, t: int, mkt_x: float):
        mm, rng = self.mm, self.rng
        tau = WINDOW_S - t
        sig = mm.sigma_bps / 1e4 * math.sqrt(max(tau, 1) / WINDOW_S)
        fair = normal_cdf((mkt_x / 1e4) / sig)
        self.noise = mm.noise_ar * self.noise + rng.gauss(0.0, mm.noise_sd)
        mid = min(0.98, max(0.02, fair + self.noise))
        self.mid_hist.append(mid)

        ub = min(0.98, max(0.01, math.floor(mid * 100 - 0.5 + 1e-9) / 100.0))
        ua = round(ub + 0.01, 2)
        d1, d2, d3, d4 = self._depth(), self._depth(), self._depth(), self._depth()
        up_bids = self._lvl([(ub, d1), (ub - 0.01, d2)])
        up_asks = self._lvl([(ua, d3), (ua + 0.01, d4)])
        dn_bids = self._lvl([(1 - ua, d3), (1 - ua - 0.01, d4)])
        dn_asks = self._lvl([(1 - ub, d1), (1 - ub + 0.01, d2)])

        extreme = mid >= mm.evap_hi or mid <= 1 - mm.evap_hi
        if tau <= 120 and extreme:
            if t % 5 == 0:
                self.evap = rng.random() < (mm.evap_p_1m if tau < 60 else mm.evap_p_2m)
            if self.evap:
                if mid >= mm.evap_hi:
                    up_asks, dn_bids = [], []
                else:
                    up_bids, dn_asks = [], []
        else:
            self.evap = False

        dmid = mid - self.mid_hist[-4]
        prints = []
        for token, bids, asks in (("Up", up_bids, up_asks), ("Down", dn_bids, dn_asks)):
            p_sell = 0.5 - mm.informed_k * dmid if token == "Up" else 0.5 + mm.informed_k * dmid
            p_sell = min(0.95, max(0.05, p_sell))
            for _ in range(_poisson(rng, mm.flow_per_token_s * mm.flow_scale)):
                size = rng.lognormvariate(math.log(mm.size_median), mm.size_sigma)
                if rng.random() < p_sell and bids:
                    price = bids[0][0]
                elif asks:
                    price = asks[0][0]
                else:
                    continue
                prints.append({"outcome": token, "price": price, "size": size})
                self.last_px[token] = price

        b_up = OrderBook(bids=up_bids, asks=up_asks, last_trade_price=self.last_px["Up"])
        b_dn = OrderBook(bids=dn_bids, asks=dn_asks, last_trade_price=self.last_px["Down"])
        return b_up, b_dn, prints


# --------------------------------------------------------------------------- #
# Single-window simulation through the real runner
# --------------------------------------------------------------------------- #
def simulate_window(
    runner: PaperRunner,
    path: Path,
    prefix: List[float],
    mm: MarketModel,
    rng: random.Random,
    t0: float,
    hist: deque,
    slug: str,
    persistent: bool = False,
) -> Dict:
    bin_open = OPEN_BIN
    poly_open = OPEN_BIN - BASIS_USD
    x = path.x

    def bin_px(t: int) -> float:
        if t < 0:
            val = prefix[t + PRE_S]
        else:
            ts = (t // path.stale_s) * path.stale_s
            val = x[ts] + (path.bin_offset[t] if path.bin_offset else 0.0)
        return bin_open * math.exp(val / 1e4)

    def poly_px(t: int) -> float:
        return poly_open * math.exp(x[max(0, t - POLY_LAG_S)] / 1e4)

    if not hist:
        for t in range(-PRE_S, 0):
            hist.append((t0 + t + 0.46, bin_px(t)))

    runner.start_window(slug, t0, t0 + WINDOW_S, poly_open, binance_open_px=bin_open, poly_spot=poly_open)
    ms = MarketSim(mm, rng)
    shock_bps = config.PARAMS["TOXICITY_SHOCK_BPS"]

    for t in range(WINDOW_S):
        now = t0 + t + 0.46
        bpx, ppx = bin_px(t), poly_px(t)
        hist.append((now, bpx))
        vel = 0.0
        for old_t, old_px in reversed(hist):
            if now - old_t >= 2.5:
                vel = (bpx - old_px) / old_px * 1e4
                break
        rv = calculate_dynamic_realized_vol(list(hist), baseline_vol_bps=15.0)
        _, _, regime = calculate_adaptive_spread_params(rv)
        spot = SpotState(
            timestamp=now, price=bpx, open_price=poly_open,
            return_bps=(bpx - bin_open) / bin_open * 1e4,
            realized_vol_bps=rv, is_toxic_shock=abs(vel) >= shock_bps, regime=regime,
            poly_price=ppx, binance_open_price=bin_open,
        )
        b_up, b_dn, prints = ms.tick(t, x[max(0, t - mm.lag_s)])
        prints.append(SENTINEL_PRINT)
        # Synthetic one-sided books model liquidity evaporation, not a feed
        # outage; keep the wall-clock dead-man switch out of the stress loop.
        runner.step(now, spot, b_up, b_dn, trade_prints=prints,
                    poly_spot=ppx, replay_mode=True)

    poly_close = poly_px(WINDOW_S)
    winner = "Up" if poly_close >= poly_open else "Down"
    up, dn = runner.pos_up, runner.pos_down
    cost = up.cash + dn.cash
    pnl = (up.shares if winner == "Up" else dn.shares) * 1.0 - cost
    fills = list(runner.fills)
    fees = sum(f["size"] * taker_fee_per_share(f["price"], config.PARAMS["TAKER_FEE_RATE"])
               for f in fills if f["kind"] == "taker")
    n_taker = sum(1 for f in fills if f["kind"] == "taker")
    if persistent:
        runner.close_window(poly_close, official_winner=winner)

    if not fills:
        cat = "no_trade"
    elif abs(up.shares - dn.shares) > 1e-6:
        cat = "naked_at_expiry"
    elif up.shares > 1e-6:
        cat = "paired"
    else:
        cat = "unwound_flat"
    return {"pnl": round(pnl, 4), "cat": cat, "fees": fees, "n_taker": n_taker,
            "naked_shares": abs(up.shares - dn.shares), "fills": len(fills), "winner": winner}


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def _stats(res: List[Dict], errors: int) -> Dict:
    n = len(res)
    if n == 0:
        return {"n": 0, "errors": errors}
    pnls = sorted(r["pnl"] for r in res)
    mean = sum(pnls) / n
    sd = math.sqrt(sum((p - mean) ** 2 for p in pnls) / max(1, n - 1))
    se = sd / math.sqrt(n)
    cats = Counter(r["cat"] for r in res)
    cat_mean = {}
    for c in cats:
        v = [r["pnl"] for r in res if r["cat"] == c]
        cat_mean[c] = sum(v) / len(v)
    envelope = config.PARAMS.get("MAX_WINDOW_LOSS_USD", 2.0)
    lo, hi = mean - 1.96 * se, mean + 1.96 * se
    return {
        "n": n, "errors": errors, "mean": mean, "sd": sd, "ci_lo": lo, "ci_hi": hi,
        "p5": pnls[max(0, int(0.05 * n) - 1)], "worst": pnls[0], "best": pnls[-1],
        "trade_rate": 1.0 - cats.get("no_trade", 0) / n,
        "breach": sum(1 for p in pnls if p < -envelope) / n,
        "naked_rate": cats.get("naked_at_expiry", 0) / n,
        "cats": {c: cats[c] / n for c in cats}, "cat_mean": cat_mean,
        "fees": sum(r["fees"] for r in res) / n,
        "verdict": "盈利" if lo > 0 else ("亏损" if hi < 0 else "中性"),
    }


def _new_runner(tmp: str) -> PaperRunner:
    r = PaperRunner(ledger_dir=tmp)
    r._write_live = lambda *a, **k: None   # no per-tick disk IO in simulation
    return r


def run_scenario(name: str, n: int, seed: int, mm: MarketModel, runner: PaperRunner):
    gen = SCENARIOS[name]["gen"]
    results, errors, first_tb = [], 0, None
    for i in range(n):
        rng = random.Random(f"{name}|{seed}|{i}")
        runner.circuit_breaker = CircuitBreaker(
            max_daily_drawdown_usd=config.PARAMS.get("MAX_DAILY_DRAWDOWN_USD", 8.0),
            loss_streak_cooling=int(config.PARAMS.get("LOSS_STREAK_COOLING", 3)),
            cooling_rounds=int(config.PARAMS.get("COOLING_ROUNDS", 3)),
            # Match the live engine. Omitting `enabled` silently defaulted to
            # True here while config had it disabled, making stress results
            # look safer than the real bankroll behaviour.
            enabled=bool(config.PARAMS.get("ENABLE_CIRCUIT_BREAKER", True)))
        runner.total_pnl = 0.0
        try:
            path = gen(rng)
            res = simulate_window(runner, path, build_prefix(rng), mm, rng,
                                  1_800_000_000.0, deque(maxlen=60), f"sim-{name}-{i}")
            results.append(res)
        except Exception:
            errors += 1
            if first_tb is None:
                first_tb = traceback.format_exc()
    st = _stats(results, errors)
    st["traceback"] = first_tb
    return st


def run_sequence(seed: int, windows: int, mm: MarketModel) -> Dict:
    """Consecutive windows through ONE runner: exercises circuit breaker / cooling / bankroll."""
    rng = random.Random(f"seq|{seed}")
    names = list(SEQUENCE_WEIGHTS)
    weights = [SEQUENCE_WEIGHTS[k] for k in names]
    with tempfile.TemporaryDirectory() as tmp:
        runner = _new_runner(tmp)
        hist = deque(maxlen=60)
        equity, peak, max_dd, streak, max_streak, traded, trips = 0.0, 0.0, 0.0, 0, 0, 0, 0
        was_tripped = False
        for i in range(windows):
            name = rng.choices(names, weights)[0]
            prng = random.Random(f"seq|{seed}|{i}")
            path = SCENARIOS[name]["gen"](prng)
            res = simulate_window(runner, path, build_prefix(prng), mm, prng,
                                  1_800_000_000.0 + i * WINDOW_S, hist, f"seq-{seed}-{i}", persistent=True)
            if res["cat"] != "no_trade":
                traded += 1
                equity += res["pnl"]
                streak = streak + 1 if res["pnl"] < -1e-4 else 0
                max_streak = max(max_streak, streak)
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
            tripped = runner.circuit_breaker.is_active(1_800_000_000.0 + (i + 1) * WINDOW_S)[0]
            if tripped and not was_tripped:
                trips += 1
            was_tripped = tripped
        return {"final": equity, "max_dd": max_dd, "traded": traded, "max_loss_streak": max_streak, "trips": trips}


# --------------------------------------------------------------------------- #
# Reporting / CLI
# --------------------------------------------------------------------------- #
def _fmt_row(name: str, desc: str, s: Dict) -> str:
    if s.get("n", 0) == 0:
        return f"| {name} | {desc} | 全部异常 ({s.get('errors')}) |"
    err = f" ⚠异常×{s['errors']}" if s["errors"] else ""
    flag = " ⚠" if s["breach"] > 0.05 else ""
    return (f"| {name} | {desc} | {s['trade_rate']*100:4.0f}% | {s['mean']:+.3f} "
            f"[{s['ci_lo']:+.3f},{s['ci_hi']:+.3f}] | {s['sd']:.2f} | {s['worst']:+.2f} | "
            f"{s['breach']*100:4.1f}%{flag} | {s['naked_rate']*100:4.1f}% | {s['verdict']}{err} |")


def run_stress(n_paths: int = 150, seed: int = 7, sensitivity: bool = True, sequences: int = 12, seq_windows: int = 60):
    t_start = time.time()
    mm = MarketModel()
    lines: List[str] = []
    out = lines.append
    out(f"# PM-Quant 合成行情压力测试报告 ({config.VERSION})")
    out(f"\n- 每场景 {n_paths} 条路径 (seed={seed})，真实 `PaperRunner.step` 链路，严格成交模型(仅真实打印+穿价)")
    out(f"- 市场模型: lag={mm.lag_s}s, σ_mkt={mm.sigma_bps}bps, 流量 {mm.flow_per_token_s}/s/token, 价差1tick")
    out(f"- 参数: MWDE tau∈({config.PARAMS.get('MWDE_MIN_TAU_S'):.0f},{config.PARAMS.get('MWDE_MAX_TAU_S'):.0f}]s 顺势低吸, "
        f"LWAA tau∈[{config.PARAMS.get('LWAA_MIN_TAU_S'):.0f},{config.PARAMS.get('LWAA_MAX_TAU_S'):.0f}]s 且 P*≥{config.PARAMS.get('LWAA_MIN_PROB')} 高确信吸收, "
        f"首腿 {config.PARAMS['LOT_SIZE']*config.PARAMS['MIN_LOTS']:.0f} 份, 单窗亏损包络 ${config.PARAMS.get('MAX_WINDOW_LOSS_USD')}")
    out("\n> 注意: 这是鲁棒性/风险包络测试，不是实盘盈利证明。PnL 取决于 MarketModel 假设，见下方敏感性分析。\n")

    out("## 1. 场景矩阵 (单窗 PnL, 美元; 括号为均值 95% 置信区间)\n")
    out("| 场景 | 描述 | 成交率 | 均值PnL [95%CI] | 标准差 | 最差 | 亏>$2占比 | 裸奔到期率 | 结论 |")
    out("|---|---|---|---|---|---|---|---|---|")
    all_stats: Dict[str, Dict] = {}
    with tempfile.TemporaryDirectory() as tmp:
        runner = _new_runner(tmp)
        for name, meta in SCENARIOS.items():
            s = run_scenario(name, n_paths, seed, mm, runner)
            all_stats[name] = s
            out(_fmt_row(name, meta["desc"], s))
            print(f"  [{name:<18}] 完成  成交率={s.get('trade_rate', 0)*100:4.0f}%  均值={s.get('mean', 0):+.3f}  "
                  f"最差={s.get('worst', 0):+.2f}  异常={s.get('errors')}", flush=True)

        out("\n## 2. 单窗结局分解 (占比 / 该类均值PnL)\n")
        out("| 场景 | 无成交 | 完美配对 | 止损砍仓 | 裸奔到期 | 平均吃单手续费 |")
        out("|---|---|---|---|---|---|")
        for name, s in all_stats.items():
            if s.get("n", 0) == 0:
                continue
            def cell(k):
                if k not in s["cats"]:
                    return "-"
                return f"{s['cats'][k]*100:.0f}% / {s['cat_mean'][k]:+.2f}"
            out(f"| {name} | {cell('no_trade')} | {cell('paired')} | {cell('unwound_flat')} | {cell('naked_at_expiry')} | ${s['fees']:.4f} |")

        errs = [(n, s["traceback"]) for n, s in all_stats.items() if s.get("traceback")]
        if errs:
            out("\n## ⚠ 运行时异常 (鲁棒性缺陷)\n")
            for n, tb in errs:
                out(f"**{n}**\n```\n{tb}\n```")

        if sensitivity:
            out("\n## 3. 敏感性分析: 结论是否依赖市场模型假设?\n")
            out("| 场景 | 变体 | 成交率 | 均值PnL [95%CI] | 最差 | 亏>$2占比 | 结论 |")
            out("|---|---|---|---|---|---|---|")
            variants = [
                ("lag=0s(市场与现货同步)", replace(mm, lag_s=0)),
                ("lag=2s(基准)", mm),
                ("lag=5s", replace(mm, lag_s=5)),
                ("lag=10s(市场迟钝)", replace(mm, lag_s=10)),
                ("流量×0.25(排队更慢)", replace(mm, flow_scale=0.25)),
                ("市场σ=8(低估波动)", replace(mm, sigma_bps=8.0)),
                ("市场σ=20(高估波动)", replace(mm, sigma_bps=20.0)),
            ]
            for name in ("normal", "range", "trend", "flash_perm"):
                for label, m2 in variants:
                    s = run_scenario(name, max(60, n_paths // 2), seed + 1, m2, runner)
                    if s.get("n", 0) == 0:
                        continue
                    out(f"| {name} | {label} | {s['trade_rate']*100:3.0f}% | {s['mean']:+.3f} "
                        f"[{s['ci_lo']:+.3f},{s['ci_hi']:+.3f}] | {s['worst']:+.2f} | {s['breach']*100:4.1f}% | {s['verdict']} |")
                print(f"  [敏感性 {name}] 完成", flush=True)

    out(f"\n## 4. 连续{seq_windows}窗混合行情 × {sequences} 条序列 (熔断/冷却/资金曲线)\n")
    seqs = [run_sequence(seed * 1000 + k, seq_windows, mm) for k in range(sequences)]
    finals = sorted(s["final"] for s in seqs)
    dds = sorted(s["max_dd"] for s in seqs)
    out(f"- 终值 PnL: 均值 {sum(finals)/len(finals):+.2f} | 最差 {finals[0]:+.2f} | 最好 {finals[-1]:+.2f} | "
        f"亏损序列占比 {sum(1 for f in finals if f < 0)/len(finals)*100:.0f}%")
    out(f"- 最大回撤: 中位 ${dds[len(dds)//2]:.2f} | 最差 ${dds[-1]:.2f} (熔断日回撤上限 ${config.PARAMS.get('MAX_DAILY_DRAWDOWN_USD')})")
    out(f"- 平均成交窗数 {sum(s['traded'] for s in seqs)/len(seqs):.1f}/{seq_windows} | "
        f"最长连亏 {max(s['max_loss_streak'] for s in seqs)} | 熔断触发总次数 {sum(s['trips'] for s in seqs)}")

    out(f"\n_耗时 {time.time()-t_start:.0f}s_")
    report = "\n".join(lines)
    rep_dir = config.REPORTS_DIR
    rep_dir.mkdir(parents=True, exist_ok=True)
    path = rep_dir / f"stress_{time.strftime('%Y%m%d_%H%M%S')}.md"
    path.write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"\n[*] 报告已保存: {path}")
    return all_stats


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    sd = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    run_stress(n, sd)
