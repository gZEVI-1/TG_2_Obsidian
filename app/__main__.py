"""Точка входа: python -m app [--check-* | --health | --backup]."""

from __future__ import annotations

import argparse
import asyncio
import logging

import aiohttp

from app.backup import create_backup
from app.bot import run_bot
from app.config import PROJECT_ROOT, Settings, load_settings
from app.health import check_health
from app.llm import LLMError, LLMPool
from app.logging_setup import setup_logging
from app.obsidian import FailoverClient, ObsidianError, create_obsidian_client

log = logging.getLogger("app")

CHECK_NOTE_PATH = "TG-OBS smoke test.md"
CHECK_NOTE_CONTENT = (
    "# TG-OBS smoke test\n\n"
    "Эта заметка создана проверкой `python -m app --check-obsidian`.\n"
    "Её можно удалить.\n"
)

CHECK_LLM_PROMPT = "Ответь одним коротким предложением: что такое Obsidian?"
CHECK_LLM_TOOL_PROMPT = "Какая сейчас погода в Москве? Вызови инструмент get_weather."
CHECK_LLM_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Узнать текущую погоду в городе",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "Название города"}},
            "required": ["city"],
        },
    },
}


def log_settings(settings: Settings) -> None:
    """Печатает безопасную сводку настроек (без значений секретов)."""
    log.info("Конфигурация загружена")
    for key, value in settings.safe_summary().items():
        log.info("  %s = %s", key, value)

    if settings.obsidian_dry_run:
        log.info("dry-run включён: запись в Obsidian запрещена")
    else:
        log.warning("dry-run выключён: изменения будут записываться в Obsidian")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="app", description="Telegram-бот для Obsidian")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check-config",
        action="store_true",
        help="проверить настройки и выйти, не запуская бота",
    )
    group.add_argument(
        "--check-obsidian",
        action="store_true",
        help="проверить Local REST API и записать тестовую заметку",
    )
    group.add_argument(
        "--check-llm",
        action="store_true",
        help="проверить LLM: ответ, tool-calls и состояние пула аккаунтов",
    )
    group.add_argument(
        "--check-rag",
        action="store_true",
        help="проверить RAG: модель эмбеддингов, индекс и семантический поиск",
    )
    group.add_argument(
        "--health",
        action="store_true",
        help="быстрая проверка готовности: хранилище, LLM, RAG, копии, доступ",
    )
    group.add_argument(
        "--backup",
        action="store_true",
        help="сделать резервную копию хранилища и журнала в BACKUP_DIR",
    )
    return parser.parse_args()


async def check_obsidian(settings: Settings) -> None:
    async with create_obsidian_client(settings) as client:
        if isinstance(client, FailoverClient):
            states = await client.backends_status()
            log.info("REST API %s: %s", settings.obsidian_base_url, states["rest"])
            log.info(
                "Файлы vault %s: %s", settings.obsidian_vault_path, states["vault"]
            )
        elif settings.effective_obsidian_mode == "vault":
            status = await client.health()
            log.info(
                "Хранилище доступно напрямую: %s (writable=%s)",
                status.get("path", settings.obsidian_vault_path),
                status.get("writable", "?"),
            )
        else:
            status = await client.health()
            log.info(
                "Local REST API доступен: %s (authenticated=%s)",
                status.get("service", "?"),
                status.get("authenticated", "?"),
            )

        written = await client.write_note(CHECK_NOTE_PATH, CHECK_NOTE_CONTENT, overwrite=True)
        if written:
            log.info("Тестовая заметка записана: %s", CHECK_NOTE_PATH)
        else:
            log.info(
                "Заметка не записана: включён dry-run. "
                "Для реальной записи поставьте OBSIDIAN_DRY_RUN=false"
            )


