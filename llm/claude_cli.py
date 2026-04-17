"""Thin async subprocess bridge to the local `claude` CLI."""
from __future__ import annotations

import asyncio
from typing import Tuple

from config import CLAUDE_CLI, CLAUDE_MODEL, CLAUDE_TIMEOUT_SEC
from utils.logger import get_logger

log = get_logger("claude")


class ClaudeCLI:
    def __init__(self, binary: str = CLAUDE_CLI, model: str = CLAUDE_MODEL) -> None:
        self._binary = binary
        self._model = model

    async def ask(self, prompt: str) -> str:
        """Run `claude -p --model <model>` with the prompt on stdin."""
        cmd = [self._binary, "-p", "--model", self._model]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=CLAUDE_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"claude CLI timed out after {CLAUDE_TIMEOUT_SEC}s")

        if proc.returncode != 0:
            out = stdout.decode("utf-8", "replace").strip()
            err = stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(
                f"claude CLI exited {proc.returncode}. "
                f"stdout={out!r} stderr={err!r} cmd={' '.join(cmd)}"
            )
        return stdout.decode("utf-8", "replace").strip()


def parse_verdict(text: str) -> Tuple[str, str]:
    """Parse '<PASS|WAIT>\\n<reason>' response; defaults to WAIT on ambiguity."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return "WAIT", "empty LLM response"
    head = lines[0].upper()
    reason = lines[1] if len(lines) > 1 else ""
    if head.startswith("PASS"):
        return "PASS", reason
    if head.startswith("WAIT"):
        return "WAIT", reason
    # tolerate the token appearing inline anywhere on the first line
    if "PASS" in head and "WAIT" not in head:
        return "PASS", reason
    return "WAIT", f"unparsable: {lines[0][:80]}"
