"""Config grids and ranking for the backtest sweep.

The "small" grid is what you actually want to run first — ~30
configurations, each takes 30-60s on cached data, full sweep ~15
minutes. The "large" grid blows it out to a couple hundred for
overnight runs.
"""
from __future__ import annotations

import itertools
from typing import Dict, List

from backtest.engine import BacktestResult


# Centred on the project defaults. Each axis spans loose → tight so the
# Pareto report can show what trading-off looks like.
SMALL_GRID: Dict[str, List[object]] = {
    # zone-mode: True = require zone containment (old strict),
    # False = current loosened mode (zones optional). Captured via
    # HTF_SWEEP_REQUIRED only — zone optionality is now built-in.
    "HTF_SWEEP_REQUIRED":      [False, True],
    "HTF_MOMENTUM_FILTER":     [False, True],
    "MIN_RR":                  [1.2, 1.5, 1.8],
    "VOL_MULT_TRIGGER":        [1.0, 1.3, 1.6],
    "MTF_EVENT_LOOKBACK_BARS": [30, 60],
    "FVG_MIN_ATR_MULT":        [0.03, 0.08],
}


LARGE_GRID: Dict[str, List[object]] = {
    "HTF_SWEEP_REQUIRED":      [False, True],
    "HTF_MOMENTUM_FILTER":     [False, True],
    "MIN_RR":                  [1.0, 1.2, 1.5, 1.8, 2.0],
    "VOL_MULT_TRIGGER":        [0.8, 1.0, 1.3, 1.6, 1.9],
    "VOL_MULT_SWEEP":          [1.0, 1.5, 2.0],
    "MTF_EVENT_LOOKBACK_BARS": [20, 30, 60, 100],
    "FVG_MIN_ATR_MULT":        [0.02, 0.05, 0.10],
    "SWEEP_WICK_RATIO":        [0.4, 0.6, 0.8],
}


def expand_grid(grid: Dict[str, List[object]]) -> List[Dict[str, object]]:
    keys = list(grid.keys())
    combos = []
    for vals in itertools.product(*[grid[k] for k in keys]):
        combos.append(dict(zip(keys, vals)))
    return combos


def label_for(combo: Dict[str, object]) -> str:
    return " ".join(f"{k}={v}" for k, v in combo.items())


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------
def _is_dominated(a: BacktestResult, b: BacktestResult) -> bool:
    """b dominates a if b is >= a on (signal_count, total_R) and > on
    at least one axis. Pareto-front membership = NOT dominated by anyone.

    We rank on raw counts — not normalised — because the user explicitly
    wants 'many orders AND good performance'. Both axes matter equally.
    """
    return (
        b.signal_count >= a.signal_count
        and b.total_r >= a.total_r
        and (b.signal_count > a.signal_count or b.total_r > a.total_r)
    )


def pareto_front(results: List[BacktestResult]) -> List[BacktestResult]:
    front = []
    for r in results:
        dominated = any(_is_dominated(r, other) for other in results if other is not r)
        if not dominated:
            front.append(r)
    return sorted(front, key=lambda x: (-x.signal_count, -x.total_r))


def rank(
    results: List[BacktestResult],
    *,
    min_trades: int = 5,
    score: str = "score",
) -> List[BacktestResult]:
    """Sort results by composite score; filter out under-traded configs.

    Default score = expectancy_R × sqrt(trades) — rewards a config that
    fires often AND has a positive edge. The sqrt damps pure
    high-frequency / zero-edge spam from leapfrogging a true
    high-expectancy / lower-frequency setup.
    """
    eligible = [r for r in results if len(r.trades) >= min_trades]

    def _score(r: BacktestResult) -> float:
        if score == "expectancy":
            return r.expectancy_r
        if score == "total":
            return r.total_r
        if score == "signals":
            return float(r.signal_count)
        # composite default
        return r.expectancy_r * (len(r.trades) ** 0.5)

    return sorted(eligible, key=_score, reverse=True)


def format_table(results: List[BacktestResult], top_n: int = 25) -> str:
    """Compact ASCII table — one config per line, sorted by composite score."""
    if not results:
        return "(no results)"
    rows = [r.as_dict() for r in results[:top_n]]
    headers = ["#", "signals", "trades", "win", "avgR", "totR", "PF", "DD", "label"]
    out = ["{:>3}  {:>7}  {:>6}  {:>5}  {:>6}  {:>6}  {:>5}  {:>6}  {}".format(*headers)]
    for i, row in enumerate(rows, 1):
        out.append(
            "{:>3}  {:>7}  {:>6}  {:>5}  {:>6}  {:>6}  {:>5}  {:>6}  {}".format(
                i,
                row["signals"],
                row["trades"],
                f"{row['win_rate']*100:.0f}%",
                f"{row['avg_R']:+.2f}",
                f"{row['total_R']:+.1f}",
                str(row["profit_factor"])[:5],
                f"{row['max_DD_R']:+.1f}",
                row["label"][:90],
            )
        )
    return "\n".join(out)
