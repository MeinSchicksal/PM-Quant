"""Event-driven historical replay engine.

This module rebuilds the order book from the recorded raw market events and
feeds it to the *same* PaperRunner / policy / queue matcher that runs live:

    recorded book snapshots + recorded trade prints + recorded oracle spot
        -> PaperRunner.step() -> maker/taker fills -> settlement

Nothing here rewrites PnL after the fact. A fill can only happen when a
recorded trade print reaches a resting order's price level (after the queue
ahead is consumed) or when the recorded book is genuinely crossed. Settlement
uses the official winner stored in the ledger summary.

The recorded summary.jsonl is treated as the settlement truth (the .json.gz
detail files are archival and may be stale).
"""
import sys
import json
import math
import tempfile
import shutil
from contextlib import contextmanager
from collections import defaultdict
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.types import OrderBook, SpotState
from runtime.runner import PaperRunner


# --------------------------------------------------------------------------
# Loading recorded truth
# --------------------------------------------------------------------------
def _iter_jsonl(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def load_settlements() -> Dict[str, Dict[str, Any]]:
    """Load per-window settlement records, current ledger taking precedence."""
    records: Dict[str, Dict[str, Any]] = {}
    ledgers = [config.CURRENT_LEDGER_DIR]
    if config.LEDGERS_DIR.exists():
        ledgers += [p for p in sorted(config.LEDGERS_DIR.iterdir()) if p.is_dir()]
    for ledger in ledgers:
        summary = ledger / "summary.jsonl"
        if not summary.exists():
            continue
        for rec in _iter_jsonl(summary):
            slug = rec.get("slug")
            if slug and slug not in records:
                records[slug] = rec
    return records


def _slug_start(slug: str) -> Optional[int]:
    try:
        return int(slug.rsplit("-", 1)[-1])
    except (ValueError, IndexError):
        return None


def _market_date_dirs() -> List[Path]:
    base = config.MARKET_DIR / "events"
    if not base.exists():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir())


def _load_date_batch(date_dir: Path, settlements: Dict[str, Dict[str, Any]]):
    """Load trades/events/oracle for the windows of one recording day.

    Returns (slug -> trade list, token -> (slug, outcome), token -> events,
    oracle spot list, oracle poly-twap list) restricted to settled windows that
    actually have recorded data.
    """
    trades_by_slug: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    token_map: Dict[str, Tuple[str, str]] = {}

    for path in sorted(date_dir.glob("trades_*.jsonl")):
        for t in _iter_jsonl(path):
            slug = t.get("slug")
            token = t.get("token_id")
            if not slug or not token or slug not in settlements:
                continue
            trades_by_slug[slug].append(t)
            token_map[token] = (slug, t.get("outcome", ""))

    if not trades_by_slug:
        return None

    events_by_token: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    tokens = set(token_map)
    for path in sorted(date_dir.glob("events_*.jsonl")):
        for ev in _iter_jsonl(path):
            token = ev.get("token_id")
            if token in tokens:
                events_by_token[token].append(ev)

    for evs in events_by_token.values():
        evs.sort(key=lambda e: float(e.get("t", 0.0)))
    for trs in trades_by_slug.values():
        trs.sort(key=lambda t: float(t.get("received_at") or t.get("timestamp") or 0.0))

    # Oracle: only keep rows that can matter for these windows.
    starts = {_slug_start(s) for s in trades_by_slug}
    lo = min((s for s in starts if s), default=0) - 10
    hi = max((s for s in starts if s), default=0) + 320
    spot_rows: List[Dict[str, Any]] = []
    poly_rows: List[Dict[str, Any]] = []
    oracle_root = date_dir.parents[1] / "oracle"
    oracle_dirs = []
    try:
        from datetime import datetime, timedelta
        d_obj = datetime.strptime(date_dir.name, "%Y-%m-%d")
        prev_day = (d_obj - timedelta(days=1)).strftime("%Y-%m-%d")
        oracle_dirs.append(oracle_root / prev_day)
    except ValueError:
        pass
    oracle_dirs.append(oracle_root / date_dir.name)
    for oracle_dir in oracle_dirs:
        if not oracle_dir.exists():
            continue
        for path in sorted(oracle_dir.glob("*.jsonl")):
            for row in _iter_jsonl(path):
                t = float(row.get("t", 0.0))
                kind = row.get("kind")
                if kind == "spot" and lo <= t <= hi:
                    spot_rows.append(row)
                elif kind == "poly_twap_live" and lo <= t <= hi:
                    poly_rows.append(row)
    spot_rows.sort(key=lambda r: float(r.get("t", 0.0)))
    poly_rows.sort(key=lambda r: float(r.get("t", 0.0)))
    return trades_by_slug, token_map, events_by_token, spot_rows, poly_rows


