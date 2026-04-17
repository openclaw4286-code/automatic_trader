"""Typed ICT primitives shared between detectors and orchestrator."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Literal, Optional


class Bias(str, Enum):
    BULL = "bullish"
    BEAR = "bearish"
    NONE = "none"


Direction = Literal["long", "short"]
SwingKind = Literal["HH", "HL", "LH", "LL"]


@dataclass
class Swing:
    idx: int            # bar index
    price: float
    kind: Optional[SwingKind]  # None until labeled
    is_high: bool


@dataclass
class StructureEvent:
    kind: Literal["BOS", "CHOCH", "BSS"]
    direction: Direction
    idx: int
    broken_price: float   # swing price that was taken out


@dataclass
class Zone:
    """FVG or OB — an unmitigated price region with directional bias."""
    kind: Literal["FVG", "OB"]
    direction: Direction      # long = bullish zone (expect price to bounce up from it)
    top: float
    bottom: float
    origin_idx: int           # bar index where zone was created
    mitigated: bool = False

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    @property
    def mid(self) -> float:
        return 0.5 * (self.top + self.bottom)


@dataclass
class Sweep:
    """Liquidity sweep event — wick past prior equal extremes then reclaim."""
    direction: Direction       # long = bearish sweep (lows swept, expect reversal up)
    idx: int
    swept_level: float


@dataclass
class TFAnalysis:
    """Per-timeframe analysis snapshot used by the top-down pipeline."""
    tf: str
    bias: Bias
    swings: List[Swing] = field(default_factory=list)
    events: List[StructureEvent] = field(default_factory=list)
    zones: List[Zone] = field(default_factory=list)
    sweep: Optional[Sweep] = None
    last_close: float = 0.0


@dataclass
class Signal:
    symbol: str
    direction: Direction
    entry: float
    sl: float
    tp: float
    reason: str
    htf: TFAnalysis
    mtf: TFAnalysis
    ltf: TFAnalysis

    @property
    def rr(self) -> float:
        risk = abs(self.entry - self.sl)
        reward = abs(self.tp - self.entry)
        return reward / risk if risk > 0 else 0.0
