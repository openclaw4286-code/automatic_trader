"""Replay the ICT pipeline over historical OHLCV and score outcomes in R.

Trade lifecycle (mirrors runner/position_manager.py):
 * entry filled at signal.entry on the bar AFTER signal emission (avoids
   look-ahead — the LTF bar that produced the signal cannot also fill
   the entry)
 * SL = signal.sl, TP-final = signal.tp
 * TP1 at 1R closes TP1_PORTION; SL moves to break-even afterwards
 * TP2 at 2R closes TP2_PORTION
 * Runner trails: once price runs >= TRAIL_ACTIVATION_RR, SL hugs the
   high-water mark by TRAIL_DISTANCE_R
 * Exit recorded as the realised R (fees ignored — we score relative
   skill, not net return)

Per-config metrics:
 signal_count, win_rate, avg_R, total_R, expectancy_R, profit_factor,
 max_drawdown_R, payoff_ratio, avg_holding_bars
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

import config as cfg
from ict.models import Signal
from ict.topdown import top_down


# Minutes-per-bar for cooldown bookkeeping — must match config TFs.
_TF_MIN = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


@dataclass
class TradeResult:
    symbol: str
    direction: str
    entry_idx: int
    exit_idx: int
    realized_r: float
    hit: str        # "TP_FULL" | "SL" | "PARTIAL_THEN_SL" | "PARTIAL_THEN_TIMEOUT" | "TIMEOUT"


@dataclass
class BacktestResult:
    config_label: str
    config: Dict[str, object]
    trades: List[TradeResult] = field(default_factory=list)
    signal_count: int = 0       # may exceed len(trades) if exec rejects
    bars_evaluated: int = 0

    # ---- derived metrics --------------------------------------------------
    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.realized_r > 0)
        return wins / len(self.trades)

    @property
    def total_r(self) -> float:
        return sum(t.realized_r for t in self.trades)

    @property
    def avg_r(self) -> float:
        return self.total_r / len(self.trades) if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.realized_r for t in self.trades if t.realized_r > 0)
        losses = -sum(t.realized_r for t in self.trades if t.realized_r < 0)
        if losses <= 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    @property
    def max_drawdown_r(self) -> float:
        equity = 0.0
        peak = 0.0
        dd = 0.0
        for t in self.trades:
            equity += t.realized_r
            peak = max(peak, equity)
            dd = min(dd, equity - peak)
        return dd

    @property
    def payoff_ratio(self) -> float:
        wins = [t.realized_r for t in self.trades if t.realized_r > 0]
        losses = [-t.realized_r for t in self.trades if t.realized_r < 0]
        if not wins or not losses:
            return 0.0
        return (sum(wins) / len(wins)) / (sum(losses) / len(losses))

    @property
    def expectancy_r(self) -> float:
        # Trader's expectancy: avg R per trade. Same as avg_r here, kept as
        # an explicit alias because rankers care about it by name.
        return self.avg_r

    def as_dict(self) -> Dict[str, object]:
        return {
            "label": self.config_label,
            "config": self.config,
            "signals": self.signal_count,
            "trades": len(self.trades),
            "win_rate": round(self.win_rate, 3),
            "avg_R": round(self.avg_r, 3),
            "total_R": round(self.total_r, 2),
            "expectancy_R": round(self.expectancy_r, 3),
            "profit_factor": round(self.profit_factor, 2)
                              if self.profit_factor != float("inf") else "inf",
            "max_DD_R": round(self.max_drawdown_r, 2),
            "payoff": round(self.payoff_ratio, 2),
        }


# --------------------------------------------------------------------------
# Trade simulator
# --------------------------------------------------------------------------
def _simulate_trade(
    sig: Signal,
    ltf: pd.DataFrame,
    start_idx: int,
    max_bars: int = 500,
) -> Optional[TradeResult]:
    """Walk LTF forward from start_idx (the bar AFTER the signal) and
    score the trade in R. Returns None if there's not enough data to
    even open the trade (start_idx beyond df)."""
    if start_idx >= len(ltf):
        return None

    direction = sig.direction
    entry = sig.entry
    sl = sig.sl
    risk = abs(entry - sl)
    if risk <= 0:
        return None

    # In R-units: TP1 = 1R, TP2 = 2R, final TP = signal.tp (clamped at MAX_RR_TP).
    tp1_price = entry + risk * cfg.TP1_RR if direction == "long" else entry - risk * cfg.TP1_RR
    tp2_price = entry + risk * cfg.TP2_RR if direction == "long" else entry - risk * cfg.TP2_RR
    tp_final = sig.tp

    portion_remaining = 1.0
    realized_r = 0.0
    tp1_hit = False
    tp2_hit = False
    trail_high = entry      # for long; for short use trail_low
    trail_low = entry
    sl_active = sl
    tp1_portion = cfg.TP1_PORTION
    tp2_portion = cfg.TP2_PORTION

    end = min(len(ltf), start_idx + max_bars)
    for i in range(start_idx, end):
        bar = ltf.iloc[i]
        high = float(bar["high"])
        low = float(bar["low"])

        if direction == "long":
            # 1) SL first (pessimistic — assume worst-case crossing order)
            if low <= sl_active:
                # remaining portion exits at SL
                exit_r = (sl_active - entry) / risk
                realized_r += portion_remaining * exit_r
                hit = "PARTIAL_THEN_SL" if (tp1_hit or tp2_hit) else "SL"
                return TradeResult(sig.symbol, direction, start_idx, i, realized_r, hit)
            # 2) TP1
            if not tp1_hit and high >= tp1_price:
                realized_r += tp1_portion * cfg.TP1_RR
                portion_remaining -= tp1_portion
                tp1_hit = True
                if cfg.BREAKEVEN_AFTER_TP1:
                    sl_active = max(sl_active, entry)
            # 3) TP2
            if tp1_hit and not tp2_hit and high >= tp2_price:
                realized_r += tp2_portion * cfg.TP2_RR
                portion_remaining -= tp2_portion
                tp2_hit = True
            # 4) Final TP for the runner
            if portion_remaining > 0 and high >= tp_final:
                tp_r = (tp_final - entry) / risk
                realized_r += portion_remaining * tp_r
                return TradeResult(sig.symbol, direction, start_idx, i, realized_r, "TP_FULL")
            # 5) Trail — only after activation R
            if cfg.TRAIL_ENABLED and high > trail_high:
                trail_high = high
                run_r = (trail_high - entry) / risk
                if run_r >= cfg.TRAIL_ACTIVATION_RR:
                    new_sl = trail_high - cfg.TRAIL_DISTANCE_R * risk
                    sl_active = max(sl_active, new_sl)
        else:   # short
            if high >= sl_active:
                exit_r = (entry - sl_active) / risk
                realized_r += portion_remaining * exit_r
                hit = "PARTIAL_THEN_SL" if (tp1_hit or tp2_hit) else "SL"
                return TradeResult(sig.symbol, direction, start_idx, i, realized_r, hit)
            if not tp1_hit and low <= tp1_price:
                realized_r += tp1_portion * cfg.TP1_RR
                portion_remaining -= tp1_portion
                tp1_hit = True
                if cfg.BREAKEVEN_AFTER_TP1:
                    sl_active = min(sl_active, entry)
            if tp1_hit and not tp2_hit and low <= tp2_price:
                realized_r += tp2_portion * cfg.TP2_RR
                portion_remaining -= tp2_portion
                tp2_hit = True
            if portion_remaining > 0 and low <= tp_final:
                tp_r = (entry - tp_final) / risk
                realized_r += portion_remaining * tp_r
                return TradeResult(sig.symbol, direction, start_idx, i, realized_r, "TP_FULL")
            if cfg.TRAIL_ENABLED and low < trail_low:
                trail_low = low
                run_r = (entry - trail_low) / risk
                if run_r >= cfg.TRAIL_ACTIVATION_RR:
                    new_sl = trail_low + cfg.TRAIL_DISTANCE_R * risk
                    sl_active = min(sl_active, new_sl)

    # ran out of bars → mark to last close
    last = float(ltf.iloc[end - 1]["close"])
    if direction == "long":
        mark_r = (last - entry) / risk
    else:
        mark_r = (entry - last) / risk
    realized_r += portion_remaining * mark_r
    hit = "PARTIAL_THEN_TIMEOUT" if (tp1_hit or tp2_hit) else "TIMEOUT"
    return TradeResult(sig.symbol, direction, start_idx, end - 1, realized_r, hit)


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------
def _slice_to_ltf_time(ltf_ts, htf_df: pd.DataFrame) -> pd.DataFrame:
    """Return the HTF/MTF subset whose bars are CLOSED at `ltf_ts` —
    i.e. ts <= ltf_ts. Avoids look-ahead from a still-forming higher
    bar leaking future info into the analysis."""
    return htf_df[htf_df["ts"] <= ltf_ts]


def replay_symbol(
    symbol: str,
    htf: pd.DataFrame,
    mtf: pd.DataFrame,
    ltf: pd.DataFrame,
    *,
    eval_every: int = 1,
    htf_warmup: int = 200,
    mtf_warmup: int = 200,
    ltf_warmup: int = 100,
) -> Tuple[List[Signal], List[TradeResult]]:
    """Walk LTF forward, evaluate top_down() at each step (or every Nth
    step), and simulate a trade for every emitted signal. Per-symbol
    cooldown matches the live bot's SAME_DIRECTION_COOLDOWN_MIN."""
    signals: List[Signal] = []
    trades: List[TradeResult] = []
    ltf_min = _TF_MIN[cfg.LTF_TIMEFRAME]
    cooldown_bars = max(1, int(cfg.SAME_DIRECTION_COOLDOWN_MIN / ltf_min))
    last_signal_idx_by_dir: Dict[str, int] = {}
    open_until_idx = -1

    start = max(htf_warmup * 4, ltf_warmup)   # rough alignment
    for i in range(start, len(ltf), eval_every):
        if i <= open_until_idx:
            continue
        ltf_ts = ltf["ts"].iloc[i]
        htf_slice = _slice_to_ltf_time(ltf_ts, htf)
        mtf_slice = _slice_to_ltf_time(ltf_ts, mtf)
        ltf_slice = ltf.iloc[: i + 1]
        if len(htf_slice) < htf_warmup or len(mtf_slice) < mtf_warmup or len(ltf_slice) < ltf_warmup:
            continue
        try:
            sig = top_down(symbol, htf_slice, mtf_slice, ltf_slice)
        except Exception:
            continue
        if sig is None:
            continue
        # cooldown
        last_idx = last_signal_idx_by_dir.get(sig.direction, -10**9)
        if i - last_idx < cooldown_bars:
            continue
        # Sanity: SL on correct side. Bad signals get dropped.
        if sig.direction == "long" and sig.sl >= sig.entry:
            continue
        if sig.direction == "short" and sig.sl <= sig.entry:
            continue
        last_signal_idx_by_dir[sig.direction] = i
        signals.append(sig)
        trade = _simulate_trade(sig, ltf, start_idx=i + 1)
        if trade is not None:
            trades.append(trade)
            open_until_idx = trade.exit_idx   # serial trades per symbol
    return signals, trades


