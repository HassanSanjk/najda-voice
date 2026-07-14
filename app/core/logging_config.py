"""
Centralized logging configuration.

Call setup_logging() once at app startup (see app/main.py). Every other
module just does `logger = logging.getLogger(__name__)` and logs
normally — no per-module setup needed.
"""

import logging
import sys

from config import settings


def setup_logging() -> None:
    level = logging.DEBUG if settings.app_env == "development" else logging.INFO

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()  # avoid duplicate handlers if reloaded
    root.addHandler(handler)

    # Quiet down noisy third-party loggers so ours aren't buried.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
