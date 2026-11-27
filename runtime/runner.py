"""PM-Quant Live/Paper Trading Runner & Ledger Journal.

Orchestrates the decision cycle, records atomic live state,
and appends single-line summary events to summary.jsonl.
"""
import os
import sys
import json
import math
import random
import time
import gzip
import threading
from collections import deque
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.types import Position, OrderBook, SpotState, Order, Decision
from core.policy import plan_decision, determine_objective_side, required_entry_edge
from core.estimator import estimate_p_fair
from core.execution import plan_order_actions, taker_fee_per_share
from core.circuit_breaker import CircuitBreaker
from runtime.retention import is_replay_archive_allowed
from sim.matcher import QueueMatcher, is_books_consistent


class PaperRunner:
    @staticmethod
    def _fsync_dir(dir_path: Path):
        """Best-effort directory fsync on POSIX so atomic rename metadata survives power loss."""
        if sys.platform == "win32":
            return
        try:
            dfd = os.open(str(dir_path), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    def _update_and_get_book_momentum(
        self,
        now: float,
        book_up: OrderBook,
        book_down: OrderBook,
        window_s: float = 5.0,
    ):
        """Record live orderbook mids and return recent ~5s (mkt_mom_up, mkt_mom_down).

        Strictly backward-looking (zero look-ahead) to guard against buying or
        resting passive bids right as the orderbook is collapsing under toxic flow.
        """
        buf = getattr(self, "_mkt_mid_buf", None)
        if buf is None:
            buf = deque(maxlen=400)
            self._mkt_mid_buf = buf

        mid_u = None
        if book_up is not None and book_up.best_bid is not None and book_up.best_ask is not None and book_up.best_bid < book_up.best_ask:
            mid_u = 0.5 * (float(book_up.best_bid) + float(book_up.best_ask))
        mid_d = None
        if book_down is not None and book_down.best_bid is not None and book_down.best_ask is not None and book_down.best_bid < book_down.best_ask:
            mid_d = 0.5 * (float(book_down.best_bid) + float(book_down.best_ask))

        if mid_u is None or mid_d is None:
            return None, None

        win_s = max(1.5, float(window_s or 5.0))
        if not buf or now > buf[-1][0] + 1e-6:
            buf.append((float(now), mid_u, mid_d))
        elif abs(now - buf[-1][0]) <= 1e-6:
            buf[-1] = (float(now), mid_u, mid_d)

        cutoff = float(now) - (win_s + 5.0)
        while len(buf) > 2 and buf[0][0] < cutoff:
            buf.popleft()

        target_t = float(now) - win_s
        ref = None
        best_dt = 9999.0
        for pt in buf:
            age = float(now) - pt[0]
            if age < 1.0:
                continue
            if age > win_s + 2.0:
                continue
            dt = abs(pt[0] - target_t)
            if dt < best_dt:
                best_dt = dt
                ref = pt

        if ref is None:
            return None, None
        return round(mid_u - ref[1], 5), round(mid_d - ref[2], 5)

    def _effective_wire_latency(self, is_cancel: bool = False, replay_mode: bool = False) -> float:
        """Compute realistic order/cancel wire + CLOB engine latency in seconds.

        - In live paper mode with real measured Polymarket RTT (`poly_latency_ms`),
          computes `max(LATENCY_MIN_S, poly_rtt_ms / 2 + LATENCY_ENGINE_OVERHEAD_MS)`
          and applies bounded +/- `LATENCY_JITTER_PCT` real-world jitter.
        - In offline replay mode (or when no RTT measurement is available), uses the
          deterministic configured `LATENCY_S` / `CANCEL_LATENCY_S` fallback.
        """
        key = "CANCEL_LATENCY_S" if is_cancel else "LATENCY_S"
        base = float(config.PARAMS.get(key, config.PARAMS.get("LATENCY_S", 0.18)))
        if base <= 1e-9:
            return 0.0
        min_s = max(0.0, float(config.PARAMS.get("LATENCY_MIN_S", 0.15)))
        max_s = max(min_s, float(config.PARAMS.get("LATENCY_MAX_S", 0.60)))
        if not replay_mode and bool(config.PARAMS.get("LATENCY_DYNAMIC_FROM_RTT", True)):
            ns = getattr(self, "net_stats", None) or {}
            poly_rtt_ms = float(ns.get("poly_latency_ms", 0.0) or 0.0)
            if poly_rtt_ms > 0.5:
                overhead_ms = float(config.PARAMS.get("LATENCY_ENGINE_OVERHEAD_MS", 110.0))
                base = max(min_s, (poly_rtt_ms * 0.5 + overhead_ms) / 1000.0)
            else:
                base = max(min_s, base)
            jitter = max(0.0, min(0.5, float(config.PARAMS.get("LATENCY_JITTER_PCT", 0.15))))
            if jitter > 0.0:
                base = base * random.uniform(1.0 - jitter, 1.0 + jitter)
            floor_after_jitter = min_s * (1.0 - jitter)
            base = max(floor_after_jitter, min(max_s, base))
        return base

    @staticmethod
    def _atomic_replace(tmp_path: Path, target_path: Path, retries: int = 5):
        """Atomically replace target_path with short backoff retries for Windows concurrent read locks."""
        for attempt in range(max(1, int(retries))):
            try:
                tmp_path.replace(target_path)
                PaperRunner._fsync_dir(target_path.parent)
                return
            except (PermissionError, OSError):
                if attempt + 1 >= retries:
                    raise
                time.sleep(0.01 * (attempt + 1))

    @staticmethod
    def _atomic_write_text(target_path: Path, content: str, tmp_suffix: str = ".tmp"):
        """Write text to temp file, flush + fsync, then atomically replace target_path."""
        tmp = target_path.with_suffix(tmp_suffix)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            PaperRunner._atomic_replace(tmp, target_path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @staticmethod
    def _atomic_write_gzip_json(target_path: Path, payload: dict):
        """Write gzipped JSON with explicit file fsync before atomic replace."""
        tmp_gz = target_path.with_suffix(".gz.tmp")
        try:
            with open(tmp_gz, "wb") as raw_f:
                with gzip.GzipFile(fileobj=raw_f, mode="wb") as gz_f:
                    gz_f.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                raw_f.flush()
                try:
                    os.fsync(raw_f.fileno())
                except OSError:
                    pass
            PaperRunner._atomic_replace(tmp_gz, target_path)
        except Exception:
            try:
                tmp_gz.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @staticmethod
    def _extract_book_prob(book: Optional[OrderBook]) -> Optional[float]:
        """Extract implied probability from a book without treating None as 0.5."""
        if book is None:
            return None
        for attr in ("mid", "best_bid", "best_ask"):
            val = getattr(book, attr, None)
            if val is not None:
                return float(val)
        return None

    def __init__(self, ledger_dir=None, async_live_write: bool = False):
        self.ledger_dir = Path(ledger_dir) if ledger_dir else config.CURRENT_LEDGER_DIR
        self.summary_file = self.ledger_dir / "summary.jsonl"
        self.live_file = self.ledger_dir / "live.json"
        self.windows_dir = self.ledger_dir / "windows"
        self.windows_dir.mkdir(parents=True, exist_ok=True)
        self._async_live_write = bool(async_live_write)
        self._live_write_lock = threading.Lock()
        self._live_write_event = threading.Event()
        self._live_write_stop = threading.Event()
        self._live_idle_event = threading.Event()
        self._live_idle_event.set()
        self._live_pending_payload: Optional[str] = None
        self._live_writer_thread: Optional[threading.Thread] = None
        if self._async_live_write:
            self._live_writer_thread = threading.Thread(
                target=self._live_writer_loop, daemon=True, name="LiveJsonWriter"
            )
            self._live_writer_thread.start()
        self.matcher = QueueMatcher(config.PARAMS["LATENCY_S"])
        self._mkt_mid_buf = deque(maxlen=400)
        
        self.circuit_breaker = CircuitBreaker(
            max_daily_drawdown_usd=config.PARAMS.get("MAX_DAILY_DRAWDOWN_USD", 8.00),
            loss_streak_cooling=int(config.PARAMS.get("LOSS_STREAK_COOLING", 3)),
            cooling_rounds=int(config.PARAMS.get("COOLING_ROUNDS", 3)),
            enabled=bool(config.PARAMS.get("ENABLE_CIRCUIT_BREAKER", True))
        )
        self.current_slug = ""
        self.window_start = 0.0
        self.window_end = 0.0
        self.pos_up = Position(0.0, 0.0)
        self.pos_down = Position(0.0, 0.0)
        self.working_orders = []
        self.pending_cancels = []
        self.pending_takers = []
        self.last_record = {}
        self.fills = []
        self.decisions = []
        self.decision_timeline = []
        self._last_timeline_key = None
        self._last_timeline_t = 0.0
        self.actions = []
        self.insurance_paid = 0.0
        self.realized_unwind_pnl = 0.0
        self.unwind_cost_basis = 0.0
        self.unwind_proceeds = 0.0
        self.total_pnl = 0.0
        self._settled_slugs = set()
        # Keep only the recent ~48h (600 windows) in memory for O(1) CircuitBreaker rebuilds
        # so 1-year ledgers (105k+ rows) never blow RAM or stall the main loop.
        self._recent_records = deque(maxlen=600)
        self._initial_capital = float(config.PARAMS.get("INITIAL_CAPITAL_USD", 100.0))
        # Monotonic order ids. The old `int(now*1000) % 1000000` wrapped every
        # ~16.7 minutes and could reuse a stale queue_ahead entry.
        self._next_order_id = 0
        self.price_history = []
        self.regime_dwell = {}
        self._last_regime = None
        self._last_regime_t = None
        self.entry_regime = None
        self._last_step_time = None
        self._spot_buf = []
        self._basis_ema = None
        self._basis_updated_t = 0.0
        self._basis_sample_count = 0
        self._basis_anomaly_active = False
        self._basis_recover_since = None
        self._last_basis_dev_usd = 0.0
        self.window_unwound = False
        self._ext_div_since_up = None
        self._ext_div_since_dn = None
        self._last_basis_poly_px = None
        self._last_basis_poly_t = 0.0
        self._last_hist_t = 0.0
        self._last_wait_dec_t = 0.0
        self._last_live_write_t = 0.0
        self._load_ledger_history()

    def _load_ledger_history(self):
        """Stream summary.jsonl with O(1) memory to restore PnL, settled slugs, and recent CircuitBreaker state."""
        self._settled_slugs = set()
        self._recent_records = deque(maxlen=600)
        self.total_pnl = 0.0
        if not self.summary_file.exists():
            return
        bad_lines = 0
        try:
            with open(self.summary_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        bad_lines += 1
                        continue
                    if not isinstance(rec, dict):
                        continue
                    slug = rec.get("slug")
                    if slug:
                        self._settled_slugs.add(slug)
                    if rec.get("traded"):
                        self.total_pnl += float(rec.get("net_pnl", 0.0))
                    self._recent_records.append(rec)
        except OSError as exc:
            print(f"[Runner] Failed to read summary.jsonl: {exc}")
            return
        if bad_lines > 0:
            print(f"[Runner] WARNING: Skipped {bad_lines} corrupted line(s) in {self.summary_file}")

        self.circuit_breaker.load_from_summary(list(self._recent_records))

    def _rebuild_recent_circuit_breaker(self):
        """Rebuild CircuitBreaker from bounded in-memory recent records in <1ms."""
        self.circuit_breaker = CircuitBreaker(
            max_daily_drawdown_usd=config.PARAMS.get("MAX_DAILY_DRAWDOWN_USD", 8.00),
            loss_streak_cooling=int(config.PARAMS.get("LOSS_STREAK_COOLING", 3)),
            cooling_rounds=int(config.PARAMS.get("COOLING_ROUNDS", 3)),
            enabled=bool(config.PARAMS.get("ENABLE_CIRCUIT_BREAKER", True))
        )
        self.circuit_breaker.load_from_summary(list(self._recent_records))

    def _rebuild_accounting(self):
        """Full fallback rebuild from summary.jsonl (used only if reconciling an ancient non-recent slug)."""
        self.total_pnl = 0.0
        self.circuit_breaker = CircuitBreaker(
            max_daily_drawdown_usd=config.PARAMS.get("MAX_DAILY_DRAWDOWN_USD", 8.00),
            loss_streak_cooling=int(config.PARAMS.get("LOSS_STREAK_COOLING", 3)),
            cooling_rounds=int(config.PARAMS.get("COOLING_ROUNDS", 3)),
            enabled=bool(config.PARAMS.get("ENABLE_CIRCUIT_BREAKER", True))
        )
        self._load_ledger_history()

    def start_window(
        self,
        slug: str,
        start_t: float,
        end_t: float,
        open_px: float,
        binance_open_px: float = None,
        poly_spot: float = None,
        poly_open_provisional: Optional[bool] = None,
        resume_from_live: bool = False,
    ):
        """Initialize fresh 5-minute session, optionally resuming mid-window state from live.json."""
        self.current_slug = slug
        self.window_start = start_t
        self.window_end = end_t
        self.open_price = open_px                                   # official Poly TWAP strike
        # The two reference chains have different opening prices. Keeping them
        # separate is what makes return_bps, delta_s and the chart honest.
        self.binance_open_price = float(binance_open_px or open_px)
        self.last_spot_price = self.binance_open_price              # real Binance spot
        self.last_poly_spot = poly_spot or open_px                  # lagging Poly TWAP
        self.poly_spot_fresh = bool(poly_spot and poly_spot > 0)
        if poly_open_provisional is None:
            self.poly_open_is_provisional = bool(binance_open_px and open_px == binance_open_px and open_px > 0)
        else:
            self.poly_open_is_provisional = bool(poly_open_provisional)
        self.pos_up = Position(0.0, 0.0)
        self.pos_down = Position(0.0, 0.0)
        self.working_orders = []
        self.pending_cancels = []
        self.pending_takers = []
        self.last_record = {}
        self.fills = []
        self.decisions = []
        self.decision_timeline = []
        self._last_timeline_key = None
        self._last_timeline_t = 0.0
        self.actions = []
        self.insurance_paid = 0.0
        self.realized_unwind_pnl = 0.0
        self.unwind_cost_basis = 0.0
        self.unwind_proceeds = 0.0
        self.window_unwound = False
        self._ext_div_since_up = None
        self._ext_div_since_dn = None
        self._ext_div_last_seen_up = None
        self._ext_div_last_seen_dn = None
        self.entries_count = 0
        self.flips_count = 0
        self.last_flip_side = None
        self.quote_attempts = 0
        self.entry_order_ids = set()
        self.last_entry_fill_t = None
        self.min_opp_ask_seen = None
        self.regime_dwell = {}
        self._last_regime = None
        self._last_regime_t = None
        self.entry_regime = None
        self._last_step_time = None
        self._last_hist_t = 0.0
        self._last_wait_dec_t = 0.0
        self._last_live_write_t = 0.0
        self.matcher = QueueMatcher(config.PARAMS["LATENCY_S"])
        self._mkt_mid_buf = deque(maxlen=400)
        self.price_history = [
            {
                "t": round(start_t, 1),
                "price": round(self.last_spot_price, 2),
                "poly_price": round(self.last_poly_spot, 2),
                "poly_stale": not self.poly_spot_fresh,
                "return_bps": 0.0,
                "poly_return_bps": 0.0,
                "poly_up": 0.5,
                "poly_down": 0.5
            }
        ] if open_px > 0 else []
        self.last_prob_up = 0.5
        self.last_poly_up = 0.5
        self.last_poly_down = 0.5
        # Independent Alpha benchmark snapshots (model P*(Up) vs market mid) for Brier Score evaluation
        self._mid_p_up = None
        self._mid_mkt_up = None
        self._entry_p_up = None
        self._entry_mkt_up = None
        self._entry_side_prob = None
        self._active_leg_entry_prob = None
        if resume_from_live:
            self._try_resume_from_live(slug)

    @staticmethod
    def _compact_price_history(hist: list, max_points: int = 1500) -> list:
        """Compress an oversized price_history WITHOUT ever truncating the 00:00 window head.

        Keeps index 0 (window open anchor) and the most recent tail intact, thinning only
        the older interior by 2x so the chart always spans the full 00:00..05:00 timeline.
        """
        if len(hist) <= max_points:
            return hist
        keep_tail = min(300, max_points // 3)
        head = hist[:1]
        older = hist[1:-keep_tail] if keep_tail > 0 else hist[1:]
        tail = hist[-keep_tail:] if keep_tail > 0 else []
        while len(head) + len(older) + len(tail) > max_points and len(older) > 2:
            older = older[::2]
        return head + older + tail

    def _try_resume_from_live(self, slug: str) -> bool:
        """Restore same-window inventory and fill actions after an unexpected restart."""
        if not slug or not self.live_file.exists():
            return False
        try:
            raw = json.loads(self.live_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(raw, dict) or raw.get("slug") != slug:
            return False

        pos_up_raw = raw.get("pos_up") or {}
        pos_dn_raw = raw.get("pos_down") or {}
        try:
            up_sh = max(0.0, float(pos_up_raw.get("shares", 0.0) or 0.0))
            up_cash = max(0.0, float(pos_up_raw.get("cash", 0.0) or 0.0))
            dn_sh = max(0.0, float(pos_dn_raw.get("shares", 0.0) or 0.0))
            dn_cash = max(0.0, float(pos_dn_raw.get("cash", 0.0) or 0.0))
        except (TypeError, ValueError):
            return False

        self.pos_up = Position(up_sh, up_cash)
        self.pos_down = Position(dn_sh, dn_cash)
        try:
            self.insurance_paid = max(0.0, float(raw.get("insurance_paid", 0.0) or 0.0))
        except (TypeError, ValueError):
            self.insurance_paid = 0.0
        try:
            u_pnl = float(raw.get("realized_unwind_pnl", 0.0) or 0.0)
            self.realized_unwind_pnl = u_pnl if math.isfinite(u_pnl) else 0.0
        except (TypeError, ValueError):
            self.realized_unwind_pnl = 0.0
        try:
            u_cost = float(raw.get("unwind_cost_basis", 0.0) or 0.0)
            u_proc = float(raw.get("unwind_proceeds", 0.0) or 0.0)
            self.unwind_cost_basis = max(0.0, u_cost) if math.isfinite(u_cost) else 0.0
            self.unwind_proceeds = max(0.0, u_proc) if math.isfinite(u_proc) else 0.0
        except (TypeError, ValueError):
            self.unwind_cost_basis = 0.0
            self.unwind_proceeds = 0.0
        self.window_unwound = bool(raw.get("window_unwound", False)) or (self.unwind_proceeds > 1e-6)

        restored_actions = raw.get("actions")
        if isinstance(restored_actions, list):
            self.actions = [a for a in restored_actions if isinstance(a, dict)]
            self.fills = [dict(a) for a in self.actions if a.get("action") == "fill"]
            entry_fills = [f for f in self.fills if f.get("purpose") == "entry"]
            seen_oids = set()
            entry_cnt = 0
            flips_cnt = 0
            last_flip = None
            sim_u = 0.0
            sim_d = 0.0
            for f in self.fills:
                purp = str(f.get("purpose") or "entry")
                f_side = f.get("side")
                f_sz = float(f.get("size", 0.0) or 0.0)
                if purp == "unwind":
                    if f_side == "Up":
                        sim_u = max(0.0, sim_u - f_sz)
                    elif f_side == "Down":
                        sim_d = max(0.0, sim_d - f_sz)
                    continue
                if purp != "entry":
                    if f_side == "Up":
                        sim_u += f_sz
                    elif f_side == "Down":
                        sim_d += f_sz
                    continue
                oid = f.get("order_id")
                pre_gap = sim_u - sim_d
                is_new_order = (oid is None) or (oid not in seen_oids)
                if oid is not None:
                    seen_oids.add(oid)
                    self.entry_order_ids.add(oid)
                is_flip_fill = is_new_order and (
                    (f_side == "Up" and pre_gap < -1e-4)
                    or (f_side == "Down" and pre_gap > 1e-4)
                    or (
                        abs(pre_gap) <= 1e-4
                        and sim_u > 1e-4
                        and sim_d > 1e-4
                        and last_flip in ("Up", "Down")
                        and f_side in ("Up", "Down")
                        and f_side != last_flip
                    )
                )
                if is_flip_fill:
                    flips_cnt += 1
                    last_flip = f_side
                if f_side == "Up":
                    sim_u += f_sz
                elif f_side == "Down":
                    sim_d += f_sz
                if is_new_order:
                    entry_cnt += 1
            self.entries_count = entry_cnt
            self.flips_count = flips_cnt
            saved_flip_side = raw.get("last_flip_side")
            self.last_flip_side = saved_flip_side if saved_flip_side in ("Up", "Down") else last_flip
            try:
                saved_qa = int(raw.get("quote_attempts", 0) or 0)
                self.quote_attempts = max(0, saved_qa)
            except (TypeError, ValueError):
                self.quote_attempts = 0
            if entry_fills:
                try:
                    self.last_entry_fill_t = float(entry_fills[-1].get("t"))
                except (TypeError, ValueError):
                    pass
                if raw.get("regime"):
                    self.entry_regime = str(raw.get("regime"))
            # Advance monotonic order_id past any previously recorded action order_id
            max_oid = self._next_order_id
            for a in self.actions:
                try:
                    if a.get("order_id") is not None:
                        max_oid = max(max_oid, int(a["order_id"]))
                except (TypeError, ValueError):
                    pass
            self._next_order_id = max_oid

        restored_hist = raw.get("price_history")
        if isinstance(restored_hist, list) and len(restored_hist) > len(self.price_history):
            self.price_history = self._compact_price_history(
                [pt for pt in restored_hist if isinstance(pt, dict)],
                max_points=1500,
            )
            # 重启恢复时不还原旧 working_orders，避免进程离线期间产生过期僵尸挂单；
            # 仅回填近 75 秒现货轨迹以保持 60s TWAP / basis_ema 热启动。
            now_ref = float(raw.get("now") or time.time())
            buf = []
            for pt in self.price_history:
                try:
                    pt_t = float(pt.get("t", 0.0) or 0.0)
                    pt_px = float(pt.get("price", 0.0) or 0.0)
                    if math.isfinite(pt_t) and math.isfinite(pt_px) and pt_px > 0 and (now_ref - pt_t) <= 75.0:
                        buf.append((pt_t, pt_px))
                except (TypeError, ValueError):
                    continue
            if buf:
                self._spot_buf = buf

        try:
            saved_basis = raw.get("basis_ema")
            saved_now = float(raw.get("now") or 0.0)
            saved_prov = str(raw.get("spot_provider") or "").strip().lower()
            cur_prov = str(
                (getattr(self, "net_stats", None) or {}).get("spot_provider")
                or config.PARAMS.get("SPOT_WS_PROVIDER", "binance")
                or "binance"
            ).strip().lower()
            def _ccy(p: str) -> str:
                return "USD" if p in ("coinbase", "kraken") else ("USDT" if p in ("binance", "okx") else "")
            prov_compat = (
                (not saved_prov)
                or (saved_prov == cur_prov and cur_prov != "auto")
                or (_ccy(saved_prov) == _ccy(cur_prov) and _ccy(cur_prov) != "")
            )
            basis_max_age = float(config.PARAMS.get("BASIS_MAX_AGE_S", 120.0))
            if (
                saved_basis is not None
                and math.isfinite(float(saved_basis))
                and saved_now > 0.0
                and (time.time() - saved_now) <= basis_max_age
                and prov_compat
            ):
                self._basis_ema = float(saved_basis)
                self._basis_updated_t = saved_now
                self._basis_sample_count = max(self._basis_sample_count, 10)
                self._basis_anomaly_active = bool(raw.get("basis_anomaly_active", False))
                try:
                    b_dev_saved = float(raw.get("basis_dev_usd", 0.0) or 0.0)
                    if math.isfinite(b_dev_saved):
                        self._last_basis_dev_usd = max(0.0, b_dev_saved)
                except (TypeError, ValueError):
                    pass
                self._basis_recover_since = None
        except (TypeError, ValueError):
            pass

        restored_tl = raw.get("decision_timeline")
        if isinstance(restored_tl, list):
            self.decision_timeline = [it for it in restored_tl if isinstance(it, dict)][-50:]
        for attr_name, json_key in (
            ("_entry_side_prob", "entry_side_prob"),
            ("_active_leg_entry_prob", "active_leg_entry_prob"),
            ("_entry_p_up", "entry_p_up"),
            ("_entry_mkt_up", "entry_mkt_up"),
            ("_mid_p_up", "mid_p_up"),
            ("_mid_mkt_up", "mid_mkt_up"),
        ):
            try:
                val = raw.get(json_key)
                if val is not None and math.isfinite(float(val)):
                    setattr(self, attr_name, float(val))
            except (TypeError, ValueError):
                pass
        if self._active_leg_entry_prob is None and self._entry_side_prob is not None:
            self._active_leg_entry_prob = self._entry_side_prob

        if up_sh > 0 or dn_sh > 0 or self.fills:
            print(
                f"[Runner] Resumed active window {slug} from live.json "
                f"(UP={up_sh:.1f}, DN={dn_sh:.1f}, fills={len(self.fills)})"
            )
        return True

    def settle_orphaned_live(self, current_slug: str) -> Optional[dict]:
        """Settle an unclosed prior window from live.json if restart crossed a 5m boundary.

        Prevents losing real paper positions/PnL when an OOM/crash/reboot spans a
        window boundary. Returns metadata so PipelineDriver can enqueue Gamma/TWAP
        official reconciliation for the recovered window.
        """
        if not self.live_file.exists():
            return None
        try:
            raw = json.loads(self.live_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        prev_slug = str(raw.get("slug") or "")
        if not prev_slug or prev_slug == current_slug or prev_slug in self._settled_slugs:
            return None

        pos_up_raw = raw.get("pos_up") or {}
        pos_dn_raw = raw.get("pos_down") or {}
        try:
            up_sh = max(0.0, float(pos_up_raw.get("shares", 0.0) or 0.0))
            up_cash = max(0.0, float(pos_up_raw.get("cash", 0.0) or 0.0))
            dn_sh = max(0.0, float(pos_dn_raw.get("shares", 0.0) or 0.0))
            dn_cash = max(0.0, float(pos_dn_raw.get("cash", 0.0) or 0.0))
        except (TypeError, ValueError):
            return None

        actions = [a for a in (raw.get("actions") or []) if isinstance(a, dict)]
        fills = [dict(a) for a in actions if a.get("action") == "fill"]
        if up_sh <= 1e-6 and dn_sh <= 1e-6 and not fills:
            return None

        prev_start = float(raw.get("start") or 0.0)
        prev_end = float(raw.get("end") or (prev_start + 300.0 if prev_start > 0 else 0.0))
        prev_open = float(raw.get("open_px") or raw.get("binance_open_px") or 0.0)
        prev_close = float(raw.get("poly_spot") or raw.get("spot") or prev_open or 0.0)
        if prev_start <= 0 and prev_slug.rsplit("-", 1)[-1].isdigit():
            prev_start = float(prev_slug.rsplit("-", 1)[-1])
            prev_end = prev_start + 300.0

        self.start_window(
            prev_slug, prev_start, prev_end, prev_open,
            binance_open_px=float(raw.get("binance_open_px") or prev_open or 0.0),
            poly_spot=prev_close,
            poly_open_provisional=bool(raw.get("poly_open_is_provisional", False)),
            resume_from_live=True,
        )
        self.close_window(close_px=prev_close, official_winner=None, winner_source="spot_fallback")
        print(
            f"[Runner] Recovered & provisionally settled orphaned window {prev_slug} "
            f"(UP={up_sh:.1f}, DN={dn_sh:.1f}); queued for official reconciliation."
        )
        return {
            "slug": prev_slug,
            "start_t": prev_start,
            "open_px": prev_open,
            "close_px": prev_close,
        }

    def update_open_price(self, poly_open: float):
        """Update provisional open price once Polymarket official TWAP finishes computing."""
        if poly_open <= 0 or abs(self.open_price - poly_open) < 0.001:
            return
        self.open_price = poly_open
        self.poly_open_is_provisional = False
        # Cancel resting working orders placed under provisional strike, keeping realistic cancel transit
        if self.working_orders:
            now_t = time.time()
            cancel_lat = float(config.PARAMS.get("CANCEL_LATENCY_S", config.PARAMS.get("LATENCY_S", 0.30)))
            for o in self.working_orders:
                o.cancel_at = now_t
                o.cancel_effective_at = now_t + cancel_lat
                if o.remaining > 1e-6 and cancel_lat > 0:
                    self.pending_cancels.append(o)
            self.working_orders = []
        if self.price_history:
            # Keep the two opening anchors distinct but consistent on the first
            # point: `price` is the Binance kline open, `poly_price` the official
            # TWAP open. Previously only poly_price was rewritten, so the live
            # chart (poly_price first) and the history chart (price first)
            # started at different values and return_bps disagreed by ~13 bps.
            self.price_history[0]["poly_price"] = round(poly_open, 2)
            self.price_history[0]["price"] = round(self.binance_open_price, 2)
            self.price_history[0]["return_bps"] = 0.0
            self.price_history[0]["poly_return_bps"] = 0.0
            for pt in self.price_history:
                if pt.get("poly_price"):
                    pt["poly_return_bps"] = round(((pt["poly_price"] - poly_open) / poly_open) * 10000.0, 2)

    def _record_fill(
        self,
        now: float,
        side: str,
        price: float,
        size: float,
        kind: str,
        purpose: str,
        order_id: Optional[int] = None,
        reason: str = "",
    ):
        """Record one executed fill and its investor-readable action event."""
        if purpose == "entry":
            pre_u = self.pos_up.shares - (size if side == "Up" else 0.0)
            pre_d = self.pos_down.shares - (size if side == "Down" else 0.0)
            pre_gap = pre_u - pre_d
            if order_id is None or order_id not in self.entry_order_ids:
                if order_id is not None:
                    self.entry_order_ids.add(order_id)
                p_u_now = float(getattr(self, "last_prob_up", 0.5))
                p_side_now = round(p_u_now if side == "Up" else (1.0 - p_u_now), 4)
                prev_flip_side = getattr(self, "last_flip_side", None)
                is_flip_entry = (
                    (side == "Up" and pre_gap < -1e-4)
                    or (side == "Down" and pre_gap > 1e-4)
                    or (
                        abs(pre_gap) <= 1e-4
                        and pre_u > 1e-4
                        and pre_d > 1e-4
                        and prev_flip_side in ("Up", "Down")
                        and side in ("Up", "Down")
                        and side != prev_flip_side
                    )
                )
                if is_flip_entry:
                    self.flips_count = getattr(self, "flips_count", 0) + 1
                    self.last_flip_side = side
                    # Reset active-leg probability anchor on flip so post-flip pyramid adds
                    # evaluate PYRAMID_MAX_PROB_DROP against the new flipped direction, not the abandoned leg.
                    self._active_leg_entry_prob = p_side_now
                # Attribute the round to the regime that actually allowed the
                # first entry, not to whichever regime spent the most ticks.
                if self.entries_count == 0 and self.entry_regime is None:
                    self.entry_regime = self._last_regime
                if self.entries_count == 0 and getattr(self, "_entry_p_up", None) is None:
                    self._entry_p_up = round(p_u_now, 4)
                    self._entry_mkt_up = round(float(getattr(self, "last_poly_up", 0.5)), 4)
                    self._entry_side_prob = p_side_now
                    self._active_leg_entry_prob = p_side_now
                elif getattr(self, "_active_leg_entry_prob", None) is None:
                    self._active_leg_entry_prob = p_side_now
                self.entries_count += 1
                # Grant each new entry stage its own fresh quote_attempts budget
                self.quote_attempts = 0
                self.last_entry_fill_t = float(now)
        fill = {
            "t": round(now, 2), "side": side, "price": price,
            "size": size, "kind": kind, "purpose": purpose,
            "poly_up": getattr(self, "last_poly_up", None),
            "poly_down": getattr(self, "last_poly_down", None),
            "spot_price": getattr(self, "last_spot_price", None),
            "tau_s": round(max(0.0, getattr(self, "window_end", 0.0) - now), 1)
        }
        if order_id is not None:
            fill["order_id"] = order_id
        if reason:
            fill["reason"] = str(reason)
        self.fills.append(fill)
        self.actions.append(dict(fill, action="fill"))

    def _apply_maker_fill(self, ts: float, order: Order, fill_sz: float):
        """Credit executed maker fill shares/cash onto inventory and journal it."""
        if order.side == "Up":
            self.pos_up.shares += fill_sz
            self.pos_up.cash += (fill_sz * order.price)
        else:
            self.pos_down.shares += fill_sz
            self.pos_down.cash += (fill_sz * order.price)
        paired = min(self.pos_up.shares, self.pos_down.shares)
        if paired > 1e-6:
            pair_cost = self.pos_up.avg_price + self.pos_down.avg_price
            self.insurance_paid = max(0.0, (pair_cost - 1.00) * paired)
        else:
            self.insurance_paid = 0.0
        self._record_fill(
            ts, order.side, order.price, fill_sz, "maker", order.purpose,
            order_id=order.id, reason=str(getattr(order, "entry_reason", "") or ""),
        )

    @staticmethod
    def _resolve_print_timestamp(tp: dict, fallback_now: float, clock_offset_s: float = 0.0) -> float:
        """Align trade-print timestamp with local sub-second order timestamps.

        Polymarket RTDS `activity/trades` truncates `timestamp` to whole seconds
        (0 sub-second precision), whereas `live_at` and `cancel_effective_at`
        have millisecond resolution. Comparing a floored integer second against
        a sub-second deadline causes false drops after activation and false
        zombie fills after cancellation. When `exch_t` is whole-second and
        `recv_t` is within normal transit bounds, use `recv_t` instead; when host
        clock offset is non-trivial (>0.25s), project sub-second `exch_t` into
        the local clock coordinate frame.
        """
        exch_t = float(tp.get("timestamp") or 0.0)
        recv_t = float(tp.get("received_at") or 0.0)
        clk_off = float(clock_offset_s or 0.0)
        if exch_t > 0.0 and (recv_t <= 0.0 or (-0.5 <= (recv_t - exch_t) <= 5.0)):
            if recv_t > 0.0 and abs(exch_t - round(exch_t)) < 1e-6:
                # 整秒截断的真实成交时刻必在 [exch_t, exch_t + 1.0) 内；
                # 映射到本机时钟坐标系的理论最晚发生时刻为 exch_t + 1.0 + clk_off，
                # 严禁滞后 1.5~2.3s 才到达的 RTDS 老成交冒充当前时刻击中刚挂的新单。
                exch_latest = exch_t + 1.0 + clk_off
                return min(recv_t, exch_latest)
            if recv_t > 0.0 and abs(clk_off) > 0.25:
                return min(recv_t, exch_t + clk_off)
            return exch_t
        return recv_t or exch_t or fallback_now

    def _apply_taker_outcome(
        self,
        fill_ts: float,
        now: float,
        action: Decision,
        want: float,
        is_buy: bool,
        filled: float,
        vwap: float,
        reject_reason: Optional[str],
    ):
        """统一处理 Taker IOC 的成交记账或拒单记录（同步/非阻塞异步在途共用）。"""
        if filled > 1e-6 and reject_reason is None:
            px = round(vwap, 6)
            fee = round(filled * taker_fee_per_share(px, config.PARAMS["TAKER_FEE_RATE"]), 4)
            if is_buy:
                if action.side == "Down":
                    self.pos_down.shares += filled
                    self.pos_down.cash += (filled * px + fee)
                else:
                    self.pos_up.shares += filled
                    self.pos_up.cash += (filled * px + fee)
                paired = min(self.pos_up.shares, self.pos_down.shares)
                if paired > 1e-6:
                    pair_cost = self.pos_up.avg_price + self.pos_down.avg_price
                    self.insurance_paid = max(0.0, (pair_cost - 1.00) * paired)
                else:
                    self.insurance_paid = 0.0
            else:
                pos = self.pos_up if action.side == "Up" else self.pos_down
                avg_entry = pos.avg_price
                cost_removed = min(pos.cash, filled * avg_entry)
                proceeds_unwind = filled * px - fee
                pos.shares = max(0.0, pos.shares - filled)
                pos.cash = max(0.0, pos.cash - cost_removed)
                if pos.shares <= 1e-6:
                    cost_removed += pos.cash
                    pos.shares = 0.0
                    pos.cash = 0.0
                self.unwind_cost_basis = getattr(self, "unwind_cost_basis", 0.0) + cost_removed
                self.unwind_proceeds = getattr(self, "unwind_proceeds", 0.0) + proceeds_unwind
                self.realized_unwind_pnl = self.unwind_proceeds - self.unwind_cost_basis
                if action.action == "taker_unwind":
                    self.window_unwound = True
            if action.action == "taker_unwind":
                purpose = "unwind"
            elif action.action == "taker_buy":
                purpose = "entry"
            else:
                purpose = "hedge"
            self._record_fill(fill_ts, action.side, px, filled, "taker", purpose, reason=str(action.reason or ""))
            if filled + 1e-6 < want:
                self.actions.append({
                    "t": round(fill_ts, 2), "action": "taker_partial", "side": action.side,
                    "price": px, "size": round(filled, 4),
                    "requested": round(want, 4), "filled": round(filled, 4),
                    "reason": "insufficient visible book depth"
                })
        else:
            self.actions.append({
                "t": round(now, 2), "action": "taker_reject", "side": action.side,
                "requested": round(want, 4),
                "reason": reject_reason or "empty/one-sided book, no visible liquidity"
            })

    def _evaluate_delayed_taker_fill(
        self,
        action: Decision,
        want: float,
        is_buy: bool,
        ioc_limit_px: float,
        lat_ms: int,
        post_up: Optional[OrderBook],
        post_dn: Optional[OrderBook],
        reject_reason: Optional[str],
    ) -> tuple:
        """Validate post-delay book consistency and execute IOC limit matching."""
        post_book = post_up if action.side == "Up" else post_dn
        book_tol = max(0.0, float(config.PARAMS.get("BOOK_CONSISTENCY_TOLERANCE", 0.015)))
        if reject_reason is not None:
            return 0.0, 0.0, reject_reason
        if post_book is None or (post_book.best_ask is None and post_book.best_bid is None):
            return 0.0, 0.0, "延迟后重拉盘口为空，IOC 放弃成交"
        if (
            bool(config.PARAMS.get("STRICT_BOOK_CONSISTENCY", True))
            and action.action == "taker_buy"
            and post_up is not None and post_dn is not None
            and not is_books_consistent(post_up, post_dn, tolerance=book_tol)
        ):
            return 0.0, 0.0, f"{lat_ms}ms 延迟后双边盘口数据损坏/不一致，IOC 放弃成交"
        filled, vwap = self.matcher.taker_ioc_fill(
            post_book, want, limit_price=ioc_limit_px, is_buy=is_buy
        )
        if filled <= 1e-6:
            reject_reason = (
                f"{lat_ms}ms 延迟后盘口已移动 (原信号价 {action.price:.2f}, IOC限价 {ioc_limit_px:.2f}, "
                f"现ask={post_book.best_ask}/bid={post_book.best_bid})，IOC 错失防滑点"
            )
        return filled, vwap, reject_reason

    def _settle_due_pending_takers(
        self,
        now: float,
        book_up: OrderBook,
        book_down: OrderBook,
        delayed_book_fetcher=None,
    ):
        """在非阻塞模式下，于后续 tick 结算已抵达交易所的在途 Taker IOC 订单。"""
        if not self.pending_takers:
            return
        remaining_takers = []
        async_fetch_timeout = max(0.30, float(config.PARAMS.get("TAKER_DELAYED_BOOK_TIMEOUT_S", 0.85)))
        for pt in self.pending_takers:
            if now < pt["arrive_at"] - 1e-9:
                remaining_takers.append(pt)
                continue
            action = pt["action"]
            want = float(pt["want"])
            is_buy = bool(pt["is_buy"])
            ioc_limit_px = float(pt["ioc_limit_px"])
            min_order_sh = float(pt["min_order_sh"])
            lat_ms = int(round(float(pt["wire_lat"]) * 1000))
            reject_reason = None

            # 持续追踪传输与异步拉盘口期间同侧在途撤单是否已成交，防止旧 Maker 在等待 HTTP 期间成交导致双倍超仓
            if action.action == "taker_buy":
                cur_side_shares = self.pos_up.shares if action.side == "Up" else self.pos_down.shares
                prev_side_shares = float(pt.get("pre_side_shares", 0.0))
                delta_filled = max(0.0, cur_side_shares - prev_side_shares)
                if delta_filled > 1e-6:
                    want = max(0.0, want - delta_filled)
                    pt["want"] = want
                    pt["pre_side_shares"] = cur_side_shares
                    pt["_cum_already_filled"] = float(pt.get("_cum_already_filled", 0.0)) + delta_filled
                if want < min_order_sh - 1e-4 and float(pt.get("_cum_already_filled", 0.0)) > 1e-6:
                    cum_filled = float(pt.get("_cum_already_filled", 0.0))
                    reject_reason = f"撤单在途内原挂单已成交({cum_filled:.1f}股)，取消重复Taker追单"

            post_up, post_dn = book_up, book_down
            if reject_reason is None:
                fut = pt.get("book_future")
                if fut is not None:
                    if not fut.done():
                        started = float(pt.get("fetch_started_at", now))
                        if (now - started) < async_fetch_timeout:
                            remaining_takers.append(pt)
                            continue
                        try:
                            fut.cancel()
                        except Exception:
                            pass
                        post_up, post_dn = None, None
                        reject_reason = f"延迟拉取盘口超时({async_fetch_timeout:.2f}s)，IOC 放弃成交"
                    else:
                        try:
                            fetched = fut.result()
                            if isinstance(fetched, tuple) and len(fetched) == 2:
                                post_up, post_dn = fetched
                            elif fetched is not None:
                                if action.side == "Up":
                                    post_up = fetched
                                else:
                                    post_dn = fetched
                        except Exception as exc:
                            post_up, post_dn = None, None
                            reject_reason = f"延迟拉取盘口异常({type(exc).__name__})，IOC 放弃成交"
                elif delayed_book_fetcher is not None:
                    try:
                        fetched = delayed_book_fetcher(action.side)
                        if hasattr(fetched, "done") and hasattr(fetched, "result") and callable(fetched.done):
                            if not fetched.done():
                                pt["book_future"] = fetched
                                pt["fetch_started_at"] = now
                                remaining_takers.append(pt)
                                continue
                            fetched = fetched.result()
                        if isinstance(fetched, tuple) and len(fetched) == 2:
                            post_up, post_dn = fetched
                        elif fetched is not None:
                            if action.side == "Up":
                                post_up = fetched
                            else:
                                post_dn = fetched
                    except Exception as exc:
                        post_up, post_dn = None, None
                        reject_reason = f"延迟拉取盘口异常({type(exc).__name__})，IOC 放弃成交"

            filled, vwap, reject_reason = self._evaluate_delayed_taker_fill(
                action, want, is_buy, ioc_limit_px, lat_ms, post_up, post_dn, reject_reason
            )
            self._apply_taker_outcome(
                fill_ts=now, now=now, action=action, want=want,
                is_buy=is_buy, filled=filled, vwap=vwap, reject_reason=reject_reason,
            )
        self.pending_takers = remaining_takers

    def step(
        self,
        now: float,
        spot: SpotState,
        book_up: OrderBook,
        book_down: OrderBook,
        net_stats: dict = None,
        trade_prints: list = None,
        poly_spot: float = None,
        replay_mode: bool = False,
        delayed_book_fetcher=None,
        delayed_prints_fetcher=None,
        api_schema_guard: dict = None,
        poly_spot_ts: Optional[float] = None,
        async_taker_delay: bool = False,
    ):
        """Execute one evaluation tick: probability, regime, risk gates, matching.

        replay_mode disables the wall-clock dead-man switch: recorded book
        updates are sparse, so a multi-second gap between two events is normal
        and must not cancel resting quotes during a replay.
        """
        tau = max(0.0, self.window_end - now)
        open_px = getattr(self, "open_price", 0.0)
        vol_bps = max(
            float(getattr(spot, "realized_vol_bps", 15.0) or 15.0),
            float(config.PARAMS.get("MIN_MODEL_VOL_BPS", 14.0)),
        )

        # None means the feed's freshness guard rejected its cached TWAP.
        poly_fresh = poly_spot is not None and poly_spot > 0
        self.poly_spot_fresh = poly_fresh
        if poly_fresh:
            self.last_poly_spot = poly_spot

        spot_stale = bool(getattr(spot, "is_stale", False)) or (spot.price <= 0)
        if not spot_stale and spot.price > 0:
            self.last_spot_price = spot.price
            self._spot_buf.append((now, float(spot.price)))
            # 保留 75 秒现货缓冲，确保即使 Chainlink HTTP 往返延迟 1-3 秒，
            # 仍能严格截取 [poly_ref_t - 60s, poly_ref_t] 的完整 60 秒窗口。
            if self._spot_buf and now - self._spot_buf[0][0] > 75.0:
                self._spot_buf = [(ts, px) for ts, px in self._spot_buf if now - ts <= 75.0]

        poly_ref_t = min(now, float(poly_spot_ts)) if (poly_spot_ts is not None and float(poly_spot_ts) > 0.0) else now
        # 非回放模式下要求现货窗口至少覆盖 55 秒，避免用短均值减去 60 秒 Chainlink TWAP 产生冷启动偏差
        min_basis_span = 5.0 if replay_mode else 55.0
        if poly_spot_ts is not None and float(poly_spot_ts) > 0.0:
            poly_changed = float(poly_spot_ts) > float(getattr(self, "_last_basis_poly_t", 0.0)) + 1e-6
        else:
            prev_ppx = getattr(self, "_last_basis_poly_px", None)
            poly_changed = (
                prev_ppx is None
                or abs(float(poly_spot or 0.0) - float(prev_ppx)) > 1e-6
                or (now - self._basis_updated_t) >= 1.5
            )

        if (
            poly_fresh
            and not spot_stale
            and len(self._spot_buf) >= 5
            and poly_changed
            and (self._basis_ema is None or (now - self._basis_updated_t) >= 0.45)
        ):
            win_start = poly_ref_t - 60.0
            bin_60s_pts = [(ts, px) for ts, px in self._spot_buf if win_start <= ts <= poly_ref_t]
            pts_span = (bin_60s_pts[-1][0] - bin_60s_pts[0][0]) if len(bin_60s_pts) >= 2 else 0.0
            if len(bin_60s_pts) >= 5 and pts_span >= min_basis_span:
                w_sum = 0.0
                dt_sum = 0.0
                for i in range(1, len(bin_60s_pts)):
                    dt = bin_60s_pts[i][0] - bin_60s_pts[i - 1][0]
                    if dt > 0.0:
                        w_sum += 0.5 * (bin_60s_pts[i - 1][1] + bin_60s_pts[i][1]) * dt
                        dt_sum += dt
                bin_60s_avg = (w_sum / dt_sum) if dt_sum > 1e-6 else (sum(p for _, p in bin_60s_pts) / len(bin_60s_pts))
                inst_basis = bin_60s_avg - float(poly_spot)
                if math.isfinite(inst_basis):
                    if self._basis_ema is None:
                        self._basis_ema = inst_basis
                        self._last_basis_dev_usd = 0.0
                        self._basis_anomaly_active = False
                        self._basis_recover_since = None
                    else:
                        # v3.4: Detect sudden spot-vs-Chainlink basis desync before Huber-clipping
                        raw_dev = abs(inst_basis - self._basis_ema)
                        self._last_basis_dev_usd = round(raw_dev, 2)
                        b_max = float(config.PARAMS.get("BASIS_ANOMALY_MAX_USD", 0.0) or 0.0)
                        b_rec = float(config.PARAMS.get("BASIS_ANOMALY_RECOVER_USD", 50.0) or 50.0)
                        b_rec_s = float(config.PARAMS.get("BASIS_ANOMALY_RECOVER_S", 5.0) or 5.0)
                        if self._basis_sample_count >= 10 and b_max > 0.0:
                            if raw_dev > b_max:
                                self._basis_anomaly_active = True
                                self._basis_recover_since = None
                            elif self._basis_anomaly_active:
                                if raw_dev <= b_rec:
                                    if self._basis_recover_since is None:
                                        self._basis_recover_since = now
                                    elif (now - self._basis_recover_since) >= b_rec_s:
                                        self._basis_anomaly_active = False
                                        self._basis_recover_since = None
                                else:
                                    self._basis_recover_since = None
                        elif b_max <= 0.0:
                            self._basis_anomaly_active = False
                            self._basis_recover_since = None

                        # Huber-clip single-sample basis jump once warmed up + slow low-pass EMA
                        # so 60s V-shaped pullbacks do not jerk the exchange basis estimate.
                        # CRITICAL: Freeze _basis_ema while basis anomaly is active (or just tripped)
                        # so corrupted feed samples cannot drag _basis_ema toward the bad value and
                        # cause a false auto-recovery.
                        if not self._basis_anomaly_active:
                            sample = inst_basis
                            if self._basis_sample_count >= 10:
                                delta = max(-35.0, min(35.0, inst_basis - self._basis_ema))
                                sample = self._basis_ema + delta
                            alpha = 0.03 if self._basis_sample_count >= 10 else 0.15
                            self._basis_ema = (1.0 - alpha) * self._basis_ema + alpha * sample
                            self._basis_sample_count += 1
                    self._basis_updated_t = now
                    if self._basis_sample_count == 0:
                        self._basis_sample_count = 1
                    self._last_basis_poly_px = float(poly_spot)
                    if poly_spot_ts is not None and float(poly_spot_ts) > 0.0:
                        self._last_basis_poly_t = float(poly_spot_ts)

        basis_max_age = float(config.PARAMS.get("BASIS_MAX_AGE_S", 120.0))
        if self._basis_ema is not None and (now - self._basis_updated_t) > basis_max_age:
            self._basis_anomaly_active = False
            self._basis_recover_since = None
        basis_ready = (
            (self._basis_ema is not None)
            and ((now - self._basis_updated_t) <= basis_max_age)
            and not getattr(self, "poly_open_is_provisional", False)
        )

        # Attach live basis & realized 60s TWAP onto SpotState for estimator
        spot.poly_price = poly_spot if poly_fresh else None
        if not getattr(spot, "binance_open_price", None):
            spot.binance_open_price = self.binance_open_price
        if basis_ready and open_px > 0:
            spot.basis_ema = self._basis_ema
            spot.open_price = open_px
            if tau <= 60.0 and getattr(spot, "twap_realized_60s", None) is None and self._spot_buf:
                win_start_60 = self.window_end - 60.0
                elapsed_60 = max(0.0, 60.0 - max(0.0, tau))
                realized_pts = [(ts, px - self._basis_ema) for ts, px in self._spot_buf if ts >= win_start_60]
                if len(realized_pts) >= 2:
                    # Time-weighted trapezoidal average eliminates 10Hz/2Hz variable-cadence sampling bias
                    weighted_sum = 0.0
                    total_dt = 0.0
                    for i in range(1, len(realized_pts)):
                        t_prev, v_prev = realized_pts[i - 1]
                        t_curr, v_curr = realized_pts[i]
                        dt = t_curr - t_prev
                        if dt > 0.0:
                            weighted_sum += 0.5 * (v_prev + v_curr) * dt
                            total_dt += dt
                    # Guard against post-outage illusion: do not extrapolate a short 5s
                    # sample across a 40s already-elapsed TWAP window.
                    coverage_ok = (elapsed_60 < 10.0) or (total_dt >= 0.45 * elapsed_60)
                    if coverage_ok and total_dt > 1e-6:
                        spot.twap_realized_60s = weighted_sum / total_dt
                    elif coverage_ok:
                        spot.twap_realized_60s = sum(v for _, v in realized_pts) / len(realized_pts)
                elif realized_pts and elapsed_60 < 10.0:
                    spot.twap_realized_60s = realized_pts[0][1]
        else:
            spot.basis_ema = None
            spot.open_price = self.binance_open_price

        p_fair = estimate_p_fair(
            spot, open_px, tau, vol_bps,
            poly_price=poly_spot if poly_fresh else None,
            unit_variance=bool(config.PARAMS.get("MODEL_T5_UNIT_VARIANCE", False)),
        )
        self.last_prob_up = p_fair

        raw_up = self._extract_book_prob(book_up)
        raw_dn = self._extract_book_prob(book_down)
        if raw_up is not None and raw_dn is not None:
            poly_up, poly_down = raw_up, raw_dn
        elif raw_up is not None:
            poly_up = raw_up
            poly_down = round(max(0.0, min(1.0, 1.0 - raw_up)), 4)
        elif raw_dn is not None:
            poly_down = raw_dn
            poly_up = round(max(0.0, min(1.0, 1.0 - raw_dn)), 4)
        else:
            prev_up = getattr(self, "last_poly_up", None)
            prev_dn = getattr(self, "last_poly_down", None)
            poly_up = float(prev_up) if prev_up is not None else 0.5
            poly_down = float(prev_dn) if prev_dn is not None else round(1.0 - poly_up, 4)
        self.last_poly_up = poly_up
        self.last_poly_down = poly_down
        # Capture one unbiased mid-window benchmark snapshot (first valid tick at tau <= 150s)
        if (
            getattr(self, "_mid_p_up", None) is None
            and tau <= 150.0
            and raw_up is not None
            and not spot_stale
        ):
            self._mid_p_up = round(float(p_fair), 4)
            self._mid_mkt_up = round(float(poly_up), 4)

        # Two independent price series used to be collapsed into one: `price`
        # and `poly_price` were both written from the lagging 60s Poly TWAP,
        # while the real Binance spot was thrown away. The chart therefore drew
        # a smoothed TWAP and labelled it "BTC price", lagging the market by
        # tens of dollars. Keep both sources, each against its OWN opening
        # anchor, and mark stale TWAP points as missing rather than frozen.
        bin_px = self.last_spot_price if self.last_spot_price and self.last_spot_price > 0 else 0.0
        poly_px = float(poly_spot) if poly_fresh else None
        bin_open = float(getattr(self, "binance_open_price", 0.0) or 0.0)
        bin_bps = ((bin_px - bin_open) / bin_open) * 10000.0 if (bin_open > 0 and bin_px > 0) else 0.0
        poly_bps = ((poly_px - open_px) / open_px) * 10000.0 if (poly_px and open_px > 0) else 0.0

        ref_px = bin_px or (poly_px or open_px)
        if ref_px > 0:
            last_hist_t = getattr(self, "_last_hist_t", 0.0)
            dt_hist = now - last_hist_t
            prev_pt = self.price_history[-1] if self.price_history else None
            fast_change = (
                prev_pt is not None
                and dt_hist >= 0.18
                and (
                    abs(poly_up - float(prev_pt.get("poly_up") or poly_up)) >= 0.008
                    or abs(bin_px - float(prev_pt.get("price") or bin_px)) >= 1.0
                    or (poly_px is not None and abs(poly_px - float(prev_pt.get("poly_price") or poly_px)) >= 0.5)
                )
            )
            if replay_mode or len(self.price_history) <= 1 or dt_hist >= 0.35 or fast_change:
                self.price_history.append({
                    "t": round(now, 1),
                    "price": round(bin_px, 2),                                   # Binance spot, USD
                    "poly_price": round(poly_px, 2) if poly_px else None,       # Chainlink 60s TWAP, USD
                    "spot_price": round(bin_px, 2),
                    "poly_stale": not poly_fresh,
                    "return_bps": round(bin_bps, 2),
                    "poly_return_bps": round(poly_bps, 2),
                    "poly_up": round(poly_up, 4),
                    "poly_down": round(poly_down, 4)
                })
                self._last_hist_t = now
                if len(self.price_history) > 1500:
                    self.price_history = self._compact_price_history(self.price_history, max_points=1500)

        if net_stats:
            self.net_stats = net_stats
        self._api_schema_guard = dict(api_schema_guard) if isinstance(api_schema_guard, dict) else {"paused": False, "source": "", "reason": ""}
        api_paused = bool(self._api_schema_guard.get("paused", False))

        # 1. Dead-Man Switch: protect against stale inputs and against a stalled
        # event loop. The old check only measured the interval between step()
        # calls and called it a "feed gap": a genuinely stale book with a fast
        # loop never tripped it, while a single slow HTTP round-trip cancelled
        # every resting quote. Both conditions are now checked separately.
        deadman_timeout = float(config.PARAMS.get("DEADMAN_TIMEOUT_S", 3.0))
        loop_gap = (now - self._last_step_time) if self._last_step_time is not None else 0.0
        input_ages = [
            now - float(getattr(spot, "timestamp", now) or now),
            now - float(getattr(book_up, "source_t", 0.0) or now),
            now - float(getattr(book_down, "source_t", 0.0) or now),
        ]
        stale_age = max(0.0, max(input_ages))
        book_empty = (
            (book_up.best_ask is None and book_up.best_bid is None)
            or (book_down.best_ask is None and book_down.best_bid is None)
        )
        if replay_mode:
            # Sparse recorded ticks are not a live feed outage.
            is_deadman_active = spot.price <= 0 or spot_stale
        else:
            is_deadman_active = (
                stale_age > deadman_timeout or loop_gap > deadman_timeout
                or spot.price <= 0 or spot_stale or book_empty
            )
        deadman_reason = (
            f"feed age {stale_age:.1f}s / loop gap {loop_gap:.1f}s > {deadman_timeout:.1f}s"
            + (" / 现货断流" if spot_stale else "")
            + (" / 空盘口(行情流中断)" if book_empty else "")
        )
        self._last_step_time = now

        # 0. 先对所有刚抵达交易所（now >= live_at）的未激活挂单执行 Post-Only 到达校验与排队锚定，
        # 防止盘口已穿盘时被逐笔成交打印绕过 Post-Only 拒单规则白捡免费 Maker 成交。
        cancel_lat = self._effective_wire_latency(is_cancel=True, replay_mode=replay_mode)
        if not hasattr(self, "pending_cancels"):
            self.pending_cancels = []
        if self.working_orders:
            kept_act = []
            for o in self.working_orders:
                if not o.activated and now >= float(o.live_at or 0.0) - 1e-9:
                    target_book = book_up if o.side == "Up" else book_down
                    if not self.matcher.activate_order(o, target_book, now, post_only=True):
                        self.actions.append({
                            "t": round(now, 2), "action": "cancel", "side": o.side,
                            "price": o.price, "size": o.remaining, "purpose": o.purpose,
                            "reason": o.cancel_reason, "order_id": o.id
                        })
                        continue
                kept_act.append(o)
            self.working_orders = kept_act
        if self.pending_cancels:
            kept_pc = []
            for o in self.pending_cancels:
                if not o.activated and now >= float(o.live_at or 0.0) - 1e-9:
                    target_book = book_up if o.side == "Up" else book_down
                    if not self.matcher.activate_order(o, target_book, now, post_only=True):
                        continue
                kept_pc.append(o)
            self.pending_cancels = kept_pc

        # Process real trade prints that occurred in (prev_t, now] against resting orders
        # AND in-flight cancelling orders (cancel latency simulation) BEFORE evaluating new decisions.
        clk_off = float((getattr(self, "net_stats", None) or {}).get("clock_offset_s", 0.0) or 0.0)
        print_touched_ids = set()
        pending_cancel_filled_sides = set()
        pending_cancel_ids = {o.id for o in self.pending_cancels}
        active_for_prints = list(self.working_orders) + list(self.pending_cancels)
        if trade_prints and active_for_prints:
            for tp in trade_prints:
                tp_side = tp.get("outcome")  # "Up" or "Down"
                tp_dir = str(tp.get("side") or "")
                tp_t = self._resolve_print_timestamp(tp, now, clock_offset_s=clk_off)
                tp_recv = float(tp.get("received_at") or tp_t)
                use_live_book = (not replay_mode) or (abs(now - tp_recv) <= 0.5)
                for o in active_for_prints:
                    if not o.activated:
                        continue
                    if tp_side and o.side != tp_side:
                        continue
                    o_book = (book_up if o.side == "Up" else book_down) if use_live_book else None
                    fill_sz = self.matcher.on_trade_print(
                        o, float(tp["price"]), float(tp["size"]), tp_t,
                        trade_side=tp_dir, current_book=o_book,
                    )
                    if fill_sz > 0 or o.remaining < o.size - 1e-9:
                        print_touched_ids.add(o.id)
                    if fill_sz > 0:
                        self._apply_maker_fill(tp_t, o, fill_sz)
                        if o.id in pending_cancel_ids:
                            pending_cancel_filled_sides.add(o.side)
            self.working_orders = [o for o in self.working_orders if o.remaining > 1e-6]

        # 0b. Crossed-book matching for already-resting orders BEFORE evaluating new decisions,
        # so an existing Maker bid that was crossed by the new book fills first instead of
        # triggering a duplicate Taker buy on the same tick.
        queue_mult = float(config.PARAMS.get("QUEUE_CANCEL_CONSERVATIVE_MULT", 1.03))
        if self.pending_cancels:
            still_pending = []
            for o in self.pending_cancels:
                if o.remaining <= 1e-6 or now >= float(getattr(o, "cancel_effective_at", 0.0) or 0.0) - 1e-9:
                    continue
                if o.id not in print_touched_ids:
                    target_book = book_up if o.side == "Up" else book_down
                    if not o.activated and now >= float(o.live_at or 0.0) - 1e-9:
                        if not self.matcher.activate_order(o, target_book, now, post_only=True):
                            continue
                    self.matcher.sync_queue_depth(o, target_book, now, conservative_mult=queue_mult)
                    fill_sz = self.matcher.match_order(o, target_book, now)
                    if fill_sz > 0:
                        self._apply_maker_fill(now, o, fill_sz)
                        pending_cancel_filled_sides.add(o.side)
                if o.remaining > 1e-6:
                    still_pending.append(o)
            self.pending_cancels = still_pending

        # If an in-flight cancel (e.g. from a requote) filled before cancellation took effect,
        # immediately pull any replacement working order on that side so we never double-fill.
        if pending_cancel_filled_sides and self.working_orders:
            kept_working = []
            for o in self.working_orders:
                if o.side in pending_cancel_filled_sides and o.purpose == "entry":
                    o.cancel_at = now
                    o.cancel_effective_at = now if not o.activated else (now + cancel_lat)
                    o.cancel_reason = "原挂单在途撤单前已成交，自动撤销替代挂单防超仓"
                    if o.activated and o.remaining > 1e-6 and cancel_lat > 0:
                        self.pending_cancels.append(o)
                    self.actions.append({
                        "t": round(now, 2), "action": "cancel", "side": o.side,
                        "price": o.price, "size": o.remaining, "purpose": o.purpose,
                        "reason": o.cancel_reason, "order_id": o.id
                    })
                else:
                    kept_working.append(o)
            self.working_orders = kept_working

        if self.working_orders:
            pre_remaining = []
            for o in self.working_orders:
                if o.id not in print_touched_ids:
                    target_book = book_up if o.side == "Up" else book_down
                    if not o.activated and now >= float(o.live_at or 0.0) - 1e-9:
                        if not self.matcher.activate_order(o, target_book, now, post_only=True):
                            self.actions.append({
                                "t": round(now, 2), "action": "cancel", "side": o.side,
                                "price": o.price, "size": o.remaining, "purpose": o.purpose,
                                "reason": o.cancel_reason, "order_id": o.id
                            })
                            continue
                    self.matcher.sync_queue_depth(o, target_book, now, conservative_mult=queue_mult)
                    fill_sz = self.matcher.match_order(o, target_book, now)
                    if fill_sz > 0:
                        self._apply_maker_fill(now, o, fill_sz)
                if o.remaining > 1e-6:
                    pre_remaining.append(o)
            self.working_orders = pre_remaining

        # 结算已抵达交易所的非阻塞在途 Taker IOC 订单（先于本 tick 新决策执行，防重复发单）
        self._settle_due_pending_takers(
            now, book_up, book_down, delayed_book_fetcher=delayed_book_fetcher
        )

        if (is_deadman_active or api_paused) and self.working_orders:
            for o in self.working_orders:
                o.cancel_at = now
                o.cancel_effective_at = now + cancel_lat
                if api_paused:
                    o.cancel_reason = f"上游接口变更安全暂停撤单 ({self._api_schema_guard.get('reason', 'schema_drift')})"
                else:
                    o.cancel_reason = f"行情延迟保护撤单 ({deadman_reason})"
                if o.remaining > 1e-6 and cancel_lat > 0:
                    self.pending_cancels.append(o)
                self.actions.append({
                    "t": round(now, 2), "action": "cancel", "side": o.side,
                    "price": o.price, "size": o.remaining, "purpose": o.purpose,
                    "reason": o.cancel_reason, "order_id": o.id
                })
            self.working_orders.clear()

        # 2. Dynamic Micro-Regime Tracking for Attribution
        from core.regime import classify_market_regime
        ref_spot = getattr(self, "last_poly_spot", None) or spot.price
        ref_open = getattr(self, "open_price", 0.0) or getattr(self, "binance_open_price", spot.price)
        regime_info = classify_market_regime(
            ref_spot, ref_open, tau, vol_bps, book_up, book_down,
            is_toxic_shock=spot.is_toxic_shock, params=config.PARAMS
        )

        # Time-weighted dwell keeps attribution independent of tick cadence
        # (book updates accelerate near the close and would otherwise make
        # tail_freeze dominate every window).
        if self._last_regime_t is not None and self._last_regime is not None:
            dt = max(0.0, now - self._last_regime_t)
            self.regime_dwell[self._last_regime] = self.regime_dwell.get(self._last_regime, 0.0) + dt
        self._last_regime_t = now
        self._last_regime = regime_info.regime

        # Keep the last classification for attribution and for the dashboard;
        # plan_decision receives it instead of re-classifying the same tick.
        self._last_snr = regime_info.snr

        # 3. Evaluate Circuit Breaker & Bankroll
        net_gap = round(self.pos_up.shares - self.pos_down.shares, 4)
        if abs(net_gap) >= 1e-4:
            opp_book = book_down if net_gap > 0 else book_up
            opp_lot = abs(net_gap)
            opp_filled, opp_vwap = self.matcher.taker_fill(opp_book, opp_lot, is_buy=True)
            opp_ask = opp_vwap if opp_filled >= opp_lot - 1e-6 else None
            if opp_ask is not None and opp_ask > 0:
                prev_min = getattr(self, "min_opp_ask_seen", None)
                self.min_opp_ask_seen = opp_ask if prev_min is None else min(prev_min, opp_ask)
            if getattr(spot, "min_opp_ask_seen", None) is None:
                spot.min_opp_ask_seen = getattr(self, "min_opp_ask_seen", None)
        else:
            self.min_opp_ask_seen = None

        cb_active, cb_reason = self.circuit_breaker.is_active(now)
        # No artificial $10 floor: once the bankroll is exhausted the policy
        # must see the real (possibly negative) free balance after subtracting
        # capital already committed in this window (including in-flight pending
        # cancels and pending takers that can fill concurrently).
        total_equity_cap = self._initial_capital + self.total_pnl
        pending_cancel_locked = sum(
            max(0.0, o.remaining) * o.price
            for o in self.pending_cancels
            if getattr(o, "is_buy", True)
        )
        fee_rate_lock = float(config.PARAMS.get("TAKER_FEE_RATE", 0.07))
        pending_taker_locked = sum(
            max(0.0, float(pt["want"]))
            * (float(pt["ioc_limit_px"]) + taker_fee_per_share(float(pt["ioc_limit_px"]), fee_rate_lock))
            for pt in getattr(self, "pending_takers", [])
            if pt.get("is_buy", True)
        )
        unwind_pnl_now = float(getattr(self, "realized_unwind_pnl", 0.0) or 0.0)
        net_window_cash = (
            self.pos_up.cash
            + self.pos_down.cash
            - unwind_pnl_now
            + pending_cancel_locked
            + pending_taker_locked
        )
        committed_cost = max(0.0, net_window_cash)
        available_cap = total_equity_cap - net_window_cash
        last_entry_t = getattr(self, "last_entry_fill_t", None)
        secs_since_entry = max(0.0, now - float(last_entry_t)) if last_entry_t is not None else 9999.0
        book_drop_win_s = float(config.PARAMS.get("BOOK_DROP_GUARD_WINDOW_S", 5.0))
        mkt_mom_up, mkt_mom_down = self._update_and_get_book_momentum(
            now, book_up, book_down, window_s=book_drop_win_s
        )
        held_leg_cost = (
            self.pos_up.cash if net_gap > 1e-4
            else (self.pos_down.cash if net_gap < -1e-4 else 0.0)
        )
        max_plaus = float(config.PARAMS.get("MAX_PLAUSIBLE_EDGE", 0.0) or 0.0)
        div_grace_s = 1.0
        if max_plaus > 0.0:
            def _update_side_div(p_s: float, bk: OrderBook, since_attr: str, seen_attr: str):
                ask_s = bk.best_ask
                bid_s = bk.best_bid
                if ask_s is not None:
                    ref_px = float(ask_s)
                    if bid_s is not None and float(bid_s) + 0.01 < ref_px:
                        ref_px = min(ref_px, round(float(bid_s) + 0.01, 2))
                    if (p_s - ref_px) > max_plaus + 1e-9:
                        if getattr(self, since_attr, None) is None:
                            setattr(self, since_attr, now)
                        setattr(self, seen_attr, now)
                    else:
                        setattr(self, since_attr, None)
                        setattr(self, seen_attr, None)
                else:
                    # Tolerate transient 1-frame empty-ask flicker (<1s) without wiping divergence clock
                    last_seen = getattr(self, seen_attr, None)
                    if last_seen is None or (now - float(last_seen)) > div_grace_s:
                        setattr(self, since_attr, None)
                        setattr(self, seen_attr, None)

            _update_side_div(float(p_fair), book_up, "_ext_div_since_up", "_ext_div_last_seen_up")
            _update_side_div(1.0 - float(p_fair), book_down, "_ext_div_since_dn", "_ext_div_last_seen_dn")
        else:
            self._ext_div_since_up = None
            self._ext_div_since_dn = None
            self._ext_div_last_seen_up = None
            self._ext_div_last_seen_dn = None
        ext_div_up_s = max(0.0, now - self._ext_div_since_up) if getattr(self, "_ext_div_since_up", None) is not None else 0.0
        ext_div_dn_s = max(0.0, now - self._ext_div_since_dn) if getattr(self, "_ext_div_since_dn", None) is not None else 0.0

        active_entry_prob = getattr(self, "_active_leg_entry_prob", None)
        if active_entry_prob is None:
            active_entry_prob = getattr(self, "_entry_side_prob", None)

        eval_params = dict(
            config.PARAMS,
            AVAILABLE_CAPITAL=available_cap,
            COMMITTED_WINDOW_COST=committed_cost,
            HELD_LEG_COST=held_leg_cost,
            ENTRY_SIDE_PROB=active_entry_prob,
            LAST_FLIP_SIDE=getattr(self, "last_flip_side", None),
            SECONDS_SINCE_LAST_ENTRY=secs_since_entry,
            FLIPS_USED=getattr(self, "flips_count", 0),
            HAS_WORKING_ORDERS=any(o.cancel_at is None and o.remaining > 1e-6 for o in self.working_orders),
            CIRCUIT_BREAKER_ACTIVE=cb_active,
            CIRCUIT_BREAKER_REASON=cb_reason,
            BASIS_READY=basis_ready,
            BASIS_ANOMALY_ACTIVE=bool(getattr(self, "_basis_anomaly_active", False)),
            BASIS_DEV_USD=float(getattr(self, "_last_basis_dev_usd", 0.0) or 0.0),
            EXTREME_DIV_UP_S=ext_div_up_s,
            EXTREME_DIV_DOWN_S=ext_div_dn_s,
            WINDOW_UNWOUND=bool(getattr(self, "window_unwound", False)),
            BOOK_MOM_UP=mkt_mom_up,
            BOOK_MOM_DOWN=mkt_mom_down,
            REQUIRE_STRIKE_AND_BASIS=(
                (not replay_mode and delayed_book_fetcher is not None)
                or (replay_mode and bool(config.PARAMS.get("REPLAY_REQUIRE_STRIKE_AND_BASIS", False)))
            ),
        )

        if api_paused:
            decision = Decision(
                action="wait",
                reason=f"API schema guard active: {self._api_schema_guard.get('reason', 'upstream API changed')}",
                urgent_cancel=True,
            )
        elif getattr(self, "pending_takers", None):
            anti_toxic_bps_pt = float(config.PARAMS.get("ANTI_TOXICITY_SHOCK_BPS", 2.5))
            vel_bps_pt = abs(float(getattr(spot, "velocity_bps", 0.0) or 0.0))
            max_vol_pt = float(config.PARAMS.get("MAX_ENTRY_VOL_BPS", 75.0))
            min_tau_pt = float(config.PARAMS.get("ENTRY_MIN_TAU_S", 15.0))
            pt_urgent = (
                bool(getattr(spot, "is_toxic_shock", False))
                or vel_bps_pt >= anti_toxic_bps_pt
                or (max_vol_pt > 0 and vol_bps > max_vol_pt)
                or tau < min_tau_pt
                or spot_stale
            )
            decision = Decision(
                action="wait",
                reason="在途 Taker IOC 传输中，等待交易所撮合",
                urgent_cancel=pt_urgent,
            )
        elif is_deadman_active:
            decision = Decision(action="wait", reason=f"Dead-man switch active: {deadman_reason}")
        else:
            decision = plan_decision(
                self.pos_up, self.pos_down, book_up, book_down, spot, tau, eval_params,
                circuit_breaker_active=cb_active, circuit_breaker_reason=cb_reason,
                p_fair=p_fair,
                entries_count=self.entries_count,
                regime_info=regime_info,
                quote_attempts=getattr(self, "quote_attempts", 0)
            )
        if (
            replay_mode
            or decision.action != "wait"
            or not self.decisions
            or self.decisions[-1].get("action") != decision.action
            or self.decisions[-1].get("reason") != decision.reason
            or (now - getattr(self, "_last_wait_dec_t", 0.0)) >= 0.45
        ):
            self.decisions.append({
                "t": round(now, 2), "action": decision.action, "side": decision.side,
                "price": decision.price, "size": decision.size, "reason": decision.reason
            })
            self._last_wait_dec_t = now

        # Record significant decision state transitions for the live announcement feed
        tl_key = (decision.action, decision.side, (decision.reason or "").split("(")[0][:36])
        if (tl_key != getattr(self, "_last_timeline_key", None)
                or decision.action in ("taker_buy", "taker_hedge", "taker_unwind", "quote")
                or (now - getattr(self, "_last_timeline_t", 0.0)) >= 20.0):
            if not hasattr(self, "decision_timeline"):
                self.decision_timeline = []
            self.decision_timeline.append({
                "t": round(now, 2),
                "tau_s": round(tau, 1),
                "action": decision.action,
                "side": decision.side,
                "price": decision.price,
                "reason": decision.reason,
            })
            if len(self.decision_timeline) > 50:
                self.decision_timeline = self.decision_timeline[-50:]
            self._last_timeline_key = tl_key
            self._last_timeline_t = now
        
        min_order_sh = max(
            float(config.PARAMS.get("LOT_SIZE", 5.0)),
            float(config.PARAMS.get("MIN_ORDER_SHARES", 5.0)),
        )
        pref_side = determine_objective_side(spot, tau, eval_params, p_fair=p_fair)
        to_cancel, action = plan_order_actions(
            self.working_orders, decision, now,
            max_queue_wait_s=float(config.PARAMS.get("MAX_QUEUE_WAIT_S", 15.0)),
            preferred_side=pref_side,
            book_up=book_up,
            book_down=book_down,
            min_requote_age_s=float(config.PARAMS.get("MIN_REQUOTE_AGE_S", 2.5)),
            bid_retreat_ticks=float(config.PARAMS.get("MAX_BID_RETREAT_CANCEL", 0.029)),
            min_order_shares=min_order_sh,
            p_up=p_fair,
            min_hold_maker_edge=float(config.PARAMS.get("MIN_HOLD_MAKER_EDGE", 0.015)),
            queue_ahead_map=getattr(self.matcher, "queue_ahead", None),
            queue_head_protect_shares=float(config.PARAMS.get("QUEUE_HEAD_PROTECT_SHARES", 25.0)),
            mkt_mom_up=mkt_mom_up,
            mkt_mom_down=mkt_mom_down,
            book_drop_guard_max=(
                float(config.PARAMS.get("BOOK_DROP_GUARD_MAX", 0.03))
                if bool(config.PARAMS.get("ENABLE_BOOK_DROP_GUARD", False))
                else None
            ),
        )
        for o in to_cancel:
            o.cancel_at = now
            o.cancel_effective_at = now + cancel_lat
            if o in self.working_orders:
                self.working_orders.remove(o)
            if o.remaining > 1e-6 and cancel_lat > 0:
                self.pending_cancels.append(o)
            self.actions.append({
                "t": round(now, 2), "action": "cancel", "side": o.side,
                "price": o.price, "size": o.remaining, "purpose": o.purpose,
                "reason": o.cancel_reason or "策略撤单", "order_id": o.id
            })
            
        # 1. Taker execution simulation (pre-submission exact IOC limit + real wire delay, zero look-ahead rejects)
        if action and action.action in ("taker_buy", "taker_hedge", "taker_unwind"):
            want = float(action.size or 0.0)
            is_buy = action.action in ("taker_buy", "taker_hedge")
            target_book = book_down if action.side == "Down" else book_up
            reject_reason = None
            fill_ts = now
            fee_r = float(config.PARAMS.get("TAKER_FEE_RATE", 0.07))
            ioc_slip = max(0.0, float(config.PARAMS.get("TAKER_IOC_SLIPPAGE_TICKS", 0.02)))

            # Compute exact pre-submission IOC limit price BEFORE wire transit.
            # On a real exchange, upward slippage / pair-cost caps are enforced natively
            # by submitting an exact limit price; once the order hits the venue, any
            # liquidity priced at or better than the limit fills unconditionally (even
            # into an adverse flash crash — no post-arrival "knife-drop" look-ahead!).
            def _compute_pre_submit_ioc_limit() -> float:
                if getattr(action, "limit_price", None) is not None:
                    return max(0.01, min(0.99, round(float(action.limit_price), 2)))
                sig_px = float(action.price if action.price is not None else (0.99 if is_buy else 0.01))
                if not is_buy:
                    return max(0.01, round(sig_px - ioc_slip, 2))
                raw_cap = min(0.99, round(sig_px + ioc_slip, 2))
                act_reason = action.reason or ""
                act_intent = str(getattr(action, "intent", "") or "")
                if action.action == "taker_hedge":
                    held_basis = (
                        self.pos_up.avg_price if action.side == "Down"
                        else self.pos_down.avg_price
                    )
                    max_pair_cap = 1.00
                    best_lim = 0.0
                    c_hi = int(round(raw_cap * 100))
                    c_lo = max(1, int(round(sig_px * 100)) - 2)
                    for c in range(c_hi, c_lo - 1, -1):
                        p = c / 100.0
                        if held_basis + p + taker_fee_per_share(p, fee_r) <= max_pair_cap + 1e-6:
                            best_lim = p
                            break
                    return best_lim if best_lim > 0 else round(sig_px, 2)

                # taker_buy: cap limit price so post-fee edge still clears required edge
                is_flip_order = (act_intent == "reversal_flip") or ("反买" in act_reason) or ("反手" in act_reason)
                is_pyramid_order = (act_intent == "pyramid_add") or ("加仓" in act_reason)
                if is_flip_order:
                    held_pos_flip = self.pos_down if action.side == "Up" else self.pos_up
                    held_basis_flip = held_pos_flip.avg_price
                    max_flip_pair = float(config.PARAMS.get("FLIP_MAX_PAIR_COST", 2.00))
                    req_edge = float(config.PARAMS.get("FLIP_MIN_TAKER_EDGE", 0.038))
                elif is_pyramid_order:
                    held_basis_flip = 0.0
                    max_flip_pair = 2.0
                    req_edge = float(config.PARAMS.get("PYRAMID_MIN_TAKER_EDGE", 0.040))
                else:
                    held_basis_flip = 0.0
                    max_flip_pair = 2.0
                    base_tk = float(config.PARAMS.get("MIN_TAKER_EDGE", 0.035))
                    req_edge = required_entry_edge(sig_px, tau, base_tk, config.PARAMS, vol_bps=vol_bps)

                if action.utility is None:
                    return raw_cap
                sig_cost = sig_px + taker_fee_per_share(sig_px, fee_r)
                p_est = sig_cost + float(action.utility)
                best_lim = 0.0
                c_hi = int(round(raw_cap * 100))
                c_lo = max(1, int(round(sig_px * 100)))
                for c in range(c_hi, c_lo - 1, -1):
                    p = c / 100.0
                    f_p = taker_fee_per_share(p, fee_r)
                    if is_flip_order and (held_basis_flip + p + f_p > max_flip_pair + 1e-6):
                        continue
                    req_p = (
                        req_edge
                        if (is_flip_order or is_pyramid_order)
                        else required_entry_edge(
                            p, tau, float(config.PARAMS.get("MIN_TAKER_EDGE", 0.035)),
                            config.PARAMS, p_side=p_est, vol_bps=vol_bps,
                        )
                    )
                    if (p_est - (p + f_p)) >= req_p - 1e-6:
                        best_lim = p
                        break
                return best_lim if best_lim > 0 else round(sig_px, 2)

            ioc_limit_px = _compute_pre_submit_ioc_limit()

            if want < min_order_sh - 1e-4:
                filled, vwap = 0.0, 0.0
                reject_reason = f"违反最小下单量规则: 下单股数 {want:.2f} < {min_order_sh:.1f} 股"
                self._apply_taker_outcome(
                    fill_ts=fill_ts, now=now, action=action, want=want,
                    is_buy=is_buy, filled=filled, vwap=vwap, reject_reason=reject_reason,
                )
            elif (
                async_taker_delay
                and bool(config.PARAMS.get("TAKER_REAL_DELAY_CHECK", True))
                and delayed_book_fetcher is not None
                and action.price is not None
            ):
                # 非阻塞在途队列（实盘与离线回放统一复用）：发单后立刻返回主循环继续摄入盘口与撤单，
                # 在 [submit_t, arrive_at) 期间自然阻塞重复发单（零机枪重试），到达 arrive_at 后诚实撮合。
                wire_lat = self._effective_wire_latency(is_cancel=False, replay_mode=replay_mode)
                pre_side_shares = self.pos_up.shares if action.side == "Up" else self.pos_down.shares
                self.pending_takers.append({
                    "submit_t": now,
                    "arrive_at": now + wire_lat,
                    "wire_lat": wire_lat,
                    "action": action,
                    "want": want,
                    "is_buy": is_buy,
                    "ioc_limit_px": ioc_limit_px,
                    "min_order_sh": min_order_sh,
                    "pre_side_shares": pre_side_shares,
                })
            elif (bool(config.PARAMS.get("TAKER_REAL_DELAY_CHECK", True))
                    and delayed_book_fetcher is not None
                    and action.price is not None):
                wire_lat = self._effective_wire_latency(is_cancel=False, replay_mode=replay_mode)
                if not replay_mode:
                    time.sleep(wire_lat)
                fill_ts = now + wire_lat
                fetched = delayed_book_fetcher(action.side)
                post_up = None
                post_dn = None
                if isinstance(fetched, tuple) and len(fetched) == 2:
                    post_up, post_dn = fetched
                    post_book = post_up if action.side == "Up" else post_dn
                else:
                    post_book = fetched
                    if action.side == "Up":
                        post_up = post_book
                    else:
                        post_dn = post_book

                if self.pending_cancels and (not replay_mode or delayed_prints_fetcher is not None):
                    pre_side_shares = self.pos_up.shares if action.side == "Up" else self.pos_down.shares
                    delayed_now = fill_ts
                    delayed_touched = set()
                    if delayed_prints_fetcher is not None:
                        for tp in (delayed_prints_fetcher() or []):
                            tp_side = tp.get("outcome")
                            tp_dir = str(tp.get("side") or "")
                            tp_t = self._resolve_print_timestamp(tp, delayed_now, clock_offset_s=clk_off)
                            tp_recv_d = float(tp.get("received_at") or tp_t)
                            use_live_book_d = (not replay_mode) or (abs(delayed_now - tp_recv_d) <= 0.5)
                            for o in list(self.pending_cancels):
                                if not o.activated:
                                    continue
                                if tp_side and o.side != tp_side:
                                    continue
                                o_book_d = (post_up if o.side == "Up" else post_dn) if use_live_book_d else None
                                fill_sz = self.matcher.on_trade_print(
                                    o, float(tp["price"]), float(tp["size"]), tp_t,
                                    trade_side=tp_dir, current_book=o_book_d,
                                )
                                if fill_sz > 0 or o.remaining < o.size - 1e-9:
                                    delayed_touched.add(o.id)
                                if fill_sz > 0:
                                    self._apply_maker_fill(tp_t, o, fill_sz)
                    still_pending_after = []
                    for o in self.pending_cancels:
                        if o.remaining <= 1e-6:
                            continue
                        cancel_eff = float(getattr(o, "cancel_effective_at", 0.0) or 0.0)
                        o_book = post_up if o.side == "Up" else post_dn
                        if o_book is not None and o.id not in delayed_touched:
                            if delayed_now <= cancel_eff + 1e-9:
                                eval_t = min(delayed_now, cancel_eff - 1e-3)
                                fill_sz = self.matcher.match_order(o, o_book, eval_t)
                                if fill_sz > 0:
                                    self._apply_maker_fill(delayed_now, o, fill_sz)
                        if o.remaining > 1e-6 and delayed_now < cancel_eff - 1e-9:
                            still_pending_after.append(o)
                    self.pending_cancels = still_pending_after

                    if action.action == "taker_buy":
                        post_side_shares = self.pos_up.shares if action.side == "Up" else self.pos_down.shares
                        already_filled = max(0.0, post_side_shares - pre_side_shares)
                        if already_filled > 1e-6:
                            want = max(0.0, want - already_filled)
                            if want < min_order_sh - 1e-4:
                                filled, vwap = 0.0, 0.0
                                reject_reason = (
                                    f"撤单在途内原挂单已成交({already_filled:.1f}股)，取消重复Taker追单"
                                )

                lat_ms = int(round(wire_lat * 1000))
                filled, vwap, reject_reason = self._evaluate_delayed_taker_fill(
                    action, want, is_buy, ioc_limit_px, lat_ms, post_up, post_dn, reject_reason
                )
                self._apply_taker_outcome(
                    fill_ts=fill_ts, now=now, action=action, want=want,
                    is_buy=is_buy, filled=filled, vwap=vwap, reject_reason=reject_reason,
                )
            else:
                filled, vwap = self.matcher.taker_fill(target_book, want, is_buy=is_buy)
                if action.action == "taker_hedge" and filled > 1e-6:
                    held_basis = (
                        self.pos_up.avg_price if action.side == "Down"
                        else self.pos_down.avg_price
                    )
                    est_pair = held_basis + vwap + taker_fee_per_share(vwap, fee_r)
                    max_pair_cap = 1.00
                    if est_pair > max_pair_cap + 1e-6:
                        reject_reason = (
                            f"下单前可见 L2 深度 VWAP 使配对成本 {est_pair:.4f} > ${max_pair_cap:.2f} "
                            f"(决策价 {action.price:.2f} -> 逐档 VWAP {vwap:.4f})，拒绝下单"
                        )
                elif action.action == "taker_buy" and filled > 1e-6 and action.price is not None:
                    sig_px = float(action.price)
                    if vwap > ioc_limit_px + 1e-6:
                        reject_reason = (
                            f"下单前可见 L2 深度 VWAP 超出限价 (决策价 {sig_px:.2f}, "
                            f"限价 {ioc_limit_px:.2f} < 逐档 VWAP {vwap:.4f})，拒绝下单"
                        )
                self._apply_taker_outcome(
                    fill_ts=fill_ts, now=now, action=action, want=want,
                    is_buy=is_buy, filled=filled, vwap=vwap, reject_reason=reject_reason,
                )

        # 2. Maker quote placement (hard rule: every placed order must be >= min_order_sh = 5.0 shares)
        placed_oid = None
        if action and action.action == "quote" and float(action.size or 0.0) >= min_order_sh - 1e-4:
            purpose = "entry"
            self._next_order_id += 1
            oid = self._next_order_id
            placed_oid = oid
            quote_lat = self._effective_wire_latency(is_cancel=False, replay_mode=replay_mode)
            new_order = Order(
                id=oid,
                client_order_id=f"pm_{self.current_slug}_{oid}",
                side=action.side,
                price=action.price,
                size=action.size,
                remaining=action.size,
                filled_size=0.0,
                purpose=purpose,
                placed_at=now,
                live_at=now + quote_lat,
                intent=str(getattr(action, "intent", "") or ""),
            )
            new_order.entry_reason = str(action.reason or "")
            target_book = book_up if action.side == "Up" else book_down
            self.matcher.register_order(new_order, target_book, latency_s=quote_lat)
            self.working_orders.append(new_order)
            if purpose == "entry":
                self.quote_attempts = getattr(self, "quote_attempts", 0) + 1
            ahead_sz = self.matcher.queue_ahead.get(new_order.id, 0.0)
            self.actions.append({
                "t": round(now, 2), "action": "quote", "side": action.side,
                "price": action.price, "size": action.size, "purpose": purpose,
                "order_id": new_order.id, "queue_ahead": round(ahead_sz, 1),
                "reason": str(action.reason or ""),
            })

        # 3. 仅针对零延迟模式本 tick 新挂且已到达 live_at 的挂单执行 Post-Only 激活校验（老单已在 0b 处理）
        if placed_oid is not None and self.working_orders:
            remaining_working = []
            for o in self.working_orders:
                if o.id == placed_oid and now >= float(o.live_at or 0.0) - 1e-9:
                    target_book = book_up if o.side == "Up" else book_down
                    if not o.activated:
                        if not self.matcher.activate_order(o, target_book, now, post_only=True):
                            self.actions.append({
                                "t": round(now, 2), "action": "cancel", "side": o.side,
                                "price": o.price, "size": o.remaining, "purpose": o.purpose,
                                "reason": o.cancel_reason, "order_id": o.id
                            })
                            continue
                    fill_sz = self.matcher.match_order(o, target_book, now)
                    if fill_sz > 0:
                        self._apply_maker_fill(now, o, fill_sz)
                if o.remaining > 1e-6:
                    remaining_working.append(o)
            self.working_orders = remaining_working

        # Update atomic live.json (skipped in offline replay for ~15x speedup;
        # ~6.7 Hz baseline cadence, plus 0ms instant flush whenever actions, fills,
        # top-of-book prices, or resting queue depth change).
        if not replay_mode:
            last_w = getattr(self, "_last_live_write_t", 0.0)
            q_total = round(
                sum(float(self.matcher.queue_ahead.get(o.id, 0.0)) for o in self.working_orders),
                1,
            )
            live_sig = (
                len(self.actions),
                len(self.fills),
                len(self.working_orders),
                len(getattr(self, "pending_takers", [])),
                book_up.best_bid,
                book_up.best_ask,
                book_down.best_bid,
                book_down.best_ask,
                q_total,
                round(float(self.last_poly_spot or 0.0), 2),
            )
            last_sig = getattr(self, "_last_live_sig", None)
            if last_w <= 0.0 or (now - last_w) >= 0.15 or live_sig != last_sig:
                self._write_live(now, spot, book_up, book_down)
                self._last_live_write_t = now
                self._last_live_sig = live_sig

    def estimate_twap_close_price(self, now: float) -> float:
        """Estimate Chainlink-chain 60s TWAP close price when official TWAP is not yet published.

        Avoids mixing Chainlink TWAP open_px with raw Binance instantaneous spot
        during the 5-20s window right after a 5-minute rollover.
        """
        basis = getattr(self, "_basis_ema", None)
        if basis is not None and math.isfinite(float(basis)) and self._spot_buf:
            pts = [(ts, px) for ts, px in self._spot_buf if (now - ts) <= 60.5 and px > 0]
            if len(pts) >= 2:
                w_sum = 0.0
                dt_sum = 0.0
                for i in range(1, len(pts)):
                    dt = pts[i][0] - pts[i - 1][0]
                    if dt > 0.0:
                        w_sum += 0.5 * (pts[i - 1][1] + pts[i][1]) * dt
                        dt_sum += dt
                if dt_sum >= 15.0:
                    est = (w_sum / dt_sum) - float(basis)
                    if math.isfinite(est) and est > 0.0:
                        return round(est, 2)

        if getattr(self, "poly_spot_fresh", False) and float(getattr(self, "last_poly_spot", 0.0) or 0.0) > 0.0:
            return round(float(self.last_poly_spot), 2)

        last_spot = float(getattr(self, "last_spot_price", 0.0) or 0.0)
        if basis is not None and math.isfinite(float(basis)) and last_spot > 0.0:
            est = last_spot - float(basis)
            if math.isfinite(est) and est > 0.0:
                return round(est, 2)

        if last_spot > 0.0:
            return round(last_spot, 2)
        return round(float(getattr(self, "open_price", 0.0) or 0.0), 2)

    def close_window(self, close_px: float, official_winner: str = None,
                     winner_source: str = "spot_fallback"):
        """Settle session, append summary record, and compress window archive."""
        if self.current_slug and self.current_slug in self._settled_slugs:
            # The journal already holds this window (engine restarted mid-window,
            # or the feed settled it twice). Appending a second row would
            # double-count PnL and the circuit breaker, so settle exactly once.
            print(f"[Runner] Window {self.current_slug} already settled; duplicate close ignored")
            for o in self.working_orders:
                o.cancel_at = self.window_end
                self.actions.append({
                    "t": round(self.window_end, 2), "action": "cancel", "side": o.side,
                    "price": o.price, "size": o.remaining, "purpose": o.purpose,
                    "reason": "本轮到期自动撤单", "order_id": o.id
                })
            self.working_orders = []
            self.pending_takers = []
            return
        # 1. Resolve winner (prefer official UMA/Gamma settlement truth if available)
        if official_winner in ("Up", "Down"):
            winner = official_winner
        else:
            if close_px <= 0.0:
                close_px = getattr(self, "last_spot_price", self.open_price)
            winner = "Up" if close_px >= self.open_price else "Down"
        unwind_cost = max(0.0, float(getattr(self, "unwind_cost_basis", 0.0) or 0.0))
        unwind_proc = max(0.0, float(getattr(self, "unwind_proceeds", 0.0) or 0.0))
        if unwind_cost <= 1e-9 and unwind_proc <= 1e-9 and abs(getattr(self, "realized_unwind_pnl", 0.0)) > 1e-9:
            realized_u = float(getattr(self, "realized_unwind_pnl", 0.0))
            if realized_u >= 0.0:
                unwind_proc = realized_u
            else:
                unwind_cost = -realized_u
        expiry_proceeds = (self.pos_up.shares if winner == "Up" else self.pos_down.shares) * 1.00
        proceeds = expiry_proceeds + unwind_proc
        total_cost = self.pos_up.cash + self.pos_down.cash + unwind_cost
        net_pnl = round(proceeds - total_cost, 4)
        self._last_settled_slug = self.current_slug
        self.last_round_abs_move = self._compute_same_chain_abs_move(close_px, winner_source)
        is_traded = len(self.fills) > 0 or total_cost > 1e-4

        # Attribution: the regime that permitted the first entry describes the
        # traded setup; otherwise fall back to the time-weighted dominant regime.
        if self.entry_regime:
            dominant_regime = self.entry_regime
        else:
            if self._last_regime is not None and self._last_regime_t is not None:
                dt = max(0.0, self.window_end - self._last_regime_t)
                self.regime_dwell[self._last_regime] = self.regime_dwell.get(self._last_regime, 0.0) + dt
            dominant_regime = max(self.regime_dwell, key=self.regime_dwell.get) if self.regime_dwell else "normal_oscillation"
        
        record = {
            "slug": self.current_slug,
            "start": self.window_start,
            "end": self.window_end,
            "winner": winner,
            "winner_source": winner_source if official_winner in ("Up", "Down") else "spot_fallback",
            "open_px": round(float(self.open_price), 2),
            "close_px": round(float(close_px), 2),
            "net_pnl": net_pnl,
            "pos_up": self.pos_up.shares,
            "pos_down": self.pos_down.shares,
            "total_cost": round(total_cost, 4),
            "proceeds": round(proceeds, 4),
            "insurance_paid": round(self.insurance_paid, 4),
            "fills_count": len(self.fills),
            "traded": is_traded,
            "regime": dominant_regime,
            # "entry_time" = classified on the tick that opened the position and
            # therefore usable for attribution; "dwell" = time-weighted fallback
            # for windows that never traded. Legacy rows have no field at all.
            "regime_source": "entry_time" if self.entry_regime else "dwell",
            "batch_id": config.BATCH_ID
        }
        if unwind_proc > 1e-9:
            record["unwind_proceeds"] = round(unwind_proc, 4)
        # 记录本场执行控仓模式（flip / maker / taker / legacy mode_b），供看板总览与场次表 0ms 零解压归因展示
        if self.fills:
            f0 = self.fills[0]
            f0_reason = str(f0.get("reason") or "")
            filled_sides = {f.get("side") for f in self.fills if f.get("side") in ("Up", "Down")}
            if (self.pos_up.shares > 1e-6 and self.pos_down.shares > 1e-6) or len(filled_sides) >= 2:
                record["entry_mode"] = "flip"
            elif "模式B" in f0_reason or "[MODE_B]" in f0_reason:
                record["entry_mode"] = "mode_b"
            elif str(f0.get("kind") or "").lower() == "maker":
                record["entry_mode"] = "maker"
            else:
                record["entry_mode"] = "taker"
        # Independent Alpha benchmark metrics (Brier Score: lower is better)
        # Evaluates whether pricing model P* actually beats Polymarket mid, decoupled from execution.
        y_up = 1.0 if winner == "Up" else 0.0
        mid_p = getattr(self, "_mid_p_up", None)
        mid_m = getattr(self, "_mid_mkt_up", None)
        ent_p = getattr(self, "_entry_p_up", None)
        ent_m = getattr(self, "_entry_mkt_up", None)
        if mid_p is not None and mid_m is not None:
            record["mid_p_up"] = round(float(mid_p), 4)
            record["mid_mkt_up"] = round(float(mid_m), 4)
        if ent_p is not None and ent_m is not None:
            record["entry_p_up"] = round(float(ent_p), 4)
            record["entry_mkt_up"] = round(float(ent_m), 4)
        # 统一以固定中段截面 (tau<=150s) 计算无偏 Brier Score（确保交易窗与未交易窗口径一致、与校准表一致）；
        # 仅在中段快照缺失时才兜底使用开仓瞬间快照。
        eval_p = mid_p if mid_p is not None else ent_p
        eval_m = mid_m if mid_m is not None else ent_m
        if eval_p is not None and eval_m is not None:
            record["brier_model"] = round((float(eval_p) - y_up) ** 2, 5)
            record["brier_market"] = round((float(eval_m) - y_up) ** 2, 5)
        
        # 1. Fast append to summary.jsonl with trailing-newline guard + flush + fsync durability
        need_leading_nl = False
        if self.summary_file.exists():
            try:
                if self.summary_file.stat().st_size > 0:
                    with open(self.summary_file, "rb") as rf:
                        rf.seek(-1, os.SEEK_END)
                        need_leading_nl = rf.read(1) != b"\n"
            except OSError:
                pass
        with open(self.summary_file, "a", encoding="utf-8") as f:
            if need_leading_nl:
                f.write("\n")
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        self._fsync_dir(self.summary_file.parent)
        self._settled_slugs.add(self.current_slug)
        self.last_record = dict(record)
        self._recent_records.append(dict(record))
            
        # Update circuit breaker metrics & total cumulative PnL
        self.circuit_breaker.record_round_result(net_pnl, is_traded, self.window_end)
        if is_traded:
            self.total_pnl += net_pnl

        # Cancel & audit any still-in-flight async taker orders before archiving window
        if getattr(self, "pending_takers", None):
            for pt in self.pending_takers:
                fut = pt.get("book_future")
                if fut is not None and hasattr(fut, "cancel"):
                    try:
                        fut.cancel()
                    except Exception:
                        pass
                act = pt.get("action")
                if act is not None:
                    self.actions.append({
                        "t": round(float(self.window_end or time.time()), 2),
                        "action": "taker_reject",
                        "side": getattr(act, "side", None),
                        "requested": round(float(pt.get("want", 0.0) or 0.0), 4),
                        "reason": "窗口切换时在途异步Taker未完成，自动取消并放弃成交",
                    })

        # 2. Archive compressed detailed window log atomically (skip under critical disk-full guard)
        if is_replay_archive_allowed():
            detail = dict(
                record,
                fills=self.fills,
                actions=self.actions,
                decisions=self.decisions,
                price_history=self.price_history
            )
            out_gz = self.windows_dir / f"{self.current_slug}.json.gz"
            try:
                self._atomic_write_gzip_json(out_gz, detail)
            except OSError as exc:
                print(f"[Runner] WARNING: Failed to archive window gz {self.current_slug}: {exc}")

        # Clear working orders
        self.working_orders = []
        self.pending_cancels = []
        self.pending_takers = []

    def _compute_same_chain_abs_move(self, close_px: float, winner_source: str) -> float:
        """Compute prior-round move magnitude without mixing Binance spot and Chainlink strike."""
        if winner_source in ("twap", "ledger") and close_px > 0 and self.open_price > 0:
            return round(abs(close_px - self.open_price), 2)
        if not getattr(self, "poly_open_is_provisional", False) and self.open_price > 0:
            if getattr(self, "poly_spot_fresh", False) and getattr(self, "last_poly_spot", 0.0) > 0:
                return round(abs(self.last_poly_spot - self.open_price), 2)
            if getattr(self, "_basis_ema", None) is not None and close_px > 0:
                last_spot = float(getattr(self, "last_spot_price", 0.0) or 0.0)
                # Avoid double-subtracting basis if close_px was already de-biased via estimate_twap_close_price()
                if last_spot > 0 and abs(close_px - last_spot) < 1.0:
                    return round(abs((close_px - float(self._basis_ema)) - self.open_price), 2)
                return round(abs(close_px - self.open_price), 2)
        bin_open = float(getattr(self, "binance_open_price", 0.0) or 0.0)
        if bin_open > 0 and close_px > 0:
            return round(abs(close_px - bin_open), 2)
        return round(abs(close_px - self.open_price), 2)

    def _apply_reconcile_to_row(
        self, r: dict, open_px: float, close_px: float, winner: str, winner_source: str
    ) -> tuple:
        """Apply official prices/winner onto one summary row dict.

        Returns (updated_row, old_traded_pnl, new_traded_pnl, resolved_winner, resolved_source).
        """
        old_traded_pnl = float(r.get("net_pnl", 0.0)) if r.get("traded") else 0.0
        resolved_winner = winner
        resolved_source = winner_source
        if r.get("winner_source") == "gamma" and winner_source != "gamma":
            resolved_winner = r.get("winner", winner)
            resolved_source = "gamma"
        else:
            r["winner_source"] = winner_source
        r["open_px"] = round(open_px, 2)
        r["close_px"] = round(close_px, 2)
        r["winner"] = resolved_winner
        pos_up = float(r.get("pos_up", 0.0))
        pos_down = float(r.get("pos_down", 0.0))
        total_cost = float(r.get("total_cost", 0.0))
        unwind_proc = float(r.get("unwind_proceeds", 0.0) or 0.0)
        proceeds = (pos_up * 1.0 if resolved_winner == "Up" else pos_down * 1.0) + unwind_proc
        new_pnl = round(proceeds - total_cost, 4)
        r["proceeds"] = round(proceeds, 4)
        r["net_pnl"] = new_pnl
        r["result"] = "win" if new_pnl > 1e-4 else ("loss" if new_pnl < -1e-4 else "flat")
        eval_p = r.get("mid_p_up") if r.get("mid_p_up") is not None else r.get("entry_p_up")
        eval_m = r.get("mid_mkt_up") if r.get("mid_mkt_up") is not None else r.get("entry_mkt_up")
        if eval_p is not None and eval_m is not None:
            y_up = 1.0 if resolved_winner == "Up" else 0.0
            r["brier_model"] = round((float(eval_p) - y_up) ** 2, 5)
            r["brier_market"] = round((float(eval_m) - y_up) ** 2, 5)
        new_traded_pnl = new_pnl if r.get("traded") else 0.0
        return r, old_traded_pnl, new_traded_pnl, resolved_winner, resolved_source

    def reconcile_record(self, slug: str, open_px: float, close_px: float,
                         winner: str, winner_source: str = "twap"):
        """Update prices in summary.jsonl and the window archive with O(1) RAM & fast tail patching.

        99.9% of reconciles touch one of the last few lines of summary.jsonl.
        Instead of json.loads/json.dumps on 100k+ lines (7s / 160MB RAM at 1 year),
        we scan the last 64KB tail first, copy the untouched prefix in 1MB binary
        chunks, and fall back to byte-level streaming if reconciling an older slug.
        """
        if not self.summary_file.exists():
            return

        target_bytes = json.dumps(slug, ensure_ascii=False).encode("utf-8")
        resolved_winner = winner
        resolved_source = winner_source
        pnl_delta = 0.0
        matched_row = None
        tmp = self.summary_file.with_suffix(".jsonl.tmp")

        def _rewrite_tail_bytes(raw_chunk: bytes) -> bytearray:
            nonlocal resolved_winner, resolved_source, pnl_delta, matched_row
            out_buf = bytearray()
            for raw_ln in raw_chunk.splitlines():
                if not raw_ln.strip():
                    continue
                if target_bytes in raw_ln:
                    try:
                        r = json.loads(raw_ln.decode("utf-8"))
                    except Exception:
                        print(f"[Runner] WARNING: Dropping corrupted line during reconcile of {self.summary_file}")
                        continue
                    if isinstance(r, dict) and r.get("slug") == slug:
                        r, old_p, new_p, rw, rs = self._apply_reconcile_to_row(
                            r, open_px, close_px, winner, winner_source
                        )
                        resolved_winner = rw
                        resolved_source = rs
                        pnl_delta += (new_p - old_p)
                        matched_row = dict(r)
                        if winner_source == "twap" and slug == getattr(self, "_last_settled_slug", None) and open_px > 0 and close_px > 0:
                            self.last_round_abs_move = round(abs(close_px - open_px), 2)
                        out_buf.extend((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8"))
                        continue
                    out_buf.extend(raw_ln + b"\n")
                    continue
                # Fast structural sanity check on tail lines only (clean corrupted tail rows)
                stripped = raw_ln.strip()
                if not (stripped.startswith(b"{") and stripped.endswith(b"}")):
                    print(f"[Runner] WARNING: Dropping corrupted line during reconcile of {self.summary_file}")
                    continue
                out_buf.extend(raw_ln + b"\n")
            return out_buf

        try:
            file_size = self.summary_file.stat().st_size
            tail_window = 65536
            tail_start = max(0, file_size - tail_window)
            with open(self.summary_file, "rb") as fin:
                if tail_start > 0:
                    fin.seek(tail_start)
                    fin.readline()  # align to next full line boundary
                    tail_start = fin.tell()
                tail_bytes = fin.read()

            if target_bytes in tail_bytes:
                # Fast path: copy untouched binary prefix in 1MB chunks + write patched tail
                new_tail = _rewrite_tail_bytes(tail_bytes)
                with open(self.summary_file, "rb") as fin, open(tmp, "wb") as fout:
                    rem = tail_start
                    while rem > 0:
                        chunk = fin.read(min(1048576, rem))
                        if not chunk:
                            break
                        fout.write(chunk)
                        rem -= len(chunk)
                    fout.write(new_tail)
                    fout.flush()
                    try:
                        os.fsync(fout.fileno())
                    except OSError:
                        pass
            else:
                # Fallback path for ancient slugs: single-pass byte-streaming (O(1) RAM)
                with open(self.summary_file, "rb") as fin, open(tmp, "wb") as fout:
                    for raw_ln in fin:
                        stripped = raw_ln.strip()
                        if not stripped:
                            continue
                        if target_bytes in raw_ln:
                            try:
                                r = json.loads(raw_ln.decode("utf-8"))
                            except Exception:
                                print(f"[Runner] WARNING: Dropping corrupted line during reconcile of {self.summary_file}")
                                continue
                            if isinstance(r, dict) and r.get("slug") == slug:
                                r, old_p, new_p, rw, rs = self._apply_reconcile_to_row(
                                    r, open_px, close_px, winner, winner_source
                                )
                                resolved_winner = rw
                                resolved_source = rs
                                pnl_delta += (new_p - old_p)
                                matched_row = dict(r)
                                fout.write((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8"))
                                continue
                            fout.write(stripped + b"\n")
                            continue
                        if not (stripped.startswith(b"{") and stripped.endswith(b"}")):
                            print(f"[Runner] WARNING: Dropping corrupted line during reconcile of {self.summary_file}")
                            continue
                        fout.write(stripped + b"\n")
                    fout.flush()
                    try:
                        os.fsync(fout.fileno())
                    except OSError:
                        pass
            self._atomic_replace(tmp, self.summary_file)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        # Incremental accounting update: O(1) PnL update + bounded recent CircuitBreaker rebuild
        if matched_row is not None:
            self.total_pnl += pnl_delta
            updated_recent = False
            for idx in range(len(self._recent_records) - 1, -1, -1):
                if self._recent_records[idx].get("slug") == slug:
                    self._recent_records[idx] = matched_row
                    updated_recent = True
                    break
            if updated_recent:
                self._rebuild_recent_circuit_breaker()
            else:
                self._rebuild_accounting()

        # Also reconcile compressed detailed window archive atomically
        gz_path = self.windows_dir / f"{slug}.json.gz"
        if gz_path.exists():
            try:
                with gzip.open(gz_path, "rt", encoding="utf-8") as handle:
                    detail = json.load(handle)
                detail, _, _, _, _ = self._apply_reconcile_to_row(
                    detail, open_px, close_px, resolved_winner, resolved_source
                )
                self._atomic_write_gzip_json(gz_path, detail)
            except Exception as e:
                print(f"[Runner] Failed to reconcile window gz {slug}: {e}")

    def _write_live(self, now: float, spot: SpotState, book_up: OrderBook, book_down: OrderBook):
        # The dashboard used to run a second, slightly different regime
        # classifier just for display, so the regime shown could disagree with
        # the one that drove decisions.
        # It now reports the classification that actually ran this tick.
        micro_regime = getattr(self, "_last_regime", None) or getattr(spot, "regime", "normal")
        snr = getattr(self, "_last_snr", 0.0)
        bin_open = float(getattr(self, "binance_open_price", 0.0) or 0.0)
        bin_px = float(getattr(self, "last_spot_price", 0.0) or 0.0)
        poly_open = float(getattr(self, "open_price", 0.0) or 0.0)
        poly_px = float(getattr(self, "last_poly_spot", 0.0) or 0.0)
        bin_bps = ((bin_px - bin_open) / bin_open * 10000.0) if bin_open > 0 else 0.0
        poly_bps = ((poly_px - poly_open) / poly_open * 10000.0) if poly_open > 0 else 0.0
        def _book_snap(b: OrderBook) -> dict:
            bids = getattr(b, "bids", None) or []
            asks = getattr(b, "asks", None) or []
            return {
                "best_bid": b.best_bid,
                "best_ask": b.best_ask,
                "best_bid_size": round(float(bids[0][1]), 1) if bids else 0.0,
                "best_ask_size": round(float(asks[0][1]), 1) if asks else 0.0,
                "bid_depth_5": round(sum(float(sz) for _, sz in bids[:5]), 1),
                "ask_depth_5": round(sum(float(sz) for _, sz in asks[:5]), 1),
            }

        total_equity = self._initial_capital + self.total_pnl
        unwind_pnl_live = float(getattr(self, "realized_unwind_pnl", 0.0) or 0.0)
        net_pos_cash = self.pos_up.cash + self.pos_down.cash - unwind_pnl_live
        pos_committed = max(0.0, net_pos_cash)
        fee_rate_live = float(config.PARAMS.get("TAKER_FEE_RATE", 0.07))
        reserved_orders = (
            sum(max(0.0, o.remaining) * o.price for o in self.working_orders if getattr(o, "is_buy", True))
            + sum(max(0.0, o.remaining) * o.price for o in self.pending_cancels if getattr(o, "is_buy", True))
            + sum(
                max(0.0, float(pt["want"]))
                * (float(pt["ioc_limit_px"]) + taker_fee_per_share(float(pt["ioc_limit_px"]), fee_rate_live))
                for pt in getattr(self, "pending_takers", [])
                if pt.get("is_buy", True)
            )
        )
        free_capital = total_equity - net_pos_cash - reserved_orders

        state = {
            "batch_id": config.BATCH_ID,
            "slug": self.current_slug,
            "start": self.window_start,
            "end": self.window_end,
            "now": round(now, 2),
            "tau_s": max(0.0, round(self.window_end - now, 1)),
            "spot": round(bin_px, 2),                    # real Binance spot
            "poly_spot": round(poly_px, 2),              # lagging Chainlink TWAP
            "poly_spot_stale": not getattr(self, "poly_spot_fresh", False),
            "spot_stale": bool(getattr(spot, "is_stale", False)),
            "spot_source": getattr(spot, "spot_source", "binance"),
            "spot_provider": str(
                (getattr(self, "net_stats", None) or {}).get("spot_provider")
                or (getattr(spot, "spot_source", "") or "").replace("_ws", "")
                or config.PARAMS.get("SPOT_WS_PROVIDER", "binance")
                or "binance"
            ).strip().lower(),
            "basis_ema": round(self._basis_ema, 2) if self._basis_ema is not None else None,
            "open_px": poly_open,
            "binance_open_px": bin_open,
            "return_bps": round(bin_bps, 2),            # Binance vs Binance open
            "poly_return_bps": round(poly_bps, 2),      # Poly TWAP vs Poly strike
            "realized_vol_bps": round(getattr(spot, "realized_vol_bps", 15.0), 2),
            "regime": micro_regime,
            "micro_regime": micro_regime,
            "snr": snr,
            "circuit_breaker": self.circuit_breaker.get_status(now),
            "equity": round(total_equity, 2),
            "committed_cost": round(pos_committed, 2),
            "reserved_orders_cost": round(reserved_orders, 2),
            "available_capital": round(free_capital, 2),
            "total_pnl": round(self.total_pnl, 4),
            "poly_up": round(getattr(self, "last_poly_up", 0.5), 4),
            "poly_down": round(getattr(self, "last_poly_down", 0.5), 4),
            "price_history": self.price_history,
            "book_up": _book_snap(book_up),
            "book_down": _book_snap(book_down),
            "working_orders": [
                {
                    "id": o.id,
                    "side": o.side,
                    "price": o.price,
                    "size": o.remaining,
                    "purpose": o.purpose,
                    "placed_at": o.placed_at,
                    "queue_ahead": round(self.matcher.queue_ahead.get(o.id, 0.0), 1),
                    "wait_s": round(max(0.0, now - (o.placed_at or now)), 1),
                }
                for o in self.working_orders if o.remaining > 1e-6
            ],
            "pos_up": {"shares": self.pos_up.shares, "cash": round(self.pos_up.cash, 4), "avg_px": round(self.pos_up.avg_price, 4)},
            "pos_down": {"shares": self.pos_down.shares, "cash": round(self.pos_down.cash, 4), "avg_px": round(self.pos_down.avg_price, 4)},
            "fills_count": len(self.fills),
            "actions": self.actions,
            "insurance_paid": round(self.insurance_paid, 4),
            "realized_unwind_pnl": round(getattr(self, "realized_unwind_pnl", 0.0), 4),
            "unwind_cost_basis": round(getattr(self, "unwind_cost_basis", 0.0), 4),
            "unwind_proceeds": round(getattr(self, "unwind_proceeds", 0.0), 4),
            "window_unwound": bool(getattr(self, "window_unwound", False)),
            "basis_anomaly_active": bool(getattr(self, "_basis_anomaly_active", False)),
            "basis_dev_usd": round(float(getattr(self, "_last_basis_dev_usd", 0.0) or 0.0), 2),
            "poly_open_is_provisional": bool(getattr(self, "poly_open_is_provisional", False)),
            "quote_attempts": int(getattr(self, "quote_attempts", 0) or 0),
            "last_flip_side": getattr(self, "last_flip_side", None),
            "network": getattr(self, "net_stats", {}),
            "api_schema_guard": getattr(self, "_api_schema_guard", {"paused": False, "source": "", "reason": ""}),
            "last_decision": self.decisions[-1] if self.decisions else None,
            "decision_timeline": getattr(self, "decision_timeline", [])[-30:],
            "entry_side_prob": getattr(self, "_entry_side_prob", None),
            "active_leg_entry_prob": getattr(self, "_active_leg_entry_prob", None),
            "entry_p_up": getattr(self, "_entry_p_up", None),
            "entry_mkt_up": getattr(self, "_entry_mkt_up", None),
            "mid_p_up": getattr(self, "_mid_p_up", None),
            "mid_mkt_up": getattr(self, "_mid_mkt_up", None),
        }
        payload_str = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        if (
            getattr(self, "_async_live_write", False)
            and getattr(self, "_live_writer_thread", None) is not None
            and self._live_writer_thread.is_alive()
        ):
            with self._live_write_lock:
                self._live_pending_payload = payload_str
                self._live_idle_event.clear()
            self._live_write_event.set()
        else:
            self._atomic_write_text(
                self.live_file,
                payload_str,
                tmp_suffix=".tmp",
            )

    def _live_writer_loop(self):
        """Background coalescing writer for live.json: keeps trading hot-path 0ms non-blocking."""
        while True:
            self._live_write_event.wait(timeout=0.5)
            self._live_write_event.clear()
            while True:
                with self._live_write_lock:
                    payload = self._live_pending_payload
                    self._live_pending_payload = None
                if payload is None:
                    break
                try:
                    self._atomic_write_text(self.live_file, payload, tmp_suffix=".tmp")
                except Exception:
                    pass
            with self._live_write_lock:
                if self._live_pending_payload is None:
                    self._live_idle_event.set()
            if self._live_write_stop.is_set():
                with self._live_write_lock:
                    if self._live_pending_payload is None:
                        break

    def flush_live(self, timeout: float = 2.0):
        """Wait until any queued async live.json snapshot is atomically persisted to disk."""
        if not getattr(self, "_async_live_write", False):
            return
        th = getattr(self, "_live_writer_thread", None)
        if th is None or not th.is_alive():
            return
        self._live_write_event.set()
        self._live_idle_event.wait(timeout=timeout)

    def close(self):
        """Flush pending live snapshot and cleanly stop background live writer thread."""
        self.flush_live(timeout=2.0)
        if getattr(self, "_live_write_stop", None) is not None:
            self._live_write_stop.set()
        if getattr(self, "_live_write_event", None) is not None:
            self._live_write_event.set()
        th = getattr(self, "_live_writer_thread", None)
        if th is not None and th.is_alive():
            th.join(timeout=1.5)
