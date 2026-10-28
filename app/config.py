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
CONFIRMATION_MODES = ("auto", "always", "never")
OBSIDIAN_MODES = ("auto", "rest", "vault")

MAX_LLM_ACCOUNTS = 20
DEFAULT_LLM_COOLDOWN_SECONDS = 3600
DEFAULT_LLM_STUB_ACCOUNTS = 3
DEFAULT_LLM_STUB_QUOTA = 2
DEFAULT_LLM_STUB_RESET_SECONDS = 60

DEFAULT_LINK_LIMIT = 10
MAX_LINK_LIMIT = 50

# Порядок = качество: первая модель лучшая, последняя — запасная.
# Список используется только в режиме заглушек, пока нет реальных API.
DEFAULT_STUB_MODELS = (
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "deepseek/deepseek-r1:free",
    "qwen/qwen3-235b-a22b:free",
    "google/gemma-4-31b-it:free",
)

_TRUE_VALUES = ("1", "true", "yes", "on")
_FALSE_VALUES = ("0", "false", "no", "off")


@dataclass(frozen=True)
class LLMAccount:
    """Один аккаунт OpenAI-совместимого провайдера.

    Ключ хранится только здесь; в логи и сообщения попадает лишь факт
    наличия ключа и его длина.
    """

    name: str
    base_url: str
    api_key: str


@dataclass(frozen=True)
class Settings:
    """Настройки приложения. Секреты хранятся только здесь, не в логах."""

    telegram_bot_token: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_accounts: tuple[LLMAccount, ...]
    llm_models: tuple[str, ...]
    llm_cooldown_seconds: int
    llm_stub: bool
    llm_stub_accounts: int
    llm_stub_quota: int
    llm_stub_reset_seconds: int
    obsidian_base_url: str
    obsidian_api_key: str
    obsidian_dry_run: bool
    agent_confirmation: str
    agent_default_folder: str
    agent_auto_folder: bool
    agent_search_limit: int
    log_level: str
    obsidian_mode: str = "auto"
    obsidian_vault_path: str = ""
    agent_template_folder: str = "Templates"
    agent_style_note: str = ""
    agent_auto_link: bool = True
    agent_link_limit: int = DEFAULT_LINK_LIMIT

    @property
    def effective_obsidian_mode(self) -> str:
        """Что реально используется: vault, rest или путь решает в auto."""
        if self.obsidian_mode == "auto":
            return "vault" if self.obsidian_vault_path else "rest"
        return self.obsidian_mode

    def safe_summary(self) -> dict[str, str]:
        """Безопасное представление настроек для логов: без значений секретов."""
        return {
            "telegram_bot_token": _describe_secret(self.telegram_bot_token),
            "llm_mode": "заглушки" if self.llm_stub else "реальный API",
            "llm_models": ", ".join(self.llm_models) or "<не заданы>",
            "llm_accounts": _describe_accounts(self.llm_accounts),
            "llm_cooldown_seconds": str(self.llm_cooldown_seconds),
            "llm_stub_quota": (
                f"{self.llm_stub_quota} запросов на пару «модель — аккаунт»"
                if self.llm_stub
                else "<не используется>"
            ),
            "llm_stub_reset_seconds": (
                str(self.llm_stub_reset_seconds)
                if self.llm_stub
                else "<не используется>"
            ),
            "obsidian_mode": (
                (
                    "vault (файлы напрямую)"
                    if self.effective_obsidian_mode == "vault"
                    else "rest (Local REST API)"
                )
                + (
                    f", OBSIDIAN_MODE={self.obsidian_mode}"
                    if self.obsidian_mode != "auto"
                    else " (авто)"
                )
            ),
            "obsidian_vault_path": self.obsidian_vault_path or "<не задан>",
            "obsidian_base_url": self.obsidian_base_url,
            "obsidian_api_key": _describe_secret(self.obsidian_api_key),
            "obsidian_dry_run": str(self.obsidian_dry_run).lower(),
            "agent_confirmation": self.agent_confirmation,
            "agent_default_folder": self.agent_default_folder,
            "agent_auto_folder": str(self.agent_auto_folder).lower(),
            "agent_search_limit": str(self.agent_search_limit),
            "agent_template_folder": self.agent_template_folder,
            "agent_style_note": self.agent_style_note or "<не задан>",
            "agent_auto_link": str(self.agent_auto_link).lower(),
            "agent_link_limit": str(self.agent_link_limit),
            "log_level": self.log_level,
        }


