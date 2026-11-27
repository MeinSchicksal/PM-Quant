"""PM-Quant investor dashboard server.

Exposes only cash results and human-readable order actions. Market internals,
model decisions and parameters remain in the local journals, not the web API.
"""
import os
import re
import sys
import json
import math
import time
import gzip
import hmac
import base64
import shutil
import signal
import datetime
import platform
import mimetypes
import threading
from collections import OrderedDict, deque
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.request import Request as UrlRequest, urlopen as url_open

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
_CLEANUP_API_LOCK = threading.Lock()
_LAST_MANUAL_CLEANUP_MONO = 0.0

_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WEAK_PASSWORDS = frozenset({
    "change_me_to_a_strong_password",
    "changeme",
    "password",
    "password123",
    "123456",
    "12345678",
    "123456789",
    "admin",
    "admin123",
    "qwerty",
    "pm-quant",
})


def _is_safe_segment(val: str) -> bool:
    """Strictly validate a single path segment (blocks '.', '..', slashes, null bytes)."""
    if not val or val in (".", ".."):
        return False
    if "/" in val or "\\" in val or ".." in val:
        return False
    return bool(_SAFE_SEGMENT_RE.match(val))


def _resolve_ledger_dir(batch: str):
    """Resolve batch directory strictly inside config.LEDGERS_DIR, else None."""
    if not _is_safe_segment(batch):
        return None
    try:
        base = config.LEDGERS_DIR.resolve()
        candidate = (base / batch).resolve()
        candidate.relative_to(base)
        if candidate == base:
            return None
        return candidate
    except (ValueError, OSError):
        return None

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
    size_val = _num(raw.get("size"))
    if action == "taker_partial" and size_val <= 0:
        size_val = _num(raw.get("filled"))
    if action == "fill" and (_num(raw.get("price")) <= 0 or size_val <= 0):
        action = "taker_reject"
    elif action == "taker_partial" and size_val <= 0:
        action = "taker_reject"
    out = {
        "t": _num(raw.get("t")),
        "action": action,
        "side": raw.get("side") if raw.get("side") in ("Up", "Down") else None,
        "price": _num(raw.get("price")),
        "size": size_val,
        "purpose": raw.get("purpose", "entry"),
        "kind": raw.get("kind", "taker" if action in ("taker_partial", "taker_reject") else "maker"),
        "poly_up": _num(raw.get("poly_up")) if raw.get("poly_up") is not None else None,
        "poly_down": _num(raw.get("poly_down")) if raw.get("poly_down") is not None else None,
        "spot_price": _num(raw.get("spot_price")) if raw.get("spot_price") is not None else None,
        "tau_s": _num(raw.get("tau_s")) if raw.get("tau_s") is not None else None,
        "reason": raw.get("reason", ""),
        "order_id": raw.get("order_id"),
        "queue_ahead": _num(raw.get("queue_ahead")) if raw.get("queue_ahead") is not None else None,
    }
    if raw.get("requested") is not None:
        out["requested"] = _num(raw.get("requested"))
    if raw.get("filled") is not None:
        out["filled"] = _num(raw.get("filled"))
    return out


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
        total_cost = (
            float(pos_up_raw.get("cash", 0.0))
            + float(pos_down_raw.get("cash", 0.0))
            + _num(record.get("unwind_cost_basis", 0.0))
        )
    else:
        total_cost = _num(record.get("total_cost", 0.0))

    proceeds = _num(record.get("proceeds", 0.0))
    if active:
        proceeds = _num(record.get("unwind_proceeds", 0.0))

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


_DETAIL_CACHE = OrderedDict()
_DETAIL_CACHE_LOCK = threading.Lock()
_DETAIL_CACHE_MAX_ENTRIES = 24

_HISTORY_CACHE = OrderedDict()
_HISTORY_CACHE_LOCK = threading.Lock()
_HISTORY_CACHE_MAX_ENTRIES = 8

_SUMMARY_AGG_CACHE = OrderedDict()
_SUMMARY_AGG_LOCK = threading.Lock()
_SUMMARY_AGG_MAX_ENTRIES = 8

_ENTRY_MODE_CACHE = OrderedDict()
_ENTRY_MODE_LOCK = threading.Lock()
_ENTRY_MODE_MAX_ENTRIES = 2000

_FX_CACHE_LOCK = threading.Lock()
_FX_CACHE = {
    "rate": 7.25,
    "date": "",
    "source": "default",
    "loaded": False,
    "refreshing": False,
    "last_attempt_mono": 0.0,
}
_FX_CACHE_FILE = config.RUNTIME_DIR / "fx_usd_cny.json"


def _today_bjt_str() -> str:
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(tz).strftime("%Y-%m-%d")


def _fetch_usd_cny_from_web() -> tuple:
    """Fetch live USD->CNY spot rate from free no-key public endpoints (called at most once/day)."""
    endpoints = (
        ("open.er-api.com", "https://open.er-api.com/v6/latest/USD"),
        ("frankfurter.app", "https://api.frankfurter.app/latest?from=USD&to=CNY"),
    )
    for src_name, url in endpoints:
        try:
            req = UrlRequest(url, headers={"User-Agent": "PM-Quant/3.4", "Accept": "application/json"})
            with url_open(req, timeout=4.0) as resp:
                if getattr(resp, "status", 200) != 200:
                    continue
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            rates = data.get("rates") if isinstance(data, dict) else None
            raw_cny = rates.get("CNY") if isinstance(rates, dict) else None
            val = float(raw_cny) if raw_cny is not None else 0.0
            if math.isfinite(val) and 5.0 <= val <= 10.0:
                return round(val, 4), src_name
        except Exception:
            continue
    return None, None


