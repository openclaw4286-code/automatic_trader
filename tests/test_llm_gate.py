"""LLM gate tests: prompt shape, verdict parsing, overwrite semantics.

The Claude CLI subprocess is replaced with a deterministic fake so the
tests run offline and without touching a real model.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from llm.claude_cli import parse_verdict
from llm.gate import LLMGate
from llm.prompt import build_prompt
from news.aggregator import NewsAggregator
from news.base import NewsItem


# -------------------------- prompt composition --------------------------
def _item(title: str, *, kind="headline", impact="low", minutes_ago=1, tickers=None) -> NewsItem:
    return NewsItem(
        source="t",
        kind=kind,
        title=title,
        url="https://x",
        published_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        tickers=tickers or [],
        impact=impact,
    )


def test_prompt_has_required_sections_and_tokens():
    prompt = build_prompt(
        [
            _item("BTC makes new high", tickers=["BTC"]),
            _item("US CPI release", kind="econ", impact="high"),
            _item("ECB speech", kind="econ", impact="medium"),
        ]
    )
    assert "HIGH-IMPACT ECONOMIC EVENTS" in prompt
    assert "HEADLINES" in prompt
    # 4-state verdict prompt
    for tok in ("PASS", "LONG_ONLY", "SHORT_ONLY", "WAIT"):
        assert tok in prompt
    assert "BTC makes new high" in prompt
    assert "CPI" in prompt


def test_prompt_truncates_on_overflow():
    fat = [_item("x" * 200) for _ in range(500)]
    prompt = build_prompt(fat)
    assert len(prompt) <= 12_050
    assert "[truncated]" in prompt or len(prompt) < 12_000


# -------------------------- verdict parsing -----------------------------
def test_parse_pass():
    assert parse_verdict("PASS\nNo major events")[0] == "PASS"


def test_parse_wait():
    assert parse_verdict("WAIT\nCPI in 3 min")[0] == "WAIT"


def test_parse_ambiguous_defaults_to_wait():
    assert parse_verdict("Hmm maybe?\n...")[0] == "WAIT"


def test_parse_empty_defaults_to_wait():
    assert parse_verdict("")[0] == "WAIT"


def test_parse_long_only_and_short_only():
    assert parse_verdict("LONG_ONLY\nrisk-on")[0] == "LONG_ONLY"
    assert parse_verdict("SHORT_ONLY\nrisk-off")[0] == "SHORT_ONLY"
    assert parse_verdict("LONG ONLY\nnoted")[0] == "LONG_ONLY"    # tolerant


def test_gate_direction_and_scale_for_all_verdicts():
    import asyncio, tempfile, os, time
    from llm.gate import Verdict

    async def _run(status, want_long, want_short, want_scale_long, want_scale_short):
        with tempfile.TemporaryDirectory() as td:
            gate, _ = _make_gate(f"{status}\nreason", td)
            await gate.evaluate_once()
            assert gate.allow_direction("long") == want_long
            assert gate.allow_direction("short") == want_short
            assert abs(gate.size_scale("long") - want_scale_long) < 1e-9
            assert abs(gate.size_scale("short") - want_scale_short) < 1e-9

    asyncio.run(_run("PASS",        True,  True,  1.0, 1.0))
    asyncio.run(_run("LONG_ONLY",   True,  False, 0.5, 0.0))
    asyncio.run(_run("SHORT_ONLY",  False, True,  0.0, 0.5))
    asyncio.run(_run("WAIT",        False, False, 0.0, 0.0))


def test_gate_position_cap_halves_on_directional():
    import asyncio, tempfile

    async def _run(status, base, expected):
        with tempfile.TemporaryDirectory() as td:
            gate, _ = _make_gate(f"{status}\nreason", td)
            await gate.evaluate_once()
            assert gate.position_cap(base) == expected

    asyncio.run(_run("PASS",       10, 10))
    asyncio.run(_run("LONG_ONLY",  10, 5))
    asyncio.run(_run("SHORT_ONLY", 10, 5))
    asyncio.run(_run("WAIT",       10, 0))


# --------------------------- overwrite gate -----------------------------
class _FakeCLI:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    async def ask(self, prompt: str) -> str:
        self.calls += 1
        return self.reply


class _FakeAgg:
    async def fetch(self, lookback_min: int | None = None):
        return [_item("peaceful markets")]


def _make_gate(reply: str, tmpdir: str) -> tuple[LLMGate, _FakeCLI]:
    fake = _FakeCLI(reply)
    gate = LLMGate(
        aggregator=_FakeAgg(),        # type: ignore[arg-type]
        cli=fake,                     # type: ignore[arg-type]
        prompt_file=os.path.join(tmpdir, "prompt.txt"),
        response_file=os.path.join(tmpdir, "response.txt"),
        verdict_file=os.path.join(tmpdir, "verdict.json"),
        interval_sec=600,
    )
    return gate, fake


def test_gate_pass_allows_trades_and_caches_until_expiry():
    async def _run():
        with tempfile.TemporaryDirectory() as td:
            gate, _ = _make_gate("PASS\nclean", td)
            assert not gate.allow_trades()
            v = await gate.evaluate_once()
            assert v.status == "PASS"
            assert gate.allow_trades()
            # manually expire and verify the gate closes
            gate._verdict.expires_at = 0        # type: ignore[union-attr]
            assert not gate.allow_trades()

    asyncio.run(_run())


def test_gate_wait_blocks_trades():
    async def _run():
        with tempfile.TemporaryDirectory() as td:
            gate, _ = _make_gate("WAIT\nCPI incoming", td)
            await gate.evaluate_once()
            assert not gate.allow_trades()

    asyncio.run(_run())


def test_files_are_overwritten_never_appended():
    async def _run():
        with tempfile.TemporaryDirectory() as td:
            gate, fake = _make_gate("PASS\nok", td)
            await gate.evaluate_once()
            # change LLM reply and re-evaluate
            fake.reply = "WAIT\nsudden CPI"
            await gate.evaluate_once()

            # exactly one prompt + one response + one verdict file in the dir
            files = sorted(os.listdir(td))
            assert files == ["prompt.txt", "response.txt", "verdict.json"]

            # response refreshed to the latest reply
            assert "WAIT" in open(gate._response_file).read()   # type: ignore[attr-defined]

            # verdict JSON reflects the latest parsed decision
            data = json.loads(open(gate._verdict_file).read())  # type: ignore[attr-defined]
            assert data["status"] == "WAIT"
            assert data["allow"] is False
            assert "sudden CPI" in data["reason"]
            assert data["news_count"] == 1
            assert "decided_at_iso" in data and "expires_at_iso" in data

    asyncio.run(_run())


def test_verdict_file_pass_allows_trades():
    async def _run():
        with tempfile.TemporaryDirectory() as td:
            gate, _ = _make_gate("PASS\nclean", td)
            await gate.evaluate_once()
            data = json.loads(open(gate._verdict_file).read())  # type: ignore[attr-defined]
            assert data["status"] == "PASS"
            assert data["allow"] is True

    asyncio.run(_run())


def test_cli_failure_defaults_to_wait():
    class _Boom:
        async def ask(self, prompt: str) -> str:
            raise RuntimeError("claude down")

    async def _run():
        with tempfile.TemporaryDirectory() as td:
            gate = LLMGate(
                aggregator=_FakeAgg(),   # type: ignore[arg-type]
                cli=_Boom(),             # type: ignore[arg-type]
                prompt_file=os.path.join(td, "p.txt"),
                response_file=os.path.join(td, "r.txt"),
                verdict_file=os.path.join(td, "v.json"),
                interval_sec=600,
            )
            v = await gate.evaluate_once()
            assert v.status == "WAIT"
            assert not gate.allow_trades()
            data = json.loads(open(os.path.join(td, "v.json")).read())
            assert data["status"] == "WAIT"
            assert "LLM failure" in data["reason"]

    asyncio.run(_run())


if __name__ == "__main__":
    test_prompt_has_required_sections_and_tokens()
    test_prompt_truncates_on_overflow()
    test_parse_pass()
    test_parse_wait()
    test_parse_ambiguous_defaults_to_wait()
    test_parse_empty_defaults_to_wait()
    test_parse_long_only_and_short_only()
    test_gate_direction_and_scale_for_all_verdicts()
    test_gate_position_cap_halves_on_directional()
    test_gate_pass_allows_trades_and_caches_until_expiry()
    test_gate_wait_blocks_trades()
    test_files_are_overwritten_never_appended()
    test_verdict_file_pass_allows_trades()
    test_cli_failure_defaults_to_wait()
    print("all llm_gate tests passed")
