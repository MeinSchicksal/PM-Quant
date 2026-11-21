"""PM-Quant investor dashboard server.

Exposes only cash results and human-readable order actions. Market internals,
model decisions and parameters remain in the local journals, not the web API.
"""
import sys
import json
import math
import time
import gzip
import mimetypes
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config

STATIC_DIR = config.ROOT / "web" / "static"

# Below this many traded windows a regime cell cannot separate a 5-cent edge
# from noise (per-window sd is roughly 0.25-1.5 USD), so the dashboard marks
# it as under-sampled instead of letting it read as a finding.
ATTRIBUTION_MIN_SAMPLES = 30


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_action(raw):
    """Keep all fields needed to explain what the robot actually did and market context."""
    action = raw.get("action", "fill")
    if action not in ("quote", "fill", "cancel", "taker_reject", "taker_partial"):
        action = "fill"
    if action in ("fill", "taker_partial") and (_num(raw.get("price")) <= 0 or _num(raw.get("size")) <= 0):
        action = "taker_reject"
    return {
        "t": _num(raw.get("t")),
        "action": action,
        "side": raw.get("side") if raw.get("side") in ("Up", "Down") else None,
        "price": _num(raw.get("price")),
        "size": _num(raw.get("size")),
        "purpose": raw.get("purpose", "entry"),
        "kind": raw.get("kind", "maker"),
        "poly_up": _num(raw.get("poly_up")) if raw.get("poly_up") is not None else None,
        "poly_down": _num(raw.get("poly_down")) if raw.get("poly_down") is not None else None,
        "spot_price": _num(raw.get("spot_price")) if raw.get("spot_price") is not None else None,
        "tau_s": _num(raw.get("tau_s")) if raw.get("tau_s") is not None else None,
        "reason": raw.get("reason", ""),
        "order_id": raw.get("order_id"),
        "queue_ahead": _num(raw.get("queue_ahead")) if raw.get("queue_ahead") is not None else None,
    }


def _legacy_actions(detail):
    """Convert old journals into placements and executions without wait ticks."""
    decisions = sorted(detail.get("decisions", []), key=lambda d: _num(d.get("t")))
    decision_by_time = {round(_num(d.get("t")), 2): d for d in decisions}
    actions = []
    working = None

    for decision in decisions:
        kind = decision.get("action")
        side = decision.get("side")
        if kind == "quote" and side in ("Up", "Down"):
            desired = (side, round(_num(decision.get("price")), 4), round(_num(decision.get("size")), 4))
            if desired != working:
                reason = decision.get("reason", "")
                actions.append({
                    "t": decision.get("t"), "action": "quote", "side": side,
                    "price": desired[1], "size": desired[2],
                    "purpose": "entry" if reason.startswith("Enter ") else "hedge",
                })
                working = desired
        elif kind in ("wait", "taker_buy", "taker_hedge", "taker_unwind"):
            working = None

    held_up = 0.0
    held_down = 0.0
    for fill in sorted(detail.get("fills", []), key=lambda f: _num(f.get("t"))):
        side = fill.get("side")
        if side not in ("Up", "Down"):
            continue
        size = _num(fill.get("size"))
        purpose = fill.get("purpose")
        decision = decision_by_time.get(round(_num(fill.get("t")), 2), {})
        if not purpose:
            if decision.get("action") == "taker_unwind":
                purpose = "unwind"
            elif decision.get("action") == "taker_hedge":
                purpose = "hedge"
            elif decision.get("action") == "taker_buy":
                purpose = "entry"
            elif abs(held_up - held_down) > 1e-6:
                held_side = "Up" if held_up > held_down else "Down"
                purpose = "hedge" if side != held_side else "entry"
            else:
                purpose = "entry"
        actions.append(dict(fill, action="fill", purpose=purpose))
        if purpose == "unwind":
            if side == "Up":
                held_up = max(0.0, held_up - size)
            else:
                held_down = max(0.0, held_down - size)
        elif side == "Up":
            held_up += size
        else:
            held_down += size

    actions.sort(key=lambda a: (_num(a.get("t")), 0 if a.get("action") == "quote" else 1))
    return [_clean_action(a) for a in actions]


