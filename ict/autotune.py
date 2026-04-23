"""Adaptive ICT-filter autotuner.

Design:
 * we record one line in logs/signals.jsonl every time the scanner
   emits a signal (success or LLM-blocked, irrelevant — what matters
   is whether the ICT engine thinks a setup exists),
 * every hour a controller counts signals in the last 24 h and moves
   each tunable parameter one step along its ladder:
     count < TARGET_LOW_PER_DAY  → loosen (more signals)
     count > TARGET_HIGH_PER_DAY → tighten (fewer signals)
 * the chosen ladder indices persist in logs/autotune.json so a
   restart picks up the current level,
 * the active values are applied by mutating the ``config`` module
   object; the ict modules read those names dynamically so the next
   scan cycle sees the new settings without a process restart.

Target signal count: ~5 per day (user-chosen); accept 3-10/day.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import config
from utils.logger import get_logger

log = get_logger("autotune")


# ---------------------------------------------------------------------------
# Parameter ladders. Index 0 is the LOOSEST end (most signals) and the
# last index is the TIGHTEST end (fewest signals). The middle roughly
# matches the project defaults so level=middle is a no-op on first run.
# ---------------------------------------------------------------------------
LADDERS: Dict[str, List[Any]] = {
    # reward-to-risk cut-off
    "MIN_RR":                  [1.0, 1.2, 1.3, 1.5, 1.8, 2.0, 2.5],
    # LTF trigger candle volume >= mult * SMA20
    "VOL_MULT_TRIGGER":        [0.8, 1.0, 1.1, 1.3, 1.5, 1.7, 2.0],
    # liquidity-sweep candle volume >= mult * SMA20
    "VOL_MULT_SWEEP":          [0.9, 1.1, 1.3, 1.5, 1.7, 2.0, 2.3],
    # rejecting-wick fraction of sweep candle
    "SWEEP_WICK_RATIO":        [0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
    # lookback window for MTF BOS/CHOCH recency check (larger = looser)
    "MTF_EVENT_LOOKBACK_BARS": [80, 60, 45, 30, 20, 12],
    # HTF EMA-slope momentum filter on/off (False = looser)
    "HTF_MOMENTUM_FILTER":     [False, True],
    # minimum FVG size as ATR multiple
    "FVG_MIN_ATR_MULT":        [0.02, 0.03, 0.05, 0.08, 0.12, 0.18],
}

# Default starting index (≈ middle = project defaults)
_DEFAULT_LEVEL: Dict[str, int] = {
    "MIN_RR":                  3,   # 1.5
    "VOL_MULT_TRIGGER":        3,   # 1.3
    "VOL_MULT_SWEEP":          3,   # 1.5
    "SWEEP_WICK_RATIO":        3,   # 0.6
    "MTF_EVENT_LOOKBACK_BARS": 3,   # 30
    "HTF_MOMENTUM_FILTER":     1,   # True
    "FVG_MIN_ATR_MULT":        2,   # 0.05
}

# Signal-rate targets (per calendar day across the universe).
TARGET_LOW_PER_DAY = 5
TARGET_HIGH_PER_DAY = 10
TUNE_MIN_INTERVAL_SEC = 55 * 60   # do not tune more than once per ~hour

STATE_FILE = Path(config.LOG_DIR) / "autotune.json"
SIGNAL_LOG = Path(config.LOG_DIR) / "signals.jsonl"


# ---------------------------------------------------------------------- state
def _load_state() -> Dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("autotune state corrupt (%s) — reinit", exc)
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


# ----------------------------------------------------------------- signal log
def record_signal(symbol: str, direction: str) -> None:
    SIGNAL_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SIGNAL_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "symbol": symbol, "direction": direction}) + "\n")


def signals_in_last(seconds: float) -> int:
    if not SIGNAL_LOG.exists():
        return 0
    cutoff = time.time() - seconds
    n = 0
    try:
        with SIGNAL_LOG.open(encoding="utf-8") as f:
            for line in f:
                try:
                    if json.loads(line).get("ts", 0) >= cutoff:
                        n += 1
                except Exception:
                    continue
    except Exception:
        return 0
    return n


# --------------------------------------------------------------------- apply
def _apply(state: Dict[str, Any]) -> None:
    values = {name: LADDERS[name][state[name]] for name in LADDERS}
    for name, val in values.items():
        setattr(config, name, val)
    log.info("autotune applied: %s", values)


def apply_state() -> Dict[str, Any]:
    """Load persisted ladder indices and apply them to `config` without
    attempting any movement. Idempotent — safe to call on every startup
    (crucially, restarting the bot no longer inadvertently tightens).
    Also persists so fresh installs get a state file seeded with defaults.
    """
    state = _load_state()
    for name, idx in _DEFAULT_LEVEL.items():
        state.setdefault(name, idx)
    _apply(state)
    _save_state(state)
    return state


def tune_once() -> Dict[str, Any]:
    """Rate-limited step move based on last 24 h signal count.

    Moves every ladder by ±1 when the count falls outside [TARGET_LOW,
    TARGET_HIGH]. No-op while we are still within TUNE_MIN_INTERVAL_SEC
    of the last successful move — this is what prevents process
    restarts from compounding tune steps. Always re-applies the active
    state so the ict modules pick it up without needing a restart."""
    state = _load_state()
    for name, idx in _DEFAULT_LEVEL.items():
        state.setdefault(name, idx)

    now = time.time()
    last = state.get("last_tuned_at", 0.0)
    cooldown = (now - last) < TUNE_MIN_INTERVAL_SEC

    count_24h = signals_in_last(86400) if SIGNAL_LOG.exists() else None
    state["last_count_24h"] = count_24h if count_24h is not None else -1

    if count_24h is None:
        log.info("autotune: no signal history yet — applying defaults without adjustment")
    elif cooldown:
        remain = TUNE_MIN_INTERVAL_SEC - (now - last)
        log.debug("autotune: in cooldown (%.1f min left)", remain / 60)
    else:
        direction = None
        if count_24h < TARGET_LOW_PER_DAY:
            direction = -1
        elif count_24h > TARGET_HIGH_PER_DAY:
            direction = +1

        if direction is not None:
            for name, ladder in LADDERS.items():
                new_idx = max(0, min(len(ladder) - 1, state[name] + direction))
                state[name] = new_idx
            state["last_tuned_at"] = now
            log.info(
                "autotune %s (count_24h=%d, target=%d..%d)",
                "loosen" if direction < 0 else "tighten",
                count_24h, TARGET_LOW_PER_DAY, TARGET_HIGH_PER_DAY,
            )
        else:
            log.debug("autotune: count_24h=%d in target band — no change", count_24h)

    _apply(state)
    _save_state(state)
    return state


def current_levels() -> Dict[str, Any]:
    """Return the active parameter values (for logs / inspection)."""
    state = _load_state()
    for name, idx in _DEFAULT_LEVEL.items():
        state.setdefault(name, idx)
    return {name: LADDERS[name][state[name]] for name in LADDERS}


def reset_to_defaults() -> Dict[str, Any]:
    """Wipe the ladder state back to project defaults. Useful after a
    misbehaving run has tightened everything to the maximum. Resets the
    cooldown so the next hourly tick is free to move again."""
    state = dict(_DEFAULT_LEVEL)
    state["last_tuned_at"] = 0.0
    state["last_count_24h"] = -1
    _save_state(state)
    _apply(state)
    log.info("autotune reset to defaults: %s", current_levels())
    return state
