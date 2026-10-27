"""Настройка логирования приложения."""

from __future__ import annotations

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str) -> None:
    """Настраивает вывод логов в stdout с единым форматом."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            # Консоль Windows не всегда умеет кириллицу в cp1251/cp866.
            stream.reconfigure(encoding="utf-8", errors="replace")

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    logging.basicConfig(level=level, handlers=[handler], force=True)
