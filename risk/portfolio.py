"""Concurrent-position gate and simple aggregate-risk checks."""
from __future__ import annotations

from typing import Any, Dict, Iterable

from config import MAX_CONCURRENT_POSITIONS
from utils.logger import get_logger

log = get_logger("portfolio")


def can_open_new(positions: Iterable[Dict[str, Any]]) -> bool:
    count = sum(1 for _ in positions)
    if count >= MAX_CONCURRENT_POSITIONS:
        log.info("position cap hit (%d/%d) — skipping new entries", count, MAX_CONCURRENT_POSITIONS)
        return False
    return True


def already_in_symbol(positions: Iterable[Dict[str, Any]], symbol: str) -> bool:
    return any(p.get("symbol") == symbol for p in positions)
