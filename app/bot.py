"""Telegram-бот: команды, подтверждения и цикл long polling."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.agent import SYSTEM_PROMPT, Agent, Proposal
from app.config import Settings
from app.llm import LLMClient, LLMError
from app.notes import (
    append_to_note,
    append_to_section,
    build_path,
    replace_section,
    validate_path,
)
from app.obsidian import ObsidianClient, ObsidianError

log = logging.getLogger(__name__)

router = Router(name="main")

MAX_HISTORY_MESSAGES = 20
PREVIEW_CHARS = 700
MAX_FOLDER_CHOICES = 3
MAX_BUTTON_CHARS = 40

START_TEXT = (
    "Привет! Я помогу работать с вашим Obsidian.\n\n"
    "Я помню контекст диалога, умею искать и читать заметки, создавать новые "
    "и править разделы.\n"
    "Команды /new и /start начинают новый диалог."
)

HELP_TEXT = (
    "Доступные команды:\n"
    "/start — начать новый диалог\n"
    "/help — эта справка\n"
    "/new — новый диалог, предыдущие сообщения забываются\n\n"
    "Примеры:\n"
    "• «сохрани мысль: …» — предложу новую заметку;\n"
    "• «допиши в заметку про X: …» — найду заметку и предложу дописать;\n"
    "• «в заметке X замени раздел Y на …».\n\n"
    "Любое изменение сначала покажу и спрошу подтверждение."
)


@dataclass
class PendingAction:
    """Предложение, которое ждёт решения пользователя."""

    proposal: Proposal
    stage: str  # folder | auto | confirm | exists
    path: str = ""
    folders: tuple[str, ...] = ()


class ChatMemory:
    """История диалога и ожидающие решения по `chat_id`.

    Живёт в памяти процесса, сбрасывается командами `/new` и `/start`;
    при перезапуске бота теряется.
    """

    def __init__(self, max_messages: int = MAX_HISTORY_MESSAGES) -> None:
        self._max_messages = max_messages
        self._messages: dict[int, list[dict[str, str]]] = {}
        self._actions: dict[int, tuple[int, PendingAction]] = {}

    def get(self, chat_id: int) -> list[dict[str, str]]:
        """Копия истории: вызывающая сторона не может изменить хранилище."""
        return list(self._messages.get(chat_id, ()))

    def add(self, chat_id: int, user_text: str, assistant_text: str) -> None:
        messages = self._messages.setdefault(chat_id, [])
        messages.append({"role": "user", "content": user_text})
        messages.append({"role": "assistant", "content": assistant_text})
        del messages[: -self._max_messages]

    def set_action(self, chat_id: int, action: PendingAction, message_id: int) -> None:
        self._actions[chat_id] = (message_id, action)

    def get_action(self, chat_id: int, message_id: int) -> PendingAction | None:
        stored = self._actions.get(chat_id)
        if stored is None or stored[0] != message_id:
            return None
        return stored[1]

    def get_action_any(self, chat_id: int) -> PendingAction | None:
        """Последнее ожидающее действие чата, без привязки к сообщению."""
        stored = self._actions.get(chat_id)
        return stored[1] if stored else None

    def drop_action(self, chat_id: int) -> None:
        self._actions.pop(chat_id, None)

    def clear(self, chat_id: int) -> None:
        self._messages.pop(chat_id, None)
        self._actions.pop(chat_id, None)


@router.message(CommandStart())
async def cmd_start(message: Message, memory: ChatMemory) -> None:
    memory.clear(message.chat.id)
    await message.answer(START_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("new"))
async def cmd_new(message: Message, memory: ChatMemory) -> None:
    memory.clear(message.chat.id)
    await message.answer("Новый диалог начат: предыдущие сообщения забыты.")


@router.message(F.text)
async def answer_text(
    message: Message,
    agent: Agent,
    memory: ChatMemory,
    settings: Settings,
    obsidian: ObsidianClient,
) -> None:
    chat_id = message.chat.id
    text = message.text or ""

    pending = memory.get_action_any(chat_id)
    if pending is not None and pending.stage == "folder_input":
        memory.drop_action(chat_id)
        choice = await _resolve_folder(obsidian, settings, pending, text)
        if choice.action is None:
            await message.answer(choice.text)
        else:
            sent = await message.answer(choice.text, reply_markup=choice.keyboard)
            memory.set_action(chat_id, choice.action, sent.message_id)
        return

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *memory.get(chat_id),
        {"role": "user", "content": text},
    ]
    try:
        result = await agent.handle(messages)
    except LLMError as error:
        log.error("LLM не ответила: %s", error)
        await message.answer(_llm_error_text(error))
        return

    if result.proposal is not None:
        memory.add(chat_id, text, _proposal_text(result.proposal))
        await _offer(message, memory, settings, obsidian, result.proposal)
        return

    memory.add(chat_id, text, result.text)
    await message.answer(result.text or "Модель вернула пустой ответ.")


async def _offer(
    message: Message,
    memory: ChatMemory,
    settings: Settings,
    obsidian: ObsidianClient,
    proposal: Proposal,
) -> None:
    chat_id = message.chat.id

    if proposal.kind == "create":
        sent = await message.answer(
            _proposal_text(proposal), reply_markup=_folder_keyboard(settings)
        )
        memory.set_action(chat_id, PendingAction(proposal, "folder"), sent.message_id)
        return

    if _needs_confirm(settings, proposal.kind):
        sent = await message.answer(
            _proposal_text(proposal), reply_markup=_confirm_keyboard()
        )
        memory.set_action(
            chat_id,
            PendingAction(proposal, "confirm", path=proposal.path),
            sent.message_id,
        )
        return

    memory.drop_action(chat_id)
    await message.answer(await _apply(obsidian, proposal, proposal.path))


@router.callback_query(F.data == "act:inbox")
async def on_inbox(
    callback: CallbackQuery,
    memory: ChatMemory,
    settings: Settings,
    obsidian: ObsidianClient,
) -> None:
    await callback.answer()
    action = await _pending(callback, memory)
    if action is None:
        return
    choice = await _resolve_folder(
        obsidian, settings, action, settings.agent_default_folder
    )
    await _show_choice(callback, memory, choice)


@router.callback_query(F.data == "act:other")
async def on_other(callback: CallbackQuery, memory: ChatMemory) -> None:
    await callback.answer()
    action = await _pending(callback, memory)
    if action is None:
        return
    memory.set_action(
        callback.message.chat.id,
        replace(action, stage="folder_input"),
        callback.message.message_id,
    )
    await callback.message.edit_text(
        "Напишите папку, например «Проекты/2026». Отмена — /start."
    )


@router.callback_query(F.data == "act:auto")
async def on_auto(
    callback: CallbackQuery,
    agent: Agent,
    memory: ChatMemory,
    settings: Settings,
    obsidian: ObsidianClient,
) -> None:
    await callback.answer("Подбираю папку…")
    action = await _pending(callback, memory)
    if action is None:
        return

    try:
        notes = await obsidian.list_notes()
    except (ObsidianError, aiohttp.ClientError) as error:
        await callback.message.edit_text(_obsidian_error_text(error))
        return

    folders = sorted({note.rsplit("/", 1)[0] for note in notes if "/" in note})[:50]
    if not folders:
        await callback.message.edit_text(
            "В хранилище пока нет папок. Сохранить в Inbox?",
            reply_markup=_inbox_keyboard(),
        )
        return

    try:
        suggestions = await agent.suggest_folders(
            action.proposal.title, action.proposal.content, folders
        )
    except LLMError as error:
        log.error("Не удалось подобрать папку: %s", error)
        await callback.message.edit_text(
            "Не удалось подобрать папку автоматически. Куда сохранить?",
            reply_markup=_inbox_keyboard(),
        )
        return

    if not suggestions:
        await callback.message.edit_text(
            "Не смог уверенно определить папку. Куда сохранить?",
            reply_markup=_inbox_keyboard(),
        )
        return

    action.folders = tuple(suggestions)
    memory.set_action(
        callback.message.chat.id, action, callback.message.message_id
    )
    await callback.message.edit_text(
        "Куда сохранить заметку?", reply_markup=_folder_choices(action)
    )


@router.callback_query(F.data.startswith("act:folder:"))
async def on_folder(
    callback: CallbackQuery,
    memory: ChatMemory,
    settings: Settings,
    obsidian: ObsidianClient,
) -> None:
    await callback.answer()
    action = await _pending(callback, memory)
    if action is None:
        return

    index = int(callback.data.rsplit(":", 1)[1])
    if index >= len(action.folders):
        await callback.message.edit_text("Этот вариант больше не актуален.")
        return
    choice = await _resolve_folder(
        obsidian, settings, action, action.folders[index]
    )
    await _show_choice(callback, memory, choice)


@router.callback_query(F.data == "act:yes")
async def on_yes(
    callback: CallbackQuery, memory: ChatMemory, obsidian: ObsidianClient
) -> None:
    await callback.answer("Записываю…")
    action = await _pending(callback, memory)
    if action is None:
        return
    memory.drop_action(callback.message.chat.id)
    await callback.message.edit_text(
        await _apply(obsidian, action.proposal, action.path)
    )


@router.callback_query(F.data == "act:no")
async def on_no(callback: CallbackQuery, memory: ChatMemory) -> None:
    await callback.answer()
    action = await _pending(callback, memory)
    if action is None:
        return
    memory.drop_action(callback.message.chat.id)
    await callback.message.edit_text("Отменено.")


@router.callback_query(F.data == "act:append")
async def on_append(
    callback: CallbackQuery, memory: ChatMemory, obsidian: ObsidianClient
) -> None:
    await callback.answer("Дописываю…")
    action = await _pending(callback, memory)
    if action is None:
        return
    memory.drop_action(callback.message.chat.id)
    proposal = Proposal(
        kind="append", path=action.path, content=action.proposal.content
    )
    await callback.message.edit_text(await _apply(obsidian, proposal, action.path))


async def _pending(callback: CallbackQuery, memory: ChatMemory) -> PendingAction | None:
    """Достаёт ожидающее предложение и сообщает, если кнопка устарела."""
    if callback.message is None:
        return None
    action = memory.get_action(
        callback.message.chat.id, callback.message.message_id
    )
    if action is None:
        await callback.message.edit_text(
            "Это предложение устарело: оно уже выполнено или отменено."
        )
    return action


@dataclass
class Choice:
    """Ответ на выбор папки: текст, клавиатура и следующее действие, если нужно."""

    text: str
    keyboard: InlineKeyboardMarkup | None = None
    action: PendingAction | None = None


async def _resolve_folder(
    obsidian: ObsidianClient,
    settings: Settings,
    action: PendingAction,
    folder: str,
) -> Choice:
    """Строит путь в выбранной папке и решает, что делать дальше (FR-01, FR-04)."""
    try:
        path = validate_path(build_path(folder, action.proposal.title))
    except ValueError as error:
        return Choice(f"Не удалось построить путь: {error}.")

    try:
        exists = await obsidian.note_exists(path)
    except (ObsidianError, aiohttp.ClientError) as error:
        return Choice(_obsidian_error_text(error))

    if exists:
        return Choice(
            f"Заметка уже существует: «{path}».\n\nДописать текст в неё?",
            _append_keyboard(),
            replace(action, stage="exists", path=path),
        )

    if settings.agent_confirmation == "always":
        return Choice(
            f"Создать заметку «{path}»?",
            _confirm_keyboard(),
            replace(action, stage="confirm", path=path),
        )

    return Choice(await _apply(obsidian, action.proposal, path))


async def _show_choice(
    callback: CallbackQuery, memory: ChatMemory, choice: Choice
) -> None:
    chat_id = callback.message.chat.id
    if choice.action is None:
        memory.drop_action(chat_id)
    else:
        memory.set_action(chat_id, choice.action, callback.message.message_id)
    await callback.message.edit_text(choice.text, reply_markup=choice.keyboard)


async def _apply(
    obsidian: ObsidianClient, proposal: Proposal, path: str
) -> str:
    """Выполняет предложение и возвращает текст результата для пользователя."""
    if proposal.kind == "create":
        try:
            written = await obsidian.write_note(path, proposal.content)
        except (ObsidianError, aiohttp.ClientError) as error:
            return _obsidian_error_text(error)
        if written:
            return f"Заметка создана: {path}"
        return (
            f"Dry-run: заметка «{path}» не записана. "
            "Включите OBSIDIAN_DRY_RUN=false для реальной записи."
        )

    try:
        current = await obsidian.read_note(path)
        if proposal.kind == "append":
            updated = append_to_note(current, proposal.content)
        elif proposal.mode == "replace":
            updated = replace_section(current, proposal.heading, proposal.content)
        else:
            updated = append_to_section(current, proposal.heading, proposal.content)
        written = await obsidian.write_note(path, updated, overwrite=True)
    except ValueError as error:
        return f"Не удалось изменить заметку: {error}."
    except (ObsidianError, aiohttp.ClientError) as error:
        return _obsidian_error_text(error)

    if written:
        return f"Изменения сохранены: {path}"
    return f"Dry-run: изменения в «{path}» не записаны."


def _needs_confirm(settings: Settings, kind: str) -> bool:
    if settings.agent_confirmation == "always":
        return True
    if settings.agent_confirmation == "never":
        return False
    return kind in ("append", "section")


def _preview(text: str) -> str:
    text = text.strip()
    if len(text) <= PREVIEW_CHARS:
        return text
    return text[:PREVIEW_CHARS] + "…"


def _proposal_text(proposal: Proposal) -> str:
    if proposal.kind == "create":
        return (
            f"Предлагаю заметку «{proposal.title}»:\n\n"
            f"{_preview(proposal.content)}\n\nКуда сохранить?"
        )
    if proposal.kind == "append":
        return (
            f"Дописать в «{proposal.path}»:\n\n"
            f"{_preview(proposal.content)}\n\nПодтвердить?"
        )
    verb = "Заменить" if proposal.mode == "replace" else "Дополнить"
    return (
        f"{verb} раздел «{proposal.heading}» в «{proposal.path}»:\n\n"
        f"{_preview(proposal.content)}\n\nПодтвердить?"
    )


def _folder_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    folder = settings.agent_default_folder
    label = "Inbox" if folder == "Inbox" else f"Inbox ({_button_label(folder)})"
    rows = [[InlineKeyboardButton(text=label, callback_data="act:inbox")]]
    if settings.agent_auto_folder:
        rows.append(
            [InlineKeyboardButton(text="Автоопределение", callback_data="act:auto")]
        )
    rows.append([InlineKeyboardButton(text="Другая папка", callback_data="act:other")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _inbox_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Inbox", callback_data="act:inbox")],
            [InlineKeyboardButton(text="Другая папка", callback_data="act:other")],
            [InlineKeyboardButton(text="Отмена", callback_data="act:no")],
        ]
    )


def _folder_choices(action: PendingAction) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=_button_label(folder), callback_data=f"act:folder:{index}"
            )
        ]
        for index, folder in enumerate(action.folders[:MAX_FOLDER_CHOICES])
    ]
    rows.append([InlineKeyboardButton(text="Inbox", callback_data="act:inbox")])
    rows.append([InlineKeyboardButton(text="Другая папка", callback_data="act:other")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="act:no")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подтвердить", callback_data="act:yes"),
                InlineKeyboardButton(text="Отмена", callback_data="act:no"),
            ]
        ]
    )


def _append_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Дописать", callback_data="act:append"),
                InlineKeyboardButton(text="Отмена", callback_data="act:no"),
            ]
        ]
    )


def _button_label(text: str) -> str:
    return text if len(text) <= MAX_BUTTON_CHARS else text[: MAX_BUTTON_CHARS - 1] + "…"


def _llm_error_text(error: LLMError) -> str:
    message = str(error)
    if "HTTP 429" in message:
        return "Модель сейчас перегружена (лимит запросов). Попробуйте позже."
    if "HTTP 401" in message or "HTTP 403" in message:
        return "Модель отклонила ключ. Проверьте LLM_API_KEY в .env."
    return "Не удалось получить ответ от модели. Попробуйте позже."


def _obsidian_error_text(error: Exception) -> str:
    status = getattr(error, "status", None)
    if status in (401, 403):
        return "Obsidian отклонил ключ. Проверьте OBSIDIAN_API_KEY в .env."
    if status == 404:
        return "Заметка не найдена."
    if status == 409:
        return "Заметка уже существует. Перезапись запрещена."
    if status == 429:
        return "Obsidian ограничил частоту запросов. Попробуйте позже."
    if isinstance(status, int) and status >= 500:
        return f"Obsidian вернул ошибку {status}. Проверьте, что приложение запущено."
    if isinstance(error, aiohttp.ClientError):
        return (
            "Не удалось связаться с Obsidian. Проверьте, что он запущен "
            "и Local REST API включён."
        )
    return f"Ошибка Obsidian: {error}"


async def run_bot(settings: Settings) -> None:
    """Запускает бота и блокируется до остановки (Ctrl+C)."""
    llm = LLMClient(settings)
    obsidian = ObsidianClient(settings)
    agent = Agent(llm, obsidian, settings)
    memory = ChatMemory()
    bot = Bot(token=settings.telegram_bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    try:
        me = await bot.get_me()
        log.info("Бот @%s запущен, ожидаю сообщения", me.username)
        # На случай, если для токена ранее был настроен webhook.
        await bot.delete_webhook(drop_pending_updates=True)
        # Объекты попадают в обработчики как keyword-аргументы (workflow data).
        await dispatcher.start_polling(
            bot,
            agent=agent,
            obsidian=obsidian,
            memory=memory,
            settings=settings,
        )
    finally:
        await llm.close()
        await obsidian.close()
        await bot.session.close()
        log.info("Бот остановлен")