def _get_usd_cny_fx() -> dict:
    """Return cached daily USD->CNY rate; asynchronously refreshes at most once per Beijing calendar day."""
    today = _today_bjt_str()
    need_fetch = False
    with _FX_CACHE_LOCK:
        if not _FX_CACHE["loaded"]:
            _FX_CACHE["loaded"] = True
            if _FX_CACHE_FILE.exists():
                try:
                    saved = json.loads(_FX_CACHE_FILE.read_text(encoding="utf-8"))
                    s_rate = float(saved.get("rate", 0.0) or 0.0)
                    s_date = str(saved.get("date") or "")
                    s_src = str(saved.get("source") or "cache")
                    if math.isfinite(s_rate) and 5.0 <= s_rate <= 10.0:
                        _FX_CACHE["rate"] = round(s_rate, 4)
                        _FX_CACHE["date"] = s_date
                        _FX_CACHE["source"] = s_src
                except Exception:
                    pass
        now_m = time.monotonic()
        if (
            _FX_CACHE["date"] != today
            and not _FX_CACHE["refreshing"]
            and (_FX_CACHE["last_attempt_mono"] <= 0.0 or (now_m - _FX_CACHE["last_attempt_mono"]) >= 1800.0)
        ):
            _FX_CACHE["refreshing"] = True
            _FX_CACHE["last_attempt_mono"] = now_m
            need_fetch = True
        snapshot = {
            "rate": _FX_CACHE["rate"],
            "date": _FX_CACHE["date"] or today,
            "source": _FX_CACHE["source"],
            "synced_today": (_FX_CACHE["date"] == today),
        }

    if need_fetch:
        def _bg_fx():
            rate, src = _fetch_usd_cny_from_web()
            if rate is not None:
                try:
                    _FX_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                    tmp = _FX_CACHE_FILE.with_suffix(".json.tmp")
                    tmp.write_text(
                        json.dumps({"rate": rate, "date": today, "source": src}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    os.replace(tmp, _FX_CACHE_FILE)
                except Exception:
                    pass
            with _FX_CACHE_LOCK:
                _FX_CACHE["refreshing"] = False
                if rate is not None:
                    _FX_CACHE["rate"] = rate
                    _FX_CACHE["date"] = today
                    _FX_CACHE["source"] = src

        threading.Thread(target=_bg_fx, daemon=True, name="FxDailySync").start()

    return snapshot


def _infer_entry_mode(ledger_dir: Path, record: dict):
    """返回该轮执行控仓模式（flip / maker / taker / legacy mode_b），旧记录通过缓存仅解压一次 detail 文件。"""
    if _num(record.get("pos_up")) > 1e-6 and _num(record.get("pos_down")) > 1e-6:
        return "flip"
    existing = record.get("entry_mode")
    if existing in ("flip", "mode_b", "maker", "taker"):
        return existing
    slug = str(record.get("slug") or "")
    if not slug:
        return None
    cache_key = (str(ledger_dir), slug)
    with _ENTRY_MODE_LOCK:
        if cache_key in _ENTRY_MODE_CACHE:
            _ENTRY_MODE_CACHE.move_to_end(cache_key)
            return _ENTRY_MODE_CACHE[cache_key]
    mode = None
    try:
        detail = _read_detail(ledger_dir, slug)
        fills = (detail or {}).get("fills") or []
        if fills:
            filled_sides = {f.get("side") for f in fills if f.get("side") in ("Up", "Down")}
            f0 = fills[0]
            r0 = str(f0.get("reason") or "")
            if len(filled_sides) >= 2:
                mode = "flip"
            elif "模式B" in r0 or "[MODE_B]" in r0:
                mode = "mode_b"
            elif str(f0.get("kind") or "").lower() == "maker":
                mode = "maker"
            else:
                mode = "taker"
    except Exception:
        mode = None
    with _ENTRY_MODE_LOCK:
        _ENTRY_MODE_CACHE[cache_key] = mode
        _ENTRY_MODE_CACHE.move_to_end(cache_key)
        while len(_ENTRY_MODE_CACHE) > _ENTRY_MODE_MAX_ENTRIES:
            _ENTRY_MODE_CACHE.popitem(last=False)
    return mode

_STATIC_CACHE = OrderedDict()
_STATIC_CACHE_LOCK = threading.Lock()
_STATIC_CACHE_MAX_ENTRIES = 16


def _lookup_summary_record(summary_file: Path, slug: str):
    """Find one slug in summary.jsonl with O(1) RAM: checks last 128KB tail first, then streams."""
    if not slug or not summary_file.exists():
        return None
    target_bytes = json.dumps(slug, ensure_ascii=False).encode("utf-8")
    try:
        file_size = summary_file.stat().st_size
        tail_start = max(0, file_size - 131072)
        with open(summary_file, "rb") as f:
            if tail_start > 0:
                f.seek(tail_start)
                f.readline()
            tail_lines = f.read().splitlines()
        for raw_ln in reversed(tail_lines):
            if target_bytes in raw_ln:
                try:
                    rec = json.loads(raw_ln.decode("utf-8"))
                    if isinstance(rec, dict) and rec.get("slug") == slug:
                        return rec
                except (ValueError, json.JSONDecodeError):
                    continue
        if tail_start == 0:
            return None
        # Fallback: stream prefix without loading the whole 1-year file into RAM
        with open(summary_file, "rb") as f:
            while f.tell() < tail_start:
                raw_ln = f.readline()
                if not raw_ln:
                    break
                if target_bytes in raw_ln:
                    try:
                        rec = json.loads(raw_ln.decode("utf-8"))
                        if isinstance(rec, dict) and rec.get("slug") == slug:
                            return rec
                    except (ValueError, json.JSONDecodeError):
                        continue
    except OSError:
        return None
    return None


def _downsample_curve(points: list, max_points: int = 600) -> list:
    """Preserve endpoints while uniformly downsampling large equity curves for mobile speed."""
    n = len(points)
    if n <= max_points or max_points < 2:
        return points
    step = (n - 1) / float(max_points - 1)
    idxs = sorted({min(n - 1, int(round(i * step))) for i in range(max_points)})
    if idxs[0] != 0:
        idxs.insert(0, 0)
    if idxs[-1] != n - 1:
        idxs.append(n - 1)
    return [points[i] for i in idxs]


def _read_detail(ledger_dir, slug):
    if not _is_safe_segment(slug):
        return {}
    path = ledger_dir / "windows" / f"{slug}.json.gz"
    if not path.exists():
        return {}
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _DETAIL_CACHE_LOCK:
        cached = _DETAIL_CACHE.get(key)
        if cached is not None:
            _DETAIL_CACHE.move_to_end(key)
            return cached
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            detail = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    with _DETAIL_CACHE_LOCK:
        _DETAIL_CACHE[key] = detail
        _DETAIL_CACHE.move_to_end(key)
        while len(_DETAIL_CACHE) > _DETAIL_CACHE_MAX_ENTRIES:
            _DETAIL_CACHE.popitem(last=False)
    return detail


_LIVE_CACHE = {"key": None, "data": None}
_LIVE_CACHE_LOCK = threading.Lock()


def _read_live():
    live_file = config.CURRENT_LEDGER_DIR / "live.json"
    if not live_file.exists():
        return None
    try:
        st = live_file.stat()
        cache_key = (str(live_file), st.st_mtime_ns, st.st_size)
        with _LIVE_CACHE_LOCK:
            if _LIVE_CACHE["key"] == cache_key and _LIVE_CACHE["data"] is not None:
                return dict(_LIVE_CACHE["data"])
        raw = json.loads(live_file.read_text(encoding="utf-8"))
        if not raw.get("slug"):
            return None
        raw["traded"] = bool(raw.get("fills_count"))
        raw["result"] = "pending"
        raw["net_pnl"] = None
        actions = raw.get("actions", [])
        raw["average_prices"] = _average_prices(actions)
        with _LIVE_CACHE_LOCK:
            _LIVE_CACHE["key"] = cache_key
            _LIVE_CACHE["data"] = raw
        return dict(raw)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _get_cached_storage(max_age_s: float = 30.0, force: bool = False) -> dict:
    now = time.time()
    min_refresh_s = 10.0 if force else max_age_s
    with _STORAGE_LOCK:
        if _STORAGE_CACHE["data"] is not None and (now - _STORAGE_CACHE["t"]) < min_refresh_s:
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


class _AuthRateLimiter:
    """Sliding-window brute-force protection for HTTP Basic Auth."""

    def __init__(self, max_failures: int = 10, window_s: float = 300.0, ban_s: float = 900.0):
        self.max_failures = max_failures
        self.window_s = window_s
        self.ban_s = ban_s
        self._lock = threading.Lock()
        self._fails = {}     # ip -> deque[monotonic_ts]
        self._banned = {}    # ip -> unban_monotonic_ts
        self._last_log = {}  # ip -> monotonic_ts

    def check_banned(self, ip: str, now: float = None) -> float:
        """Return remaining ban seconds (>0 if banned), else 0.0."""
        t = time.monotonic() if now is None else float(now)
        with self._lock:
            until = self._banned.get(ip, 0.0)
            if until > t:
                return round(until - t, 1)
            if until > 0.0:
                self._banned.pop(ip, None)
            return 0.0

    def record_failure(self, ip: str, now: float = None) -> float:
        """Record one auth failure; returns ban duration (>0) if threshold crossed."""
        t = time.monotonic() if now is None else float(now)
        should_log = False
        ban_left = 0.0
        with self._lock:
            q = self._fails.setdefault(ip, deque())
            cutoff = t - self.window_s
            while q and q[0] < cutoff:
                q.popleft()
            q.append(t)
            if len(q) >= self.max_failures:
                self._banned[ip] = t + self.ban_s
                q.clear()
                ban_left = self.ban_s
                should_log = True
            elif (t - self._last_log.get(ip, 0.0)) >= 10.0:
                self._last_log[ip] = t
                should_log = True
            # Cap map size against spoofed-source floods
            if len(self._fails) > 2048:
                self._fails.pop(next(iter(self._fails)), None)
            if len(self._banned) > 2048:
                self._banned.pop(next(iter(self._banned)), None)
            if len(self._last_log) > 2048:
                self._last_log.pop(next(iter(self._last_log)), None)
        if should_log:
            if ban_left > 0:
                print(f"[WebAuth] BANNED {ip} for {int(ban_left)}s after {self.max_failures} failed login attempts.")
            else:
                print(f"[WebAuth] Failed Basic Auth attempt from {ip}.")
        return ban_left

    def record_success(self, ip: str):
        with self._lock:
            self._fails.pop(ip, None)
            self._banned.pop(ip, None)
            self._last_log.pop(ip, None)


_AUTH_LIMITER = _AuthRateLimiter()


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
    """ThreadingHTTPServer with bounded concurrency and scanner-disconnect noise suppression."""
    request_queue_size = 32
    daemon_threads = True
    max_concurrent_requests = 16

    def __init__(self, *args, **kwargs):
        self._req_sem = threading.BoundedSemaphore(self.max_concurrent_requests)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._req_sem.acquire(blocking=False):
            try:
                # Never let a zero-window scanner block the main accept() loop on sendall()
                request.settimeout(0.5)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: text/plain; charset=utf-8\r\n"
                    b"Content-Length: 11\r\n"
                    b"Connection: close\r\n\r\nServer busy"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._req_sem.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._req_sem.release()

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

    def _client_ip(self) -> str:
        peer_ip = (self.client_address[0] if self.client_address else "") or "unknown"
        # Trust forwarding headers ONLY when connection originates from local reverse proxy (Caddy/Nginx)
        if peer_ip in ("127.0.0.1", "::1"):
            real_ip = (self.headers.get("X-Real-IP") or "").strip()
            if real_ip:
                return real_ip
            xff = (self.headers.get("X-Forwarded-For") or "").strip()
            if xff:
                # 取最右侧由本地反代追加的真实来源 IP，防止客户端伪造左侧 XFF 绕过防爆破
                return xff.split(",")[-1].strip() or peer_ip
        return peer_ip

    def _require_auth(self) -> bool:
        expected_pass = os.environ.get("PM_WEB_PASS", "")
        if not expected_pass:
            return True
        ip = self._client_ip()
        ban_left = _AUTH_LIMITER.check_banned(ip)
        if ban_left > 0:
            body = b"Too Many Requests"
            self.send_response(429)
            self.send_header("Retry-After", str(max(1, int(math.ceil(ban_left)))))
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False
        auth_hdr = self.headers.get("Authorization", "")
        if _check_basic_auth(auth_hdr):
            _AUTH_LIMITER.record_success(ip)
            return True
        # Only count requests that actually attempted credentials against brute-force ban;
        # initial browser challenges (no Authorization header) must not lock out legitimate users.
        if auth_hdr.strip():
            ban_now = _AUTH_LIMITER.record_failure(ip)
            if ban_now > 0:
                body = b"Too Many Requests"
                self.send_response(429)
                self.send_header("Retry-After", str(max(1, int(math.ceil(ban_now)))))
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return False
        body = b"Unauthorized"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="PM-Quant"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _send_json_bytes(self, body: bytes, status: int = 200, gz_body: bytes = None):
        accept_enc = (self.headers.get("Accept-Encoding") or "").lower()
        use_gzip = "gzip" in accept_enc and len(body) > 1024
        payload = (gz_body if gz_body is not None else gzip.compress(body, compresslevel=5)) if use_gzip else body
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Vary", "Accept-Encoding")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send_json_bytes(body, status=status)

    def _check_same_origin(self) -> bool:
        """Basic CSRF check for mutating POST endpoints when browser sends Origin header."""
        origin = (self.headers.get("Origin") or "").strip()
        host = (self.headers.get("Host") or "").strip().lower()
        if not origin or not host:
            return True
        try:
            o_host = (urlparse(origin).netloc or "").lower()
            return o_host == host
        except Exception:
            return False

    def do_POST(self):
        global _LAST_MANUAL_CLEANUP_MONO
        if not self._require_auth():
            return
        if not self._check_same_origin():
            self.send_json({"error": "Cross-origin POST rejected"}, status=403)
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/cleanup":
            now_m = time.monotonic()
            with _CLEANUP_API_LOCK:
                if _LAST_MANUAL_CLEANUP_MONO > 0.0 and (now_m - _LAST_MANUAL_CLEANUP_MONO) < 30.0:
                    self.send_json(
                        {"error": "Cleanup cooldown active, retry in 30s", "system": _get_system_status()},
                        status=429,
                    )
                    return
                _LAST_MANUAL_CLEANUP_MONO = now_m
            res = cleanup_old_data()
            sys_state = _get_system_status(force_storage=True)
            self.send_json({"cleanup": res, "system": sys_state})
        else:
            self.send_json({"error": "Not found"}, status=404)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Unauthenticated minimal liveness probe for external monitors (UptimeRobot / Healthchecks).
        # Exposes NO PnL, positions, or internals; returns 503 if heartbeat stale > 60s or API schema paused.
        if path == "/healthz":
            live_data = _read_live()
            hb_age = None
            if live_data and live_data.get("now"):
                hb_age = round(max(0.0, time.time() - _num(live_data.get("now"))), 1)
            api_guard = (live_data.get("api_schema_guard") or {}) if isinstance(live_data, dict) else {}
            api_paused = bool(api_guard.get("paused", False))
            healthy = (hb_age is not None and hb_age <= 60.0 and not api_paused)
            status_str = "paused_api_changed" if api_paused else ("ok" if healthy else "offline")
            payload = {
                "status": status_str,
                "heartbeat_age_s": hb_age,
            }
            if api_paused:
                payload["api_source"] = str(api_guard.get("source") or "")
            self.send_json(payload, status=200 if healthy else 503)
            return

        if not self._require_auth():
            return

        if path == "/api/fx":
            self.send_json(_get_usd_cny_fx())
            return

        if path == "/api/health":
            live_data = _read_live()
            hb_age = round(max(0.0, time.time() - _num(live_data.get("now"))), 1) if (live_data and live_data.get("now")) else None
            self.send_json({
                "status": "ok",
                "service_status": "online" if (hb_age is not None and hb_age <= 15.0) else ("lagging" if (hb_age is not None and hb_age <= 60.0) else "offline"),
                "heartbeat_age_s": hb_age,
                "batch_id": config.BATCH_ID,
                "time": time.time(),
            })
        elif path == "/api/system":
            qs = parse_qs(parsed.query)
            force = qs.get("refresh", ["0"])[0] in ("1", "true", "yes")
            self.send_json(_get_system_status(force_storage=force))
        elif path == "/api/batches":
            available = (
                sorted([d.name for d in config.LEDGERS_DIR.iterdir() if d.is_dir() and d.name.startswith("v")])
                if config.LEDGERS_DIR.exists() else []
            )
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
            ledger_dir = _resolve_ledger_dir(target_batch)
            if not _is_safe_segment(slug) or ledger_dir is None:
                self.send_json({"error": "Invalid slug or batch"}, status=400)
                return
            detail = _read_detail(ledger_dir, slug)
            summary_file = ledger_dir / "summary.jsonl"
            rec = _lookup_summary_record(summary_file, slug)
            if not detail:
                active = _read_live() if target_batch == config.BATCH_ID else None
                if active and active.get("slug") == slug:
                    detail = dict(active)
                elif rec:
                    detail = dict(rec)
                    detail["archive_pruned"] = True
                    detail.setdefault("actions", [])
                    detail.setdefault("fills", [])
                    detail.setdefault("price_history", [])
                else:
                    self.send_json({"error": "Window not found"}, status=404)
                    return
            else:
                detail = dict(detail)
            # Authoritative tail-first O(1)-RAM overlay from summary.jsonl guarantees 100% consistency
            if rec:
                for key in ("open_px", "close_px", "winner", "winner_source", "proceeds", "net_pnl", "result", "traded"):
                    if rec.get(key) is not None:
                        detail[key] = rec[key]

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
            ledger_dir = _resolve_ledger_dir(target_batch)
            if ledger_dir is None:
                self.send_json({"error": "Invalid batch"}, status=400)
                return
            include_details = qs.get("include_details", ["0"])[0] in ("1", "true", "yes")
            raw_limit = qs.get("limit", ["300"])[0].strip().lower()
            if raw_limit in ("0", "all"):
                record_limit = 5000
            else:
                try:
                    record_limit = max(20, min(5000, int(raw_limit)))
                except ValueError:
                    record_limit = 300
            summary_file = ledger_dir / "summary.jsonl"
            try:
                st = summary_file.stat()
                sum_sig = (st.st_mtime_ns, st.st_size)
            except OSError:
                sum_sig = (0, 0)

            active = _read_live() if target_batch == config.BATCH_ID else None
            active_cost = 0.0
            active_sig = None
            if active:
                pos_up = active.get("pos_up") or {}
                pos_dn = active.get("pos_down") or {}
                active_unwind_pnl = _num(active.get("realized_unwind_pnl"))
                active_reserved = max(0.0, _num(active.get("reserved_orders_cost")))
                # 与 runner.py 的 live.json 口径保持 100% 一致：持仓净成本 + 挂单/在途冻结资金
                active_cost = round(
                    max(0.0, _num(pos_up.get("cash")) + _num(pos_dn.get("cash")) - active_unwind_pnl) + active_reserved,
                    4,
                )
                active_sig = (
                    active.get("slug"),
                    active.get("open_px"),
                    round(_num(pos_up.get("shares")), 4),
                    round(_num(pos_dn.get("shares")), 4),
                    active_cost,
                    round(active_reserved, 4),
                    round(active_unwind_pnl, 4),
                    int(active.get("fills_count") or 0),
                    len(active.get("actions") or []),
                )
            available_batches = (
                tuple(sorted([d.name for d in config.LEDGERS_DIR.iterdir() if d.is_dir() and d.name.startswith("v")]))
                if config.LEDGERS_DIR.exists() else ()
            )
            today_cal = datetime.datetime.now(_display_tz()).strftime("%Y-%m-%d")
            hist_cache_key = (target_batch, include_details, record_limit, sum_sig, active_sig, available_batches, today_cal)
            if not include_details:
                with _HISTORY_CACHE_LOCK:
                    cached_pair = _HISTORY_CACHE.get(hist_cache_key)
                    if cached_pair is not None:
                        _HISTORY_CACHE.move_to_end(hist_cache_key)
                        self._send_json_bytes(cached_pair[0], gz_body=cached_pair[1])
                        return

            # 静态 summary.jsonl 聚合按 (batch, file_sig, limit, today) 独立缓存，
            # 避免当前活跃轮次每 1 秒写 live.json 导致 10 万行账本被高频重扫。
            agg_cache_key = (target_batch, sum_sig, record_limit, today_cal)
            agg = None
            with _SUMMARY_AGG_LOCK:
                agg = _SUMMARY_AGG_CACHE.get(agg_cache_key)
                if agg is not None:
                    _SUMMARY_AGG_CACHE.move_to_end(agg_cache_key)

            if agg is None:
                keep_recent = max(record_limit, 50)
                recent_buf = deque(maxlen=keep_recent)
                total_rounds = 0
                total_pnl = 0.0
                total_won = 0.0
                total_lost = 0.0
                turnover = 0.0
                wins = 0
                losses = 0
                paired_rounds = 0
                total_fills = 0
                count = 0
                running_pnl = 0.0
                peak_pnl = 0.0
                max_drawdown = 0.0
                equity_curve_raw = []
                regime_stats = {}
                occurrence = {}
                brier_model_sum = 0.0
                brier_market_sum = 0.0
                brier_count = 0
                tz = _display_tz()
                daily_map = {}
                first_start_ts = 0.0
                last_end_ts = 0.0
                exec_breakdown = {
                    "maker": {"count": 0, "wins": 0, "losses": 0, "pnl": 0.0},
                    "taker": {"count": 0, "wins": 0, "losses": 0, "pnl": 0.0},
                    "flip": {"count": 0, "wins": 0, "losses": 0, "pnl": 0.0},
                }

                if summary_file.exists():
                    try:
                        with open(summary_file, "r", encoding="utf-8") as f:
                            for line in f:
                                if not line.strip():
                                    continue
                                try:
                                    w = json.loads(line)
                                except (json.JSONDecodeError, ValueError):
                                    continue
                                if not isinstance(w, dict):
                                    continue
                                # Unit-test journals must never be presented as investor returns.
                                if str(w.get("slug", "")).startswith("test_"):
                                    continue

                                total_rounds += 1
                                st_ts = _num(w.get("start"))
                                if st_ts <= 0:
                                    _slug_s = str(w.get("slug") or "")
                                    if _slug_s.rsplit("-", 1)[-1].isdigit():
                                        st_ts = float(_slug_s.rsplit("-", 1)[-1])
                                en_ts = _num(w.get("end")) or (st_ts + 300.0 if st_ts > 0 else 0.0)
                                if st_ts > 0 and (first_start_ts <= 0 or st_ts < first_start_ts):
                                    first_start_ts = st_ts
                                if en_ts > last_end_ts:
                                    last_end_ts = en_ts
                                recent_buf.append(w)
                                fills_c = int(_num(w.get("fills_count")))
                                total_fills += fills_c
                                pnl = _num(w.get("net_pnl"))
                                is_traded = bool(w.get("traded") or fills_c > 0 or abs(pnl) > 1e-9)

                                raw_reg = w.get("regime") or "unlabeled"
                                occurrence[raw_reg] = occurrence.get(raw_reg, 0) + 1
                                bm_v = w.get("brier_model")
                                bk_v = w.get("brier_market")
                                if bm_v is not None and bk_v is not None:
                                    try:
                                        bm_f = float(bm_v)
                                        bk_f = float(bk_v)
                                        if math.isfinite(bm_f) and math.isfinite(bk_f):
                                            brier_model_sum += bm_f
                                            brier_market_sum += bk_f
                                            brier_count += 1
                                    except (TypeError, ValueError):
                                        pass

                                if is_traded:
                                    e_mode = _infer_entry_mode(ledger_dir, w)
                                    if e_mode:
                                        w["entry_mode"] = e_mode
                                        eb_key = "taker" if e_mode == "mode_b" else e_mode
                                        eb = exec_breakdown.get(eb_key)
                                        if eb is not None:
                                            eb["count"] += 1
                                            eb["pnl"] = round(eb["pnl"] + pnl, 4)
                                            if pnl > 1e-9:
                                                eb["wins"] += 1
                                            elif pnl < -1e-9:
                                                eb["losses"] += 1
                                    count += 1
                                    total_pnl += pnl
                                    running_pnl += pnl
                                    if running_pnl > peak_pnl:
                                        peak_pnl = running_pnl
                                    dd_now = peak_pnl - running_pnl
                                    if dd_now > max_drawdown:
                                        max_drawdown = dd_now
                                    turnover += _num(w.get("total_cost"))
                                    if pnl > 1e-9:
                                        total_won += pnl
                                        wins += 1
                                    elif pnl < -1e-9:
                                        total_lost += abs(pnl)
                                        losses += 1
                                    if _num(w.get("pos_up")) > 1e-6 and _num(w.get("pos_down")) > 1e-6:
                                        paired_rounds += 1

                                    equity_curve_raw.append((
                                        count,
                                        w.get("slug"),
                                        round(running_pnl, 4),
                                        round(pnl, 4),
                                        _num(w.get("start")),
                                    ))

                                    ts = _num(w.get("end", w.get("start")))
                                    day_k = datetime.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d") if ts > 0 else today_cal
                                    b_day = daily_map.setdefault(day_k, {"date": day_k, "pnl": 0.0, "traded": 0, "wins": 0, "losses": 0})
                                    b_day["pnl"] = round(b_day["pnl"] + pnl, 4)
                                    b_day["traded"] += 1
                                    if pnl > 1e-9:
                                        b_day["wins"] += 1
                                    elif pnl < -1e-9:
                                        b_day["losses"] += 1

                                    # Welford online mean/variance for O(1) memory regime attribution
                                    reg = raw_reg if w.get("regime_source") == "entry_time" else "legacy"
                                    bucket = regime_stats.setdefault(reg, {
                                        "rounds": 0, "traded": 0, "wins": 0, "losses": 0, "flats": 0,
                                        "net_pnl": 0.0, "_n": 0, "_mean": 0.0, "_m2": 0.0,
                                    })
                                    bucket["traded"] += 1
                                    bucket["net_pnl"] = round(bucket["net_pnl"] + pnl, 4)
                                    if pnl > 1e-9:
                                        bucket["wins"] += 1
                                    elif pnl < -1e-9:
                                        bucket["losses"] += 1
                                    else:
                                        bucket["flats"] += 1
                                    n_cur = bucket["_n"] + 1
                                    delta = pnl - bucket["_mean"]
                                    new_mean = bucket["_mean"] + delta / n_cur
                                    bucket["_n"] = n_cur
                                    bucket["_mean"] = new_mean
                                    bucket["_m2"] += delta * (pnl - new_mean)
                    except OSError:
                        pass

                flats = count - wins - losses

                for reg, r_cnt in occurrence.items():
                    regime_stats.setdefault(reg, {
                        "rounds": 0, "traded": 0, "wins": 0, "losses": 0, "flats": 0,
                        "net_pnl": 0.0, "_n": 0, "_mean": 0.0, "_m2": 0.0,
                    })["rounds"] = r_cnt

                if "legacy" in regime_stats:
                    regime_stats["legacy"]["rounds"] = regime_stats["legacy"]["traded"]

                for bucket in regime_stats.values():
                    n = bucket.pop("_n", 0)
                    mean = bucket.pop("_mean", 0.0)
                    m2 = bucket.pop("_m2", 0.0)
                    if n:
                        var = (m2 / (n - 1)) if n > 1 else 0.0
                        sd = math.sqrt(max(0.0, var))
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

                rev_recent = list(reversed(recent_buf))
                daily_summary = [daily_map[k] for k in sorted(daily_map.keys(), reverse=True)[:14]]
                today_bucket = daily_map.get(today_cal, {"pnl": 0.0, "traded": 0, "wins": 0, "losses": 0})
                curve_pts = _downsample_curve(equity_curve_raw, max_points=600)
                curve_compact = [
                    {"idx": idx, "slug": s, "pnl": cum_p, "round_pnl": r_p, "start": st_t}
                    for idx, s, cum_p, r_p, st_t in curve_pts
                ]
                base_rounds_50 = [_public_round(w, {}) for w in rev_recent[:50]]
                # 线性回归斜率评估收益是否持续向上（包含初始起点 (0, 0) 与所有已成交累计净值点）
                trend_slope = 0.0
                pnl_trend = "pending"
                if count >= 3 and equity_curve_raw:
                    xs = [0.0] + [float(i + 1) for i in range(len(equity_curve_raw))]
                    ys = [0.0] + [float(pt[2]) for pt in equity_curve_raw]
                    n_pts = len(xs)
                    mx = sum(xs) / n_pts
                    my = sum(ys) / n_pts
                    denom = sum((x - mx) ** 2 for x in xs)
                    if denom > 1e-9:
                        trend_slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
                    if trend_slope > 0.08 and total_pnl > 0:
                        pnl_trend = "up"
                    elif trend_slope < -0.08 and total_pnl < 0:
                        pnl_trend = "down"
                    else:
                        pnl_trend = "flat"

                agg = {
                    "total_rounds": total_rounds,
                    "first_start_ts": first_start_ts,
                    "last_end_ts": last_end_ts,
                    "trend_slope": round(trend_slope, 4),
                    "pnl_trend": pnl_trend,
                    "total_pnl": total_pnl,
                    "total_won": total_won,
                    "total_lost": total_lost,
                    "turnover": turnover,
                    "wins": wins,
                    "losses": losses,
                    "flats": flats,
                    "paired_rounds": paired_rounds,
                    "total_fills": total_fills,
                    "peak_pnl": peak_pnl,
                    "max_drawdown": max_drawdown,
                    "count": count,
                    "regime_stats": regime_stats,
                    "rev_recent": rev_recent,
                    "page_records": rev_recent[:record_limit],
                    "base_rounds_50": base_rounds_50,
                    "daily_summary": daily_summary,
                    "today_bucket": today_bucket,
                    "curve_compact": curve_compact,
                    "brier_count": brier_count,
                    "brier_model": round(brier_model_sum / brier_count, 5) if brier_count > 0 else None,
                    "brier_market": round(brier_market_sum / brier_count, 5) if brier_count > 0 else None,
                    "execution_breakdown": {
                        k: {
                            "count": v["count"],
                            "wins": v["wins"],
                            "losses": v["losses"],
                            "win_rate": round((v["wins"] / (v["wins"] + v["losses"])) * 100.0, 1) if (v["wins"] + v["losses"]) > 0 else 0.0,
                            "pnl": round(v["pnl"], 4),
                        }
                        for k, v in exec_breakdown.items()
                    },
                }
                with _SUMMARY_AGG_LOCK:
                    _SUMMARY_AGG_CACHE[agg_cache_key] = agg
                    _SUMMARY_AGG_CACHE.move_to_end(agg_cache_key)
                    while len(_SUMMARY_AGG_CACHE) > _SUMMARY_AGG_MAX_ENTRIES:
                        _SUMMARY_AGG_CACHE.popitem(last=False)

            total_rounds = agg["total_rounds"]
            total_pnl = agg["total_pnl"]
            total_won = agg["total_won"]
            total_lost = agg["total_lost"]
            turnover = agg["turnover"]
            wins = agg["wins"]
            losses = agg["losses"]
            flats = agg["flats"]
            paired_rounds = agg["paired_rounds"]
            total_fills = agg["total_fills"]
            peak_pnl = agg["peak_pnl"]
            max_drawdown = agg["max_drawdown"]
            count = agg["count"]
            regime_stats = agg["regime_stats"]
            daily_summary = agg["daily_summary"]
            today_bucket = agg["today_bucket"]
            page_records = agg["page_records"]
            curve_compact = agg["curve_compact"]
            today_key = today_cal

            if include_details:
                latest = agg["rev_recent"][:20]
                rounds = [
                    _public_round(w, _read_detail(ledger_dir, w.get("slug", "")))
                    for w in latest
                ]
            else:
                rounds = list(agg["base_rounds_50"])
            if active and not any(r.get("slug") == active.get("slug") for r in rounds):
                rounds.insert(0, _public_round(active, active, active=True))

            decisive = wins + losses
            win_rate = round((wins / decisive) * 100.0, 1) if decisive else 0.0

            INITIAL_CAPITAL = float(config.PARAMS.get("INITIAL_CAPITAL_USD", 100.0))
            available_capital = round(INITIAL_CAPITAL + total_pnl, 4)
            roi_pct = round((total_pnl / INITIAL_CAPITAL) * 100.0, 2)
            turnover_roi_pct = round((total_pnl / turnover) * 100.0, 2) if turnover > 1e-4 else 0.0

            free_capital = round(max(0.0, available_capital - active_cost), 4)

            exp_start_ts = float(agg.get("first_start_ts") or 0.0)
            if exp_start_ts <= 0 and active:
                exp_start_ts = _num(active.get("start"))
            now_ref_ts = time.time()
            if exp_start_ts > 0:
                end_ref_ts = now_ref_ts if (active and active.get("slug")) else max(float(agg.get("last_end_ts") or 0.0), exp_start_ts)
                elapsed_s = max(0.0, end_ref_ts - exp_start_ts)
            else:
                elapsed_s = 0.0
            elapsed_h = elapsed_s / 3600.0
            if exp_start_ts > 0 and count > 0:
                eff_h = max(1.0, elapsed_h)
                hourly_pnl = round(total_pnl / eff_h, 4)
                hourly_projected = elapsed_h < 1.0
                if elapsed_h >= 24.0:
                    daily_pnl = round(total_pnl / (elapsed_h / 24.0), 4)
                    daily_projected = False
                else:
                    daily_pnl = round(hourly_pnl * 24.0, 4)
                    daily_projected = True
            else:
                hourly_pnl = 0.0
                daily_pnl = 0.0
                hourly_projected = True
                daily_projected = True

            payload_obj = {
                "batch_id": target_batch,
                "current_batch": config.BATCH_ID,
                "available_batches": list(available_batches),
                "initial_capital": INITIAL_CAPITAL,
                "available_capital": available_capital,
                "active_cost": active_cost,
                "free_capital": free_capital,
                "roi_pct": roi_pct,
                "turnover_roi_pct": turnover_roi_pct,
                "total_pnl": round(total_pnl, 4),
                "peak_pnl": round(peak_pnl, 4),
                "max_drawdown": round(max_drawdown, 4),
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
                "paired_rounds": paired_rounds,
                "total_fills": total_fills,
                "total_rounds": total_rounds,
                "records_total": total_rounds,
                "records_truncated": total_rounds > len(page_records),
                "equity_curve": curve_compact,
                "records": page_records,
                "rounds": rounds,
                "regime_stats": regime_stats,
                "brier_count": agg.get("brier_count", 0),
                "brier_model": agg.get("brier_model"),
                "brier_market": agg.get("brier_market"),
                "execution_breakdown": agg.get("execution_breakdown") or {},
                "experiment_start_ts": exp_start_ts if exp_start_ts > 0 else None,
                "elapsed_seconds": round(elapsed_s, 1),
                "elapsed_hours": round(elapsed_h, 2),
                "hourly_pnl": hourly_pnl,
                "hourly_projected": hourly_projected,
                "daily_pnl": daily_pnl,
                "daily_projected": daily_projected,
                "pnl_trend": agg.get("pnl_trend", "pending"),
                "trend_slope": agg.get("trend_slope", 0.0),
            }
            raw_bytes = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")
            gz_bytes = gzip.compress(raw_bytes, compresslevel=5) if len(raw_bytes) > 1024 else None
            if not include_details:
                with _HISTORY_CACHE_LOCK:
                    _HISTORY_CACHE[hist_cache_key] = (raw_bytes, gz_bytes)
                    _HISTORY_CACHE.move_to_end(hist_cache_key)
                    while len(_HISTORY_CACHE) > _HISTORY_CACHE_MAX_ENTRIES:
                        _HISTORY_CACHE.popitem(last=False)
            self._send_json_bytes(raw_bytes, gz_body=gz_bytes)
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
                try:
                    st = file_path.stat()
                except OSError:
                    self.send_error(404)
                    return
                etag = f'W/"{st.st_mtime_ns:x}-{st.st_size:x}"'
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    return
                mime, _ = mimetypes.guess_type(str(file_path))
                mime = mime or "application/octet-stream"
                cache_key = (str(file_path), st.st_mtime_ns, st.st_size)
                with _STATIC_CACHE_LOCK:
                    cached_st = _STATIC_CACHE.get(cache_key)
                    if cached_st is not None:
                        _STATIC_CACHE.move_to_end(cache_key)
                if cached_st is None:
                    raw_body = file_path.read_bytes()
                    is_compressible = len(raw_body) > 1024 and (
                        "text" in mime or "javascript" in mime or "json" in mime or "svg" in mime or "css" in mime
                    )
                    gz_body = gzip.compress(raw_body, compresslevel=5) if is_compressible else None
                    cached_st = (raw_body, gz_body)
                    with _STATIC_CACHE_LOCK:
                        _STATIC_CACHE[cache_key] = cached_st
                        _STATIC_CACHE.move_to_end(cache_key)
                        while len(_STATIC_CACHE) > _STATIC_CACHE_MAX_ENTRIES:
                            _STATIC_CACHE.popitem(last=False)
                raw_body, gz_body = cached_st
                accept_enc = (self.headers.get("Accept-Encoding") or "").lower()
                use_gzip = gz_body is not None and "gzip" in accept_enc
                payload = gz_body if use_gzip else raw_body
                self.send_response(200)
                self.send_header("Content-Type", f"{mime}; charset=utf-8" if "text" in mime or "javascript" in mime else mime)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Vary", "Accept-Encoding")
                if use_gzip:
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_error(404)


def _resolve_bind_host(host=None) -> str:
    if host:
        return str(host)
    return os.environ.get("PM_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"


def _validate_bind_security(bind_host: str):
    """拒绝弱密码/占位密码，且在非回环地址或 PM_REQUIRE_AUTH=1 时强制要求配置强密码。"""
    pwd = os.environ.get("PM_WEB_PASS", "").strip()
    require_auth = (
        bind_host not in ("127.0.0.1", "localhost", "::1")
        or os.environ.get("PM_REQUIRE_AUTH", "").strip().lower() in ("1", "true", "yes")
    )
    if require_auth and not pwd:
        raise RuntimeError(
            f"Refusing to start dashboard on '{bind_host}' without PM_WEB_PASS configured. "
            f"Set a strong PM_WEB_PASS (>= 8 chars) in /etc/pm-quant.env."
        )
    if pwd and (len(pwd) < 8 or pwd.lower() in _WEAK_PASSWORDS):
        raise RuntimeError(
            f"Refusing to start dashboard on '{bind_host}' with a weak/placeholder PM_WEB_PASS. "
            f"Use a strong password (>= 8 chars, not a default placeholder)."
        )


def start_server_in_background(port=8900, host=None):
    """Start investor dashboard HTTP server in a daemon background thread."""
    bind_host = _resolve_bind_host(host)
    _validate_bind_security(bind_host)
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
    _validate_bind_security(bind_host)
    server_address = (bind_host, port)
    httpd = _HardenedHTTPServer(server_address, QuantHandler)
    disp_host = "127.0.0.1" if bind_host == "0.0.0.0" else bind_host
    auth_note = " [BasicAuth 已开启]" if os.environ.get("PM_WEB_PASS") else ""
    print(f"PM-Quant investor dashboard: http://{disp_host}:{port}/{auth_note}")

    def _graceful_term(_sig, _frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    if hasattr(signal, "SIGTERM") and threading.current_thread() is threading.main_thread():
        try:
            signal.signal(signal.SIGTERM, _graceful_term)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("\nServer stopped.")
        httpd.server_close()


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 8900)
