"""Проверка готовности бота: Telegram, хранилище, LLM, RAG, журнал, копии и доступ.

Ничего не пишет в хранилище и не вызывает модель: токен проверяется через
`getMe`, хранилище — через `health()`, поэтому команду можно запускать в любой
момент.
"""

from __future__ import annotations

import importlib.util
import logging
import os

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.config import Settings, is_local_url
from app.obsidian import FailoverClient, ObsidianError, create_obsidian_client

log = logging.getLogger(__name__)

OK = "ok"
WARN = "warn"
FAIL = "fail"

_MARKS = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}


async def check_health(settings: Settings) -> bool:
    """Печатает отчёт и возвращает True, если критичных ошибок нет."""
    results: list[tuple[str, str, str]] = []
    results.append(await _check_telegram(settings))
    results.append(await _check_storage(settings))
    results.append(_check_llm(settings))
    results.append(_check_rag(settings))
    results.append(_check_audit(settings))
    results.append(_check_backup(settings))
    results.append(_check_access(settings))
    results.append(_check_log(settings))
    results.append(_check_write_mode(settings))

    for name, status, detail in results:
        log.info("%-16s %s  %s", name, _MARKS[status], detail)
    return not any(status == FAIL for _name, status, _detail in results)


async def _check_telegram(settings: Settings) -> tuple[str, str, str]:
    """Токен бота и список разрешённых: без этого бот не запустится."""
    if not settings.telegram_bot_token:
        return ("Telegram", FAIL, "TELEGRAM_BOT_TOKEN не задан")
    try:
        bot = Bot(token=settings.telegram_bot_token)
    except ValueError as error:
        return ("Telegram", FAIL, f"токен не похож на токен бота: {error}")
    try:
        me = await bot.get_me()
    except (TelegramAPIError, aiohttp.ClientError) as error:
        return ("Telegram", FAIL, f"токен не принят: {error}")
    finally:
        await bot.session.close()

    who = f"@{me.username}" if me.username else f"id {me.id}"
    if not settings.telegram_allowed_users:
        return (
            "Telegram",
            WARN,
            f"{who}: TELEGRAM_ALLOWED_USERS не задан — отвечает любому чату",
        )
    return (
        "Telegram",
        OK,
        f"{who}: разрешено {len(settings.telegram_allowed_users)}",
    )


async def _check_storage(settings: Settings) -> tuple[str, str, str]:
    """Хранилище: в auto годятся REST или файлы, в rest/vault — только основной."""
    try:
        async with create_obsidian_client(settings) as client:
            if isinstance(client, FailoverClient):
                states = await client.backends_status()
                detail = "; ".join(f"{name}: {value}" for name, value in states.items())
                available = any(
                    value.startswith("доступен") for value in states.values()
                )
                return ("хранилище", OK if available else FAIL, detail)
            status = await client.health()
            if settings.effective_obsidian_mode == "vault":
                return (
                    "хранилище",
                    OK,
                    f"файлы {status.get('path', settings.obsidian_vault_path)}",
                )
            return (
                "хранилище",
                OK,
                f"REST {settings.obsidian_base_url} ({status.get('service', '?')})",
            )
    except (ObsidianError, aiohttp.ClientError) as error:
        return ("хранилище", FAIL, f"{settings.obsidian_mode}: {error}")


def _check_llm(settings: Settings) -> tuple[str, str, str]:
    if settings.llm_stub:
        return ("LLM", WARN, "режим заглушек: заметки не создаются")
    if not settings.llm_accounts:
        return ("LLM", FAIL, "нет аккаунтов LLM_API_<N>_*")
    if not settings.llm_models:
        return ("LLM", FAIL, "не задан LLM_MODELS")
    return (
        "LLM",
        OK,
        f"аккаунтов {len(settings.llm_accounts)}, моделей {len(settings.llm_models)} "
        f"({settings.llm_models[0]})",
    )


def _check_rag(settings: Settings) -> tuple[str, str, str]:
    if not settings.rag_enabled:
        return ("RAG", OK, "выключен (RAG_ENABLED=false)")
    if importlib.util.find_spec("sentence_transformers") is None:
        return ("RAG", WARN, "нет зависимостей: pip install -r requirements-rag.txt")
    index = settings.state_dir / "rag"
    state = "есть" if index.is_dir() else "ещё нет"
    return ("RAG", OK, f"зависимости на месте, индекс: {state}")


def _check_audit(settings: Settings) -> tuple[str, str, str]:
    if not settings.audit_enabled:
        return ("аудит", OK, "выключен (AUDIT_ENABLED=false)")
    try:
        settings.audit_root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return ("аудит", FAIL, f"нет доступа к {settings.audit_root}: {error}")
    if not os.access(settings.audit_root, os.W_OK):
        return ("аудит", FAIL, f"нет записи в {settings.audit_root}")
    return ("аудит", OK, str(settings.audit_root))


def _check_backup(settings: Settings) -> tuple[str, str, str]:
    target = settings.backup_path
    if target is None:
        return ("копии", WARN, "BACKUP_DIR не задан: команда --backup недоступна")
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return ("копии", FAIL, f"нет доступа к {target}: {error}")
    if not os.access(target, os.W_OK):
        return ("копии", FAIL, f"нет записи в {target}")
    return ("копии", OK, f"{target}, хранить {settings.backup_keep}")


def _check_access(settings: Settings) -> tuple[str, str, str]:
    """Local REST API должен слушать только этот компьютер."""
    if not is_local_url(settings.obsidian_base_url):
        return (
            "доступ",
            WARN,
            f"{settings.obsidian_base_url} виден по сети без TLS: "
            "нужен HTTPS или туннель",
        )
    return ("доступ", OK, f"{settings.obsidian_base_url} — только этот компьютер")


def _check_log(settings: Settings) -> tuple[str, str, str]:
    path = settings.log_path
    if path is None:
        return ("лог", WARN, "LOG_FILE пуст: только консоль")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return ("лог", FAIL, f"нет доступа к {path.parent}: {error}")
    if not os.access(path.parent, os.W_OK):
        return ("лог", FAIL, f"нет записи в {path.parent}")
    return ("лог", OK, str(path))


def _check_write_mode(settings: Settings) -> tuple[str, str, str]:
    if settings.obsidian_dry_run:
        return ("запись", WARN, "dry-run: изменения только в лог")
    return ("запись", OK, "реальная запись в хранилище разрешена")
