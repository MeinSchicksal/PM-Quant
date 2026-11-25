"""Strongly-typed data structures for PM-Quant.

Lightweight, self-documenting dataclasses designed for high readability and testability.
"""
from dataclasses import dataclass, field
from typing import Literal, Optional, List, Tuple

Side = Literal["Up", "Down"]
OrderKind = Literal["maker", "taker"]
OrderPurpose = Literal["entry", "hedge", "unwind"]
ActionType = Literal["quote", "taker_buy", "taker_hedge", "taker_unwind", "cancel", "wait"]


@dataclass
class OrderBook:
    bids: List[Tuple[float, float]] = field(default_factory=list)
    asks: List[Tuple[float, float]] = field(default_factory=list)
    last_trade_price: Optional[float] = None
    source_t: float = 0.0
    received_at: float = 0.0

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return round((self.best_bid + self.best_ask) / 2.0, 6)
        return self.best_bid or self.best_ask

    @property
    def spread(self) -> float:
        if self.best_bid is not None and self.best_ask is not None:
            return max(0.0, round(self.best_ask - self.best_bid, 6))
        return 1.0


@dataclass
class SpotState:
    timestamp: float
    price: float
    open_price: float
    return_bps: float = 0.0
    realized_vol_bps: float = 15.0      # Dynamic rolling EWMA volatility in bps / 5m
    is_toxic_shock: bool = False
    regime: str = "normal"              # "calm" (<5 bps), "normal" (5-30 bps), "volatile" (>30 bps)
    poly_price: Optional[float] = None
    binance_open_price: Optional[float] = None
    basis_ema: Optional[float] = None   # Live rolling (Binance_60s_avg - Chainlink_60s_TWAP)
    twap_realized_60s: Optional[float] = None  # Estimated Chainlink average already locked in [end-60, t]
    spot_source: str = "binance"
    is_stale: bool = False
    velocity_bps: float = 0.0
    min_opp_ask_seen: Optional[float] = None  # Lowest opposite ask observed since Leg-1 fill


@dataclass
class Position:
    shares: float = 0.0
    cash: float = 0.0

    @property
    def avg_price(self) -> float:
        return (self.cash / self.shares) if self.shares > 1e-6 else 0.0


@dataclass
class Order:
    id: int
    client_order_id: str
    side: Side
    price: float
    size: float
    remaining: float
    filled_size: float = 0.0
    kind: OrderKind = "maker"
    purpose: OrderPurpose = "entry"
    placed_at: float = 0.0
    live_at: float = 0.0
    cancel_at: Optional[float] = None
    cancel_effective_at: Optional[float] = None
    cancel_reason: str = ""
    activated: bool = False
    # Structured strategy intent (e.g. "entry_lead", "entry_value", "pyramid_add", "reversal_flip")
    intent: str = ""


@dataclass
class Decision:
    action: ActionType
    side: Optional[Side] = None
    price: Optional[float] = None
    size: Optional[float] = None
    utility: Optional[float] = None
    reason: str = ""
    timestamp: float = 0.0
    # Structural cancel signal. A 'wait' decision with urgent_cancel=True must
    # pull all resting quotes immediately (toxic shock, tail freeze, regime
    # lockout, risk envelope breach). This replaces substring-matching on
    # `reason`, which silently broke whenever a message was reworded.
    urgent_cancel: bool = False
    # Pre-submission hard IOC limit price computed BEFORE wire transit.
    # On real Polymarket CLOB, limit protection is enforced natively by the
    # matching engine at this price — never via post-arrival look-ahead rejects.
    limit_price: Optional[float] = None
    # Structured execution intent so execution/runner never have to parse Chinese `reason` text.
    intent: str = ""
