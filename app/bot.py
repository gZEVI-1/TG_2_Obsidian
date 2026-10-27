"""Telegram-бот: обработчики команд и цикл long polling."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.config import Settings

log = logging.getLogger(__name__)

router = Router(name="main")

START_TEXT = (
    "Привет! Я помогу работать с вашим Obsidian.\n\n"
    "Пока умею немного: повторяю текст и показываю справку.\n"
    "Создание заметок появится на следующих этапах."
)

HELP_TEXT = (
    "Доступные команды:\n"
    "/start — начать заново\n"
    "/help — эта справка\n\n"
    "Любой текст я пока просто повторю эхом."
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(START_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(F.text)
async def echo_text(message: Message) -> None:
    await message.answer(message.text)


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Пока я понимаю только текст.")


async def run_bot(settings: Settings) -> None:
    """Запускает бота и блокируется до остановки (Ctrl+C)."""
    bot = Bot(token=settings.telegram_bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    try:
        me = await bot.get_me()
        log.info("Бот @%s запущен, ожидаю сообщения", me.username)
        # На случай, если для токена ранее был настроен webhook.
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
        log.info("Бот остановлен")