def _describe_secret(value: str) -> str:
    """Возвращает факт наличия секрета и его длину, но не сам секрет."""
    if not value:
        return "<не задан>"
    return f"<задан, {len(value)} симв.>"


def _describe_accounts(accounts: tuple[LLMAccount, ...]) -> str:
    """Список аккаунтов для лога: имена и адреса, без ключей."""
    if not accounts:
        return "<не заданы>"
    parts = [f"{account.name} @ {account.base_url}" for account in accounts]
    return f"{len(accounts)}: " + ", ".join(parts)


def _load_llm_accounts(legacy_url: str, legacy_key: str) -> tuple[LLMAccount, ...]:
    """Читает аккаунты LLM_API_<N>_*, при их отсутствии — одиночный режим."""
    accounts: list[LLMAccount] = []
    for index in range(1, MAX_LLM_ACCOUNTS + 1):
        prefix = f"LLM_API_{index}"
        base_url = _env_str(f"{prefix}_BASE_URL")
        api_key = _env_str(f"{prefix}_KEY") or _env_str(f"{prefix}_API_KEY")
        if not base_url and not api_key:
            continue
        if not base_url or not api_key:
            raise ValueError(
                f"{prefix}: нужны оба значения — {prefix}_BASE_URL и {prefix}_KEY"
            )
        name = _env_str(f"{prefix}_NAME", f"account-{index}")
        accounts.append(
            LLMAccount(name=name, base_url=base_url.rstrip("/"), api_key=api_key)
        )

    if not accounts and legacy_url and legacy_key:
        accounts.append(
            LLMAccount(
                name="default",
                base_url=legacy_url.rstrip("/"),
                api_key=legacy_key,
            )
        )

    names = [account.name for account in accounts]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(
            "LLM_API_*_NAME: имена аккаунтов должны быть уникальны, повторяются: "
            + ", ".join(duplicates)
        )
    return tuple(accounts)


def _load_llm_models(legacy_model: str) -> tuple[str, ...]:
    """Читает упорядоченный по качеству список моделей из LLM_MODELS."""
    raw = _env_str("LLM_MODELS") or legacy_model
    models: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if name and name not in models:
            models.append(name)
    return tuple(models)


def build_stub_accounts(count: int) -> tuple[LLMAccount, ...]:
    """Создаёт API-заглушки: реальные ключи и сеть не используются."""
    return tuple(
        LLMAccount(
            name=f"stub-{index}",
            base_url=f"stub://account-{index}",
            api_key=f"stub-key-{index}",
        )
        for index in range(1, count + 1)
    )


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


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name}: ожидается целое число, получено {raw!r}") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name}: ожидается от {minimum} до {maximum}, получено {value}")
    return value


def _env_folder(name: str, default: str) -> str:
    folder = _env_str(name, default).strip().strip("/")
    if not folder:
        return default
    if ".." in folder.split("/"):
        raise ValueError(f"{name}: путь не должен содержать «..»")
    return folder


