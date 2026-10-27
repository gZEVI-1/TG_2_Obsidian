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

MAX_HISTORY_MESSAGES = 20

START_TEXT = (
    "Привет! Я помогу работать с вашим Obsidian.\n\n"
    "Я помню контекст диалога. Команда /new начинает новый диалог.\n"
    "Создание заметок появится на следующих этапах."
)

HELP_TEXT = (
    "Доступные команды:\n"
    "/start — начать заново\n"
    "/help — эта справка\n"
    "/new — новый диалог, предыдущие сообщения забываются\n\n"
    "Любой текст я отправлю модели и верну её ответ."
)


class ChatMemory:
    """История диалога по `chat_id`, живёт в памяти процесса.

    Сбрасывается командой `/new`; при перезапуске бота история теряется.
    """

    def __init__(self, max_messages: int = MAX_HISTORY_MESSAGES) -> None:
        self._max_messages = max_messages
        self._messages: dict[int, list[dict[str, str]]] = {}

    def get(self, chat_id: int) -> list[dict[str, str]]:
        """Копия истории: вызывающая сторона не может изменить хранилище."""
        return list(self._messages.get(chat_id, ()))

    def add(self, chat_id: int, user_text: str, assistant_text: str) -> None:
        messages = self._messages.setdefault(chat_id, [])
        messages.append({"role": "user", "content": user_text})
        messages.append({"role": "assistant", "content": assistant_text})
        del messages[: -self._max_messages]

    def clear(self, chat_id: int) -> None:
        self._messages.pop(chat_id, None)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(START_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("new"))
async def cmd_new(message: Message, memory: ChatMemory) -> None:
    memory.clear(message.chat.id)
    await message.answer("Новый диалог начат: предыдущие сообщения забыты.")


@router.message(F.text)
async def answer_text(message: Message, llm: LLMClient, memory: ChatMemory) -> None:
    text = message.text or ""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *memory.get(message.chat.id),
        {"role": "user", "content": text},
    ]
    try:
        response = await llm.chat(messages)
    except LLMError as error:
        log.error("LLM не ответила: %s", error)
        await message.answer("Не удалось получить ответ от модели. Попробуйте позже.")
        return

    if response.text:
        memory.add(message.chat.id, text, response.text)

    await message.answer(response.text or "Модель вернула пустой ответ.")


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Пока я понимаю только текст.")


async def run_bot(settings: Settings) -> None:
    """Запускает бота и блокируется до остановки (Ctrl+C)."""
    llm = LLMClient(settings)
    memory = ChatMemory()
    bot = Bot(token=settings.telegram_bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    try:
        me = await bot.get_me()
        log.info("Бот @%s запущен, ожидаю сообщения", me.username)
        # На случай, если для токена ранее был настроен webhook.
        await bot.delete_webhook(drop_pending_updates=True)
        # llm и memory попадают в обработчики как keyword-аргументы (workflow data).
        await dispatcher.start_polling(bot, llm=llm, memory=memory)
    finally:
        await llm.close()
        await bot.session.close()
        log.info("Бот остановлен")
