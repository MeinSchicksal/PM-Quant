"""PM-Quant investor dashboard server.

Exposes only cash results and human-readable order actions. Market internals,
model decisions and parameters remain in the local journals, not the web API.
"""
import os
import sys
import json
import math
import time
import gzip
import hmac
import base64
import shutil
import datetime
import platform
import mimetypes
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from runtime.pipeline import PID_FILE, is_pid_running
from runtime.retention import (
    cleanup_old_data,
    get_disk_guard_state,
    get_storage_breakdown,
    is_raw_recording_allowed,
)

STATIC_DIR = config.ROOT / "web" / "static"
_SERVER_START_T = time.time()
_STORAGE_CACHE = {"t": 0.0, "data": None}
_STORAGE_LOCK = threading.Lock()

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


def _get_cached_storage(max_age_s: float = 30.0, force: bool = False) -> dict:
    now = time.time()
    if not force:
        with _STORAGE_LOCK:
            if _STORAGE_CACHE["data"] is not None and (now - _STORAGE_CACHE["t"]) < max_age_s:
                return dict(_STORAGE_CACHE["data"])
    data = get_storage_breakdown()
    with _STORAGE_LOCK:
        _STORAGE_CACHE["t"] = now
        _STORAGE_CACHE["data"] = data
    return dict(data)


def _read_host_memory() -> dict:
    """Read host RAM & Swap usage without external dependencies (Linux + Windows)."""
    out = {
        "mem_total_mb": None,
        "mem_used_mb": None,
        "mem_used_pct": None,
        "swap_total_mb": None,
        "swap_used_mb": None,
        "swap_used_pct": None,
    }
    try:
        if sys.platform.startswith("linux") and Path("/proc/meminfo").exists():
            kv = {}
            for ln in Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines():
                parts = ln.split(":")
                if len(parts) == 2:
                    nums = parts[1].strip().split()
                    if nums and nums[0].isdigit():
                        kv[parts[0].strip()] = int(nums[0])  # KiB
            mt = kv.get("MemTotal", 0)
            ma = kv.get("MemAvailable", kv.get("MemFree", 0))
            st = kv.get("SwapTotal", 0)
            sf = kv.get("SwapFree", 0)
            if mt > 0:
                mu = max(0, mt - ma)
                out["mem_total_mb"] = round(mt / 1024.0, 1)
                out["mem_used_mb"] = round(mu / 1024.0, 1)
                out["mem_used_pct"] = round((mu / mt) * 100.0, 1)
            if st > 0:
                su = max(0, st - sf)
                out["swap_total_mb"] = round(st / 1024.0, 1)
                out["swap_used_mb"] = round(su / 1024.0, 1)
                out["swap_used_pct"] = round((su / st) * 100.0, 1)
            else:
                out["swap_total_mb"] = 0.0
                out["swap_used_mb"] = 0.0
                out["swap_used_pct"] = 0.0
        elif sys.platform == "win32":
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            ms = _MEMORYSTATUSEX()
            ms.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                mb = 1024.0 * 1024.0
                mt = ms.ullTotalPhys / mb
                mu = (ms.ullTotalPhys - ms.ullAvailPhys) / mb
                out["mem_total_mb"] = round(mt, 1)
                out["mem_used_mb"] = round(mu, 1)
                out["mem_used_pct"] = round((mu / mt) * 100.0, 1) if mt > 0 else 0.0
                pf_tot = max(0.0, (ms.ullTotalPageFile - ms.ullTotalPhys) / mb)
                pf_used = max(0.0, ((ms.ullTotalPageFile - ms.ullAvailPageFile) - (ms.ullTotalPhys - ms.ullAvailPhys)) / mb)
                out["swap_total_mb"] = round(pf_tot, 1)
                out["swap_used_mb"] = round(min(pf_tot, pf_used), 1)
                out["swap_used_pct"] = round((min(pf_tot, pf_used) / pf_tot) * 100.0, 1) if pf_tot > 0 else 0.0
    except Exception:
        pass
    return out


