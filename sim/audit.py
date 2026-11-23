"""PM-Quant 账本科学体检引擎 (Scientific Ledger Audit & Diagnostics).

支持直接体检本地账本目录、summary.jsonl 文件，或远程导出的 .tar.gz 压缩包。
核心输出：
1. 盈亏总览、单样本 t 统计量、95% 置信区间与最大回撤（含样本量诚实提示）
2. 独立 Alpha 基准：Brier Score（模型 P* vs 盘口隐含概率，旧账本自动从 windows/*.json.gz 回溯补算）
3. 概率校准表（按预测置信度分桶对比预测胜率 vs 实际胜率）
4. 执行结构拆解（首仓 Maker vs Taker、顺势加仓、反手配对、锁利/止损）
5. 按自然日 (UTC+8) 稳定性切片与网络延迟拓扑健康度
"""
import datetime
import gzip
import json
import math
import tarfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_TZ_BJ = datetime.timezone(datetime.timedelta(hours=8))


def _safe_float(val: Any, default: Optional[float] = None) -> Optional[float]:
    if val is None:
        return default
    try:
        out = float(val)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _load_source(target: Path) -> Tuple[List[dict], Dict[str, dict], Optional[dict], str]:
    """加载 summary 记录、windows 详情字典与可选的 live.json。"""
    records: List[dict] = []
    details: Dict[str, dict] = {}
    live_data: Optional[dict] = None
    label = str(target)

    if target.is_file() and (target.name.endswith(".tar.gz") or target.name.endswith(".tgz")):
        with tarfile.open(target, "r:gz") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                norm = member.name.replace("\\", "/")
                base = Path(norm).name
                if base == "summary.jsonl":
                    f = tf.extractfile(member)
                    if f is not None:
                        for raw_line in f.read().decode("utf-8", errors="ignore").splitlines():
                            if not raw_line.strip():
                                continue
                            try:
                                rec = json.loads(raw_line)
                                if isinstance(rec, dict) and not str(rec.get("slug", "")).startswith("test_"):
                                    records.append(rec)
                            except (json.JSONDecodeError, ValueError):
                                continue
                elif base == "live.json":
                    f = tf.extractfile(member)
                    if f is not None:
                        try:
                            live_data = json.loads(f.read().decode("utf-8", errors="ignore"))
                        except (json.JSONDecodeError, ValueError):
                            pass
                elif norm.endswith(".json.gz") and "/windows/" in f"/{norm}":
                    f = tf.extractfile(member)
                    if f is not None:
                        try:
                            raw_gz = f.read()
                            det = json.loads(gzip.decompress(raw_gz).decode("utf-8", errors="ignore"))
                            slug = str(det.get("slug") or base.replace(".json.gz", ""))
                            if slug:
                                details[slug] = det
                        except Exception:
                            continue
        return records, details, live_data, label

    if target.is_file() and target.name.endswith(".jsonl"):
        ledger_dir = target.parent
        summary_file = target
    else:
        ledger_dir = target
        summary_file = ledger_dir / "summary.jsonl"

    if summary_file.exists():
        with open(summary_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    if isinstance(rec, dict) and not str(rec.get("slug", "")).startswith("test_"):
                        records.append(rec)
                except (json.JSONDecodeError, ValueError):
                    continue

    live_file = ledger_dir / "live.json"
    if live_file.exists():
        try:
            live_data = json.loads(live_file.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            pass

    win_dir = ledger_dir / "windows"
    if win_dir.exists() and win_dir.is_dir():
        for gz_path in sorted(win_dir.glob("*.json.gz")):
            try:
                with gzip.open(gz_path, "rt", encoding="utf-8", errors="ignore") as f:
                    det = json.load(f)
                slug = str(det.get("slug") or gz_path.name.replace(".json.gz", ""))
                if slug:
                    details[slug] = det
            except Exception:
                continue

    return records, details, live_data, label


def _calc_t_stat(vals: List[float]) -> Tuple[float, float, float, float, float]:
    """返回 (mean, sd, t_stat, ci_low, ci_high)。"""
    n = len(vals)
    if n == 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    mean = sum(vals) / n
    if n < 2:
        return mean, 0.0, 0.0, mean, mean
    var = sum((x - mean) ** 2 for x in vals) / (n - 1)
    sd = math.sqrt(max(0.0, var))
    se = sd / math.sqrt(n)
    t_stat = (mean / se) if se > 1e-12 else 0.0
    return mean, sd, t_stat, mean - 1.96 * se, mean + 1.96 * se


def _extract_mid_snapshot_from_detail(det: dict) -> Tuple[Optional[float], Optional[float]]:
    """对旧账本（无 brier 字段）从 price_history 的 tau<=150s 首个有效点补算 (p_up, mkt_up)。"""
    start_t = _safe_float(det.get("start"), 0.0) or 0.0
    end_t = _safe_float(det.get("end"), start_t + 300.0 if start_t > 0 else 0.0) or 0.0
    hist = det.get("price_history") or []
    if not isinstance(hist, list) or end_t <= 0:
        return None, None
    for pt in hist:
        if not isinstance(pt, dict):
            continue
        t_pt = _safe_float(pt.get("t"))
        if t_pt is None:
            continue
        tau = end_t - t_pt
        if tau <= 150.0:
            p_m = _safe_float(pt.get("prob_up"))
            p_k = _safe_float(pt.get("poly_up"))
            if p_m is not None and p_k is not None and 0.0 <= p_m <= 1.0 and 0.0 <= p_k <= 1.0:
                return p_m, p_k
    return None, None


def audit_ledger(target: Path) -> Dict[str, Any]:
    """对指定账本目录 / summary.jsonl / .tar.gz 执行全面科学体检。"""
    target = Path(target)
    records, details, live_data, label = _load_source(target)

    # 若 summary 缺失但有 windows 详情，兜底从 details 重建记录列表
    if not records and details:
        records = sorted(details.values(), key=lambda x: float(x.get("start", 0.0) or 0.0))

    total_windows = len(records)
    traded_pnls: List[float] = []
    wins = 0
    losses = 0
    flats = 0
    total_won = 0.0
    total_lost = 0.0
    turnover = 0.0
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    gamma_verified = 0

    brier_model_list: List[float] = []
    brier_market_list: List[float] = []
    calib_samples: List[Tuple[float, int]] = []  # (p_pred_side >= 0.5, hit_1_or_0)

    daily: Dict[str, Dict[str, Any]] = {}
    exec_buckets: Dict[str, List[float]] = {
        "maker_leg1": [],
        "taker_leg1": [],
        "pyramid_windows": [],
        "flip_windows": [],
        "hedge_or_lock": [],
        "salvage_unwind": [],
    }
    first_fill_count = 0

    for rec in records:
        slug = str(rec.get("slug") or "")
        det = details.get(slug)
        winner = rec.get("winner") or (det.get("winner") if det else None)
        if rec.get("winner_source") == "gamma":
            gamma_verified += 1

        # 1. Brier Score 与校准样本（全窗口覆盖，避免只看交易窗带来的选择偏差）
        if winner in ("Up", "Down"):
            y_up = 1.0 if winner == "Up" else 0.0
            bm = _safe_float(rec.get("brier_model"))
            bk = _safe_float(rec.get("brier_market"))
            p_up = _safe_float(rec.get("mid_p_up"))
            m_up = _safe_float(rec.get("mid_mkt_up"))
            if (bm is None or bk is None or p_up is None) and det is not None:
                p_fb, m_fb = _extract_mid_snapshot_from_detail(det)
                if p_up is None:
                    p_up = p_fb
                if m_up is None:
                    m_up = m_fb
                if bm is None and p_up is not None:
                    bm = (p_up - y_up) ** 2
                if bk is None and m_up is not None:
                    bk = (m_up - y_up) ** 2
            if bm is not None and bk is not None:
                brier_model_list.append(bm)
                brier_market_list.append(bk)
            if p_up is not None:
                if p_up >= 0.5:
                    calib_samples.append((p_up, 1 if winner == "Up" else 0))
                else:
                    calib_samples.append((1.0 - p_up, 1 if winner == "Down" else 0))

        # 2. 交易盈亏与按日统计
        fills_c = int(_safe_float(rec.get("fills_count"), 0.0) or 0)
        pnl = float(_safe_float(rec.get("net_pnl"), 0.0) or 0.0)
        is_traded = bool(rec.get("traded") or fills_c > 0 or abs(pnl) > 1e-9)
        if not is_traded:
            continue

        traded_pnls.append(pnl)
        turnover += float(_safe_float(rec.get("total_cost"), 0.0) or 0.0)
        running += pnl
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
        if pnl > 1e-6:
            wins += 1
            total_won += pnl
        elif pnl < -1e-6:
            losses += 1
            total_lost += abs(pnl)
        else:
            flats += 1

        ts = float(_safe_float(rec.get("end"), _safe_float(rec.get("start"), 0.0)) or 0.0)
        day_k = datetime.datetime.fromtimestamp(ts, _TZ_BJ).strftime("%Y-%m-%d") if ts > 0 else "unknown"
        db = daily.setdefault(day_k, {"date": day_k, "pnls": [], "wins": 0, "losses": 0, "pnl": 0.0})
        db["pnls"].append(pnl)
        db["pnl"] = round(db["pnl"] + pnl, 4)
        if pnl > 1e-6:
            db["wins"] += 1
        elif pnl < -1e-6:
            db["losses"] += 1

        # 3. 成交结构拆解（依赖 windows/*.json.gz；兼容 kind/mode 字段及旧版无 reason 账本）
        if det and isinstance(det.get("fills"), list) and det["fills"]:
            fills = det["fills"]
            first_fill_count += 1
            f0 = fills[0]
            mode0 = str(f0.get("kind") or f0.get("mode") or "").lower()
            if mode0 == "maker":
                exec_buckets["maker_leg1"].append(pnl)
            elif mode0 == "taker":
                exec_buckets["taker_leg1"].append(pnl)
            reasons = " | ".join(
                str(f.get("reason") or "") for f in fills
            ) + " | " + " | ".join(
                str(a.get("reason") or "") for a in (det.get("actions") or []) if isinstance(a, dict)
            )
            purposes = {str(f.get("purpose") or "").lower() for f in fills}
            entry_fills = [f for f in fills if str(f.get("purpose") or "").lower() == "entry"]
            first_side = str(entry_fills[0].get("side") or "") if entry_fills else str(f0.get("side") or "")
            has_same_side_add = (
                "加仓" in reasons
                or any(
                    i > 0 and str(ef.get("side") or "") == first_side
                    and str(ef.get("order_id") or i) != str(entry_fills[0].get("order_id") or 0)
                    for i, ef in enumerate(entry_fills)
                )
            )
            has_opp_flip = (
                ("反手" in reasons) or ("反买" in reasons)
                or any(str(ef.get("side") or "") != first_side for ef in entry_fills[1:])
            )
            if has_same_side_add:
                exec_buckets["pyramid_windows"].append(pnl)
            if has_opp_flip:
                exec_buckets["flip_windows"].append(pnl)
            if "hedge" in purposes or "锁利" in reasons:
                exec_buckets["hedge_or_lock"].append(pnl)
            if "unwind" in purposes or "残值" in reasons:
                exec_buckets["salvage_unwind"].append(pnl)

    traded_count = len(traded_pnls)
    total_pnl = round(sum(traded_pnls), 4)
    mean_pnl, sd_pnl, t_stat, ci_lo, ci_hi = _calc_t_stat(traded_pnls)
    decisive = wins + losses
    win_rate = round((wins / decisive) * 100.0, 2) if decisive > 0 else 0.0
    avg_win = (total_won / wins) if wins > 0 else 0.0
    avg_loss = (total_lost / losses) if losses > 0 else 0.0
    payoff_ratio = round(avg_win / avg_loss, 3) if avg_loss > 1e-9 else None

    # 校准表
    calib_edges = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.01)]
    calibration = []
    for lo, hi in calib_edges:
        pts = [s for s in calib_samples if lo <= s[0] < hi]
        if pts:
            pred_avg = sum(p for p, _ in pts) / len(pts)
            real_avg = sum(y for _, y in pts) / len(pts)
            calibration.append({
                "bucket": f"{int(lo*100)}-{min(100, int(hi*100))}%",
                "n": len(pts),
                "pred_win_pct": round(pred_avg * 100.0, 1),
                "actual_win_pct": round(real_avg * 100.0, 1),
                "gap_pct": round((real_avg - pred_avg) * 100.0, 1),
            })

    # 按日统计
    daily_rows = []
    for day_k in sorted(daily.keys()):
        b = daily[day_k]
        d_run = 0.0
        d_pk = 0.0
        d_dd = 0.0
        for x in b["pnls"]:
            d_run += x
            if d_run > d_pk:
                d_pk = d_run
            d_dd = max(d_dd, d_pk - d_run)
        d_dec = b["wins"] + b["losses"]
        daily_rows.append({
            "date": day_k,
            "traded": len(b["pnls"]),
            "wins": b["wins"],
            "losses": b["losses"],
            "win_rate_pct": round((b["wins"] / d_dec) * 100.0, 1) if d_dec > 0 else 0.0,
            "net_pnl": round(b["pnl"], 4),
            "max_drawdown": round(d_dd, 4),
        })

    # 执行结构统计
    exec_summary = {}
    for k, arr in exec_buckets.items():
        if not arr:
            exec_summary[k] = {"n": 0, "wins": 0, "win_rate_pct": 0.0, "net_pnl": 0.0, "avg_pnl": 0.0}
        else:
            w_k = sum(1 for x in arr if x > 1e-6)
            l_k = sum(1 for x in arr if x < -1e-6)
            d_k = w_k + l_k
            exec_summary[k] = {
                "n": len(arr),
                "wins": w_k,
                "win_rate_pct": round((w_k / d_k) * 100.0, 1) if d_k > 0 else 0.0,
                "net_pnl": round(sum(arr), 4),
                "avg_pnl": round(sum(arr) / len(arr), 4),
            }

    brier_n = len(brier_model_list)
    brier_model_avg = round(sum(brier_model_list) / brier_n, 5) if brier_n > 0 else None
    brier_market_avg = round(sum(brier_market_list) / brier_n, 5) if brier_n > 0 else None
    brier_diff = (
        round(brier_model_avg - brier_market_avg, 5)
        if (brier_model_avg is not None and brier_market_avg is not None)
        else None
    )

    net_health = None
    if isinstance(live_data, dict) and isinstance(live_data.get("net_stats"), dict):
        ns = live_data["net_stats"]
        poly_ms = _safe_float(ns.get("poly_latency_ms"))
        bin_ms = _safe_float(ns.get("binance_latency_ms"))
        sig_diff = _safe_float(ns.get("signal_minus_venue_ms"))
        if sig_diff is None and poly_ms is not None and bin_ms is not None and poly_ms > 0 and bin_ms > 0:
            sig_diff = round(bin_ms - poly_ms, 1)
        net_health = {
            "spot_provider": ns.get("spot_provider") or live_data.get("spot_source") or "binance",
            "poly_latency_ms": poly_ms,
            "spot_latency_ms": bin_ms,
            "chainlink_latency_ms": _safe_float(ns.get("chainlink_latency_ms")),
            "signal_minus_venue_ms": sig_diff,
            "clock_offset_s": _safe_float(ns.get("clock_offset_s")),
        }

    return {
        "source": label,
        "total_windows": total_windows,
        "gamma_verified": gamma_verified,
        "traded": traded_count,
        "participation_pct": round((traded_count / total_windows) * 100.0, 1) if total_windows > 0 else 0.0,
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate_pct": win_rate,
        "total_pnl": total_pnl,
        "turnover": round(turnover, 2),
        "turnover_roi_pct": round((total_pnl / turnover) * 100.0, 2) if turnover > 1e-4 else 0.0,
        "avg_pnl": round(mean_pnl, 4),
        "sd_pnl": round(sd_pnl, 4),
        "t_stat": round(t_stat, 2),
        "ci_low": round(ci_lo, 4),
        "ci_high": round(ci_hi, 4),
        "max_drawdown": round(max_dd, 4),
        "payoff_ratio": payoff_ratio,
        "brier": {
            "n": brier_n,
            "model": brier_model_avg,
            "market": brier_market_avg,
            "diff": brier_diff,
        },
        "calibration": calibration,
        "execution_breakdown": exec_summary,
        "details_matched": first_fill_count,
        "daily": daily_rows,
        "net_health": net_health,
    }


