"""Autotune ladder tests (signal rate → ladder step)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch


def _isolated_paths(tmp: Path, monkeypatch):
    """Point autotune's state + signal-log files at a temp directory."""
    from ict import autotune
    monkeypatch.setattr(autotune, "STATE_FILE", tmp / "state.json")
    monkeypatch.setattr(autotune, "SIGNAL_LOG", tmp / "signals.jsonl")


def test_default_levels_match_project_defaults(tmp_path, monkeypatch):
    from ict import autotune
    import config

    _isolated_paths(tmp_path, monkeypatch)
    autotune.apply_state()
    # middle-index levels must match the constants config.py ships with
    assert config.MIN_RR == autotune.LADDERS["MIN_RR"][autotune._DEFAULT_LEVEL["MIN_RR"]]
    assert config.VOL_MULT_TRIGGER == autotune.LADDERS["VOL_MULT_TRIGGER"][autotune._DEFAULT_LEVEL["VOL_MULT_TRIGGER"]]


def test_low_signal_count_loosens_ladders(tmp_path, monkeypatch):
    from ict import autotune

    _isolated_paths(tmp_path, monkeypatch)
    autotune.apply_state()             # seed state at defaults
    before = autotune._load_state().copy()

    # signal log exists but empty → count_24h = 0, prior tune was >1h ago
    autotune.SIGNAL_LOG.touch()
    state = autotune._load_state()
    state["last_tuned_at"] = time.time() - 7200
    autotune._save_state(state)

    autotune.tune_once()
    after = autotune._load_state()
    for name in autotune.LADDERS:
        assert after[name] == max(0, before[name] - 1), name


def test_high_signal_count_tightens_ladders(tmp_path, monkeypatch):
    from ict import autotune

    _isolated_paths(tmp_path, monkeypatch)
    autotune.apply_state()
    before = autotune._load_state().copy()

    # 20 signals in last hour, older tune
    now = time.time()
    with autotune.SIGNAL_LOG.open("w") as f:
        for _ in range(20):
            f.write(json.dumps({"ts": now - 60, "symbol": "X", "direction": "long"}) + "\n")
    state = autotune._load_state()
    state["last_tuned_at"] = time.time() - 7200
    autotune._save_state(state)

    autotune.tune_once()
    after = autotune._load_state()
    for name, ladder in autotune.LADDERS.items():
        assert after[name] == min(len(ladder) - 1, before[name] + 1), name


def test_in_target_band_no_change(tmp_path, monkeypatch):
    from ict import autotune

    _isolated_paths(tmp_path, monkeypatch)
    autotune.apply_state()
    before = autotune._load_state().copy()

    # 5 signals in last 24h → in target band [3, 10]
    now = time.time()
    with autotune.SIGNAL_LOG.open("w") as f:
        for _ in range(5):
            f.write(json.dumps({"ts": now - 3600, "symbol": "X", "direction": "long"}) + "\n")
    state = autotune._load_state()
    state["last_tuned_at"] = time.time() - 7200
    autotune._save_state(state)

    autotune.tune_once()
    after = autotune._load_state()
    for name in autotune.LADDERS:
        assert after[name] == before[name], name


def test_ladder_clamps_at_endpoints(tmp_path, monkeypatch):
    from ict import autotune

    _isolated_paths(tmp_path, monkeypatch)
    # set all ladders to index 0 (loosest) then try to loosen further
    state = {name: 0 for name in autotune.LADDERS}
    state["last_tuned_at"] = time.time() - 7200
    autotune._save_state(state)

    autotune.tune_once()      # count is 0 → loosen
    after = autotune._load_state()
    for name in autotune.LADDERS:
        assert after[name] == 0, name


if __name__ == "__main__":
    import tempfile

    class _MP:
        def __init__(self):
            self._reset = []
        def setattr(self, obj, name, val):
            self._reset.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)
        def undo(self):
            for o, n, v in self._reset:
                setattr(o, n, v)

    for fn in (
        test_default_levels_match_project_defaults,
        test_low_signal_count_loosens_ladders,
        test_high_signal_count_tightens_ladders,
        test_in_target_band_no_change,
        test_ladder_clamps_at_endpoints,
    ):
        with tempfile.TemporaryDirectory() as td:
            mp = _MP()
            fn(Path(td), mp)
            mp.undo()
    print("all autotune tests passed")