async def check_llm(settings: Settings) -> None:
    """Проверяет пул: ответ, поддержку tool-calls и состояние ротации."""
    async with LLMPool(settings) as pool:
        if settings.llm_stub:
            log.info(
                "LLM: режим заглушек (LLM_STUB=true), реальный API не вызывается. "
                "Квота: %d запросов на пару, сброс через %d с.",
                settings.llm_stub_quota,
                settings.llm_stub_reset_seconds,
            )

        answer = await pool.chat([{"role": "user", "content": CHECK_LLM_PROMPT}])
        log.info(
            "Модель ответила (%s, аккаунт %s): %s",
            answer.model,
            pool.status().active_account,
            answer.text or "<пусто>",
        )

        if settings.llm_stub:
            log.info("Режим заглушек: проверка tool-calls пропущена")
        else:
            probe = await pool.chat(
                [{"role": "user", "content": CHECK_LLM_TOOL_PROMPT}],
                tools=[CHECK_LLM_TOOL],
            )
            if probe.tool_calls:
                call = probe.tool_calls[0]
                log.info("Tool-calls поддерживаются: %s(%s)", call.name, call.arguments)
            else:
                log.warning(
                    "Модель не вызвала инструмент (finish_reason=%s, ответ: %s). "
                    "Tool-calls под вопросом.",
                    probe.finish_reason,
                    probe.text or "<пусто>",
                )

        status = pool.status()
        log.info(
            "LLM-пул: доступно %d из %d пар, активна %s / %s",
            status.available_slots,
            status.total_slots,
            status.active_model,
            status.active_account,
        )
        if status.reset_at is not None:
            log.warning("LLM-пул: лимиты исчерпаны, автосброс позже")


async def check_rag(settings: Settings) -> None:
    """Проверяет модель эмбеддингов и обновляет индекс (первый раз — долго)."""
    try:
        from app.rag import LocalEmbedder, SemanticSearch, store_dir
    except ImportError as error:
        log.error(
            "RAG недоступен: %s. Установите зависимости: "
            "pip install -r requirements-rag.txt",
            error,
        )
        raise SystemExit(2) from error

    target = settings.obsidian_vault_path or settings.obsidian_base_url
    semantic = SemanticSearch(
        LocalEmbedder(settings.rag_model),
        store_dir(PROJECT_ROOT, target),
        exclude_folder=settings.agent_template_folder,
        exclude_note=settings.agent_style_note,
    )
    async with create_obsidian_client(settings) as client:
        stats = await semantic.refresh(client)
        log.info(
            "RAG-индекс: заметок %d, фрагментов %d, обновлено %d, ошибок %d",
            stats.notes,
            stats.chunks,
            stats.updated,
            stats.failed,
        )
        hits = await semantic.search("проверка семантического поиска", limit=3)
        if not hits:
            log.warning("RAG: поиск ничего не нашёл — индекс пуст?")
        for hit in hits:
            log.info("  %s (%s): %s", hit["path"], hit["score"], hit["excerpt"][:80])


def main() -> None:
    # Логирование включаем до чтения настроек, чтобы предупреждение
    # о недостающем .env тоже попало в лог. Уровень уточняем после.
    setup_logging("INFO")
    settings = load_settings()
    setup_logging(settings.log_level, settings.log_path)
    log_settings(settings)

    args = parse_args()

    if args.check_config:
        print("config OK")
        return

    if args.health:
        healthy = asyncio.run(check_health(settings))
        print("health OK" if healthy else "health FAILED")
        if not healthy:
            raise SystemExit(2)
        return

    if args.backup:
        try:
            archive = create_backup(settings)
        except ValueError as error:
            log.error("Резервная копия: %s", error)
            raise SystemExit(2) from error
        print(str(archive))
        return

    if args.check_obsidian:
        try:
            asyncio.run(check_obsidian(settings))
        except ObsidianError as error:
            log.error("Obsidian: %s", error)
            raise SystemExit(2) from error
        except aiohttp.ClientError as error:
            if settings.effective_obsidian_mode == "vault":
                log.error(
                    "Не удалось прочитать хранилище %s: %s.",
                    settings.obsidian_vault_path,
                    error,
                )
            else:
                log.error(
                    "Не удалось подключиться к %s: %s. Проверьте, что Obsidian "
                    "запущен и сервер Local REST API включён.",
                    settings.obsidian_base_url,
                    error,
                )
            raise SystemExit(2) from error
        return

    if args.check_llm:
        try:
            asyncio.run(check_llm(settings))
        except LLMError as error:
            log.error("LLM: %s", error)
            raise SystemExit(2) from error
        except aiohttp.ClientError as error:
            log.error(
                "Не удалось подключиться к LLM (%s): %s. Проверьте аккаунты, "
                "LLM_STUB и сеть.",
                settings.llm_base_url or "LLM_API_*",
                error,
            )
            raise SystemExit(2) from error
        return

    if args.check_rag:
        asyncio.run(check_rag(settings))
        return

    if not settings.telegram_bot_token:
        log.error("TELEGRAM_BOT_TOKEN не задан: впишите токен от @BotFather в .env")
        raise SystemExit(2)

    try:
        asyncio.run(run_bot(settings))
    except LLMError as error:
        log.error("LLM: %s", error)
        raise SystemExit(2) from error
    except ObsidianError as error:
        log.error("Obsidian: %s", error)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