# --------------------------------------------------------------------------
# Config application — mirrors autotune._apply but takes an explicit dict
# --------------------------------------------------------------------------
def apply_config_overrides(overrides: Dict[str, object]) -> Dict[str, object]:
    """Mutate the `config` module so ICT modules pick up the new values.
    Returns the previous values so the caller can restore them."""
    prev: Dict[str, object] = {}
    for k, v in overrides.items():
        prev[k] = getattr(cfg, k, None)
        setattr(cfg, k, v)
    return prev


def restore_config(prev: Dict[str, object]) -> None:
    for k, v in prev.items():
        setattr(cfg, k, v)


def run_backtest(
    label: str,
    overrides: Dict[str, object],
    cache: Dict[str, Dict[str, pd.DataFrame]],
    *,
    eval_every: int = 5,
) -> BacktestResult:
    """Full backtest for one configuration across all cached symbols."""
    prev = apply_config_overrides(overrides)
    try:
        result = BacktestResult(config_label=label, config=dict(overrides))
        for symbol, by_tf in cache.items():
            try:
                htf = by_tf[cfg.HTF_TIMEFRAME]
                mtf = by_tf[cfg.MTF_TIMEFRAME]
                ltf = by_tf[cfg.LTF_TIMEFRAME]
            except KeyError:
                continue
            if min(len(htf), len(mtf), len(ltf)) < 200:
                continue
            sigs, trades = replay_symbol(symbol, htf, mtf, ltf, eval_every=eval_every)
            result.signal_count += len(sigs)
            result.trades.extend(trades)
            result.bars_evaluated += len(ltf)
        return result
    finally:
        restore_config(prev)
