"""Точка входа: python -m app."""

from __future__ import annotations

import logging

from app.config import load_settings
from app.logging_setup import setup_logging

log = logging.getLogger("app")


def main() -> None:
    # Логирование включаем до чтения настроек, чтобы предупреждение
    # о недостающем .env тоже попало в лог. Уровень уточняем после.
    setup_logging("INFO")
    settings = load_settings()
    logging.getLogger().setLevel(settings.log_level)

    log.info("Конфигурация загружена")
    for key, value in settings.safe_summary().items():
        log.info("  %s = %s", key, value)

    if settings.obsidian_dry_run:
        log.info("dry-run включён: запись в Obsidian запрещена")
    else:
        log.warning("dry-run выключён: изменения будут записываться в Obsidian")

    print("config OK")


if __name__ == "__main__":
    main()
