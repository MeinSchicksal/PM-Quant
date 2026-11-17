"""PM-Quant Live/Paper Trading Runner & Ledger Journal.

Orchestrates the decision cycle, records atomic live state,
and appends single-line summary events to summary.jsonl.
"""
import sys
import json
import time
import gzip
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.types import Position, OrderBook, SpotState, Order, Decision
from core.policy import plan_decision, determine_objective_side
from core.execution import plan_order_actions, taker_fee_per_share
from core.circuit_breaker import CircuitBreaker
from sim.matcher import QueueMatcher, make_complementary, is_books_consistent


class PaperRunner:
    def __init__(self, ledger_dir=None):
        self.ledger_dir = Path(ledger_dir) if ledger_dir else config.CURRENT_LEDGER_DIR
        self.summary_file = self.ledger_dir / "summary.jsonl"
        self.live_file = self.ledger_dir / "live.json"
        self.windows_dir = self.ledger_dir / "windows"
        self.windows_dir.mkdir(parents=True, exist_ok=True)
        self.matcher = QueueMatcher(config.PARAMS["LATENCY_S"])
        
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
        self.fills = []
        self.decisions = []
        self.decision_timeline = []
        self._last_timeline_key = None
        self._last_timeline_t = 0.0
        self.actions = []
        self.insurance_paid = 0.0
        self.total_pnl = 0.0
        self._settled_slugs = set()
        self._initial_capital = float(config.PARAMS.get("INITIAL_CAPITAL_USD", 100.0))
        # Monotonic order ids. The old `int(now*1000) % 1000000` wrapped every
        # ~16.7 minutes and could reuse a stale queue_ahead entry.
        self._next_order_id = 0
        self.price_history = []
        self.regime_counts = {}
        self.regime_dwell = {}
        self._last_regime = None
        self._last_regime_t = None
        self.entry_regime = None
        self._last_step_time = None
        self.consecutive_up = 0
        self.consecutive_down = 0
        self._spot_buf = []
        self._basis_ema = None
        self._basis_updated_t = 0.0
        self._basis_sample_count = 0
        self._load_ledger_history()

    def _load_ledger_history(self):
        """Restore circuit breaker metrics, consecutive trends, and total cumulative PnL from summary.jsonl."""
        self._settled_slugs = set()
        self.consecutive_up = 0
        self.consecutive_down = 0
        if not self.summary_file.exists():
            return
        records = []
        try:
            for line in self.summary_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(json.loads(line))
            self._settled_slugs = {r.get("slug") for r in records if r.get("slug")}
            self.circuit_breaker.load_from_summary(records)
            self.total_pnl = sum(float(r.get("net_pnl", 0.0)) for r in records if r.get("traded"))
            for r in records:
                w = r.get("winner")
                if w == "Up":
                    self.consecutive_up += 1
                    self.consecutive_down = 0
                elif w == "Down":
                    self.consecutive_down += 1
                    self.consecutive_up = 0
        except Exception:
            pass

    def _rebuild_accounting(self):
        """Rebuild total PnL and circuit-breaker state from the summary journal."""
        self.total_pnl = 0.0
        self.circuit_breaker = CircuitBreaker(
            max_daily_drawdown_usd=config.PARAMS.get("MAX_DAILY_DRAWDOWN_USD", 8.00),
            loss_streak_cooling=int(config.PARAMS.get("LOSS_STREAK_COOLING", 3)),
            cooling_rounds=int(config.PARAMS.get("COOLING_ROUNDS", 3)),
            enabled=bool(config.PARAMS.get("ENABLE_CIRCUIT_BREAKER", True))
        )
        self._load_ledger_history()

    def start_window(self, slug: str, start_t: float, end_t: float, open_px: float, binance_open_px: float = None, poly_spot: float = None, poly_open_provisional: Optional[bool] = None):
        """Initialize fresh 5-minute session."""
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
        self.fills = []
        self.decisions = []
        self.decision_timeline = []
        self._last_timeline_key = None
        self._last_timeline_t = 0.0
        self.actions = []
        self.insurance_paid = 0.0
        self.entries_count = 0
        self.quote_attempts = 0
        self.entry_order_ids = set()
        self.min_opp_ask_seen = None
        self._active_entry_is_post_shock = False
        self.regime_counts = {}
        self.regime_dwell = {}
        self._last_regime = None
        self._last_regime_t = None
        self.entry_regime = None
        self._last_step_time = None
        self.matcher = QueueMatcher(config.PARAMS["LATENCY_S"])
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
        self.last_poly_up = 0.5
        self.last_poly_down = 0.5
        self.first_60s_high = self.last_poly_spot
        self.first_60s_low = self.last_poly_spot
        self.first_60s_range = 0.0

    def update_open_price(self, poly_open: float):
        """Update provisional open price once Polymarket official TWAP finishes computing."""
        if poly_open <= 0 or abs(self.open_price - poly_open) < 0.001:
            return
        self.open_price = poly_open
        self.poly_open_is_provisional = False
        # Cancel resting working orders placed under provisional strike to prevent execution on stale baseline
        if self.working_orders:
            for o in self.working_orders:
                o.cancel_at = time.time()
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

    def _record_fill(self, now: float, side: str, price: float, size: float, kind: str, purpose: str, order_id: Optional[int] = None):
        """Record one executed fill and its investor-readable action event."""
        if purpose == "entry":
            if order_id is None or order_id not in self.entry_order_ids:
                if order_id is not None:
                    self.entry_order_ids.add(order_id)
                # Attribute the round to the regime that actually allowed the
                # first entry, not to whichever regime spent the most ticks.
                if self.entries_count == 0 and self.entry_regime is None:
                    self.entry_regime = self._last_regime
                self.entries_count += 1
        fill = {
            "t": round(now, 2), "side": side, "price": price,
            "size": size, "kind": kind, "purpose": purpose,
            "poly_up": getattr(self, "last_poly_up", None),
            "poly_down": getattr(self, "last_poly_down", None),
            "spot_price": getattr(self, "last_spot_price", None),
            "tau_s": round(max(0.0, getattr(self, "window_end", 0.0) - now), 1)
        }
        self.fills.append(fill)
        self.actions.append(dict(fill, action="fill"))

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
        delayed_book_fetcher=None
    ):
        """Execute one evaluation tick: probability, regime, risk gates, matching.

        replay_mode disables the wall-clock dead-man switch: recorded book
        updates are sparse, so a multi-second gap between two events is normal
        and must not cancel resting quotes during a replay.
        """
        tau = max(0.0, self.window_end - now)
        if not bool(config.PARAMS.get("STRICT_BOOK_CONSISTENCY", True)):
            book_up, book_down = make_complementary(book_up, book_down)
        open_px = getattr(self, "open_price", 0.0)
        vol_bps = float(getattr(spot, "realized_vol_bps", 15.0) or 15.0)

        # None means the feed's freshness guard rejected its cached TWAP.
        poly_fresh = poly_spot is not None and poly_spot > 0
        self.poly_spot_fresh = poly_fresh
        if poly_fresh:
            self.last_poly_spot = poly_spot

        spot_stale = bool(getattr(spot, "is_stale", False)) or (spot.price <= 0)
        if not spot_stale and spot.price > 0:
            self.last_spot_price = spot.price
            self._spot_buf.append((now, float(spot.price)))
            if len(self._spot_buf) > 120 or (self._spot_buf and now - self._spot_buf[0][0] > 65.0):
                self._spot_buf = [(ts, px) for ts, px in self._spot_buf if now - ts <= 65.0]

        if poly_fresh and not spot_stale and len(self._spot_buf) >= 5:
            bin_60s = [px for ts, px in self._spot_buf if now - ts <= 60.0]
            if bin_60s:
                inst_basis = (sum(bin_60s) / len(bin_60s)) - float(poly_spot)
                self._basis_ema = inst_basis if self._basis_ema is None else (0.9 * self._basis_ema + 0.1 * inst_basis)
                self._basis_updated_t = now
                self._basis_sample_count += 1

        basis_max_age = float(config.PARAMS.get("BASIS_MAX_AGE_S", 120.0))
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
            if tau <= 60.0 and self._spot_buf:
                win_start_60 = self.window_end - 60.0
                realized_vals = [px - self._basis_ema for ts, px in self._spot_buf if ts >= win_start_60]
                if realized_vals:
                    spot.twap_realized_60s = sum(realized_vals) / len(realized_vals)
        else:
            spot.basis_ema = None
            spot.open_price = self.binance_open_price

        poly_up = getattr(book_up, "mid", None) or getattr(book_up, "best_bid", None) or getattr(book_up, "best_ask", None) or 0.5
        poly_down = getattr(book_down, "mid", None) or getattr(book_down, "best_bid", None) or getattr(book_down, "best_ask", None) or round(1.0 - poly_up, 4)
        self.last_poly_up = poly_up
        self.last_poly_down = poly_down

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
        if self.price_history:
            old_pt = None
            for pt in reversed(self.price_history):
                age = now - float(pt.get("t", now))
                if age >= 5.0:
                    old_pt = pt
                    if age >= 12.0:
                        break
            if old_pt is not None:
                if abs(float(getattr(spot, "ret_change_15s_bps", 0.0) or 0.0)) < 1e-9:
                    spot.ret_change_15s_bps = round(bin_bps - float(old_pt.get("return_bps", 0.0) or 0.0), 3)
                if abs(float(getattr(spot, "up_mid_change_15s", 0.0) or 0.0)) < 1e-9:
                    spot.up_mid_change_15s = round(poly_up - float(old_pt.get("poly_up", 0.5) or 0.5), 4)
        if ref_px > 0:
            if now - self.window_start <= 60.0:
                self.first_60s_high = max(getattr(self, "first_60s_high", ref_px), ref_px)
                self.first_60s_low = min(getattr(self, "first_60s_low", ref_px), ref_px)
                self.first_60s_range = round(self.first_60s_high - self.first_60s_low, 2)
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
            if len(self.price_history) > 720:
                self.price_history = self.price_history[-720:]

        if net_stats:
            self.net_stats = net_stats

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

        # 0. Process real trade prints that occurred in (prev_t, now] against resting orders
        # BEFORE evaluating new decisions/cancellations for `now`.
        print_touched_ids = set()
        if trade_prints and self.working_orders:
            for tp in trade_prints:
                tp_side = tp.get("outcome")  # "Up" or "Down"
                tp_t = float(tp.get("received_at") or tp.get("timestamp") or now)
                for o in self.working_orders:
                    if tp_side and o.side != tp_side:
                        continue
                    fill_sz = self.matcher.on_trade_print(
                        o, float(tp["price"]), float(tp["size"]), tp_t
                    )
                    if fill_sz > 0 or o.remaining < o.size - 1e-9:
                        print_touched_ids.add(o.id)
                    if fill_sz > 0:
                        if o.side == "Up":
                            self.pos_up.shares += fill_sz
                            self.pos_up.cash += (fill_sz * o.price)
                        else:
                            self.pos_down.shares += fill_sz
                            self.pos_down.cash += (fill_sz * o.price)
                        self._record_fill(tp_t, o.side, o.price, fill_sz, "maker", o.purpose, order_id=o.id)
            self.working_orders = [o for o in self.working_orders if o.remaining > 1e-6]

        if is_deadman_active and self.working_orders:
            for o in self.working_orders:
                o.cancel_at = now
                o.cancel_reason = f"行情延迟保护撤单 ({deadman_reason})"
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
        self.regime_counts[regime_info.regime] = self.regime_counts.get(regime_info.regime, 0) + 1

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
        # must see the real (possibly negative) balance and stop opening.
        available_cap = self._initial_capital + self.total_pnl
        committed_cost = self.pos_up.cash + self.pos_down.cash
        eval_params = dict(
            config.PARAMS,
            AVAILABLE_CAPITAL=available_cap,
            COMMITTED_WINDOW_COST=committed_cost,
            CIRCUIT_BREAKER_ACTIVE=cb_active,
            CIRCUIT_BREAKER_REASON=cb_reason,
            BASIS_READY=basis_ready,
            REQUIRE_STRIKE_AND_BASIS=(not replay_mode and delayed_book_fetcher is not None),
            PREV_ROUND_ABS_MOVE=getattr(self, "last_round_abs_move", 35.0),
            FIRST_60S_RANGE=getattr(self, "first_60s_range", 5.0),
            CONSECUTIVE_UP_ROUNDS=getattr(self, "consecutive_up", 0),
            CONSECUTIVE_DOWN_ROUNDS=getattr(self, "consecutive_down", 0)
        )

        if is_deadman_active:
            decision = Decision(action="wait", reason=f"Dead-man switch active: {deadman_reason}")
        else:
            decision = plan_decision(
                self.pos_up, self.pos_down, book_up, book_down, spot, tau, eval_params,
                circuit_breaker_active=cb_active, circuit_breaker_reason=cb_reason,
                entries_count=self.entries_count,
                regime_info=regime_info,
                quote_attempts=getattr(self, "quote_attempts", 0)
            )
        self.decisions.append({
            "t": round(now, 2), "action": decision.action, "side": decision.side,
            "price": decision.price, "size": decision.size, "reason": decision.reason
        })

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
        
        if not self.working_orders:
            self._active_entry_is_post_shock = False
        pref_side = (
            None if getattr(self, "_active_entry_is_post_shock", False)
            else determine_objective_side(spot, tau, eval_params)
        )
        to_cancel, action = plan_order_actions(
            self.working_orders, decision, now,
            max_queue_wait_s=float(config.PARAMS.get("MAX_QUEUE_WAIT_S", 15.0)),
            preferred_side=pref_side,
            book_up=book_up,
            book_down=book_down,
            min_requote_age_s=float(config.PARAMS.get("MIN_REQUOTE_AGE_S", 2.5)),
            bid_retreat_ticks=float(config.PARAMS.get("MAX_BID_RETREAT_CANCEL", 0.029)),
        )
        for o in to_cancel:
            o.cancel_at = now
            if o in self.working_orders:
                self.working_orders.remove(o)
            self.actions.append({
                "t": round(now, 2), "action": "cancel", "side": o.side,
                "price": o.price, "size": o.remaining, "purpose": o.purpose,
                "reason": o.cancel_reason or "策略撤单", "order_id": o.id
            })
            
        # 1. Taker execution simulation (depth-aware + optional real wire latency IOC check)
        if action and action.action in ("taker_buy", "taker_hedge", "taker_unwind"):
            want = float(action.size or 0.0)
            is_buy = action.action in ("taker_buy", "taker_hedge")
            target_book = book_down if action.side == "Down" else book_up
            reject_reason = None

            if (bool(config.PARAMS.get("TAKER_REAL_DELAY_CHECK", True))
                    and delayed_book_fetcher is not None
                    and action.price is not None):
                if not replay_mode:
                    time.sleep(float(config.PARAMS.get("LATENCY_S", 0.30)))
                post_book = delayed_book_fetcher(action.side)
                if post_book is None or (post_book.best_ask is None and post_book.best_bid is None):
                    filled, vwap = 0.0, 0.0
                    reject_reason = "延迟后重拉盘口为空，IOC 放弃成交"
                else:
                    filled, vwap = self.matcher.taker_ioc_fill(
                        post_book, want, limit_price=float(action.price), is_buy=is_buy
                    )
                    if filled <= 1e-6:
                        reject_reason = (
                            f"300ms 延迟后盘口已移动 (原信号限价 {action.price:.2f}, "
                            f"现ask={post_book.best_ask}/bid={post_book.best_bid})，IOC 错失防滑点"
                        )
            else:
                filled, vwap = self.matcher.taker_fill(target_book, want, is_buy=is_buy)

            # The policy judged the hedge on best_ask, but a taker actually
            # walks the ladder. Re-check the *real* VWAP before booking what
            # would otherwise be a falsely "risk-free" sub-$1 pair.
            if action.action == "taker_hedge" and filled > 1e-6:
                held_basis = (self.pos_up.avg_price if action.side == "Down"
                              else self.pos_down.avg_price)
                est_fee = taker_fee_per_share(vwap, config.PARAMS["TAKER_FEE_RATE"])
                est_pair = held_basis + vwap + est_fee
                max_pair_cap = (
                    float(config.PARAMS.get("TRAILING_HEDGE_MAX_PAIR_COST", 0.97))
                    if "Trailing Pair Lock" in (action.reason or "")
                    else 1.00
                )
                if est_pair > max_pair_cap + 1e-6:
                    reject_reason = (
                        f"VWAP 滑点使配对成本 {est_pair:.4f} > ${max_pair_cap:.2f} "
                        f"(决策价 {action.price:.2f} -> 逐档 VWAP {vwap:.4f})，拒绝伪无风险锁仓"
                    )
            if action.action == "taker_buy" and filled > 1e-6:
                slip = vwap - float(action.price if action.price is not None else vwap)
                if slip > 0.02:
                    reject_reason = (
                        f"VWAP 滑点过大 (决策价 {action.price:.2f} -> "
                        f"逐档 VWAP {vwap:.4f}, 滑点 {slip:+.4f})，拒绝吃单"
                    )
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
                    # Insurance premium is a stock on the *paired* shares, not
                    # a per-fill flow. The old code accumulated on every buy
                    # using two averages that are only comparable when both
                    # legs hold the same number of shares.
                    paired = min(self.pos_up.shares, self.pos_down.shares)
                    if paired > 1e-6:
                        pair_cost = self.pos_up.avg_price + self.pos_down.avg_price
                        self.insurance_paid = max(0.0, (pair_cost - 1.00) * paired)
                    else:
                        self.insurance_paid = 0.0
                else:
                    if action.side == "Up":
                        self.pos_up.shares -= filled
                        self.pos_up.cash -= (filled * px - fee)
                    else:
                        self.pos_down.shares -= filled
                        self.pos_down.cash -= (filled * px - fee)
                if action.action == "taker_unwind":
                    purpose = "unwind"
                elif action.action == "taker_buy":
                    purpose = "entry"
                else:
                    purpose = "hedge"
                self._record_fill(now, action.side, px, filled, "taker", purpose)
                if filled + 1e-6 < want:
                    self.actions.append({
                        "t": round(now, 2), "action": "taker_partial", "side": action.side,
                        "requested": round(want, 4), "filled": round(filled, 4),
                        "reason": "insufficient visible book depth"
                    })
            else:
                # No executable liquidity: the order is rejected rather than
                # filled at a fictional best price.
                self.actions.append({
                    "t": round(now, 2), "action": "taker_reject", "side": action.side,
                    "requested": round(want, 4),
                    "reason": reject_reason or "empty/one-sided book, no visible liquidity"
                })

        # 2. Maker quote placement
        elif action and action.action == "quote":
            purpose = "hedge" if abs(self.pos_up.shares - self.pos_down.shares) > 1e-6 else "entry"
            self._next_order_id += 1
            oid = self._next_order_id
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
                live_at=now + config.PARAMS["LATENCY_S"]
            )
            target_book = book_up if action.side == "Up" else book_down
            self.matcher.register_order(new_order, target_book)
            self.working_orders.append(new_order)
            self._active_entry_is_post_shock = (
                purpose == "entry" and "Post-Shock" in (decision.reason or "")
            )
            self.quote_attempts = getattr(self, "quote_attempts", 0) + 1
            ahead_sz = self.matcher.queue_ahead.get(new_order.id, 0.0)
            self.actions.append({
                "t": round(now, 2), "action": "quote", "side": action.side,
                "price": action.price, "size": action.size, "purpose": purpose,
                "order_id": new_order.id, "queue_ahead": round(ahead_sz, 1)
            })

        # 3. Crossed-book matching for remaining resting maker orders
        remaining_working = []
        for o in self.working_orders:
            if o.id in print_touched_ids:
                if o.remaining > 1e-6:
                    remaining_working.append(o)
                continue
            target_book = book_up if o.side == "Up" else book_down
            fill_sz = self.matcher.match_order(o, target_book, now)
            if fill_sz > 0:
                if o.side == "Up":
                    self.pos_up.shares += fill_sz
                    self.pos_up.cash += (fill_sz * o.price)
                else:
                    self.pos_down.shares += fill_sz
                    self.pos_down.cash += (fill_sz * o.price)
                self._record_fill(now, o.side, o.price, fill_sz, "maker", o.purpose, order_id=o.id)

            if o.remaining > 1e-6:
                remaining_working.append(o)
        self.working_orders = remaining_working

        # Update atomic live.json
        self._write_live(now, spot, book_up, book_down)

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
            return
        # 1. Resolve winner (prefer official UMA/Gamma settlement truth if available)
        if official_winner in ("Up", "Down"):
            winner = official_winner
        else:
            if close_px <= 0.0:
                close_px = getattr(self, "last_spot_price", self.open_price)
            winner = "Up" if close_px >= self.open_price else "Down"
        proceeds = (self.pos_up.shares if winner == "Up" else self.pos_down.shares) * 1.00
        total_cost = self.pos_up.cash + self.pos_down.cash
        net_pnl = round(proceeds - total_cost, 4)
        self.last_round_abs_move = round(abs(close_px - self.open_price), 2)
        if winner == "Up":
            self.consecutive_up = getattr(self, "consecutive_up", 0) + 1
            self.consecutive_down = 0
        elif winner == "Down":
            self.consecutive_down = getattr(self, "consecutive_down", 0) + 1
            self.consecutive_up = 0
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
            "open_px": self.open_price,
            "close_px": close_px,
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
        
        # 1. Fast append to summary.jsonl
        with open(self.summary_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._settled_slugs.add(self.current_slug)
            
        # Update circuit breaker metrics & total cumulative PnL
        self.circuit_breaker.record_round_result(net_pnl, is_traded, self.window_end)
        if is_traded:
            self.total_pnl += net_pnl

        # 2. Archive compressed detailed window log
        detail = dict(
            record,
            fills=self.fills,
            actions=self.actions,
            decisions=self.decisions,
            price_history=self.price_history
        )
        out_gz = self.windows_dir / f"{self.current_slug}.json.gz"
        with gzip.open(out_gz, "wt", encoding="utf-8") as gz:
            json.dump(detail, gz, ensure_ascii=False)

        # Clear working orders
        self.working_orders = []

    def reconcile_record(self, slug: str, open_px: float, close_px: float,
                         winner: str, winner_source: str = "twap"):
        """Update prices in summary.jsonl and the window archive.

        winner_source="gamma" is authoritative. A later TWAP-derived reconcile
        may refresh open/close prices, but must never overwrite a winner that
        was already confirmed by UMA/Gamma resolution.
        """
        if not self.summary_file.exists():
            return
        lines = self.summary_file.read_text(encoding="utf-8").splitlines()
        updated = []
        resolved_winner = winner
        resolved_source = winner_source
        for line in lines:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                if r.get("slug") == slug:
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
                    proceeds = pos_up * 1.0 if resolved_winner == "Up" else pos_down * 1.0
                    new_pnl = round(proceeds - total_cost, 4)
                    r["proceeds"] = round(proceeds, 4)
                    r["net_pnl"] = new_pnl
                    r["result"] = "win" if new_pnl > 1e-4 else ("loss" if new_pnl < -1e-4 else "flat")
                updated.append(json.dumps(r, ensure_ascii=False))
            except Exception:
                updated.append(line)
        tmp = self.summary_file.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(updated) + "\n", encoding="utf-8")
        tmp.replace(self.summary_file)
        # Summary is the single accounting source of truth: rebuild the running
        # total and circuit-breaker state from the rewritten file instead of
        # applying a fragile in-memory delta.
        self._rebuild_accounting()

        # Also reconcile compressed detailed window archive
        gz_path = self.windows_dir / f"{slug}.json.gz"
        if gz_path.exists():
            try:
                with gzip.open(gz_path, "rt", encoding="utf-8") as handle:
                    detail = json.load(handle)
                detail["open_px"] = round(open_px, 2)
                detail["close_px"] = round(close_px, 2)
                if detail.get("winner_source") == "gamma" and resolved_source != "gamma":
                    detail_winner = detail.get("winner", resolved_winner)
                    detail["winner"] = detail_winner
                    detail["winner_source"] = "gamma"
                    resolved_winner = detail_winner
                else:
                    detail["winner"] = resolved_winner
                    detail["winner_source"] = resolved_source
                pos_up = float(detail.get("pos_up", 0.0))
                pos_down = float(detail.get("pos_down", 0.0))
                total_cost = float(detail.get("total_cost", 0.0))
                proceeds = pos_up * 1.0 if resolved_winner == "Up" else pos_down * 1.0
                detail["proceeds"] = round(proceeds, 4)
                detail["net_pnl"] = round(proceeds - total_cost, 4)
                detail["result"] = "win" if detail["net_pnl"] > 1e-4 else ("loss" if detail["net_pnl"] < -1e-4 else "flat")
                with gzip.open(gz_path, "wt", encoding="utf-8") as handle:
                    json.dump(detail, handle, ensure_ascii=False)
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
            "available_capital": round(self._initial_capital + self.total_pnl, 2),
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
            "network": getattr(self, "net_stats", {}),
            "last_decision": self.decisions[-1] if self.decisions else None,
            "decision_timeline": getattr(self, "decision_timeline", [])[-30:]
        }
        tmp = self.live_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        for attempt in range(3):
            try:
                tmp.replace(self.live_file)
                break
            except (PermissionError, OSError):
                if attempt == 2:
                    raise
                time.sleep(0.01)