def _env_note_path(name: str) -> str:
    """Путь к заметке в хранилище; пусто — настройка не используется."""
    path = _env_str(name).strip().strip("/")
    if not path:
        return ""
    if not path.lower().endswith(".md"):
        raise ValueError(f"{name}: ожидается путь к .md-заметке, получено {path!r}")
    if ".." in path.split("/"):
        raise ValueError(f"{name}: путь не должен содержать «..»")
    return path


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

    confirmation = _env_str("AGENT_CONFIRMATION", "auto").lower()
    if confirmation not in CONFIRMATION_MODES:
        raise ValueError(
            "AGENT_CONFIRMATION: ожидается одно из "
            f"{', '.join(CONFIRMATION_MODES)}, получено {confirmation!r}"
        )

    obsidian_mode = _env_str("OBSIDIAN_MODE", "auto").lower()
    if obsidian_mode not in OBSIDIAN_MODES:
        raise ValueError(
            "OBSIDIAN_MODE: ожидается одно из "
            f"{', '.join(OBSIDIAN_MODES)}, получено {obsidian_mode!r}"
        )
    obsidian_vault_path = _env_str("OBSIDIAN_VAULT_PATH")
    if obsidian_mode == "vault" and not obsidian_vault_path:
        raise ValueError(
            "OBSIDIAN_MODE=vault: задайте путь к хранилищу в OBSIDIAN_VAULT_PATH"
        )
    if obsidian_mode == "rest" and obsidian_vault_path:
        log.warning(
            "OBSIDIAN_MODE=rest: OBSIDIAN_VAULT_PATH (%s) игнорируется",
            obsidian_vault_path,
        )

    legacy_llm_url = _env_str("LLM_BASE_URL")
    legacy_llm_key = _env_str("LLM_API_KEY")
    legacy_llm_model = _env_str("LLM_MODEL")

    llm_stub = _env_bool("LLM_STUB", default=False)
    llm_stub_accounts = _env_int(
        "LLM_STUB_ACCOUNTS",
        default=DEFAULT_LLM_STUB_ACCOUNTS,
        minimum=1,
        maximum=MAX_LLM_ACCOUNTS,
    )
    llm_stub_quota = _env_int(
        "LLM_STUB_QUOTA",
        default=DEFAULT_LLM_STUB_QUOTA,
        minimum=1,
        maximum=1000,
    )
    llm_stub_reset_seconds = _env_int(
        "LLM_STUB_RESET_SECONDS",
        default=DEFAULT_LLM_STUB_RESET_SECONDS,
        minimum=5,
        maximum=86400,
    )

    llm_models = _load_llm_models(legacy_llm_model)
    if llm_stub:
        # Режим заглушек: реальные ключи не читаем и в сеть не ходим.
        llm_accounts = build_stub_accounts(llm_stub_accounts)
        if not llm_models:
            llm_models = DEFAULT_STUB_MODELS
    else:
        llm_accounts = _load_llm_accounts(legacy_llm_url, legacy_llm_key)
        if llm_accounts and not llm_models:
            raise ValueError("LLM: аккаунты заданы, а LLM_MODELS (или LLM_MODEL) пуст")
        if llm_models and not llm_accounts:
            raise ValueError(
                "LLM: модели заданы, а аккаунты нет. Заполните LLM_API_1_BASE_URL "
                "и LLM_API_1_KEY (или LLM_BASE_URL и LLM_API_KEY)"
            )

    return Settings(
        telegram_bot_token=_env_str("TELEGRAM_BOT_TOKEN"),
        llm_base_url=legacy_llm_url,
        llm_api_key=legacy_llm_key,
        llm_model=legacy_llm_model,
        llm_accounts=llm_accounts,
        llm_models=llm_models,
        llm_cooldown_seconds=_env_int(
            "LLM_COOLDOWN_SECONDS",
            default=DEFAULT_LLM_COOLDOWN_SECONDS,
            minimum=60,
            maximum=604800,
        ),
        llm_stub=llm_stub,
        llm_stub_accounts=llm_stub_accounts,
        llm_stub_quota=llm_stub_quota,
        llm_stub_reset_seconds=llm_stub_reset_seconds,
        obsidian_base_url=_env_str("OBSIDIAN_BASE_URL", DEFAULT_OBSIDIAN_BASE_URL).rstrip("/"),
        obsidian_api_key=_env_str("OBSIDIAN_API_KEY"),
        obsidian_dry_run=_env_bool("OBSIDIAN_DRY_RUN", default=True),
        agent_confirmation=confirmation,
        agent_default_folder=_env_folder("AGENT_DEFAULT_FOLDER", "Inbox"),
        agent_auto_folder=_env_bool("AGENT_AUTO_FOLDER", default=True),
        agent_search_limit=_env_int("AGENT_SEARCH_LIMIT", default=5, minimum=1, maximum=20),
        log_level=log_level,
        obsidian_mode=obsidian_mode,
        obsidian_vault_path=obsidian_vault_path,
        agent_template_folder=_env_folder("AGENT_TEMPLATE_FOLDER", "Templates"),
        agent_style_note=_env_note_path("AGENT_STYLE_NOTE"),
        agent_auto_link=_env_bool("AGENT_AUTO_LINK", default=True),
        agent_link_limit=_env_int(
            "AGENT_LINK_LIMIT",
            default=DEFAULT_LINK_LIMIT,
            minimum=1,
            maximum=MAX_LINK_LIMIT,
        ),
    )