def format_audit_report(r: Dict[str, Any]) -> str:
    """将体检结果字典渲染为清晰易读的中文终端报告。"""
    lines = []
    lines.append("=" * 74)
    lines.append("                PM-Quant 账本科学体检报告 (Scientific Audit)")
    lines.append("=" * 74)
    lines.append(f"数据源     : {r['source']}")
    lines.append(
        f"样本覆盖   : {r['total_windows']} 窗 (官方 Gamma 核验 {r['gamma_verified']} 窗) | "
        f"交易 {r['traded']} 笔 (参与率 {r['participation_pct']:.1f}%)"
    )
    if r["traded"] == 0:
        lines.append("提示       : 当前账本暂无已结算交易记录。")
        lines.append("=" * 74)
        return "\n".join(lines)

    pr_str = f"{r['payoff_ratio']:.2f}" if r["payoff_ratio"] is not None else "N/A"
    lines.append(
        f"盈亏总览   : 净盈亏 {r['total_pnl']:+.2f} USD | 胜率 {r['win_rate_pct']:.1f}% "
        f"({r['wins']}胜/{r['losses']}负/{r['flats']}平) | 盈亏比 {pr_str} | 最大回撤 -{r['max_drawdown']:.2f} USD"
    )
    lines.append(
        f"统计检验   : 笔均 {r['avg_pnl']:+.4f} USD (SD {r['sd_pnl']:.4f}) | "
        f"t-stat = {r['t_stat']:+.2f} | 95% CI [{r['ci_low']:+.4f}, {r['ci_high']:+.4f}]"
    )
    if r["traded"] < 300:
        lines.append(
            f"样本警告   : 当前仅 {r['traded']} 笔交易 (< 300 笔统计门槛)，任何单因子胜率起伏均可能由噪声主导，切勿盲目调参。"
        )
    elif r["t_stat"] < 1.96:
        lines.append(
            f"显著性提示 : t-stat = {r['t_stat']:+.2f} < 1.96，尚未达到 95% 置信水平的正期望证据。"
        )

    b = r["brier"]
    lines.append("-" * 74)
    if b["n"] > 0 and b["model"] is not None and b["market"] is not None:
        verdict = "模型优于盘口 (存在独立概率 Alpha)" if b["diff"] < -1e-4 else (
            "模型弱于盘口 (缺乏独立预测优势，慎防吃毒流)" if b["diff"] > 1e-4 else "与盘口持平"
        )
        lines.append(
            f"Brier 基准 : n={b['n']} 窗 | Model={b['model']:.5f} vs Market={b['market']:.5f} "
            f"| 差值={b['diff']:+.5f} -> {verdict}"
        )
    else:
        lines.append("Brier 基准 : 暂无可用中段概率快照（新版本 v2.10+ 将自动记录）")

    if r["calibration"]:
        lines.append("概率校准度 :")
        lines.append(f"  {'预测置信区间':<12s} {'样本窗':>6s} {'预测胜率':>10s} {'实际胜率':>10s} {'偏差(实-预)':>12s}")
        for row in r["calibration"]:
            lines.append(
                f"  {row['bucket']:<12s} {row['n']:>6d} {row['pred_win_pct']:>9.1f}% "
                f"{row['actual_win_pct']:>9.1f}% {row['gap_pct']:>+11.1f}%"
            )

    if r["details_matched"] > 0:
        lines.append("-" * 74)
        lines.append(f"执行结构拆解 (已匹配 {r['details_matched']} 场详情包):")
        labels = [
            ("maker_leg1", "首仓 Maker 挂单"),
            ("taker_leg1", "首仓 Taker 吃单"),
            ("pyramid_windows", "含顺势加仓窗口"),
            ("flip_windows", "含反手配对窗口"),
            ("hedge_or_lock", "含锁利/对冲窗口"),
            ("salvage_unwind", "含残值止损窗口"),
        ]
        lines.append(f"  {'类别':<16s} {'笔数':>5s} {'胜率':>8s} {'累计盈亏':>12s} {'笔均盈亏':>12s}")
        for key, title in labels:
            st = r["execution_breakdown"][key]
            if st["n"] > 0:
                lines.append(
                    f"  {title:<16s} {st['n']:>5d} {st['win_rate_pct']:>7.1f}% "
                    f"{st['net_pnl']:>+11.2f}$ {st['avg_pnl']:>+11.4f}$"
                )

    if r["daily"]:
        lines.append("-" * 74)
        lines.append("按自然日 (UTC+8) 稳定性切片:")
        lines.append(f"  {'日期':<12s} {'交易':>5s} {'胜/负':>9s} {'胜率':>8s} {'日盈亏':>12s} {'日内最大回撤':>12s}")
        for d in r["daily"]:
            lines.append(
                f"  {d['date']:<12s} {d['traded']:>5d} {d['wins']:>4d}/{d['losses']:<4d} "
                f"{d['win_rate_pct']:>7.1f}% {d['net_pnl']:>+11.2f}$ {-d['max_drawdown']:>+11.2f}$"
            )

    nh = r.get("net_health")
    if nh:
        lines.append("-" * 74)
        diff = nh.get("signal_minus_venue_ms")
        diff_str = f"{diff:+.1f} ms" if diff is not None else "N/A"
        lines.append(
            f"网络拓扑   : 现货源={nh.get('spot_provider')} | Poly={nh.get('poly_latency_ms')}ms | "
            f"Spot={nh.get('spot_latency_ms')}ms | 信号-交易所差={diff_str} | 时钟偏移={nh.get('clock_offset_s')}s"
        )
        if diff is not None and diff > 100.0:
            lines.append(
                "拓扑告警   : 现货信号源比 Polymarket 慢 >100ms！建议运行 `python main.py probe` 并设置 PM_SPOT_WS_PROVIDER=auto。"
            )
    lines.append("=" * 74)
    return "\n".join(lines)
