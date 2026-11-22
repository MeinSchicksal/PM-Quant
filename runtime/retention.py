"""PM-Quant Automatic Data Retention & Anti-Disk-Full Capacity Guard.

Two-stage disk protection so small cloud servers (e.g. 30 GiB) never run out of space:
1. Age-based retention: prunes raw `data/1_market/` older than MARKET_RETENTION_DAYS,
   detail replays `windows/*.json.gz` older than WINDOW_REPLAY_RETENTION_DAYS, and
   aged runtime logs.
2. Capacity/watermark auto-guard: monitors host free disk space, disk utilization %,
   and total `data/` quota (`MAX_DATA_DIR_MB`). Under disk pressure, evicts oldest
   raw market folders -> oldest logs -> oldest replay `.json.gz` (keeping at least
   `MIN_KEEP_REPLAY_FILES` recent replays unless critically full). If disk remains
   critically full (`< CRITICAL_FREE_DISK_GB` or `>= DISK_CRITICAL_PAUSE_PCT`),
   automatically pauses raw `1_market` recording so `summary.jsonl` stays safe.

CRITICAL INVARIANT:
- `summary.jsonl` (permanent per-round PnL history) and `live.json` are NEVER deleted.
- Today's UTC date folder under `data/1_market/` is NEVER deleted.
"""
import datetime
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, Any, Optional, Callable

import config

_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SLUG_EPOCH_RE = re.compile(r"(\d{9,11})\.json\.gz$")
# Heavy raw stream categories safe to prune; tiny official settlement truth is preserved
_PRUNABLE_MARKET_CATS = ("events", "oracle", "books", "spot")

_GUARD_LOCK = threading.Lock()
_DISK_GUARD_STATE: Dict[str, Any] = {
    "paused": False,
    "reason": "",
    "checked_at": 0.0,
    "pressure_triggered": False,
    "last_cleanup": None,
}


def is_raw_recording_allowed() -> bool:
    """Return False when raw market recording is disabled or paused by critical disk guard."""
    if not bool(config.PARAMS.get("RECORD_RAW_MARKET", True)):
        return False
    with _GUARD_LOCK:
        return not bool(_DISK_GUARD_STATE.get("paused", False))


def get_disk_guard_state() -> Dict[str, Any]:
    with _GUARD_LOCK:
        return dict(_DISK_GUARD_STATE)


def _dir_stats(path: Path) -> tuple:
    """Return (total_bytes, file_count) for a path."""
    total = 0
    cnt = 0
    if not path.exists():
        return 0, 0
    if path.is_file():
        try:
            return path.stat().st_size, 1
        except OSError:
            return 0, 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
                cnt += 1
            except OSError:
                pass
    return total, cnt


def _parse_window_gz_ts(file_path: Path) -> float:
    """Extract window epoch from `btc-updown-5m-<epoch>.json.gz`, fallback to mtime."""
    m = _SLUG_EPOCH_RE.search(file_path.name)
    if m:
        try:
            return float(int(m.group(1)))
        except ValueError:
            pass
    try:
        return float(file_path.stat().st_mtime)
    except OSError:
        return time.time()