# --------------------------------------------------------------------------
# Replay one window
# --------------------------------------------------------------------------
def _book_from_event(ev: Dict[str, Any]) -> OrderBook:
    bids = [(float(px), float(sz)) for px, sz in (ev.get("bid_depth") or [])]
    asks = [(float(px), float(sz)) for px, sz in (ev.get("ask_depth") or [])]
    return OrderBook(
        bids=bids,
        asks=asks,
        last_trade_price=ev.get("last_trade_price"),
        source_t=float(ev.get("t", 0.0)),
        received_at=float(ev.get("t", 0.0)),
    )


def _spot_at(rows: List[Dict[str, Any]], idx: int, t: float) -> Tuple[Optional[Dict[str, Any]], int]:
    while idx + 1 < len(rows) and float(rows[idx + 1].get("t", 0.0)) <= t:
        idx += 1
    if idx < 0 or idx >= len(rows) or float(rows[idx].get("t", 0.0)) > t:
        return None, idx
    return rows[idx], idx


def replay_window(
    runner: PaperRunner,
    slug: str,
    settlement: Dict[str, Any],
    trades: List[Dict[str, Any]],
    events_up: List[Dict[str, Any]],
    events_dn: List[Dict[str, Any]],
    spot_rows: List[Dict[str, Any]],
    poly_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    start = float(settlement.get("start") or _slug_start(slug) or 0.0)
    end = float(settlement.get("end") or (start + 300.0))
    open_px = float(settlement.get("open_px") or 0.0)
    winner = settlement.get("winner")
    close_px = float(settlement.get("close_px") or 0.0)

    # Use first recorded Binance spot near window start as binance_open_px (zero look-ahead)
    first_bin = next(
        (float(r.get("price") or 0.0) for r in spot_rows
         if start <= float(r.get("t", 0.0)) <= end and float(r.get("price") or 0.0) > 0),
        open_px,
    )
    runner.start_window(slug, start, end, open_px, binance_open_px=first_bin)

    # Timeline: every recorded book update in either token, restricted to the
    # window. One tick per book update keeps the replay at recording cadence.
    times = [float(e.get("t", 0.0)) for e in events_up if start <= float(e.get("t", 0.0)) <= end]
    times += [float(e.get("t", 0.0)) for e in events_dn if start <= float(e.get("t", 0.0)) <= end]
    times = sorted(set(times))

    book_up = OrderBook()
    book_dn = OrderBook()
    iu = idn = 0
    it = 0
    isp = ipoly = -1
    last_poly = None
    prev_t = start
    carry_prints: List[Dict[str, Any]] = []
    lat_s = max(float(config.PARAMS.get("LATENCY_S", 0.30)), 0.50)

    def _book_at_time(evs: List[Dict[str, Any]], start_idx: int, target_t: float) -> Optional[OrderBook]:
        if not evs:
            return None
        j = max(0, start_idx - 1)
        while j + 1 < len(evs) and float(evs[j + 1].get("t", 0.0)) <= target_t:
            j += 1
        return _book_from_event(evs[j])

    for t in times:
        while iu < len(events_up) and float(events_up[iu].get("t", 0.0)) <= t:
            book_up = _book_from_event(events_up[iu])
            iu += 1
        while idn < len(events_dn) and float(events_dn[idn].get("t", 0.0)) <= t:
            book_dn = _book_from_event(events_dn[idn])
            idn += 1

        spot_row, isp = _spot_at(spot_rows, isp, t)
        poly_row, ipoly = _spot_at(poly_rows, ipoly, t)
        if poly_row:
            last_poly = float(poly_row.get("price") or 0.0)

        # Always drain the print cursor, even when this tick has no oracle row.
        prints = list(carry_prints)
        carry_prints = []
        while it < len(trades):
            tr = trades[it]
            tr_t = float(tr.get("received_at") or tr.get("timestamp") or 0.0)
            if tr_t > t:
                break
            if tr_t > prev_t:
                prints.append(tr)
            it += 1
        prev_t = t

        if spot_row is None:
            carry_prints = prints
            continue

        cur_px = float(spot_row.get("price") or 0.0)
        # Compute 2-3s velocity from recorded spot_rows for accurate anti-toxicity parity
        vel_bps = 0.0
        if isp > 0 and cur_px > 0:
            for k in range(isp - 1, max(-1, isp - 10), -1):
                old_t = float(spot_rows[k].get("t", 0.0))
                old_px = float(spot_rows[k].get("price") or 0.0)
                if (t - old_t) >= 2.0 and old_px > 0:
                    vel_bps = ((cur_px - old_px) / old_px) * 10000.0
                    break

        def _delayed_replay_book(side_name: str, _t=t, _iu=iu, _idn=idn):
            target_t = _t + lat_s
            if side_name == "Up":
                return _book_at_time(events_up, _iu, target_t)
            return _book_at_time(events_dn, _idn, target_t)

        spot = SpotState(
            timestamp=t,
            price=cur_px,
            open_price=open_px,
            return_bps=float(spot_row.get("return_bps") or 0.0),
            realized_vol_bps=float(spot_row.get("realized_vol_bps") or 15.0),
            is_toxic_shock=(abs(vel_bps) >= float(config.PARAMS.get("TOXICITY_SHOCK_BPS", 3.5))),
            regime=str(spot_row.get("regime") or "normal"),
            poly_price=last_poly,
            binance_open_price=first_bin,
            velocity_bps=round(vel_bps, 3),
        )
        runner.step(t, spot, book_up, book_dn, trade_prints=prints,
                    poly_spot=last_poly, replay_mode=True,
                    delayed_book_fetcher=_delayed_replay_book)

    if not times:
        # No recorded book data for this window: cannot claim any result.
        return {
            "slug": slug, "replayed": False, "reason": "no recorded book events",
            "recorded_pnl": float(settlement.get("net_pnl") or 0.0), "replay_pnl": 0.0,
        }

    runner.close_window(close_px=close_px, official_winner=winner,
                        winner_source="ledger")
    rec = runner.last_record
    return {
        "slug": slug,
        "replayed": True,
        "ticks": len(times),
        "recorded_traded": bool(settlement.get("traded")),
        "recorded_pnl": float(settlement.get("net_pnl") or 0.0),
        "replay_pnl": float(rec.get("net_pnl") or 0.0),
        "replay_traded": bool(rec.get("traded")),
        "fills": int(rec.get("fills_count") or 0),
        "winner": winner,
        "regime": rec.get("regime"),
        "regime_source": rec.get("regime_source"),
        "n_decisions": len(runner.decisions),
        "sample_reason": (runner.decisions[0]["reason"] if runner.decisions else ""),
        "fills_detail": [(f.get("side"), f.get("purpose"), f.get("price"),
                          f.get("size")) for f in runner.fills],
    }


# --------------------------------------------------------------------------
# Parameter overrides
# --------------------------------------------------------------------------
@contextmanager
def overridden_params(overrides):
    """Temporarily patch config.PARAMS (restored even if the body raises)."""
    saved = {k: config.PARAMS[k] for k in overrides if k in config.PARAMS}
    added = [k for k in overrides if k not in config.PARAMS]
    config.PARAMS.update(overrides)
    try:
        yield
    finally:
        for k, v in saved.items():
            config.PARAMS[k] = v
        for k in added:
            config.PARAMS.pop(k, None)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def run_replay(
    limit: Optional[int] = None,
    overrides: Optional[Dict[str, Any]] = None,
    quiet: bool = False,
    write: bool = True,
) -> List[Dict[str, Any]]:
    """Replay every recorded window through the live decision pipeline.

    ``overrides`` temporarily patches config.PARAMS for the whole run (used by
    tests and research to vary parameters). The caller's parameters are restored
    even if the replay raises.
    """
    if not quiet:
        print("==================================================================")
        print(f"   PM-Quant 事件驱动历史回放 ({config.VERSION})")
        print("==================================================================")

    settlements = load_settlements()
    if not settlements:
        print("[!] 未找到任何账本结算记录 (summary.jsonl)。")
        return []

    tmp_dir = Path(tempfile.mkdtemp(prefix="pm_replay_"))
    results: List[Dict[str, Any]] = []
    try:
        runner = PaperRunner(ledger_dir=tmp_dir)
        # Patch: expose the record written by close_window for bookkeeping.
        runner.last_record = {}

        original_close = runner.close_window

        def _close_and_capture(close_px, official_winner=None, winner_source="ledger"):
            original_close(close_px, official_winner=official_winner,
                           winner_source=winner_source)
            lines = runner.summary_file.read_text(encoding="utf-8").splitlines()
            rec = json.loads(lines[-1]) if lines else {}
            # A duplicate close returns early; the last journal line then belongs
            # to the previous window and must not be reused as this window's PnL.
            runner.last_record = rec if rec.get("slug") == runner.current_slug else {}
        runner.close_window = _close_and_capture

        with overridden_params(overrides or {}):
            for date_dir in _market_date_dirs():
                batch = _load_date_batch(date_dir, settlements)
                if not batch:
                    continue
                trades_by_slug, token_map, events_by_token, spot_rows, poly_rows = batch
                slugs = sorted(trades_by_slug, key=lambda s: _slug_start(s) or 0)
                if not quiet:
                    print(f"[*] {date_dir.name}: {len(slugs)} 场有录制数据的已结算窗口")
                for slug in slugs:
                    if limit is not None and len(results) >= limit:
                        break
                    tokens = [tok for tok, (s, _) in token_map.items() if s == slug]
                    ev_up = events_by_token.get(
                        next((tok for tok in tokens if token_map[tok][1] == "Up"), ""), []
                    )
                    ev_dn = events_by_token.get(
                        next((tok for tok in tokens if token_map[tok][1] == "Down"), ""), []
                    )
                    r = replay_window(
                        runner, slug, settlements[slug], trades_by_slug[slug],
                        ev_up, ev_dn, spot_rows, poly_rows,
                    )
                    results.append(r)
                if limit is not None and len(results) >= limit:
                    break
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if write:
        _write_report(results)
    if not quiet:
        _print_report(results)
    return results


def _write_report(results: List[Dict[str, Any]]) -> None:
    """Persist raw replay output so future claims can be traced to a run."""
    try:
        import time as _time
        config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = _time.strftime("%Y%m%d_%H%M%S")
        out = config.REPORTS_DIR / f"replay_{stamp}.jsonl"
        with open(out, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        played = [r for r in results if r.get("replayed")]
        pnls = [r["replay_pnl"] for r in played]
        print(f"[*] 回放明细已保存: {out} ({len(played)} 场, ${sum(pnls):+.3f})")
    except Exception as e:
        print(f"[!] 回放报告写入失败: {e}")


def _print_report(results: List[Dict[str, Any]]):
    played = [r for r in results if r.get("replayed")]
    if not played:
        print("[!] 没有窗口具备完整录制数据，无法回放。")
        return

    pnls = [r["replay_pnl"] for r in played]
    fills = [r["fills"] for r in played]
    traded = [r for r in played if r["replay_traded"]]
    wins = [r["replay_pnl"] for r in traded if r["replay_pnl"] > 1e-4]
    losses = [r["replay_pnl"] for r in traded if r["replay_pnl"] < -1e-4]
    mean = _mean(pnls)
    sd = _stdev(pnls)
    t_stat = mean / (sd / math.sqrt(len(pnls))) if sd > 0 and len(pnls) > 1 else 0.0

    print("---------------- 事件驱动回放统计 ----------------")
    print(f"  具备完整录制数据窗口 : {len(played)} 场")
    print(f"  回放发生交易窗口     : {len(traded)} 场")
    print(f"  回放累计盈亏         : ${sum(pnls):+.3f}")
    print(f"  单窗期望             : ${mean:+.4f}  (sd ${sd:.4f}, t={t_stat:+.2f})")
    print(f"  胜/负/平             : {len(wins)} / {len(losses)} / {len(traded) - len(wins) - len(losses)}")
    if wins:
        print(f"  平均盈利 / 平均亏损  : ${_mean(wins):+.4f} / ${_mean(losses):+.4f}" if losses else f"  平均盈利 : ${_mean(wins):+.4f}")
    print(f"  成交笔数             : {sum(fills)} 笔")
    print(f"  单窗最大亏损         : ${min(pnls):+.4f}")
    print(f"  同窗口账本实际盈亏   : ${sum(r['recorded_pnl'] for r in played):+.3f}")
    print("--------------------------------------------------")
