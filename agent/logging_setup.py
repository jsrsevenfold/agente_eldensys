"""Rotating log setup."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import LOG_DIR


def setup_logging(level: str = "INFO") -> logging.Logger:
    log_path = LOG_DIR / "agent.log"
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level.upper())
    # Remove duplicate handlers across reloads
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(file_handler)

    # Also stream to stderr in dev
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    return logging.getLogger("eldensys.agent")
