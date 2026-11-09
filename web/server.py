"""PM-Quant investor dashboard server.

Exposes only cash results and human-readable order actions. Market internals,
model decisions and parameters remain in the local journals, not the web API.
"""
import sys
import json
import time
import gzip
import mimetypes
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config

STATIC_DIR = config.ROOT / "web" / "static"


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_action(raw):
    """Keep only fields needed to explain what the robot actually did."""
    action = raw.get("action", "fill")
    if action not in ("quote", "fill"):
        action = "fill"
    return {
        "t": _num(raw.get("t")),
        "action": action,
        "side": raw.get("side") if raw.get("side") in ("Up", "Down") else None,
        "price": _num(raw.get("price")),
        "size": _num(raw.get("size")),
        "purpose": raw.get("purpose", "entry"),
        "kind": raw.get("kind", "maker"),
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
        elif kind in ("wait", "taker_hedge", "taker_unwind"):
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

    return {
        "slug": slug,
        "start": start,
        "end": end,
        "winner": record.get("winner") if not active else None,
        "net_pnl": None if active else round(pnl, 4),
        "traded": traded,
        "result": result,
        "average_prices": _average_prices(actions),
        "actions": actions,
    }


def _read_detail(ledger_dir, slug):
    if not slug or Path(slug).name != slug:
        return {}
    path = ledger_dir / "windows" / f"{slug}.json.gz"
    if not path.exists():
        return {}
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


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
        elif path == "/api/live":
            active = _read_live()
            self.send_json(active or {"status": "idle", "message": "当前没有进行中的轮次"})
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
                self.send_json({"error": "Window not found"}, status=404)
                return
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

            latest = list(reversed(records))[:50]
            rounds = [_public_round(w, _read_detail(ledger_dir, w.get("slug", ""))) for w in latest]
            active = _read_live() if target_batch == config.BATCH_ID else None
            if active and not any(r.get("slug") == active.get("slug") for r in rounds):
                rounds.insert(0, active)

            decisive = wins + losses
            win_rate = round((wins / decisive) * 100.0, 1) if decisive else 0.0

            INITIAL_CAPITAL = 100.0
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
