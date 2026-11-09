"""PM-Quant Live/Paper Trading Runner & Ledger Journal.

Orchestrates the decision cycle, records atomic live state,
and appends single-line summary events to summary.jsonl.
"""
import sys
import json
import time
import gzip
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from core.types import Position, OrderBook, SpotState, Order, Decision
from core.policy import plan_decision
from core.execution import plan_order_actions, quantize_lots
from sim.matcher import QueueMatcher


class PaperRunner:
    def __init__(self):
        self.ledger_dir = config.CURRENT_LEDGER_DIR
        self.summary_file = self.ledger_dir / "summary.jsonl"
        self.live_file = self.ledger_dir / "live.json"
        self.windows_dir = self.ledger_dir / "windows"
        self.windows_dir.mkdir(parents=True, exist_ok=True)
        self.matcher = QueueMatcher(config.PARAMS["LATENCY_S"])
        
        self.current_slug = ""
        self.window_start = 0.0
        self.window_end = 0.0
        self.pos_up = Position(0.0, 0.0)
        self.pos_down = Position(0.0, 0.0)
        self.working_orders = []
        self.fills = []
        self.decisions = []
        self.actions = []
        self.insurance_paid = 0.0

    def start_window(self, slug: str, start_t: float, end_t: float, open_px: float):
        """Initialize fresh 5-minute session."""
        self.current_slug = slug
        self.window_start = start_t
        self.window_end = end_t
        self.open_price = open_px
        self.last_spot_price = open_px
        self.pos_up = Position(0.0, 0.0)
        self.pos_down = Position(0.0, 0.0)
        self.working_orders = []
        self.fills = []
        self.decisions = []
        self.actions = []
        self.insurance_paid = 0.0
        self.matcher = QueueMatcher(config.PARAMS["LATENCY_S"])

    def _record_fill(self, now: float, side: str, price: float, size: float, kind: str, purpose: str):
        """Record one executed fill and its investor-readable action event."""
        fill = {
            "t": round(now, 2), "side": side, "price": price,
            "size": size, "kind": kind, "purpose": purpose
        }
        self.fills.append(fill)
        self.actions.append(dict(fill, action="fill"))

    def step(self, now: float, spot: SpotState, book_up: OrderBook, book_down: OrderBook, net_stats: dict = None):
        """Execute one evaluation tick."""
        self.last_spot_price = spot.price
        if net_stats:
            self.net_stats = net_stats
        tau = max(0.0, self.window_end - now)
        decision = plan_decision(
            self.pos_up, self.pos_down, book_up, book_down, spot, tau, config.PARAMS
        )
        self.decisions.append({
            "t": round(now, 2), "action": decision.action, "side": decision.side,
            "price": decision.price, "size": decision.size, "reason": decision.reason
        })
        
        to_cancel, action = plan_order_actions(self.working_orders, decision, now)
        for o in to_cancel:
            o.cancel_at = now
            if o in self.working_orders:
                self.working_orders.remove(o)
            
        # 1. Taker execution simulation
        if action and action.action in ("taker_hedge", "taker_unwind"):
            sz = action.size or 0.0
            px = action.price or 0.0
            fee = sz * 0.005
            if action.action == "taker_hedge":
                if action.side == "Down":
                    self.pos_down.shares += sz
                    self.pos_down.cash += (sz * px + fee)
                else:
                    self.pos_up.shares += sz
                    self.pos_up.cash += (sz * px + fee)
                pair_cost = self.pos_up.avg_price + self.pos_down.avg_price
                if pair_cost > 1.00:
                    self.insurance_paid += (pair_cost - 1.00) * sz
            elif action.action == "taker_unwind":
                if action.side == "Up":
                    self.pos_up.shares -= sz
                    self.pos_up.cash -= (sz * px - fee)
                else:
                    self.pos_down.shares -= sz
                    self.pos_down.cash -= (sz * px - fee)
            purpose = "unwind" if action.action == "taker_unwind" else "hedge"
            self._record_fill(now, action.side, px, sz, "taker", purpose)

        # 2. Maker quote placement
        elif action and action.action == "quote":
            purpose = "hedge" if abs(self.pos_up.shares - self.pos_down.shares) > 1e-6 else "entry"
            new_order = Order(
                id=int(now * 1000) % 1000000,
                client_order_id=f"pm_{int(now * 1000)}",
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
            self.actions.append({
                "t": round(now, 2), "action": "quote", "side": action.side,
                "price": action.price, "size": action.size, "purpose": purpose
            })

        # 3. FIFO Queue Matcher evaluation against book cross and trade prints
        remaining_working = []
        for o in self.working_orders:
            target_book = book_up if o.side == "Up" else book_down
            fill_sz = self.matcher.match_order(o, target_book, now)
            if fill_sz > 0:
                if o.side == "Up":
                    self.pos_up.shares += fill_sz
                    self.pos_up.cash += (fill_sz * o.price)
                else:
                    self.pos_down.shares += fill_sz
                    self.pos_down.cash += (fill_sz * o.price)
                self._record_fill(now, o.side, o.price, fill_sz, "maker", o.purpose)

            if o.remaining > 1e-6:
                remaining_working.append(o)
        self.working_orders = remaining_working

        # Update atomic live.json
        self._write_live(now, spot, book_up, book_down)

    def close_window(self, close_px: float, official_winner: str = None):
        """Settle session, append summary record, and compress window archive."""
        # 1. Resolve winner (prefer official UMA/Gamma settlement truth if available)
        if official_winner in ("Up", "Down"):
            winner = official_winner
        else:
            if close_px <= 0.0:
                close_px = getattr(self, "last_spot_price", self.open_price)
            winner = "Up" if close_px > self.open_price else "Down"
        proceeds = (self.pos_up.shares if winner == "Up" else self.pos_down.shares) * 1.00
        total_cost = self.pos_up.cash + self.pos_down.cash
        net_pnl = round(proceeds - total_cost, 4)
        is_traded = len(self.fills) > 0 or total_cost > 1e-4
        
        record = {
            "slug": self.current_slug,
            "start": self.window_start,
            "end": self.window_end,
            "winner": winner,
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
            "batch_id": config.BATCH_ID
        }
        
        # 1. Fast append to summary.jsonl
        with open(self.summary_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            
        # 2. Archive compressed detailed window log
        detail = dict(record, fills=self.fills, actions=self.actions, decisions=self.decisions)
        out_gz = self.windows_dir / f"{self.current_slug}.json.gz"
        with gzip.open(out_gz, "wt", encoding="utf-8") as gz:
            json.dump(detail, gz, ensure_ascii=False)

        # Clear working orders
        self.working_orders = []

    def reconcile_record(self, slug: str, open_px: float, close_px: float, winner: str):
        """Update summary.jsonl and window detail archive with final official Chainlink TWAP truth."""
        if not self.summary_file.exists():
            return
        lines = self.summary_file.read_text(encoding="utf-8").splitlines()
        updated = []
        for line in lines:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                if r.get("slug") == slug:
                    r["open_px"] = round(open_px, 2)
                    r["close_px"] = round(close_px, 2)
                    r["winner"] = winner
                    pos_up = float(r.get("pos_up", 0.0))
                    pos_down = float(r.get("pos_down", 0.0))
                    total_cost = float(r.get("total_cost", 0.0))
                    proceeds = pos_up * 1.0 if winner == "Up" else pos_down * 1.0
                    r["proceeds"] = round(proceeds, 4)
                    r["net_pnl"] = round(proceeds - total_cost, 4)
                    r["result"] = "win" if r["net_pnl"] > 1e-4 else ("loss" if r["net_pnl"] < -1e-4 else "flat")
                updated.append(json.dumps(r, ensure_ascii=False))
            except Exception:
                updated.append(line)
        self.summary_file.write_text("\n".join(updated) + "\n", encoding="utf-8")

    def _write_live(self, now: float, spot: SpotState, book_up: OrderBook, book_down: OrderBook):
        state = {
            "batch_id": config.BATCH_ID,
            "slug": self.current_slug,
            "start": self.window_start,
            "end": self.window_end,
            "now": round(now, 2),
            "tau_s": max(0.0, round(self.window_end - now, 1)),
            "spot": spot.price,
            "open_px": getattr(self, "open_price", 0.0),
            "return_bps": round(spot.return_bps, 2),
            "book_up": {"best_bid": book_up.best_bid, "best_ask": book_up.best_ask},
            "book_down": {"best_bid": book_down.best_bid, "best_ask": book_down.best_ask},
            "working_orders": [
                {"side": o.side, "price": o.price, "size": o.remaining}
                for o in self.working_orders if o.remaining > 1e-6
            ],
            "pos_up": {"shares": self.pos_up.shares, "cash": round(self.pos_up.cash, 4), "avg_px": round(self.pos_up.avg_price, 4)},
            "pos_down": {"shares": self.pos_down.shares, "cash": round(self.pos_down.cash, 4), "avg_px": round(self.pos_down.avg_price, 4)},
            "fills_count": len(self.fills),
            "actions": self.actions,
            "insurance_paid": round(self.insurance_paid, 4),
            "network": getattr(self, "net_stats", {}),
            "last_decision": self.decisions[-1] if self.decisions else None
        }
        tmp = self.live_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.live_file)
