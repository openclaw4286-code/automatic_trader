"""Tiny helpers that the manager uses to inspect live exchange state."""
from __future__ import annotations

from typing import Any, Dict, Iterable


def already_in_symbol(positions: Iterable[Dict[str, Any]], symbol: str) -> bool:
    return any(p.get("symbol") == symbol for p in positions)
