"""Точка входа: python -m app [--check-config | --check-obsidian | --check-llm]."""

from __future__ import annotations

import argparse
import asyncio
import logging

import aiohttp

from app.bot import run_bot
from app.config import Settings, load_settings
from app.llm import LLMClient, LLMError
from app.logging_setup import setup_logging
from app.obsidian import ObsidianClient, ObsidianError

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
        help="проверить LLM: ответ модели и поддержку tool-calls",
    )
    return parser.parse_args()


async def check_obsidian(settings: Settings) -> None:
    async with ObsidianClient(settings) as client:
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
    """Проверяет ключ, модель и поддержку tool-calls (два запроса к LLM)."""
    async with LLMClient(settings) as client:
        answer = await client.chat([{"role": "user", "content": CHECK_LLM_PROMPT}])
        log.info("Модель ответила (%s): %s", answer.model, answer.text or "<пусто>")

        probe = await client.chat(
            [{"role": "user", "content": CHECK_LLM_TOOL_PROMPT}],
            tools=[CHECK_LLM_TOOL],
        )
        if probe.tool_calls:
            call = probe.tool_calls[0]
            log.info("Tool-calls поддерживаются: %s(%s)", call.name, call.arguments)
        else:
            log.warning(
                "Модель не вызвала инструмент (finish_reason=%s, ответ: %s). "
                "Tool-calls под вопросом — проверим на Этапе 5.",
                probe.finish_reason,
                probe.text or "<пусто>",
            )


def main() -> None:
    # Логирование включаем до чтения настроек, чтобы предупреждение
    # о недостающем .env тоже попало в лог. Уровень уточняем после.
    setup_logging("INFO")
    settings = load_settings()
    logging.getLogger().setLevel(settings.log_level)
    log_settings(settings)

    args = parse_args()

    if args.check_config:
        print("config OK")
        return

    if args.check_obsidian:
        try:
            asyncio.run(check_obsidian(settings))
        except ObsidianError as error:
            log.error("Obsidian: %s", error)
            raise SystemExit(2) from error
        except aiohttp.ClientError as error:
            log.error(
                "Не удалось подключиться к %s: %s. Проверьте, что Obsidian запущен "
                "и сервер Local REST API включён.",
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
                "Не удалось подключиться к %s: %s. Проверьте LLM_BASE_URL и сеть.",
                settings.llm_base_url,
                error,
            )
            raise SystemExit(2) from error
        return

    if not settings.telegram_bot_token:
        log.error("TELEGRAM_BOT_TOKEN не задан: впишите токен от @BotFather в .env")
        raise SystemExit(2)

    try:
        asyncio.run(run_bot(settings))
    except LLMError as error:
        log.error("LLM: %s", error)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