def get_storage_breakdown(
    market_dir: Optional[Path] = None,
    ledgers_dir: Optional[Path] = None,
    reports_dir: Optional[Path] = None,
    runtime_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Return detailed MB and file-count breakdown for settings & monitoring views."""
    m_dir = Path(market_dir) if market_dir is not None else config.MARKET_DIR
    l_dir = Path(ledgers_dir) if ledgers_dir is not None else config.LEDGERS_DIR
    r_dir = Path(reports_dir) if reports_dir is not None else config.REPORTS_DIR
    rt_dir = Path(runtime_dir) if runtime_dir is not None else config.RUNTIME_DIR

    m_b, m_f = _dir_stats(m_dir)
    r_b, r_f = _dir_stats(r_dir)
    rt_b, rt_f = _dir_stats(rt_dir)

    # Split 2_ledgers into permanent summary.jsonl vs replay windows/*.json.gz vs other
    sum_b = sum_f = rep_b = rep_f = other_l_b = other_l_f = 0
    if l_dir.exists():
        for item in l_dir.rglob("*"):
            if not item.is_file():
                continue
            try:
                sz = item.stat().st_size
            except OSError:
                continue
            if item.name == "summary.jsonl":
                sum_b += sz
                sum_f += 1
            elif item.name.endswith(".json.gz") and item.parent.name == "windows":
                rep_b += sz
                rep_f += 1
            else:
                other_l_b += sz
                other_l_f += 1

    l_b = sum_b + rep_b + other_l_b
    l_f = sum_f + rep_f + other_l_f
    tot_b = m_b + l_b + r_b + rt_b
    tot_f = m_f + l_f + r_f + rt_f
    mb = 1024.0 * 1024.0
    return {
        "market_mb": round(m_b / mb, 2),
        "market_files": m_f,
        "ledgers_mb": round(l_b / mb, 2),
        "ledgers_files": l_f,
        "summary_mb": round(sum_b / mb, 3),
        "summary_files": sum_f,
        "replays_mb": round(rep_b / mb, 2),
        "replay_files": rep_f,
        "reports_mb": round(r_b / mb, 2),
        "reports_files": r_f,
        "runtime_mb": round(rt_b / mb, 2),
        "runtime_files": rt_f,
        "total_mb": round(tot_b / mb, 2),
        "total_files": tot_f,
    }


def _read_disk_metrics(anchor: Path, freed_bytes: int = 0, disk_usage_fn: Optional[Callable] = None) -> Dict[str, float]:
    fn = disk_usage_fn or shutil.disk_usage
    target = anchor if anchor.exists() else config.ROOT
    du = fn(target)
    gb = 1024.0 ** 3
    # When dry_run=True or custom mock disk_usage_fn is used, reflect virtual freed_bytes cleanly
    used = max(0, int(du.used) - int(freed_bytes)) if disk_usage_fn is not None else int(du.used)
    free = min(int(du.total), int(du.free) + int(freed_bytes)) if disk_usage_fn is not None else int(du.free)
    total = max(1, int(du.total))
    return {
        "disk_total_gb": round(total / gb, 2),
        "disk_used_gb": round(used / gb, 2),
        "disk_free_gb": round(free / gb, 2),
        "disk_used_pct": round((used / total) * 100.0, 1),
    }


def cleanup_old_data(
    market_dir: Optional[Path] = None,
    ledgers_dir: Optional[Path] = None,
    logs_dir: Optional[Path] = None,
    market_days: Optional[int] = None,
    replay_days: Optional[int] = None,
    log_days: Optional[int] = None,
    now: Optional[float] = None,
    dry_run: bool = False,
    disk_usage_fn: Optional[Callable] = None,
    max_data_mb: Optional[float] = None,
    min_free_gb: Optional[float] = None,
    disk_hard_pct: Optional[float] = None,
    disk_soft_pct: Optional[float] = None,
    update_guard_state: bool = True,
) -> Dict[str, Any]:
    """Prune expired files AND automatically relieve disk pressure before disk fills up.

    Never touches `summary.jsonl`, `live.json`, `.gitkeep`, or today's active UTC folder.
    """
    now_ts = time.time() if now is None else float(now)
    m_dir = Path(market_dir) if market_dir is not None else config.MARKET_DIR
    l_dir = Path(ledgers_dir) if ledgers_dir is not None else config.LEDGERS_DIR
    g_dir = Path(logs_dir) if logs_dir is not None else config.LOGS_DIR

    m_days = int(config.PARAMS.get("MARKET_RETENTION_DAYS", 3)) if market_days is None else int(market_days)
    r_days = int(config.PARAMS.get("WINDOW_REPLAY_RETENTION_DAYS", 7)) if replay_days is None else int(replay_days)
    lg_days = int(config.PARAMS.get("RUNTIME_LOG_RETENTION_DAYS", 7)) if log_days is None else int(log_days)
    min_keep_replays = int(config.PARAMS.get("MIN_KEEP_REPLAY_FILES", 30))

    quota_mb = float(config.PARAMS.get("MAX_DATA_DIR_MB", 4096.0)) if max_data_mb is None else float(max_data_mb)
    free_floor_gb = float(config.PARAMS.get("MIN_FREE_DISK_GB", 3.0)) if min_free_gb is None else float(min_free_gb)
    hard_pct = float(config.PARAMS.get("DISK_HARD_LIMIT_PCT", 85.0)) if disk_hard_pct is None else float(disk_hard_pct)
    soft_pct = float(config.PARAMS.get("DISK_SOFT_LIMIT_PCT", 75.0)) if disk_soft_pct is None else float(disk_soft_pct)
    crit_pct = float(config.PARAMS.get("DISK_CRITICAL_PAUSE_PCT", 93.0))
    crit_free_gb = float(config.PARAMS.get("CRITICAL_FREE_DISK_GB", 0.8))

    today_utc = datetime.datetime.fromtimestamp(now_ts, datetime.timezone.utc).date()
    market_cutoff_date = today_utc - datetime.timedelta(days=max(1, m_days))
    replay_cutoff_ts = now_ts - max(1, r_days) * 86400.0
    log_cutoff_ts = now_ts - max(1, lg_days) * 86400.0

    removed_dirs = 0
    removed_files = 0
    freed_bytes = 0
    pressure_removed_dirs = 0
    pressure_removed_files = 0
    errors = []
    deleted_paths = set()

    # ── Stage 1: Normal Age-Based Retention ──────────────────────────────────
    if m_days > 0 and m_dir.exists():
        for cat in _PRUNABLE_MARKET_CATS:
            cat_dir = m_dir / cat
            if not cat_dir.is_dir():
                continue
            for child in sorted(cat_dir.iterdir()):
                if not child.is_dir() or not _DATE_DIR_RE.match(child.name):
                    continue
                try:
                    folder_date = datetime.date.fromisoformat(child.name)
                except ValueError:
                    continue
                if folder_date >= today_utc or folder_date >= market_cutoff_date:
                    continue
                sz, fcnt = _dir_stats(child)
                if not dry_run:
                    try:
                        shutil.rmtree(child)
                    except OSError as exc:
                        errors.append(f"{child}: {exc}")
                        continue
                deleted_paths.add(child)
                removed_dirs += 1
                removed_files += fcnt
                freed_bytes += sz

    if r_days > 0 and l_dir.exists():
        for batch_dir in sorted(l_dir.iterdir()):
            if not batch_dir.is_dir():
                continue
            win_dir = batch_dir / "windows"
            if not win_dir.is_dir():
                continue
            for gz_file in win_dir.glob("*.json.gz"):
                if not gz_file.is_file():
                    continue
                ts = _parse_window_gz_ts(gz_file)
                if ts >= replay_cutoff_ts:
                    continue
                try:
                    sz = gz_file.stat().st_size
                except OSError:
                    sz = 0
                if not dry_run:
                    try:
                        gz_file.unlink()
                    except OSError as exc:
                        errors.append(f"{gz_file}: {exc}")
                        continue
                deleted_paths.add(gz_file)
                removed_files += 1
                freed_bytes += sz

    if lg_days > 0 and g_dir.exists():
        for log_file in g_dir.iterdir():
            if not log_file.is_file() or log_file.name == ".gitkeep":
                continue
            try:
                st = log_file.stat()
            except OSError:
                continue
            if st.st_mtime >= log_cutoff_ts:
                continue
            if not dry_run:
                try:
                    log_file.unlink()
                except OSError as exc:
                    errors.append(f"{log_file}: {exc}")
                    continue
            deleted_paths.add(log_file)
            removed_files += 1
            freed_bytes += st.st_size

    # ── Stage 2: Disk Capacity & Quota Watermark Guard ───────────────────────
    anchor = m_dir.parent if m_dir.parent.exists() else config.DATA_DIR
    cur_storage = get_storage_breakdown(
        market_dir=m_dir,
        ledgers_dir=l_dir,
        reports_dir=config.REPORTS_DIR if market_dir is None else (m_dir.parent / "3_reports"),
        runtime_dir=config.RUNTIME_DIR if logs_dir is None else g_dir.parent,
    )
    virtual_freed_mb = (freed_bytes / (1024.0 * 1024.0)) if dry_run else 0.0
    cur_data_mb = max(0.0, cur_storage["total_mb"] - virtual_freed_mb)
    disk_m = _read_disk_metrics(anchor, freed_bytes=freed_bytes if (dry_run or disk_usage_fn) else 0, disk_usage_fn=disk_usage_fn)

    def _is_under_pressure(dm: Dict[str, float], data_mb: float, use_soft: bool = False) -> bool:
        pct_lim = soft_pct if use_soft else hard_pct
        free_lim = (free_floor_gb + 0.5) if use_soft else free_floor_gb
        q_lim = (quota_mb * 0.85) if (use_soft and quota_mb > 0) else quota_mb
        if dm["disk_used_pct"] >= pct_lim:
            return True
        if dm["disk_free_gb"] < free_lim:
            return True
        if q_lim > 0 and data_mb > q_lim:
            return True
        return False

    pressure_triggered = _is_under_pressure(disk_m, cur_data_mb, use_soft=False)
    if pressure_triggered:
        # 2a. Evict oldest non-today 1_market date folders first
        market_candidates = []
        if m_dir.exists():
            for cat in _PRUNABLE_MARKET_CATS:
                cat_dir = m_dir / cat
                if not cat_dir.is_dir():
                    continue
                for child in cat_dir.iterdir():
                    if child in deleted_paths or not child.is_dir() or not _DATE_DIR_RE.match(child.name):
                        continue
                    try:
                        fdate = datetime.date.fromisoformat(child.name)
                    except ValueError:
                        continue
                    if fdate >= today_utc:
                        continue
                    market_candidates.append((fdate, child))
        market_candidates.sort(key=lambda x: x[0])

        for _, child in market_candidates:
            if not _is_under_pressure(disk_m, cur_data_mb, use_soft=True):
                break
            sz, fcnt = _dir_stats(child)
            if not dry_run:
                try:
                    shutil.rmtree(child)
                except OSError as exc:
                    errors.append(f"{child}: {exc}")
                    continue
            deleted_paths.add(child)
            removed_dirs += 1
            pressure_removed_dirs += 1
            removed_files += fcnt
            pressure_removed_files += fcnt
            freed_bytes += sz
            cur_data_mb = max(0.0, cur_data_mb - sz / (1024.0 * 1024.0))
            disk_m = _read_disk_metrics(anchor, freed_bytes=freed_bytes if (dry_run or disk_usage_fn) else 0, disk_usage_fn=disk_usage_fn)

        # 2b. If still under pressure, evict oldest runtime logs
        if _is_under_pressure(disk_m, cur_data_mb, use_soft=True) and g_dir.exists():
            log_candidates = []
            for lf in g_dir.iterdir():
                if lf in deleted_paths or not lf.is_file() or lf.name == ".gitkeep":
                    continue
                try:
                    st = lf.stat()
                    log_candidates.append((st.st_mtime, st.st_size, lf))
                except OSError:
                    continue
            log_candidates.sort(key=lambda x: x[0])
            for _, sz, lf in log_candidates:
                if not _is_under_pressure(disk_m, cur_data_mb, use_soft=True):
                    break
                if not dry_run:
                    try:
                        lf.unlink()
                    except OSError as exc:
                        errors.append(f"{lf}: {exc}")
                        continue
                deleted_paths.add(lf)
                removed_files += 1
                pressure_removed_files += 1
                freed_bytes += sz
                cur_data_mb = max(0.0, cur_data_mb - sz / (1024.0 * 1024.0))
                disk_m = _read_disk_metrics(anchor, freed_bytes=freed_bytes if (dry_run or disk_usage_fn) else 0, disk_usage_fn=disk_usage_fn)

        # 2c. If still under pressure, evict oldest replay `.json.gz` while preserving recent replays
        if _is_under_pressure(disk_m, cur_data_mb, use_soft=True) and l_dir.exists():
            gz_candidates = []
            for batch_dir in l_dir.iterdir():
                if not batch_dir.is_dir():
                    continue
                win_dir = batch_dir / "windows"
                if not win_dir.is_dir():
                    continue
                for gz in win_dir.glob("*.json.gz"):
                    if gz in deleted_paths or not gz.is_file():
                        continue
                    try:
                        sz = gz.stat().st_size
                    except OSError:
                        sz = 0
                    gz_candidates.append((_parse_window_gz_ts(gz), sz, gz))
            gz_candidates.sort(key=lambda x: x[0])
            # Keep newest `min_keep_replays` unless disk is critically full
            is_critical_now = (disk_m["disk_used_pct"] >= crit_pct) or (disk_m["disk_free_gb"] < crit_free_gb)
            evictable = gz_candidates if is_critical_now else gz_candidates[:-min_keep_replays] if min_keep_replays > 0 else gz_candidates
            for _, sz, gz in evictable:
                if not _is_under_pressure(disk_m, cur_data_mb, use_soft=True):
                    break
                if not dry_run:
                    try:
                        gz.unlink()
                    except OSError as exc:
                        errors.append(f"{gz}: {exc}")
                        continue
                deleted_paths.add(gz)
                removed_files += 1
                pressure_removed_files += 1
                freed_bytes += sz
                cur_data_mb = max(0.0, cur_data_mb - sz / (1024.0 * 1024.0))
                disk_m = _read_disk_metrics(anchor, freed_bytes=freed_bytes if (dry_run or disk_usage_fn) else 0, disk_usage_fn=disk_usage_fn)

    # ── Stage 3: Critical Disk-Full Circuit-Breaker ──────────────────────────
    should_pause = (disk_m["disk_used_pct"] >= crit_pct) or (disk_m["disk_free_gb"] < crit_free_gb)
    pause_reason = ""
    if should_pause:
        pause_reason = (
            f"磁盘空间危急 (已用 {disk_m['disk_used_pct']:.1f}% / 剩余 {disk_m['disk_free_gb']:.2f} GB)，"
            f"已自动暂停原始行情录制以保护账本"
        )

    result = {
        "removed_dirs": removed_dirs,
        "removed_files": removed_files,
        "pressure_triggered": pressure_triggered,
        "pressure_removed_dirs": pressure_removed_dirs,
        "pressure_removed_files": pressure_removed_files,
        "freed_bytes": freed_bytes,
        "freed_mb": round(freed_bytes / (1024.0 * 1024.0), 2),
        "market_retention_days": m_days,
        "replay_retention_days": r_days,
        "log_retention_days": lg_days,
        "max_data_dir_mb": quota_mb,
        "min_free_disk_gb": free_floor_gb,
        "disk_guard_paused": should_pause,
        "disk_guard_reason": pause_reason,
        "disk": disk_m,
        "dry_run": bool(dry_run),
        "errors": errors,
        "timestamp": round(now_ts, 2),
    }

    if update_guard_state and not dry_run:
        with _GUARD_LOCK:
            _DISK_GUARD_STATE["paused"] = should_pause
            _DISK_GUARD_STATE["reason"] = pause_reason
            _DISK_GUARD_STATE["checked_at"] = now_ts
            _DISK_GUARD_STATE["pressure_triggered"] = pressure_triggered
            _DISK_GUARD_STATE["last_cleanup"] = {
                "timestamp": result["timestamp"],
                "freed_mb": result["freed_mb"],
                "removed_files": result["removed_files"],
                "removed_dirs": result["removed_dirs"],
                "pressure_triggered": pressure_triggered,
            }
    return result