def _average_prices(actions):
    totals = {"Up": [0.0, 0.0], "Down": [0.0, 0.0]}
    for action in actions:
        if action["action"] != "fill" or action["purpose"] == "unwind" or not action["side"]:
            continue
        size = action["size"]
        totals[action["side"]][0] += action["price"] * size
        totals[action["side"]][1] += size
    return {
        side: round(cost / size, 4) if size > 1e-9 else None
        for side, (cost, size) in totals.items()
    }


def _public_round(record, detail=None, active=False):
    detail = detail or {}
    if detail.get("actions"):
        actions = [_clean_action(a) for a in detail["actions"]]
    elif record.get("actions"):
        actions = [_clean_action(a) for a in record["actions"]]
    else:
        actions = _legacy_actions(detail)

    start = _num(record.get("start"))
    end = _num(record.get("end"))
    slug = record.get("slug", "")
    if start <= 0 and slug.rsplit("-", 1)[-1].isdigit():
        start = float(slug.rsplit("-", 1)[-1])
        end = start + 300.0

    pnl = _num(record.get("net_pnl"))
    traded = bool(record.get("traded") or record.get("fills_count") or any(a["action"] == "fill" for a in actions))
    if active:
        result = "pending"
    elif not traded:
        result = "no_trade"
    elif pnl > 1e-9:
        result = "win"
    elif pnl < -1e-9:
        result = "loss"
    else:
        result = "flat"

    # Extract numeric shares and cost whether record is summary row or live state dict
    pos_up_raw = record.get("pos_up", 0.0)
    pos_down_raw = record.get("pos_down", 0.0)
    pos_up = float(pos_up_raw.get("shares", 0.0)) if isinstance(pos_up_raw, dict) else _num(pos_up_raw)
    pos_down = float(pos_down_raw.get("shares", 0.0)) if isinstance(pos_down_raw, dict) else _num(pos_down_raw)
    
    if isinstance(pos_up_raw, dict) and isinstance(pos_down_raw, dict):
        total_cost = float(pos_up_raw.get("cash", 0.0)) + float(pos_down_raw.get("cash", 0.0))
    else:
        total_cost = _num(record.get("total_cost", 0.0))

    proceeds = _num(record.get("proceeds", 0.0))
    if active:
        proceeds = 0.0

    return {
        "slug": slug,
        "start": start,
        "end": end,
        "winner": record.get("winner") if not active else None,
        "net_pnl": None if active else round(pnl, 4),
        "traded": traded,
        "result": result,
        "open_px": record.get("open_px"),
        "close_px": record.get("close_px"),
        "pos_up": round(pos_up, 2),
        "pos_down": round(pos_down, 2),
        "total_cost": round(total_cost, 4),
        "proceeds": round(proceeds, 4),
        "fills_count": record.get("fills_count", 0),
        "insurance_paid": record.get("insurance_paid", 0.0),
        "average_prices": _average_prices(actions),
        "actions": actions,
    }


_DETAIL_CACHE = {}


def _read_detail(ledger_dir, slug):
    if not slug or Path(slug).name != slug:
        return {}
    path = ledger_dir / "windows" / f"{slug}.json.gz"
    if not path.exists():
        return {}
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _DETAIL_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            detail = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    # Bound the cache: one entry per window archive revision.
    if len(_DETAIL_CACHE) > 256:
        _DETAIL_CACHE.clear()
    _DETAIL_CACHE[key] = detail
    return detail


def _read_live():
    live_file = config.CURRENT_LEDGER_DIR / "live.json"
    if not live_file.exists():
        return None
    try:
        raw = json.loads(live_file.read_text(encoding="utf-8"))
        if not raw.get("slug"):
            return None
        raw["traded"] = bool(raw.get("fills_count"))
        raw["result"] = "pending"
        raw["net_pnl"] = None
        actions = raw.get("actions", [])
        raw["average_prices"] = _average_prices(actions)
        return raw
    except (OSError, ValueError, json.JSONDecodeError):
        return None


class QuantHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/health":
            self.send_json({"status": "ok", "batch_id": config.BATCH_ID, "time": time.time()})
        elif path == "/api/batches":
            available = sorted([d.name for d in config.LEDGERS_DIR.iterdir() if d.is_dir() and d.name.startswith("v")])
            self.send_json({"batches": available, "current_batch": config.BATCH_ID})
        elif path == "/api/live":
            active = _read_live()
            if active:
                payload = dict(active, server_time=time.time())
            else:
                payload = {"status": "idle", "batch_id": config.BATCH_ID, "server_time": time.time(), "message": "当前没有进行中的轮次"}
            self.send_json(payload)
        elif path == "/api/window":
            qs = parse_qs(parsed.query)
            slug = qs.get("slug", [""])[0]
            target_batch = qs.get("batch", [config.BATCH_ID])[0]
            if not slug or Path(slug).name != slug or Path(target_batch).name != target_batch:
                self.send_json({"error": "Invalid slug or batch"}, status=400)
                return
            ledger_dir = config.LEDGERS_DIR / target_batch
            detail = _read_detail(ledger_dir, slug)
            if not detail:
                active = _read_live()
                if active and active.get("slug") == slug:
                    detail = dict(active)
                else:
                    self.send_json({"error": "Window not found"}, status=404)
                    return
            else:
                detail = dict(detail)
            # Authoritative overlay from summary.jsonl guarantees 100% data consistency
            summary_file = ledger_dir / "summary.jsonl"
            if summary_file.exists():
                try:
                    for line in summary_file.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        rec = json.loads(line)
                        if rec.get("slug") == slug:
                            for key in ("open_px", "close_px", "winner", "proceeds", "net_pnl", "result", "traded"):
                                if rec.get(key) is not None:
                                    detail[key] = rec[key]
                            break
                except Exception:
                    pass

            # Enrich actions with decisions reasons and micro-prices
            raw_actions = detail.get("actions") or []
            decisions = detail.get("decisions") or []
            fills = detail.get("fills") or []
            enriched = []
            for a in raw_actions:
                item = _clean_action(a)
                t_val = item["t"]
                # Match closest decision for reason
                if not item.get("reason") and decisions:
                    closest_d = min(decisions, key=lambda d: abs(_num(d.get("t")) - t_val), default=None)
                    if closest_d and abs(_num(closest_d.get("t")) - t_val) < 2.0:
                        item["reason"] = closest_d.get("reason", "")
                # Match closest fill for micro prices (only for actual executed fills)
                if item.get("action") in ("fill", "taker_partial") and (item.get("poly_up") is None or item.get("spot_price") is None) and fills:
                    closest_f = min(fills, key=lambda f: abs(_num(f.get("t")) - t_val), default=None)
                    if closest_f and abs(_num(closest_f.get("t")) - t_val) < 2.0:
                        for k in ("poly_up", "poly_down", "spot_price", "tau_s"):
                            if closest_f.get(k) is not None:
                                item[k] = closest_f[k]
                enriched.append(item)
            detail["actions"] = enriched
            self.send_json(detail)
        elif path == "/api/history":
            qs = parse_qs(parsed.query)
            target_batch = qs.get("batch", [config.BATCH_ID])[0]
            if Path(target_batch).name != target_batch:
                self.send_json({"error": "Invalid batch"}, status=400)
                return
            ledger_dir = config.LEDGERS_DIR / target_batch
            summary_file = ledger_dir / "summary.jsonl"
            records = []
            if summary_file.exists():
                try:
                    for line in summary_file.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
                except OSError:
                    pass

            # Unit-test journals must never be presented as investor returns.
            records = [w for w in records if not str(w.get("slug", "")).startswith("test_")]
            traded = [w for w in records if w.get("traded") or _num(w.get("fills_count")) > 0 or abs(_num(w.get("net_pnl"))) > 1e-9]
            total_pnl = sum(_num(w.get("net_pnl")) for w in traded)
            total_won = sum(_num(w.get("net_pnl")) for w in traded if _num(w.get("net_pnl")) > 1e-9)
            total_lost = sum(abs(_num(w.get("net_pnl"))) for w in traded if _num(w.get("net_pnl")) < -1e-9)
            turnover = sum(_num(w.get("total_cost")) for w in traded)
            wins = sum(1 for w in traded if _num(w.get("net_pnl")) > 1e-9)
            losses = sum(1 for w in traded if _num(w.get("net_pnl")) < -1e-9)
            count = len(traded)
            flats = count - wins - losses

            # Calculate cumulative equity curve
            equity_curve = []
            running_pnl = 0.0
            for w in traded:
                running_pnl += _num(w.get("net_pnl"))
                equity_curve.append({
                    "slug": w.get("slug"),
                    "pnl": round(running_pnl, 4),
                    "start": _num(w.get("start"))
                })

            # Aggregate Regime Performance Attribution.
            #
            # Only traded windows carry PnL evidence, and only labels recorded at
            # entry time are meaningful. Rows written before the entry-time label
            # existed have no regime at all; they are reported as "unlabeled"
            # instead of being folded into normal_oscillation (the old fallback
            # silently invented a regime for most of the history).
            regime_stats = {}
            # Occurrence counts use whatever label the round carries (entry-time
            # or the dwell fallback) so the matrix still shows how often a
            # regime was live. PnL evidence is only taken from entry-time labels:
            # a dwell label says nothing about the conditions that opened the
            # position, and legacy rows have no usable label at all.
            occurrence = {}
            for w in records:
                occurrence[w.get("regime") or "unlabeled"] = occurrence.get(w.get("regime") or "unlabeled", 0) + 1

                reg = w.get("regime") or "unlabeled"
                if w.get("regime_source") != "entry_time":
                    # Traded rounds from the pre-provenance era cannot be
                    # attributed to any regime; keep them visible in one
                    # explicit bucket instead of dropping them from the table.
                    traded_legacy = (
                        bool(w.get("traded"))
                        or _num(w.get("fills_count")) > 0
                        or abs(_num(w.get("net_pnl"))) > 1e-9
                    )
                    if not traded_legacy:
                        continue
                    reg = "legacy"
                bucket = regime_stats.setdefault(reg, {
                    "rounds": 0, "traded": 0, "wins": 0, "losses": 0, "flats": 0,
                    "net_pnl": 0.0, "_pnls": [],
                })
                traded_row = (
                    bool(w.get("traded"))
                    or _num(w.get("fills_count")) > 0
                    or abs(_num(w.get("net_pnl"))) > 1e-9
                )
                if not traded_row:
                    continue
                pnl = _num(w.get("net_pnl"))
                bucket["traded"] += 1
                bucket["net_pnl"] = round(bucket["net_pnl"] + pnl, 4)
                bucket["_pnls"].append(pnl)
                if pnl > 1e-9:
                    bucket["wins"] += 1
                elif pnl < -1e-9:
                    bucket["losses"] += 1
                else:
                    bucket["flats"] += 1

            for reg, rounds in occurrence.items():
                regime_stats.setdefault(reg, {
                    "rounds": 0, "traded": 0, "wins": 0, "losses": 0, "flats": 0,
                    "net_pnl": 0.0, "_pnls": [],
                })["rounds"] = rounds

            # Every legacy row is traded by construction, so its round count is
            # its sample size (the occurrence map has no "legacy" key).
            if "legacy" in regime_stats:
                regime_stats["legacy"]["rounds"] = regime_stats["legacy"]["traded"]

            for bucket in regime_stats.values():
                pnls = bucket.pop("_pnls", [])
                n = len(pnls)
                if n:
                    mean = sum(pnls) / n
                    var = sum((x - mean) ** 2 for x in pnls) / (n - 1) if n > 1 else 0.0
                    sd = math.sqrt(var)
                    se = sd / math.sqrt(n) if n else 0.0
                    bucket["mean"] = round(mean, 4)
                    bucket["sd"] = round(sd, 4)
                    bucket["ci_low"] = round(mean - 1.96 * se, 4)
                    bucket["ci_high"] = round(mean + 1.96 * se, 4)
                    bucket["t_stat"] = round(mean / se, 2) if se else 0.0
                else:
                    bucket["mean"] = bucket["sd"] = 0.0
                    bucket["ci_low"] = bucket["ci_high"] = 0.0
                    bucket["t_stat"] = 0.0
                bucket["sample_ok"] = n >= ATTRIBUTION_MIN_SAMPLES

            latest = list(reversed(records))[:50]
            rounds = [_public_round(w, _read_detail(ledger_dir, w.get("slug", ""))) for w in latest]
            active = _read_live() if target_batch == config.BATCH_ID else None
            if active and not any(r.get("slug") == active.get("slug") for r in rounds):
                rounds.insert(0, _public_round(active, active, active=True))

            decisive = wins + losses
            win_rate = round((wins / decisive) * 100.0, 1) if decisive else 0.0

            INITIAL_CAPITAL = float(config.PARAMS.get("INITIAL_CAPITAL_USD", 100.0))
            available_capital = round(INITIAL_CAPITAL + total_pnl, 4)
            roi_pct = round((total_pnl / INITIAL_CAPITAL) * 100.0, 2)
            turnover_roi_pct = round((total_pnl / turnover) * 100.0, 2) if turnover > 1e-4 else 0.0

            active_cost = 0.0
            if active:
                pos_up = active.get("pos_up") or {}
                pos_dn = active.get("pos_down") or {}
                active_cost = round(_num(pos_up.get("cash")) + _num(pos_dn.get("cash")), 4)

            free_capital = round(max(0.0, available_capital - active_cost), 4)

            self.send_json({
                "batch_id": target_batch,
                "current_batch": config.BATCH_ID,
                "available_batches": sorted([d.name for d in config.LEDGERS_DIR.iterdir() if d.is_dir() and d.name.startswith("v")]),
                "initial_capital": INITIAL_CAPITAL,
                "available_capital": available_capital,
                "active_cost": active_cost,
                "free_capital": free_capital,
                "roi_pct": roi_pct,
                "turnover_roi_pct": turnover_roi_pct,
                "total_pnl": round(total_pnl, 4),
                "total_won": round(total_won, 4),
                "total_lost": round(total_lost, 4),
                "turnover": round(turnover, 4),
                "win_rate": win_rate,
                "win_rate_all": round((wins / count) * 100, 1) if count else 0.0,
                "avg_pnl": round(total_pnl / count, 4) if count else 0.0,
                "avg_win": round(total_won / wins, 4) if wins else 0.0,
                "avg_loss": round(total_lost / losses, 4) if losses else 0.0,
                "wins": wins,
                "losses": losses,
                "flats": flats,
                "traded_rounds": count,
                "total_rounds": len(records),
                "equity_curve": equity_curve,
                "records": list(reversed(records)),
                "rounds": rounds,
                "regime_stats": regime_stats,
            })
        else:
            rel_path = path.lstrip("/")
            if rel_path.startswith("static/"):
                rel_path = rel_path[len("static/"):]
            if not rel_path or rel_path == "index.html":
                file_path = STATIC_DIR / "index.html"
            else:
                file_path = STATIC_DIR / rel_path

            try:
                file_path = file_path.resolve()
                file_path.relative_to(STATIC_DIR.resolve())
            except (ValueError, OSError):
                self.send_error(403)
                return

            if file_path.exists() and file_path.is_file():
                mime, _ = mimetypes.guess_type(str(file_path))
                mime = mime or "application/octet-stream"
                body = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", f"{mime}; charset=utf-8" if "text" in mime or "javascript" in mime else mime)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)


def start_server_in_background(port=8900):
    """Start investor dashboard HTTP server in a daemon background thread."""
    server_address = ("127.0.0.1", port)
    try:
        httpd = ThreadingHTTPServer(server_address, QuantHandler)
    except OSError as e:
        print(f"[Web] 端口 {port} 已被占用 ({e})，复用既有看板服务。")
        return None
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def run(port=8900):
    # Loopback-only: the investor dashboard must not be exposed on the LAN.
    server_address = ("127.0.0.1", port)
    httpd = ThreadingHTTPServer(server_address, QuantHandler)
    print(f"PM-Quant investor dashboard: http://127.0.0.1:{port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        httpd.server_close()


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 8900)