def _get_system_status(active=None, force_storage: bool = False) -> dict:
    """Collect cross-platform server health, disk, memory, and pipeline heartbeat status."""
    now = time.time()
    mem = _read_host_memory()

    disk_total_gb = disk_used_gb = disk_free_gb = disk_used_pct = None
    try:
        du = shutil.disk_usage(config.DATA_DIR)
        gb = 1024.0 ** 3
        disk_total_gb = round(du.total / gb, 2)
        disk_used_gb = round(du.used / gb, 2)
        disk_free_gb = round(du.free / gb, 2)
        disk_used_pct = round((du.used / du.total) * 100.0, 1) if du.total > 0 else 0.0
    except Exception:
        pass

    load_1m = load_5m = None
    try:
        if hasattr(os, "getloadavg"):
            l1, l5, _ = os.getloadavg()
            load_1m = round(l1, 2)
            load_5m = round(l5, 2)
    except OSError:
        pass

    uptime_s = round(max(0.0, now - _SERVER_START_T), 1)
    host_uptime_s = None
    try:
        if sys.platform.startswith("linux") and Path("/proc/uptime").exists():
            raw_up = Path("/proc/uptime").read_text(encoding="utf-8").split()[0]
            host_uptime_s = round(float(raw_up), 1)
    except Exception:
        pass

    pipe_pid = None
    pipe_running = False
    try:
        if PID_FILE.exists():
            pid_val = int(PID_FILE.read_text(encoding="utf-8").strip())
            pipe_pid = pid_val
            pipe_running = bool(is_pid_running(pid_val) or pid_val == os.getpid())
    except Exception:
        pass

    live_data = active if active is not None else _read_live()
    hb_age = None
    if live_data and live_data.get("now"):
        hb_age = round(max(0.0, now - _num(live_data.get("now"))), 1)

    if hb_age is not None and hb_age <= 15.0:
        svc_status = "online"
    elif pipe_running and (hb_age is None or hb_age <= 60.0):
        svc_status = "lagging"
    else:
        svc_status = "offline"

    storage = _get_cached_storage(force=force_storage)
    guard = get_disk_guard_state()
    guard["raw_recording_allowed"] = is_raw_recording_allowed()
    return {
        "service_status": svc_status,
        "pipeline_running": pipe_running,
        "pipeline_pid": pipe_pid,
        "heartbeat_age_s": hb_age,
        "web_uptime_s": uptime_s,
        "host_uptime_s": host_uptime_s,
        "cpu_count": os.cpu_count() or 1,
        "load_1m": load_1m,
        "load_5m": load_5m,
        "platform": platform.system(),
        "disk_total_gb": disk_total_gb,
        "disk_used_gb": disk_used_gb,
        "disk_free_gb": disk_free_gb,
        "disk_used_pct": disk_used_pct,
        "storage": storage,
        "disk_guard": guard,
        "retention": {
            "record_raw_market": bool(config.PARAMS.get("RECORD_RAW_MARKET", True)),
            "market_days": int(config.PARAMS.get("MARKET_RETENTION_DAYS", 3)),
            "replay_days": int(config.PARAMS.get("WINDOW_REPLAY_RETENTION_DAYS", 7)),
            "min_keep_replays": int(config.PARAMS.get("MIN_KEEP_REPLAY_FILES", 30)),
            "log_days": int(config.PARAMS.get("RUNTIME_LOG_RETENTION_DAYS", 7)),
            "cleanup_interval_s": float(config.PARAMS.get("CLEANUP_INTERVAL_S", 1800.0)),
            "max_data_dir_mb": float(config.PARAMS.get("MAX_DATA_DIR_MB", 4096.0)),
            "min_free_disk_gb": float(config.PARAMS.get("MIN_FREE_DISK_GB", 3.0)),
            "disk_soft_limit_pct": float(config.PARAMS.get("DISK_SOFT_LIMIT_PCT", 75.0)),
            "disk_hard_limit_pct": float(config.PARAMS.get("DISK_HARD_LIMIT_PCT", 85.0)),
            "disk_critical_pause_pct": float(config.PARAMS.get("DISK_CRITICAL_PAUSE_PCT", 93.0)),
        },
        **mem,
    }


def _display_tz() -> datetime.timezone:
    """Return display timezone (defaults to UTC+8 Beijing time for consistent daily stats)."""
    try:
        offset_h = float(os.environ.get("PM_TZ_OFFSET_HOURS", "8"))
    except ValueError:
        offset_h = 8.0
    return datetime.timezone(datetime.timedelta(hours=offset_h))


