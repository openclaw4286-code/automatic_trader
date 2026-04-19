"""Fixed-fractional ICT position sizing.

Flow (matches spec):
 1. ICT decides entry / SL / TP  (stage 3)
 2. size so that a stop-out costs exactly equity * RISK_PER_TRADE
 3. choose the largest leverage (<= LEVERAGE_MAX) whose liquidation sits
    strictly beyond the SL (so the stop triggers first)
 4. if the resulting margin > equity * MAX_MARGIN, cap margin at that
    value and scale position size down accordingly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict

from config import LEVERAGE_MAX, MAX_MARGIN, MIN_SL_PCT, RISK_PER_TRADE
from ict.models import Signal
from utils.logger import get_logger

log = get_logger("risk")


@dataclass
class PositionPlan:
    symbol: str
    direction: str          # "long" | "short"
    side: str               # "buy" | "sell"
    entry: float
    sl: float
    tp: float
    qty_contracts: float    # units ccxt expects for create_order amount
    qty_base: float         # equivalent base-asset amount (BTC, ETH, ...)
    notional_usdt: float
    leverage: int
    margin_usdt: float
    expected_loss_usdt: float   # realised loss if SL hits (pre-fees)
    rr: float
    capped_by_margin: bool

    def as_log(self) -> str:
        return (
            f"{self.symbol} {self.direction} "
            f"entry={self.entry:.6g} sl={self.sl:.6g} tp={self.tp:.6g} "
            f"qty={self.qty_base:.6g} notional={self.notional_usdt:.2f} "
            f"lev=x{self.leverage} margin={self.margin_usdt:.2f} "
            f"loss@sl={self.expected_loss_usdt:.2f} rr={self.rr:.2f}"
            f"{' (margin-capped)' if self.capped_by_margin else ''}"
        )


def _contract_size(market: Dict[str, Any]) -> float:
    """ccxt market['contractSize'] — how many base units per one contract."""
    cs = market.get("contractSize")
    return float(cs) if cs else 1.0


def _symbol_leverage_cap(market: Dict[str, Any]) -> int:
    """Gate.io publishes a per-symbol leverage cap. Respect it."""
    limits = (market.get("limits") or {}).get("leverage") or {}
    for key in ("max",):
        v = limits.get(key)
        if v:
            try:
                return int(float(v))
            except (TypeError, ValueError):
                pass
    info = market.get("info") or {}
    for key in ("leverage_max", "leverageMax"):
        v = info.get(key)
        if v:
            try:
                return int(float(v))
            except (TypeError, ValueError):
                pass
    return LEVERAGE_MAX


def _amount_precision(market: Dict[str, Any], qty: float) -> float:
    """Round qty down to the exchange's amount step."""
    step = None
    precision = market.get("precision", {}) or {}
    if "amount" in precision and precision["amount"] is not None:
        step = precision["amount"]
    limits = market.get("limits", {}) or {}
    min_amt = (limits.get("amount") or {}).get("min")
    if step is None or step <= 0:
        return qty
    # ccxt uses either number-of-decimals or tick step depending on exchange
    if step < 1:
        # treat as tick size
        qty = math.floor(qty / step) * step
    else:
        # treat as decimal places
        qty = math.floor(qty * 10 ** step) / 10 ** step
    if min_amt and qty < float(min_amt):
        return 0.0
    return qty


def plan_position(
    signal: Signal,
    equity_usdt: float,
    market: Dict[str, Any],
    *,
    risk_scale: float = 1.0,
    margin_scale: float = 1.0,
) -> PositionPlan | None:
    """Return a concrete, executable position plan, or None if unsizable.

    ``risk_scale`` multiplies RISK_PER_TRADE (defensive LLM regimes pass
    0.5 here so a LONG_ONLY short uses 1.25% of equity instead of 2.5%).
    ``margin_scale`` multiplies MAX_MARGIN (cap the final margin at e.g.
    5% of equity in defensive mode, down from the default 10%)."""
    if equity_usdt <= 0:
        return None

    entry = signal.entry
    sl = signal.sl
    tp = signal.tp
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return None

    sl_pct = sl_dist / entry
    if sl_pct < MIN_SL_PCT:
        log.info(
            "%s rejected: SL %.3f%% < MIN_SL_PCT %.3f%% (fees would dominate)",
            signal.symbol, sl_pct * 100, MIN_SL_PCT * 100,
        )
        return None
    risk_budget = equity_usdt * RISK_PER_TRADE * risk_scale

    # 1) notional from risk budget (linear USDT-settled: PnL = notional * dPct)
    notional = risk_budget / sl_pct

    # 2) max leverage whose liquidation is beyond SL; safety haircut of 0.9
    #    liquidation ≈ 1/L from entry on isolated linear perps.
    #    Respect both the global cap and the per-symbol exchange cap
    #    (Gate.io caps many alts below LEVERAGE_MAX).
    lev_from_sl = int(math.floor(0.9 / sl_pct)) if sl_pct > 0 else LEVERAGE_MAX
    symbol_cap = _symbol_leverage_cap(market)
    leverage = max(1, min(LEVERAGE_MAX, symbol_cap, lev_from_sl))

    margin = notional / leverage
    capped = False
    max_margin_usdt = equity_usdt * MAX_MARGIN * margin_scale

    # 3) margin cap
    if margin > max_margin_usdt:
        margin = max_margin_usdt
        notional = margin * leverage
        capped = True

    # 4) convert notional -> contracts (respect contractSize + precision)
    cs = _contract_size(market)
    qty_base = notional / entry               # base-asset amount
    qty_contracts_raw = qty_base / cs
    qty_contracts = _amount_precision(market, qty_contracts_raw)
    if qty_contracts <= 0:
        log.info("%s unsizable: qty rounds to 0 (raw=%.8g)", signal.symbol, qty_contracts_raw)
        return None

    # recompute with rounded quantity
    qty_base = qty_contracts * cs
    notional = qty_base * entry
    margin = notional / leverage
    loss_at_sl = qty_base * sl_dist

    return PositionPlan(
        symbol=signal.symbol,
        direction=signal.direction,
        side="buy" if signal.direction == "long" else "sell",
        entry=entry,
        sl=sl,
        tp=tp,
        qty_contracts=qty_contracts,
        qty_base=qty_base,
        notional_usdt=notional,
        leverage=leverage,
        margin_usdt=margin,
        expected_loss_usdt=loss_at_sl,
        rr=signal.rr,
        capped_by_margin=capped,
    )
