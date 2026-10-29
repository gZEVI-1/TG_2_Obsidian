"""Настройка логирования приложения: консоль и файл с ротацией."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_MAX_BYTES = 2_000_000
LOG_BACKUPS = 5


def setup_logging(level: str, log_file: Path | None = None) -> None:
    """Настраивает вывод в stdout и, если задан путь, в файл с ротацией.

    Повторный вызов (после чтения настроек) заменяет обработчики, а не
    добавляет вторые: иначе каждая строка печаталась бы дважды.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            # Консоль Windows не всегда умеет кириллицу в cp1251/cp866.
            stream.reconfigure(encoding="utf-8", errors="replace")

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    handlers[0].setFormatter(formatter)

    if log_file is not None:
        file_handler = _open_file_handler(log_file)
        if file_handler is not None:
            file_handler.setFormatter(formatter)
            handlers.append(file_handler)

    logging.basicConfig(level=level, handlers=handlers, force=True)


def _open_file_handler(log_file: Path) -> logging.Handler | None:
    """Файловый обработчик; при ошибке доступа пишет предупреждение и молчит."""
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        return RotatingFileHandler(
            log_file,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUPS,
            encoding="utf-8",
        )
    except OSError as error:
        print(f"Лог в файл недоступен ({log_file}): {error}", file=sys.stderr)
        return None