def _compute_daily_summary(traded_records: list, max_days: int = 14) -> tuple:
    """Aggregate traded rounds by calendar day in display timezone (UTC+8 default)."""
    tz = _display_tz()
    today_key = datetime.datetime.now(tz).strftime("%Y-%m-%d")
    daily_map = {}
    for w in traded_records:
        ts = _num(w.get("end", w.get("start")))
        day_k = datetime.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d") if ts > 0 else today_key
        b = daily_map.setdefault(day_k, {"date": day_k, "pnl": 0.0, "traded": 0, "wins": 0, "losses": 0})
        p = _num(w.get("net_pnl"))
        b["pnl"] = round(b["pnl"] + p, 4)
        b["traded"] += 1
        if p > 1e-9:
            b["wins"] += 1
        elif p < -1e-9:
            b["losses"] += 1
    daily_summary = [daily_map[k] for k in sorted(daily_map.keys(), reverse=True)[:max_days]]
    today_bucket = daily_map.get(today_key, {"pnl": 0.0, "traded": 0, "wins": 0, "losses": 0})
    return today_key, today_bucket, daily_summary


def _check_basic_auth(auth_header: str) -> bool:
    """Validate optional HTTP Basic Auth when PM_WEB_PASS is configured."""
    expected_pass = os.environ.get("PM_WEB_PASS", "")
    if not expected_pass:
        return True
    expected_user = os.environ.get("PM_WEB_USER", "admin")
    if not auth_header or not auth_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:].strip()).decode("utf-8")
        user, pwd = decoded.split(":", 1)
    except Exception:
        return False
    return hmac.compare_digest(user, expected_user) and hmac.compare_digest(pwd, expected_pass)


class _HardenedHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with scanner-disconnect noise suppression."""
    request_queue_size = 32

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, TimeoutError, OSError)):
            return
        super().handle_error(request, client_address)


class QuantHandler(BaseHTTPRequestHandler):
    # Prevent Slowloris / public port-scanner idle sockets from leaking worker threads
    timeout = 10

    def log_message(self, format, *args):
        pass

    def _require_auth(self) -> bool:
        if _check_basic_auth(self.headers.get("Authorization", "")):
            return True
        body = b"Unauthorized"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="PM-Quant"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/cleanup":
            res = cleanup_old_data()
            sys_state = _get_system_status(force_storage=True)
            self.send_json({"cleanup": res, "system": sys_state})
        else:
            self.send_json({"error": "Not found"}, status=404)

    def do_GET(self):
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/health":
            self.send_json({"status": "ok", "batch_id": config.BATCH_ID, "time": time.time()})
        elif path == "/api/system":
            qs = parse_qs(parsed.query)
            force = qs.get("refresh", ["0"])[0] in ("1", "true", "yes")
            self.send_json(_get_system_status(force_storage=force))
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
            include_details = qs.get("include_details", ["0"])[0] in ("1", "true", "yes")
            rounds = [
                _public_round(w, _read_detail(ledger_dir, w.get("slug", "")) if include_details else {})
                for w in latest
            ]
            active = _read_live() if target_batch == config.BATCH_ID else None
            if active and not any(r.get("slug") == active.get("slug") for r in rounds):
                rounds.insert(0, _public_round(active, active, active=True))

            today_key, today_bucket, daily_summary = _compute_daily_summary(traded)

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
                "today_date": today_key,
                "today_pnl": round(today_bucket["pnl"], 4),
                "today_traded": today_bucket["traded"],
                "today_wins": today_bucket["wins"],
                "today_losses": today_bucket["losses"],
                "daily_summary": daily_summary,
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
            if rel_path in ("m", "m/", "mobile", "mobile/"):
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
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


def _resolve_bind_host(host=None) -> str:
    if host:
        return str(host)
    return os.environ.get("PM_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"


def start_server_in_background(port=8900, host=None):
    """Start investor dashboard HTTP server in a daemon background thread."""
    bind_host = _resolve_bind_host(host)
    server_address = (bind_host, port)
    try:
        httpd = _HardenedHTTPServer(server_address, QuantHandler)
    except OSError as e:
        print(f"[Web] 端口 {port} 已被占用 ({e})，复用既有看板服务。")
        return None
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def run(port=8900, host=None):
    # Defaults to loopback-only (127.0.0.1). Set PM_WEB_HOST=0.0.0.0 and PM_WEB_PASS=...
    # on a remote server to enable password-protected mobile/remote access.
    bind_host = _resolve_bind_host(host)
    server_address = (bind_host, port)
    httpd = _HardenedHTTPServer(server_address, QuantHandler)
    disp_host = "127.0.0.1" if bind_host == "0.0.0.0" else bind_host
    auth_note = " [BasicAuth 已开启]" if os.environ.get("PM_WEB_PASS") else ""
    print(f"PM-Quant investor dashboard: http://{disp_host}:{port}/{auth_note}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        httpd.server_close()


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 8900)
