"""PM-Quant Circuit Breaker & Watchdog Engine.

Guards the account against catastrophic regime failure:
1. Hard daily drawdown circuit breaker (MAX_DAILY_DRAWDOWN_USD).
2. Consecutive loss streak cooling lock (LOSS_STREAK_COOLING).
3. "Reduce-only / Hedge-only" mode when active: strictly disables new Leg-1 entries,
   while permitting inventory-reducing hedges or unwinds.
"""
import time
import datetime
from typing import Tuple, Dict, Any, List


class CircuitBreaker:
    def __init__(
        self,
        max_daily_drawdown_usd: float = 8.00,
        loss_streak_cooling: int = 3,
        cooling_rounds: int = 3,
        enabled: bool = True
    ):
        self.enabled = enabled
        self.max_daily_drawdown_usd = max_daily_drawdown_usd
        self.loss_streak_cooling = loss_streak_cooling
        self.cooling_rounds = cooling_rounds

        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.cooling_rounds_left = 0
        self.is_tripped = False
        self.trip_reason = ""
        self.current_day = self._get_day_str()

    @staticmethod
    def _get_day_str(ts: float = None) -> str:
        t = ts or time.time()
        return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d")

    def _check_day_rollover(self, ts: float = None):
        today = self._get_day_str(ts)
        if today != self.current_day:
            self.current_day = today
            self.daily_pnl = 0.0
            self.is_tripped = False
            self.trip_reason = ""

    def record_round_result(self, net_pnl: float, traded: bool, ts: float = None):
        """Update circuit breaker metrics upon completion of a trading window."""
        self._check_day_rollover(ts)
        
        if not traded:
            # If round had no fills, tick down cooldown if cooling
            if self.cooling_rounds_left > 0:
                self.cooling_rounds_left -= 1
            return

        self.daily_pnl += net_pnl

        # Track consecutive loss streak
        if net_pnl < -1e-4:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.loss_streak_cooling:
                self.cooling_rounds_left = self.cooling_rounds
                self.trip_reason = f"Loss streak: {self.consecutive_losses} consecutive losing rounds"
        else:
            self.consecutive_losses = 0
            if self.cooling_rounds_left > 0:
                self.cooling_rounds_left -= 1

        # Check account-level daily drawdown limit
        if self.daily_pnl <= -self.max_daily_drawdown_usd:
            self.is_tripped = True
            self.trip_reason = f"Daily drawdown ${abs(self.daily_pnl):.2f} exceeded limit ${self.max_daily_drawdown_usd:.2f}"

    def is_active(self, ts: float = None) -> Tuple[bool, str]:
        """Check whether circuit breaker watchdog is currently active.
        
        Returns:
            (active, reason): active=True indicates 'reduce-only' (no new entries).
        """
        if not self.enabled:
            return False, "Disabled (熔断器已禁用)"
        self._check_day_rollover(ts)
        if self.is_tripped or self.daily_pnl <= -self.max_daily_drawdown_usd:
            reason = self.trip_reason or f"Daily drawdown limit reached (-${abs(self.daily_pnl):.2f})"
            return True, reason
        if self.cooling_rounds_left > 0:
            return True, f"Loss streak cooling ({self.cooling_rounds_left} rounds remaining)"
        return False, "Normal"

    def get_status(self, ts: float = None) -> Dict[str, Any]:
        """Return serialized status for live journal and dashboard."""
        if not self.enabled:
            return {
                "active": False,
                "reason": "Disabled (已取消熔断)",
                "status": "DISABLED",
                "daily_pnl": round(self.daily_pnl, 4),
                "is_tripped": False,
                "cooling_rounds_left": 0,
                "consecutive_losses": self.consecutive_losses,
                "max_daily_drawdown_usd": self.max_daily_drawdown_usd,
                "loss_streak_cooling": self.loss_streak_cooling
            }
        active, reason = self.is_active(ts)
        status_tag = "TRIPPED" if self.is_tripped else ("COOLING" if self.cooling_rounds_left > 0 else "NORMAL")
        return {
            "status": status_tag,
            "active": active,
            "reason": reason,
            "daily_pnl": round(self.daily_pnl, 4),
            "consecutive_losses": self.consecutive_losses,
            "cooling_rounds_left": self.cooling_rounds_left,
            "max_daily_drawdown_usd": self.max_daily_drawdown_usd,
            "loss_streak_cooling": self.loss_streak_cooling
        }

    def load_from_summary(self, summary_records: List[Dict[str, Any]]):
        """Restore circuit breaker state chronologically from historical summary lines."""
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.cooling_rounds_left = 0
        self.is_tripped = False
        self.trip_reason = ""
        self.current_day = self._get_day_str()

        # Sort chronologically
        sorted_recs = sorted(summary_records, key=lambda r: float(r.get("end") or r.get("start") or 0.0))
        for rec in sorted_recs:
            t = float(rec.get("end") or rec.get("start") or time.time())
            pnl = float(rec.get("net_pnl", 0.0))
            traded = bool(rec.get("traded"))
            self.record_round_result(pnl, traded=traded, ts=t)
