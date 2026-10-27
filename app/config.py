"""Загрузка настроек из переменных окружения и файла .env."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

DEFAULT_OBSIDIAN_BASE_URL = "http://127.0.0.1:27123"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_TRUE_VALUES = ("1", "true", "yes", "on")
_FALSE_VALUES = ("0", "false", "no", "off")


@dataclass(frozen=True)
class Settings:
    """Настройки приложения. Секреты хранятся только здесь, не в логах."""

    telegram_bot_token: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    obsidian_base_url: str
    obsidian_api_key: str
    obsidian_dry_run: bool
    log_level: str

    def safe_summary(self) -> dict[str, str]:
        """Безопасное представление настроек для логов: без значений секретов."""
        return {
            "telegram_bot_token": _describe_secret(self.telegram_bot_token),
            "llm_base_url": self.llm_base_url or "<не задан>",
            "llm_api_key": _describe_secret(self.llm_api_key),
            "llm_model": self.llm_model or "<не задан>",
            "obsidian_base_url": self.obsidian_base_url,
            "obsidian_api_key": _describe_secret(self.obsidian_api_key),
            "obsidian_dry_run": str(self.obsidian_dry_run).lower(),
            "log_level": self.log_level,
        }


def _describe_secret(value: str) -> str:
    """Возвращает факт наличия секрета и его длину, но не сам секрет."""
    if not value:
        return "<не задан>"
    return f"<задан, {len(value)} симв.>"


def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name)
    if not raw:
        return default
    value = raw.lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError(f"{name}: ожидается true или false, получено {raw!r}")


def load_settings() -> Settings:
    """Читает .env (если есть) и переменные окружения, возвращает Settings."""
    if ENV_FILE.is_file():
        load_dotenv(ENV_FILE)
    else:
        log.warning("Файл .env не найден (%s), используются значения по умолчанию", ENV_FILE)

    log_level = _env_str("LOG_LEVEL", "INFO").upper()
    if log_level not in LOG_LEVELS:
        raise ValueError(
            f"LOG_LEVEL: ожидается одно из {', '.join(LOG_LEVELS)}, получено {log_level!r}"
        )

    return Settings(
        telegram_bot_token=_env_str("TELEGRAM_BOT_TOKEN"),
        llm_base_url=_env_str("LLM_BASE_URL"),
        llm_api_key=_env_str("LLM_API_KEY"),
        llm_model=_env_str("LLM_MODEL"),
        obsidian_base_url=_env_str("OBSIDIAN_BASE_URL", DEFAULT_OBSIDIAN_BASE_URL).rstrip("/"),
        obsidian_api_key=_env_str("OBSIDIAN_API_KEY"),
        obsidian_dry_run=_env_bool("OBSIDIAN_DRY_RUN", default=True),
        log_level=log_level,
    )
