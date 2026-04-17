"""10-minute LLM PASS/WAIT gate.

Cycle:
 1. Aggregate news (in-memory — no per-cycle log files).
 2. Build a prompt and OVERWRITE a single rolling file
    (`LLM_PROMPT_FILE`) — no history of past prompts is kept.
 3. Ask Claude CLI, OVERWRITE `LLM_RESPONSE_FILE` with the raw reply.
 4. Parse PASS / WAIT + reason, OVERWRITE `LLM_VERDICT_FILE` with the
    structured verdict (JSON).
 5. Cache verdict + expiry; `allow_trades()` returns True while the
    cached verdict is PASS and not expired, else False.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import List, Optional

from config import (
    LLM_GATE_INTERVAL_SEC,
    LLM_PROMPT_FILE,
    LLM_RESPONSE_FILE,
    LLM_VERDICT_FILE,
)
from llm.claude_cli import ClaudeCLI, parse_verdict
from llm.prompt import build_prompt
from news.aggregator import NewsAggregator
from news.base import NewsItem
from utils.logger import get_logger

log = get_logger("llm_gate")


@dataclass
class Verdict:
    status: str                 # "PASS" | "WAIT"
    reason: str
    decided_at: float           # epoch seconds
    expires_at: float

    def fresh(self) -> bool:
        return time.time() < self.expires_at

    def allow(self) -> bool:
        return self.status == "PASS" and self.fresh()


class LLMGate:
    def __init__(
        self,
        aggregator: Optional[NewsAggregator] = None,
        cli: Optional[ClaudeCLI] = None,
        prompt_file: str = LLM_PROMPT_FILE,
        response_file: str = LLM_RESPONSE_FILE,
        verdict_file: str = LLM_VERDICT_FILE,
        interval_sec: int = LLM_GATE_INTERVAL_SEC,
    ) -> None:
        self._news = aggregator or NewsAggregator()
        self._cli = cli or ClaudeCLI()
        self._prompt_file = prompt_file
        self._response_file = response_file
        self._verdict_file = verdict_file
        self._interval = interval_sec
        self._verdict: Optional[Verdict] = None
        self._lock = asyncio.Lock()

    @property
    def verdict(self) -> Optional[Verdict]:
        return self._verdict

    def allow_trades(self) -> bool:
        return bool(self._verdict and self._verdict.allow())

    async def evaluate_once(self) -> Verdict:
        async with self._lock:
            items = await self._news.fetch()
            prompt = build_prompt(items)
            self._write_overwrite(self._prompt_file, prompt)

            try:
                reply = await self._cli.ask(prompt)
            except Exception as exc:
                log.warning("LLM call failed, defaulting to WAIT: %s", exc)
                reply = f"WAIT\nLLM failure: {exc}"

            self._write_overwrite(self._response_file, reply)
            status, reason = parse_verdict(reply)
            now = time.time()
            v = Verdict(
                status=status,
                reason=reason,
                decided_at=now,
                expires_at=now + self._interval,
            )
            self._verdict = v
            self._write_verdict(v, news_count=len(items))
            log.info(
                "verdict=%s (news=%d, reason=%s)",
                status,
                len(items),
                reason[:120],
            )
            return v

    async def run_forever(self) -> None:
        while True:
            try:
                await self.evaluate_once()
            except Exception as exc:
                log.exception("gate loop error: %s", exc)
            await asyncio.sleep(self._interval)

    # ---------- helpers ----------
    @staticmethod
    def _write_overwrite(path: str, body: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        header = f"# written: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        with open(path, "w", encoding="utf-8") as f:   # "w" truncates — no history kept
            f.write(header)
            f.write(body)
            if not body.endswith("\n"):
                f.write("\n")

    def _write_verdict(self, v: Verdict, *, news_count: int) -> None:
        os.makedirs(os.path.dirname(self._verdict_file), exist_ok=True)
        payload = {
            **asdict(v),
            "decided_at_iso": datetime.fromtimestamp(v.decided_at, tz=timezone.utc).isoformat(timespec="seconds"),
            "expires_at_iso": datetime.fromtimestamp(v.expires_at, tz=timezone.utc).isoformat(timespec="seconds"),
            "news_count": news_count,
            "allow": v.allow(),
        }
        with open(self._verdict_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")


def news_summary(items: List[NewsItem]) -> str:
    """Convenience helper for runners that want to print the list."""
    return "\n".join(i.short() for i in items[:20])
