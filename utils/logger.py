"""Rotating file + rich console logger used across the system."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from rich.logging import RichHandler

from config import LOG_BACKUPS, LOG_DIR, LOG_LEVEL, LOG_ROTATE_MB

_FMT = "%(asctime)s | %(name)s | %(levelname)s | %(message)s"
_configured: set[str] = set()


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if name in _configured:
        return logger

    os.makedirs(LOG_DIR, exist_ok=True)
    logger.setLevel(LOG_LEVEL)
    logger.propagate = False

    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, f"{name}.log"),
        maxBytes=LOG_ROTATE_MB * 1024 * 1024,
        backupCount=LOG_BACKUPS,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(file_handler)

    console = RichHandler(rich_tracebacks=True, show_path=False, markup=False)
    console.setFormatter(logging.Formatter("%(name)s | %(message)s"))
    logger.addHandler(console)

    _configured.add(name)
    return logger
