"""Telegram-бот: обработчики команд и цикл long polling."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.config import Settings
from app.llm import LLMClient, LLMError

log = logging.getLogger(__name__)

router = Router(name="main")

SYSTEM_PROMPT = "Ты помощник в Telegram. Отвечай по-русски, кратко и по делу."

START_TEXT = (
    "Привет! Я помогу работать с вашим Obsidian.\n\n"
    "Пока я отвечаю на сообщения через языковую модель.\n"
    "Создание заметок появится на следующих этапах."
)

HELP_TEXT = (
    "Доступные команды:\n"
    "/start — начать заново\n"
    "/help — эта справка\n\n"
    "Любой текст я отправлю модели и верну её ответ."
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(START_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(F.text)
async def answer_text(message: Message, llm: LLMClient) -> None:
    try:
        response = await llm.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": message.text or ""},
            ]
        )
    except LLMError as error:
        log.error("LLM не ответила: %s", error)
        await message.answer("Не удалось получить ответ от модели. Попробуйте позже.")
        return

    await message.answer(response.text or "Модель вернула пустой ответ.")


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Пока я понимаю только текст.")


async def run_bot(settings: Settings) -> None:
    """Запускает бота и блокируется до остановки (Ctrl+C)."""
    llm = LLMClient(settings)
    bot = Bot(token=settings.telegram_bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    try:
        me = await bot.get_me()
        log.info("Бот @%s запущен, ожидаю сообщения", me.username)
        # На случай, если для токена ранее был настроен webhook.
        await bot.delete_webhook(drop_pending_updates=True)
        # llm попадает в обработчики как keyword-аргумент (workflow data).
        await dispatcher.start_polling(bot, llm=llm)
    finally:
        await llm.close()
        await bot.session.close()
        log.info("Бот остановлен")
