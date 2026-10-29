"""Telegram-бот: команды, подтверждения и цикл long polling."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app.agent import Agent, Proposal
from app.audit import AuditLog, AuditedBackend
from app.config import PROJECT_ROOT, Settings
from app.links import (
    add_links,
    duplicate_titles,
    existing_links,
    is_linked,
    link_label,
    mention_paths,
)
from app.llm import LLMAllExhaustedError, LLMError, LLMPool, LLMStatus
from app.notes import (
    append_to_note,
    append_to_section,
    build_path,
    clean_folder,
    contains_text,
    create_section,
    find_section,
    folder_from_text,
    remove_text,
    replace_section,
    sections,
    split_folder_title,
    validate_path,
)
from app.obsidian import (
    ObsidianBackend,
    ObsidianError,
    create_obsidian_client,
)
from app.templates import MAX_TEMPLATE_CHARS, template_name, template_paths

log = logging.getLogger(__name__)

router = Router(name="main")

MAX_HISTORY_MESSAGES = 20
PREVIEW_CHARS = 700
MAX_FOLDER_CHOICES = 3
MAX_TEMPLATE_CHOICES = 6
MAX_BUTTON_CHARS = 40
BULK_LINK_NOTES = 300
BULK_LINK_EXAMPLES = 5

BTN_NEW = "🆕 Новый диалог"
BTN_HELP = "📖 Справка"
BTN_START = "🔄 Начать заново"
BTN_STAY = "💬 Остаться в этом чате"
BTN_BACK = "⬅️ Назад"

BTN_NOTE_NEW = "📝 Новая заметка"
BTN_NOTE_APPEND = "➕ Дописать"
BTN_NOTE_EDIT = "✏️ Править раздел"
BTN_NOTE_FIND = "🔎 Найти"
BTN_FOLDERS = "🗂 Папки"
BTN_LINKS = "🔗 Связи во всех заметках"

# Страница 1: вход в чат. Страница 2: сценарии работы с заметками.
KEYBOARD_HOME = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_STAY)],
        [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_START)],
        [KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
)

KEYBOARD_SCENARIOS = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_NOTE_NEW), KeyboardButton(text=BTN_NOTE_APPEND)],
        [KeyboardButton(text=BTN_NOTE_EDIT), KeyboardButton(text=BTN_NOTE_FIND)],
        [KeyboardButton(text=BTN_FOLDERS)],
        [KeyboardButton(text=BTN_LINKS)],
        [KeyboardButton(text=BTN_BACK)],
    ],
    resize_keyboard=True,
)

COMMANDS = [
    BotCommand(command="start", description="Начать заново"),
    BotCommand(command="help", description="Справка"),
    BotCommand(command="new", description="Новый диалог"),
    BotCommand(command="folders", description="Папки хранилища"),
    BotCommand(command="links", description="Связи во всех заметках"),
    BotCommand(command="rag", description="Состояние семантического поиска"),
    BotCommand(command="reindex", description="Обновить RAG-индекс"),
    BotCommand(command="audit", description="Журнал изменений"),
    BotCommand(command="llm", description="Состояние LLM-пула"),
    BotCommand(command="llm_reset", description="Сбросить лимиты LLM"),
]

START_TEXT = (
    "Привет! Я ваш помощник по Obsidian.\n\n"
    "Могу найти и прочитать заметку, сохранить новую мысль, дописать в нужный "
    "раздел или поправить его, создать раздел, поработать с папками, "
    "переместить, скопировать или удалить заметку. Говорите обычными словами — "
    "разберусь.\n\n"
    "/new или /start начнут разговор с чистого листа."
)

HELP_TEXT = (
    "Внизу две страницы кнопок.\n\n"
    "Первая: «Остаться в этом чате» — перейти к работе с заметками, "
    "«Новый диалог» и «Начать заново» — очистить контекст и открыть сценарии, "
    "«Справка» — эта подсказка.\n\n"
    "Вторая страница — сценарии, они идут по шагам и не требуют, чтобы модель "
    "угадывала смысл:\n"
    "• 📝 Новая заметка — выберите шаблон (если он есть), пришлите текст, "
    "потом папку;\n"
    "• ➕ Дописать — найдите заметку, выберите раздел, пришлите текст;\n"
    "• ✏️ Править раздел — заменить раздел целиком;\n"
    "• 🔎 Найти — показать заметку;\n"
    "• 🗂 Папки — список папок хранилища;\n"
    "• 🔗 Связи во всех заметках — план и массовая простановка связей;\n"
    "• ⬅️ Назад — вернуться на первую страницу.\n\n"
    "Обычными словами можно и больше:\n"
    "• «создай в заметке … раздел …» — новый раздел;\n"
    "• «создай папку …», «переименуй папку … в …», «удали папку …»;\n"
    "• «перемести заметку … в …», «скопируй заметку … в …», «удали заметку …»;\n"
    "• «удали из заметки … строку/ссылку/абзац» — удаление фрагмента текста.\n"
    "Опасные действия бот показывает превью и выполняет только после "
    "подтверждения.\n\n"
    "Связи появляются в разделе «Связи»: при создании заметки бот предлагает "
    "их кнопкой, а в существующей — по просьбе «свяжи заметку…».\n\n"
    "Поиск понимает смысл: «что я писал про сроки релиза» найдёт заметку, "
    "даже если таких слов в ней нет. /rag — состояние индекса, /reindex — "
    "пересобрать после больших правок. /audit — последние изменения.\n\n"
    "Команды: /start, /help, /new, /folders, /links, /rag, /reindex, /audit, "
    "/llm — состояние моделей и лимитов."
)


@dataclass
class PendingAction:
    """Предложение, которое ждёт решения пользователя."""

    proposal: Proposal
    stage: str  # folder | auto | confirm | exists | links
    path: str = ""
    folders: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    plan: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclass
class Flow:
    """Пошаговый сценарий, запущенный кнопкой (без участия модели)."""

    kind: str  # create | append | edit | find
    step: str  # template | text | note | pick_note | section | content
    path: str = ""
    section: str = ""
    candidates: tuple[str, ...] = ()
    sections: tuple[str, ...] = ()
    template: str = ""


class ChatMemory:
    """История диалога и ожидающие решения по `chat_id`.

    Живёт в памяти процесса, сбрасывается командами `/new` и `/start`;
    при перезапуске бота теряется.
    """

    def __init__(self, max_messages: int = MAX_HISTORY_MESSAGES) -> None:
        self._max_messages = max_messages
        self._messages: dict[int, list[dict[str, str]]] = {}
        self._actions: dict[int, tuple[int, PendingAction]] = {}
        self._flows: dict[int, Flow] = {}
        self._pages: dict[int, str] = {}
        self._operations: dict[int, str] = {}

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

    def set_flow(self, chat_id: int, flow: Flow) -> None:
        self._flows[chat_id] = flow

    def get_flow(self, chat_id: int) -> Flow | None:
        return self._flows.get(chat_id)

    def clear_flow(self, chat_id: int) -> None:
        self._flows.pop(chat_id, None)

    def set_page(self, chat_id: int, page: str) -> None:
        """Запоминает, какая страница клавиатуры сейчас у пользователя."""
        self._pages[chat_id] = page

    def get_page(self, chat_id: int) -> str:
        return self._pages.get(chat_id, "home")

    def clear(self, chat_id: int) -> None:
        self._messages.pop(chat_id, None)
        self._actions.pop(chat_id, None)
        self._flows.pop(chat_id, None)
        self._pages.pop(chat_id, None)

    def begin_operation(self, chat_id: int, name: str) -> bool:
        """Долгая операция одна на чат: повторный запуск не начнётся молча."""
        if chat_id in self._operations:
            return False
        self._operations[chat_id] = name
        return True

    def end_operation(self, chat_id: int) -> None:
        self._operations.pop(chat_id, None)

    def current_operation(self, chat_id: int) -> str:
        return self._operations.get(chat_id, "")


def _keyboard_for(chat_id: int, memory: ChatMemory) -> ReplyKeyboardMarkup:
    """Клавиатура текущей страницы чата."""
    return (
        KEYBOARD_SCENARIOS
        if memory.get_page(chat_id) == "scenarios"
        else KEYBOARD_HOME
    )


@router.message(CommandStart())
async def cmd_start(message: Message, memory: ChatMemory) -> None:
    memory.clear(message.chat.id)
    memory.set_page(message.chat.id, "home")
    await message.answer(START_TEXT, reply_markup=KEYBOARD_HOME)


@router.message(Command("help"))
async def cmd_help(message: Message, memory: ChatMemory) -> None:
    await message.answer(
        HELP_TEXT, reply_markup=_keyboard_for(message.chat.id, memory)
    )


@router.message(Command("new"))
async def cmd_new(message: Message, memory: ChatMemory) -> None:
    memory.clear(message.chat.id)
    memory.set_page(message.chat.id, "scenarios")
    await message.answer(
        "Новый диалог начат: предыдущие сообщения забыты.",
        reply_markup=KEYBOARD_SCENARIOS,
    )


@router.message(Command("llm_reset"))
async def cmd_llm_reset(
    message: Message, llm: LLMPool, memory: ChatMemory
) -> None:
    """Сбрасывает лимиты и возвращает пул к лучшей модели/первому аккаунту."""
    llm.reset(reason="команда /llm_reset")
    status = llm.status()
    await message.answer(
        "LLM: лимиты сброшены. Снова работают все пары, старт — "
        f"{status.active_model} / {status.active_account}.",
        reply_markup=_keyboard_for(message.chat.id, memory),
    )


@router.message(Command("llm"))
async def cmd_llm(message: Message, llm: LLMPool, memory: ChatMemory) -> None:
    """Показывает, какая модель/аккаунт активны и когда будет автосброс."""
    await message.answer(
        _llm_status_text(llm.status()),
        reply_markup=_keyboard_for(message.chat.id, memory),
    )


@router.message(Command("rag"))
async def cmd_rag(
    message: Message, memory: ChatMemory, semantic: Any
) -> None:
    """Состояние семантического поиска: модель, размер индекса, когда обновлён."""
    await message.answer(
        _rag_status_text(semantic),
        reply_markup=_keyboard_for(message.chat.id, memory),
    )


@router.message(Command("reindex"))
async def cmd_reindex(
    message: Message,
    memory: ChatMemory,
    obsidian: ObsidianBackend,
    semantic: Any,
) -> None:
    """Обновляет RAG-индекс: первый запуск строит его с нуля."""
    keyboard = _keyboard_for(message.chat.id, memory)
    if semantic is None:
        await message.answer(_rag_status_text(None), reply_markup=keyboard)
        return
    if not memory.begin_operation(message.chat.id, "индексация"):
        await message.answer(
            "Уже выполняется: "
            f"{memory.current_operation(message.chat.id)}. Дождитесь окончания.",
            reply_markup=keyboard,
        )
        return

    status = await message.answer(
        "Обновляю RAG-индекс: читаю заметки и считаю эмбеддинги…"
    )
    try:
        stats = await semantic.refresh(obsidian)
    except Exception as error:  # индекс необязателен: бот продолжает работать
        log.error("RAG: индекс не обновлён: %s", error)
        await status.edit_text(
            f"Не удалось обновить индекс: {error}", reply_markup=keyboard
        )
        return
    finally:
        memory.end_operation(message.chat.id)

    await status.edit_text(
        "RAG-индекс готов.\n"
        f"Заметок: {stats.notes}, фрагментов: {stats.chunks}.\n"
        f"Обновлено: {stats.updated}, без изменений: {stats.skipped}, "
        f"удалено: {stats.removed}, ошибок: {stats.failed}.",
        reply_markup=keyboard,
    )


def _rag_status_text(semantic: Any) -> str:
    """Сводка RAG для пользователя."""
    if semantic is None:
        return (
            "Семантический поиск выключен: нет RAG-зависимостей или "
            "RAG_ENABLED=false. Установите `requirements-rag.txt` "
            "и перезапустите бота."
        )
    lines = [
        f"RAG: модель {semantic.model_name}",
        f"В индексе: заметок — {semantic.notes}, фрагментов — {semantic.chunks}",
    ]
    if semantic.updated_at:
        lines.append(f"Обновлён: {semantic.updated_at}")
    else:
        lines.append("Индекс ещё не построен — выполните /reindex")
    return "\n".join(lines)


@router.message(Command("audit"))
async def cmd_audit(
    message: Message, memory: ChatMemory, audit: AuditLog
) -> None:
    """Последние изменения хранилища из журнала."""
    entries = await audit.tail(5)
    await message.answer(
        _audit_text(entries),
        reply_markup=_keyboard_for(message.chat.id, memory),
    )


def _audit_text(entries: list[dict[str, Any]]) -> str:
    """Короткая сводка последних записей журнала."""
    if not entries:
        return "Журнал пуст: изменений ещё не было."
    lines = ["Последние изменения:"]
    for entry in entries:
        stamp = str(entry.get("ts", ""))[11:16]
        action = entry.get("action", "?")
        target = entry.get("path") or "—"
        status = entry.get("status", "")
        lines.append(f"• {stamp} — {action}: {target} ({status})")
    lines.append("\nЖурнал лежит в `state/audit/`.")
    return "\n".join(lines)


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    """Последний рубеж: необработанная ошибка не должна убивать диалог."""
    log.error("Необработанная ошибка: %s", event.exception, exc_info=event.exception)
    message = getattr(event.update, "message", None)
    if message is None:
        return True
    try:
        await message.answer(
            "Что-то пошло не так при обработке запроса. "
            "Попробуйте ещё раз или начните новый диалог командой /new."
        )
    except TelegramAPIError as send_error:
        log.warning("Не удалось сообщить об ошибке: %s", send_error)
    return True


# Кнопки клавиатуры дублируют команды точным совпадением текста: модель
# не участвует, поэтому нажатие всегда означает одно и то же.
@router.message(F.text == BTN_START)
async def btn_start(message: Message, memory: ChatMemory) -> None:
    """Начать заново: чистим диалог и уходим к сценариям."""
    memory.clear(message.chat.id)
    memory.set_page(message.chat.id, "scenarios")
    await message.answer(
        "Начали заново. Выберите сценарий ниже.",
        reply_markup=KEYBOARD_SCENARIOS,
    )


@router.message(F.text == BTN_HELP)
async def btn_help(message: Message, memory: ChatMemory) -> None:
    await cmd_help(message, memory)


@router.message(F.text == BTN_NEW)
async def btn_new(message: Message, memory: ChatMemory) -> None:
    await cmd_new(message, memory)


@router.message(F.text == BTN_STAY)
async def btn_stay(message: Message, memory: ChatMemory) -> None:
    """Остаёмся в текущем диалоге и переходим к сценариям заметок."""
    memory.set_page(message.chat.id, "scenarios")
    await message.answer(
        "Чем займёмся? Выберите сценарий ниже.", reply_markup=KEYBOARD_SCENARIOS
    )


@router.message(F.text == BTN_BACK)
async def btn_back(message: Message, memory: ChatMemory) -> None:
    """Возврат на первую страницу; незавершённый сценарий отменяется."""
    memory.clear_flow(message.chat.id)
    memory.set_page(message.chat.id, "home")
    await message.answer("Главное меню.", reply_markup=KEYBOARD_HOME)


@router.message(F.text == BTN_NOTE_NEW)
async def btn_note_new(
    message: Message,
    memory: ChatMemory,
    template_files: tuple[str, ...],
) -> None:
    """Создание заметки: сначала шаблон (если есть), затем текст."""
    chat_id = message.chat.id
    if not template_files:
        memory.set_flow(chat_id, Flow("create", "text"))
        await message.answer(
            "Пришлите текст заметки — первую строку сделаю заголовком.",
            reply_markup=_flow_cancel_keyboard(),
        )
        return

    flow = Flow("create", "template", candidates=template_files)
    memory.set_flow(chat_id, flow)
    rows = [
        [InlineKeyboardButton(text="Без шаблона", callback_data="flow:template:-1")]
    ]
    rows.extend(
        [
            InlineKeyboardButton(
                text=_button_label(template_name(path)),
                callback_data=f"flow:template:{index}",
            )
        ]
        for index, path in enumerate(template_files)
    )
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="flow:cancel")])
    await message.answer(
        "Оформить по шаблону?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.message(F.text == BTN_NOTE_APPEND)
async def btn_note_append(message: Message, memory: ChatMemory) -> None:
    memory.set_flow(message.chat.id, Flow("append", "note"))
    await message.answer(
        "В какую заметку дописать? Напишите часть названия или слово из текста.",
        reply_markup=_flow_cancel_keyboard(),
    )


@router.message(F.text == BTN_NOTE_EDIT)
async def btn_note_edit(message: Message, memory: ChatMemory) -> None:
    memory.set_flow(message.chat.id, Flow("edit", "note"))
    await message.answer(
        "Какую заметку правим? Напишите часть названия.",
        reply_markup=_flow_cancel_keyboard(),
    )


@router.message(F.text == BTN_NOTE_FIND)
async def btn_note_find(message: Message, memory: ChatMemory) -> None:
    memory.set_flow(message.chat.id, Flow("find", "note"))
    await message.answer(
        "Что ищем? Напишите слово или часть названия.",
        reply_markup=_flow_cancel_keyboard(),
    )


@router.message(Command("folders"))
@router.message(F.text == BTN_FOLDERS)
async def cmd_folders(
    message: Message, memory: ChatMemory, obsidian: ObsidianBackend
) -> None:
    """Список папок и подсказка, как ими управлять словами."""
    memory.clear_flow(message.chat.id)
    memory.set_page(message.chat.id, "scenarios")
    try:
        folders = await obsidian.list_folders()
    except (ObsidianError, aiohttp.ClientError) as error:
        await message.answer(
            _obsidian_error_text(error),
            reply_markup=_keyboard_for(message.chat.id, memory),
        )
        return

    if not folders:
        text = "Папок пока нет."
    else:
        shown: list[str] = []
        used = 0
        for folder in folders:
            if len(shown) >= 50 or used + len(folder) + 3 > 3500:
                break
            shown.append(folder)
            used += len(folder) + 3
        text = "Папки хранилища:\n" + "\n".join(f"• {folder}" for folder in shown)
        if len(folders) > len(shown):
            text += f"\n… и ещё {len(folders) - len(shown)}"
    text += (
        "\n\nМожно словами: «создай папку …», «переименуй папку … в …», "
        "«удали папку …»."
    )
    await message.answer(
        text, reply_markup=_keyboard_for(message.chat.id, memory)
    )


@router.message(Command("links"))
@router.message(F.text == BTN_LINKS)
async def cmd_links(
    message: Message,
    memory: ChatMemory,
    settings: Settings,
    obsidian: ObsidianBackend,
) -> None:
    """Массовая простановка связей: план по всем заметкам (FR-06)."""
    memory.clear_flow(message.chat.id)
    memory.set_page(message.chat.id, "scenarios")
    await _offer_bulk(message, memory, settings, obsidian)


@router.message(F.text)
async def answer_text(
    message: Message,
    agent: Agent,
    memory: ChatMemory,
    settings: Settings,
    obsidian: ObsidianBackend,
) -> None:
    chat_id = message.chat.id
    text = message.text or ""

    flow = memory.get_flow(chat_id)
    if flow is not None:
        await _handle_flow(message, agent, memory, settings, obsidian, flow)
        return

    pending = memory.get_action_any(chat_id)
    if pending is not None and pending.stage in ("folder", "folder_input"):
        await _reply_folder(message, memory, settings, obsidian, pending, text)
        return

    messages = [
        {"role": "system", "content": agent.system_prompt},
        *memory.get(chat_id),
        {"role": "user", "content": text},
    ]
    bot = getattr(message, "bot", None)
    if bot is not None:
        try:
            await bot.send_chat_action(chat_id, "typing")
        except TelegramAPIError as error:
            log.debug("Не удалось отправить статус «печатает»: %s", error)

    try:
        result = await agent.handle(messages)
    except LLMError as error:
        log.error("LLM не ответила: %s", error)
        await message.answer(
            _llm_error_text(error), reply_markup=_keyboard_for(chat_id, memory)
        )
        return

    if result.proposal is not None:
        memory.add(chat_id, text, _proposal_text(result.proposal))
        await _offer(message, agent, memory, settings, obsidian, result.proposal)
        return

    if result.clarify:
        memory.set_page(chat_id, "scenarios")
        memory.add(chat_id, text, result.text)
        await message.answer(
            result.text
            + "\n\nВыберите действие кнопкой ниже или сформулируйте иначе.",
            reply_markup=KEYBOARD_SCENARIOS,
        )
        return

    memory.add(chat_id, text, result.text)
    await message.answer(
        result.text or "Модель вернула пустой ответ.",
        reply_markup=_keyboard_for(chat_id, memory)
    )


async def _offer(
    message: Message,
    agent: Agent,
    memory: ChatMemory,
    settings: Settings,
    obsidian: ObsidianBackend,
    proposal: Proposal,
) -> None:
    chat_id = message.chat.id

    if proposal.kind == "bulk":
        await _offer_bulk(message, memory, settings, obsidian)
        return

    if proposal.kind == "links":
        links = tuple(await _proposal_links(agent, obsidian, proposal))
        if not links:
            memory.drop_action(chat_id)
            await message.answer("Новых связей не нашёл: всё уже связано.")
            return
        if settings.agent_confirmation == "never":
            memory.drop_action(chat_id)
            await message.answer(
                await _apply(obsidian, proposal, proposal.path, links)
            )
            return
        sent = await message.answer(
            _links_text(proposal.path, links), reply_markup=_links_keyboard()
        )
        memory.set_action(
            chat_id,
            PendingAction(proposal, "links", path=proposal.path, links=links),
            sent.message_id,
        )
        return

    if proposal.kind == "create":
        folders = await _known_folders(obsidian)
        proposal = _with_folder(proposal, folders)
        links = (
            tuple(await _proposal_links(agent, obsidian, proposal))
            if settings.agent_auto_link
            else ()
        )
        suggestions = await _folder_suggestions(agent, settings, proposal, folders)
        sent = await message.answer(
            _proposal_text(proposal, links),
            reply_markup=_folder_keyboard(settings, suggestions),
        )
        memory.set_action(
            chat_id,
            PendingAction(
                proposal,
                "folder",
                folders=tuple(suggestions),
                links=links,
            ),
            sent.message_id,
        )
        return

    if proposal.kind == "text_remove":
        try:
            current = await obsidian.read_note(proposal.path)
            remove_text(current, proposal.content)
        except ValueError as error:
            memory.drop_action(chat_id)
            await message.answer(
                f"Не нашёл этот текст в заметке: {error}. "
                "Проверьте точную формулировку — возможно, ссылка или строка "
                "называется иначе."
            )
            return
        except (ObsidianError, aiohttp.ClientError) as error:
            memory.drop_action(chat_id)
            await message.answer(_obsidian_error_text(error))
            return

    links: tuple[str, ...] = ()
    if settings.agent_auto_link and proposal.kind in (
        "append",
        "section",
        "section_create",
    ):
        links = tuple(await _proposal_links(agent, obsidian, proposal))

    if _needs_confirm(settings, proposal.kind):
        sent = await message.answer(
            _proposal_text(proposal, links), reply_markup=_confirm_keyboard()
        )
        memory.set_action(
            chat_id,
            PendingAction(proposal, "confirm", path=proposal.path, links=links),
            sent.message_id,
        )
        return

    memory.drop_action(chat_id)
    await message.answer(await _apply(obsidian, proposal, proposal.path, links))


async def _proposal_links(
    agent: Agent, obsidian: ObsidianBackend, proposal: Proposal
) -> list[str]:
    """Кандидаты связей для предложения: создание, правка или готовая заметка."""
    if proposal.kind == "create":
        text = proposal.content
        title = proposal.title
        exclude = ""
    else:
        try:
            current = await obsidian.read_note(proposal.path)
        except (ObsidianError, aiohttp.ClientError) as error:
            log.warning("Не удалось прочитать заметку для связей: %s", error)
            return []
        text = (
            current
            if proposal.kind == "links"
            else f"{current}\n\n{proposal.content}"
        )
        title = proposal.path.rsplit("/", 1)[-1].removesuffix(".md")
        exclude = proposal.path

    try:
        return await agent.suggest_links(text, title=title, exclude_path=exclude)
    except (ObsidianError, aiohttp.ClientError) as error:
        log.warning("Не удалось подобрать связи: %s", error)
        return []


async def _known_folders(obsidian: ObsidianBackend) -> list[str]:
    """Папки хранилища для подсказок; недоступное хранилище — не ошибка."""
    try:
        return await obsidian.list_folders()
    except (ObsidianError, aiohttp.ClientError) as error:
        log.warning("Не удалось получить папки хранилища: %s", error)
        return []


def _with_folder(proposal: Proposal, folders: list[str]) -> Proposal:
    """Папка из аргумента модели или из заголовка («Люди/Имя» → папка «Люди»)."""
    folder = clean_folder(proposal.folder) if proposal.folder else ""
    title = proposal.title

    if folder:
        prefix = f"{folder}/"
        if title.casefold().startswith(prefix.casefold()):
            title = title[len(prefix) :].strip() or proposal.title
    else:
        title, found = split_folder_title(title, folders)
        folder = clean_folder(found)

    if not title.strip():
        title = proposal.title
    if title == proposal.title and folder == proposal.folder:
        return proposal
    return replace(proposal, title=title, folder=folder)


async def _folder_suggestions(
    agent: Agent,
    settings: Settings,
    proposal: Proposal,
    folders: list[str],
) -> list[str]:
    """Варианты папок для кнопок: сначала названная, иначе выбор модели."""
    if proposal.folder:
        return [proposal.folder]
    if not settings.agent_auto_folder:
        return []
    try:
        return await agent.suggest_folders(proposal.title, proposal.content, folders)
    except LLMError as error:
        log.warning("Не удалось подобрать папку: %s", error)
        return _guess_folders(proposal.title, proposal.content, folders)


async def _offer_bulk(
    message: Message,
    memory: ChatMemory,
    settings: Settings,
    obsidian: ObsidianBackend,
) -> None:
    """FR-06: план связей по всему хранилищу, подтверждение и отчёт."""
    chat_id = message.chat.id
    if not memory.begin_operation(chat_id, "сбор плана связей"):
        await message.answer(
            "Уже выполняется: "
            f"{memory.current_operation(chat_id)}. Дождитесь окончания."
        )
        return

    status = await message.answer("Сканирую хранилище и собираю план связей…")
    try:
        plan, errors, total, checked = await _bulk_plan(obsidian, settings)
    except (ObsidianError, aiohttp.ClientError) as error:
        await status.edit_text(_obsidian_error_text(error))
        return
    finally:
        memory.end_operation(chat_id)

    if not plan:
        memory.drop_action(chat_id)
        if errors:
            await status.edit_text(
                "Не удалось прочитать заметки:\n"
                + "\n".join(f"• {error}" for error in errors[:BULK_LINK_EXAMPLES])
            )
        else:
            await status.edit_text(
                "Новых связей нет: в заметках не нашлось упоминаний других заметок."
            )
        return

    text = _bulk_plan_text(plan, total, checked, errors)
    if settings.agent_confirmation == "never":
        memory.drop_action(chat_id)
        if not memory.begin_operation(chat_id, "простановка связей"):
            await status.edit_text(
                "Уже выполняется: "
                f"{memory.current_operation(chat_id)}. Дождитесь окончания."
            )
            return
        try:
            result = await _apply_bulk(obsidian, plan)
        finally:
            memory.end_operation(chat_id)
        await status.edit_text(f"{text}\n\n{result}")
        return

    await status.edit_text(f"{text}\n\nПрименить?", reply_markup=_bulk_keyboard())
    memory.set_action(
        chat_id,
        PendingAction(Proposal(kind="bulk"), "confirm", plan=tuple(plan)),
        status.message_id,
    )


async def _bulk_plan(
    obsidian: ObsidianBackend, settings: Settings
) -> tuple[list[tuple[str, tuple[str, ...]]], list[str], int, int]:
    """Строит план: каким заметкам какие ссылки добавить (без записи).

    Шаблоны и заметка стиля пропускаются: их содержимое копируется в новые
    заметки, и связи в них стали бы частью шаблона.
    """
    notes = await obsidian.list_notes()
    scanned = [
        note
        for note in notes[:BULK_LINK_NOTES]
        if not _is_template_note(note, settings)
    ]
    duplicates = duplicate_titles(scanned)
    plan: list[tuple[str, tuple[str, ...]]] = []
    errors: list[str] = []

    for path in scanned:
        try:
            text = await obsidian.read_note(path)
        except (ObsidianError, aiohttp.ClientError) as error:
            errors.append(f"{path}: {error}")
            continue

        linked = existing_links(text)
        targets: list[str] = []
        for candidate in mention_paths(text, scanned, exclude=path, strict=True):
            label = link_label(candidate, duplicates)
            if is_linked(label, linked) or label in targets:
                continue
            targets.append(label)
            if len(targets) >= settings.agent_link_limit:
                break
        if targets:
            plan.append((path, tuple(targets)))
    return plan, errors, len(notes), len(scanned)


def _is_template_note(path: str, settings: Settings) -> bool:
    """Шаблоны и заметка стиля не участвуют в массовой простановке связей."""
    value = path.strip("/").casefold()
    style = settings.agent_style_note.strip("/").casefold()
    if style and value == style:
        return True
    folder = settings.agent_template_folder.strip("/").casefold()
    return bool(folder) and value.startswith(f"{folder}/")


def _bulk_plan_text(
    plan: list[tuple[str, tuple[str, ...]]],
    total: int,
    checked: int,
    errors: list[str],
) -> str:
    """Сводка плана: сколько заметок, сколько ссылок, примеры."""
    links_count = sum(len(targets) for _, targets in plan)
    lines = [
        "План связей:",
        f"• заметок в хранилище — {total}",
        f"• проверено — {checked}",
        f"• изменятся — {len(plan)}",
        f"• новых ссылок — {links_count}",
    ]
    if total > checked:
        lines.append(f"• пропущено — {total - checked} (шаблоны или лимит)")
    for path, targets in plan[:BULK_LINK_EXAMPLES]:
        links = ", ".join(f"[[{target}]]" for target in targets)
        lines.append(f"• {path}: {links}")
    if len(plan) > BULK_LINK_EXAMPLES:
        lines.append(f"… и ещё {len(plan) - BULK_LINK_EXAMPLES} заметок")
    if errors:
        lines.append("")
        lines.append(f"Не прочитаны ({len(errors)}):")
        lines.extend(f"• {error}" for error in errors[:BULK_LINK_EXAMPLES])
    return "\n".join(lines)


async def _apply_bulk(
    obsidian: ObsidianBackend, plan: tuple[tuple[str, tuple[str, ...]], ...]
) -> str:
    """Применяет план по заметке за раз: ошибки собираются, удачные пишутся."""
    changed = 0
    added = 0
    failed: list[str] = []
    dry_run = False

    for path, targets in plan:
        try:
            text = await obsidian.read_note(path)
            updated = add_links(text, list(targets))
            if updated == text:
                continue
            if await obsidian.write_note(path, updated, overwrite=True):
                changed += 1
                added += len(targets)
            else:
                dry_run = True
        except (ObsidianError, aiohttp.ClientError) as error:
            failed.append(f"{path}: {error}")

    if dry_run:
        links_count = sum(len(targets) for _, targets in plan)
        return (
            f"Dry-run: ничего не записано ({len(plan)} заметок, "
            f"{links_count} ссылок в плане)."
        )

    lines = [f"Готово: изменено заметок — {changed}, добавлено ссылок — {added}."]
    if failed:
        lines.append(f"Не удалось ({len(failed)}):")
        lines.extend(f"• {error}" for error in failed[:BULK_LINK_EXAMPLES])
    return "\n".join(lines)


def _flow_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="flow:cancel")]
        ]
    )


def _section_text(note: str, heading: str) -> str:
    """Текст раздела по заголовку; пусто, если раздела нет."""
    span = find_section(note, heading)
    if span is None:
        return note[-2000:]
    lines = note.splitlines()
    return "\n".join(lines[span.start : span.end]).strip()


async def _handle_flow(
    message: Message,
    agent: Agent,
    memory: ChatMemory,
    settings: Settings,
    obsidian: ObsidianBackend,
    flow: Flow,
) -> None:
    """Продолжает пошаговый сценарий, запущенный кнопкой.

    Текст пользователя в любом сценарии проходит через модель: она
    переписывает его так, чтобы он хорошо лёг в заметку.
    """
    chat_id = message.chat.id
    text = (message.text or "").strip()
    keyboard = _keyboard_for(chat_id, memory)

    if flow.kind == "create" and flow.step == "text":
        memory.clear_flow(chat_id)
        if not text:
            await message.answer("Пусто — пришлите текст заметки.", reply_markup=keyboard)
            return
        try:
            title, content = await agent.compose_note(text, flow.template)
        except LLMError as error:
            log.error("Не удалось оформить заметку: %s", error)
            await message.answer(
                "Не смог обработать текст: " + _llm_error_text(error),
                reply_markup=keyboard,
            )
            return
        await _offer(
            message,
            agent,
            memory,
            settings,
            obsidian,
            Proposal(kind="create", title=title, content=content),
        )
        return

    if flow.step == "note":
        await _flow_search(message, memory, obsidian, flow, text)
        return

    if flow.step == "content":
        memory.clear_flow(chat_id)
        if not text:
            await message.answer("Пусто — пришлите текст.", reply_markup=keyboard)
            return
        try:
            note = await obsidian.read_note(flow.path)
        except (ObsidianError, aiohttp.ClientError) as error:
            await message.answer(_obsidian_error_text(error), reply_markup=keyboard)
            return

        section_text = _section_text(note, flow.section) if flow.section else note
        try:
            if flow.kind == "append":
                composed = await agent.compose_append(
                    flow.path, flow.section, section_text, text
                )
                proposal = Proposal(
                    kind="append",
                    path=flow.path,
                    heading=flow.section,
                    content=composed,
                )
            else:
                composed = await agent.compose_section(
                    flow.path, flow.section, section_text, text
                )
                proposal = Proposal(
                    kind="section",
                    path=flow.path,
                    heading=flow.section,
                    mode="replace",
                    content=composed,
                )
        except LLMError as error:
            log.error("Не удалось обработать текст: %s", error)
            await message.answer(
                "Не смог обработать текст: " + _llm_error_text(error),
                reply_markup=keyboard,
            )
            return

        await _offer(message, agent, memory, settings, obsidian, proposal)
        return

    await message.answer(
        "Выберите вариант кнопкой выше или нажмите «Отмена».",
        reply_markup=_flow_cancel_keyboard(),
    )


async def _flow_search(
    message: Message,
    memory: ChatMemory,
    obsidian: ObsidianBackend,
    flow: Flow,
    query: str,
) -> None:
    chat_id = message.chat.id
    if not query:
        await message.answer("Напишите слово или часть названия.")
        return

    try:
        hits = await obsidian.search_notes(query, limit=8)
    except (ObsidianError, aiohttp.ClientError) as error:
        memory.clear_flow(chat_id)
        await message.answer(
            _obsidian_error_text(error), reply_markup=_keyboard_for(chat_id, memory)
        )
        return

    paths = [hit["path"] for hit in hits if hit.get("path")]
    if not paths:
        memory.clear_flow(chat_id)
        await message.answer(
            "Ничего не нашёл. Попробуйте другие слова.",
            reply_markup=_keyboard_for(chat_id, memory),
        )
        return

    if len(paths) == 1:
        await _flow_pick(message, memory, obsidian, flow, paths[0])
        return

    flow.candidates = tuple(paths[:5])
    flow.step = "pick_note"
    memory.set_flow(chat_id, flow)
    rows = [
        [
            InlineKeyboardButton(
                text=_button_label(path), callback_data=f"flow:note:{index}"
            )
        ]
        for index, path in enumerate(flow.candidates)
    ]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="flow:cancel")])
    await message.answer(
        f"Нашёл {len(paths)}. Какая именно?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _flow_pick(
    message: Message,
    memory: ChatMemory,
    obsidian: ObsidianBackend,
    flow: Flow,
    path: str,
) -> None:
    chat_id = message.chat.id
    try:
        note = await obsidian.read_note(path)
    except (ObsidianError, aiohttp.ClientError) as error:
        memory.clear_flow(chat_id)
        await message.answer(
            _obsidian_error_text(error), reply_markup=_keyboard_for(chat_id, memory)
        )
        return

    if flow.kind == "find":
        memory.clear_flow(chat_id)
        await message.answer(
            f"«{path}»:\n\n{_preview(note)}",
            reply_markup=_keyboard_for(chat_id, memory),
        )
        return

    headings = sections(note)
    if flow.kind == "edit" and not headings:
        memory.clear_flow(chat_id)
        await message.answer(
            f"В заметке «{path}» нет разделов — править нечего. "
            "Можно дописать текст в конец.",
            reply_markup=_keyboard_for(chat_id, memory),
        )
        return

    flow.path = path
    flow.sections = tuple(headings[:8])
    flow.step = "section"
    memory.set_flow(chat_id, flow)

    rows = [
        [
            InlineKeyboardButton(
                text=_button_label(heading), callback_data=f"flow:section:{index}"
            )
        ]
        for index, heading in enumerate(flow.sections)
    ]
    if flow.kind == "append":
        rows.append(
            [InlineKeyboardButton(text="В конец заметки", callback_data="flow:end")]
        )
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="flow:cancel")])
    question = "Куда дописать?" if flow.kind == "append" else "Какой раздел заменить?"
    await message.answer(
        f"{question}\n\n«{path}»",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _active_flow(
    callback: CallbackQuery, memory: ChatMemory, step: str
) -> Flow | None:
    if callback.message is None:
        return None
    flow = memory.get_flow(callback.message.chat.id)
    if flow is None or flow.step != step:
        await callback.message.edit_text(
            "Этот шаг уже неактуален — начните заново кнопкой."
        )
        return None
    return flow


@router.callback_query(F.data == "flow:cancel")
async def on_flow_cancel(callback: CallbackQuery, memory: ChatMemory) -> None:
    await callback.answer("Отменено")
    if callback.message is None:
        return
    memory.clear_flow(callback.message.chat.id)
    await callback.message.edit_text("Отменил.")


@router.callback_query(F.data.startswith("flow:template:"))
async def on_flow_template(
    callback: CallbackQuery, memory: ChatMemory, obsidian: ObsidianBackend
) -> None:
    await callback.answer()
    flow = await _active_flow(callback, memory, "template")
    if flow is None:
        return

    index = int(callback.data.rsplit(":", 1)[1])
    template = ""
    if index >= 0:
        if index >= len(flow.candidates):
            await callback.message.edit_text("Этот вариант больше не актуален.")
            return
        try:
            template = await obsidian.read_note(flow.candidates[index])
        except (ObsidianError, aiohttp.ClientError) as error:
            memory.clear_flow(callback.message.chat.id)
            await callback.message.edit_text(_obsidian_error_text(error))
            return
        template = template[:MAX_TEMPLATE_CHARS]

    flow.template = template
    flow.step = "text"
    flow.candidates = ()
    memory.set_flow(callback.message.chat.id, flow)
    await callback.message.edit_text(
        "Пришлите текст заметки — оформлю его по выбранному шаблону."
        if template
        else "Пришлите текст заметки — первую строку сделаю заголовком."
    )


@router.callback_query(F.data.startswith("flow:note:"))
async def on_flow_note(
    callback: CallbackQuery, memory: ChatMemory, obsidian: ObsidianBackend
) -> None:
    await callback.answer()
    flow = await _active_flow(callback, memory, "pick_note")
    if flow is None:
        return

    index = int(callback.data.rsplit(":", 1)[1])
    if index >= len(flow.candidates):
        await callback.message.edit_text("Этот вариант больше не актуален.")
        return
    await _flow_pick(callback.message, memory, obsidian, flow, flow.candidates[index])


@router.callback_query(F.data.startswith("flow:section:"))
async def on_flow_section(callback: CallbackQuery, memory: ChatMemory) -> None:
    await callback.answer()
    flow = await _active_flow(callback, memory, "section")
    if flow is None:
        return

    index = int(callback.data.rsplit(":", 1)[1])
    if index >= len(flow.sections):
        await callback.message.edit_text("Этот вариант больше не актуален.")
        return

    flow.section = flow.sections[index]
    flow.step = "content"
    memory.set_flow(callback.message.chat.id, flow)
    verb = "дописать в" if flow.kind == "append" else "записать в"
    await callback.message.edit_text(
        f"Пришлите текст, который {verb} раздел «{flow.section}»."
    )


@router.callback_query(F.data == "flow:end")
async def on_flow_end(callback: CallbackQuery, memory: ChatMemory) -> None:
    await callback.answer()
    flow = await _active_flow(callback, memory, "section")
    if flow is None:
        return

    flow.section = ""
    flow.step = "content"
    memory.set_flow(callback.message.chat.id, flow)
    await callback.message.edit_text("Пришлите текст, который дописать в конец заметки.")


@router.callback_query(F.data == "act:inbox")
async def on_inbox(
    callback: CallbackQuery,
    memory: ChatMemory,
    settings: Settings,
    obsidian: ObsidianBackend,
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
        "Напишите папку, например «Люди» или «Проекты/2026». "
        "Если такой папки нет, спрошу, создавать ли её."
    )


@router.callback_query(F.data == "act:auto")
async def on_auto(
    callback: CallbackQuery,
    agent: Agent,
    memory: ChatMemory,
    settings: Settings,
    obsidian: ObsidianBackend,
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

    source = ""
    try:
        suggestions = await agent.suggest_folders(
            action.proposal.title, action.proposal.content, folders
        )
    except LLMError as error:
        log.warning("Не удалось подобрать папку: %s", error)
        guesses = _guess_folders(
            action.proposal.title, action.proposal.content, folders
        )
        if guesses:
            suggestions = guesses
            source = "Модель сейчас недоступна, папки подобраны по названию.\n\n"
        elif folders:
            suggestions = folders[:MAX_FOLDER_CHOICES]
            source = "Модель сейчас недоступна. Вот папки хранилища:\n\n"
        else:
            suggestions = []
            source = "Модель сейчас недоступна, а папок в хранилище пока нет.\n\n"

    if not suggestions:
        if folders:
            action.folders = tuple(folders[:MAX_FOLDER_CHOICES])
            memory.set_action(
                callback.message.chat.id, action, callback.message.message_id
            )
            await callback.message.edit_text(
                source + "Не понял, какая папка подходит. Выберите:",
                reply_markup=_folder_choices(action),
            )
        else:
            await callback.message.edit_text(
                source + "Куда сохранить заметку?",
                reply_markup=_inbox_keyboard(),
            )
        return

    action.folders = tuple(suggestions)
    memory.set_action(
        callback.message.chat.id, action, callback.message.message_id
    )
    await callback.message.edit_text(
        source + "Куда сохранить заметку?", reply_markup=_folder_choices(action)
    )


@router.callback_query(F.data.startswith("act:folder:"))
async def on_folder(
    callback: CallbackQuery,
    memory: ChatMemory,
    settings: Settings,
    obsidian: ObsidianBackend,
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
    callback: CallbackQuery, memory: ChatMemory, obsidian: ObsidianBackend
) -> None:
    await callback.answer("Записываю…")
    action = await _pending(callback, memory)
    if action is None:
        return
    memory.drop_action(callback.message.chat.id)
    await callback.message.edit_text(
        await _apply(obsidian, action.proposal, action.path, action.links)
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
    callback: CallbackQuery, memory: ChatMemory, obsidian: ObsidianBackend
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


@router.callback_query(F.data == "act:links:yes")
async def on_links_yes(
    callback: CallbackQuery, memory: ChatMemory, obsidian: ObsidianBackend
) -> None:
    """Подтверждение связей: запись с ними или без — одним действием."""
    await callback.answer("Добавляю связи…")
    action = await _pending(callback, memory)
    if action is None:
        return
    memory.drop_action(callback.message.chat.id)
    await callback.message.edit_text(
        await _apply(obsidian, action.proposal, action.path, action.links)
    )


@router.callback_query(F.data == "act:links:no")
async def on_links_no(
    callback: CallbackQuery, memory: ChatMemory, obsidian: ObsidianBackend
) -> None:
    await callback.answer()
    action = await _pending(callback, memory)
    if action is None:
        return
    memory.drop_action(callback.message.chat.id)
    await callback.message.edit_text(
        await _apply(obsidian, action.proposal, action.path)
    )


@router.callback_query(F.data == "act:bulk:yes")
async def on_bulk_yes(
    callback: CallbackQuery, memory: ChatMemory, obsidian: ObsidianBackend
) -> None:
    await callback.answer("Проставляю связи…")
    action = await _pending(callback, memory)
    if action is None:
        return
    chat_id = callback.message.chat.id
    memory.drop_action(chat_id)
    if not memory.begin_operation(chat_id, "простановка связей"):
        await callback.message.edit_text(
            "Уже выполняется: "
            f"{memory.current_operation(chat_id)}. Дождитесь окончания."
        )
        return
    try:
        result = await _apply_bulk(obsidian, action.plan)
    finally:
        memory.end_operation(chat_id)
    await callback.message.edit_text(result)


async def _reply_folder(
    message: Message,
    memory: ChatMemory,
    settings: Settings,
    obsidian: ObsidianBackend,
    action: PendingAction,
    text: str,
) -> None:
    """Текст на шаге выбора папки — это папка, а не новый запрос к модели.

    «Создай папку люди и добавь туда» понимается как папка «Люди»; если такой
    папки нет, дальше будет вопрос «Создать её?», а не молчаливая ошибка.
    """
    chat_id = message.chat.id
    folders = await _known_folders(obsidian)
    folder = folder_from_text(text, folders)
    if not folder and action.stage == "folder_input":
        # Пользователю задали прямой вопрос «напишите папку» — берём текст как есть.
        folder = clean_folder(text)
    folder = _match_folder(folder, folders)

    if not folder:
        await message.answer(
            "Не понял папку. Выберите вариант кнопкой выше или напишите название, "
            "например «Люди»."
        )
        return

    memory.drop_action(chat_id)
    choice = await _resolve_folder(obsidian, settings, action, folder)
    if choice.action is None:
        await message.answer(choice.text)
    else:
        sent = await message.answer(choice.text, reply_markup=choice.keyboard)
        memory.set_action(chat_id, choice.action, sent.message_id)


def _match_folder(name: str, folders: list[str]) -> str:
    """Существующее написание папки, если имя совпало без учёта регистра."""
    for folder in folders:
        if folder.casefold() == name.casefold():
            return folder
    return name


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
    obsidian: ObsidianBackend,
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

    new_folder = False
    if folder.strip("/") != settings.agent_default_folder:
        try:
            new_folder = not await obsidian.folder_exists(folder)
        except (ObsidianError, aiohttp.ClientError) as error:
            return Choice(_obsidian_error_text(error))

    if exists:
        return Choice(
            f"Заметка уже существует: «{path}».\n\nДописать текст в неё?",
            _append_keyboard(),
            replace(action, stage="exists", path=path),
        )

    if settings.agent_confirmation == "always" or new_folder:
        question = (
            f"Папки «{folder}» пока нет. Создать её и сохранить заметку «{path}»?"
            if new_folder
            else f"Создать заметку «{path}»?"
        )
        return Choice(
            question + _links_line(action.links),
            _confirm_keyboard(),
            replace(action, stage="confirm", path=path),
        )

    if action.links and settings.agent_confirmation != "never":
        return Choice(
            _links_text(path, action.links),
            _links_keyboard(),
            replace(action, stage="links", path=path),
        )

    return Choice(await _apply(obsidian, action.proposal, path, action.links))


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
    obsidian: ObsidianBackend,
    proposal: Proposal,
    path: str,
    links: tuple[str, ...] = (),
) -> str:
    """Выполняет предложение и возвращает текст результата для пользователя."""
    if proposal.kind == "section_create":
        try:
            current = await obsidian.read_note(path)
            updated = create_section(
                current, proposal.heading, proposal.content, proposal.level
            )
            if links:
                updated = add_links(updated, list(links))
            written = await obsidian.write_note(path, updated, overwrite=True)
        except ValueError as error:
            return f"Не удалось создать раздел: {error}."
        except (ObsidianError, aiohttp.ClientError) as error:
            return _obsidian_error_text(error)
        if written:
            return f"Раздел «{proposal.heading}» создан: {path}{_links_line(links)}"
        return f"Dry-run: раздел в «{path}» не создан."

    if proposal.kind in (
        "folder_create",
        "folder_rename",
        "folder_delete",
        "note_move",
        "note_copy",
        "note_delete",
    ):
        try:
            if proposal.kind == "folder_create":
                done = await obsidian.create_folder(proposal.path)
            elif proposal.kind == "folder_rename":
                done = await obsidian.rename_folder(
                    proposal.path, proposal.destination
                )
            elif proposal.kind == "folder_delete":
                done = await obsidian.delete_folder(proposal.path)
            elif proposal.kind == "note_move":
                done = await obsidian.move_note(
                    proposal.path, proposal.destination
                )
            elif proposal.kind == "note_copy":
                done = await obsidian.copy_note(
                    proposal.path, proposal.destination
                )
            else:
                done = await obsidian.delete_note(proposal.path)
        except ValueError as error:
            return f"Некорректный путь: {error}."
        except (ObsidianError, aiohttp.ClientError) as error:
            return _obsidian_error_text(error)

        if not done:
            return f"Dry-run: действие «{_proposal_title(proposal)}» не выполнено."

        if proposal.kind == "folder_create":
            return f"Папка создана: {proposal.path}"
        if proposal.kind == "folder_rename":
            return f"Папка переименована: {proposal.path} → {proposal.destination}"
        if proposal.kind == "folder_delete":
            return f"Папка удалена (в корзину): {proposal.path}"
        if proposal.kind == "note_move":
            return f"Заметка перемещена: {proposal.path} → {proposal.destination}"
        if proposal.kind == "note_copy":
            return f"Заметка скопирована: {proposal.path} → {proposal.destination}"
        return f"Заметка удалена (в корзину): {proposal.path}"

    if proposal.kind == "create":
        content = add_links(proposal.content, list(links)) if links else proposal.content
        try:
            written = await obsidian.write_note(path, content)
        except (ObsidianError, aiohttp.ClientError) as error:
            return _obsidian_error_text(error)
        if written:
            return f"Заметка создана: {path}{_links_line(links)}"
        return (
            f"Dry-run: заметка «{path}» не записана. "
            "Включите OBSIDIAN_DRY_RUN=false для реальной записи."
        )

    if proposal.kind == "text_remove":
        try:
            current = await obsidian.read_note(path)
            updated, removed = remove_text(current, proposal.content)
            written = await obsidian.write_note(path, updated, overwrite=True)
        except ValueError as error:
            return f"Не удалось удалить текст: {error}."
        except (ObsidianError, aiohttp.ClientError) as error:
            return _obsidian_error_text(error)
        if written:
            return f"Удалено из «{path}»: вхождений — {removed}."
        return f"Dry-run: текст из «{path}» не удалён."

    if proposal.kind == "links":
        try:
            current = await obsidian.read_note(path)
            updated = add_links(current, list(links))
            if updated == current:
                return "Новых связей нет: всё уже связано."
            written = await obsidian.write_note(path, updated, overwrite=True)
        except (ObsidianError, aiohttp.ClientError) as error:
            return _obsidian_error_text(error)
        if written:
            return f"Связи добавлены: {path}{_links_line(links)}"
        return f"Dry-run: связи в «{path}» не записаны."

    try:
        current = await obsidian.read_note(path)
        if proposal.kind == "append":
            if contains_text(current, proposal.content):
                return "Такой текст в заметке уже есть — дубликат не добавляю."
            if proposal.heading:
                updated = append_to_section(
                    current, proposal.heading, proposal.content
                )
            else:
                updated = append_to_note(current, proposal.content)
        elif proposal.mode == "replace":
            updated = replace_section(current, proposal.heading, proposal.content)
        else:
            updated = append_to_section(current, proposal.heading, proposal.content)
        if links:
            updated = add_links(updated, list(links))
        written = await obsidian.write_note(path, updated, overwrite=True)
    except ValueError as error:
        return f"Не удалось изменить заметку: {error}."
    except (ObsidianError, aiohttp.ClientError) as error:
        return _obsidian_error_text(error)

    if written:
        return f"Изменения сохранены: {path}{_links_line(links)}"
    return f"Dry-run: изменения в «{path}» не записаны."


def _needs_confirm(settings: Settings, kind: str) -> bool:
    if settings.agent_confirmation == "always":
        return True
    if settings.agent_confirmation == "never":
        return False
    return kind in (
        "append",
        "section",
        "section_create",
        "text_remove",
        "folder_create",
        "folder_rename",
        "folder_delete",
        "note_move",
        "note_copy",
        "note_delete",
    )


def _preview(text: str) -> str:
    text = text.strip()
    if len(text) <= PREVIEW_CHARS:
        return text
    return text[:PREVIEW_CHARS] + "…"


def _proposal_text(proposal: Proposal, links: tuple[str, ...] = ()) -> str:
    hint = _links_line(links)
    if proposal.kind == "section_create":
        body = _preview(proposal.content) if proposal.content.strip() else "(пустой)"
        return (
            f"Создать раздел «{proposal.heading}» в «{proposal.path}»:\n\n"
            f"{body}{hint}\n\nПодтвердить?"
        )
    if proposal.kind == "folder_create":
        return f"Создать папку «{proposal.path}»?"
    if proposal.kind == "folder_rename":
        return (
            f"Переименовать папку «{proposal.path}» → "
            f"«{proposal.destination}»?\n\nСодержимое папки сохранится."
        )
    if proposal.kind == "folder_delete":
        return (
            f"Удалить папку «{proposal.path}» со всем содержимым?\n\n"
            "В файловом режиме она переместится в `.trash`."
        )
    if proposal.kind == "note_delete":
        return (
            f"Удалить заметку «{proposal.path}»?\n\n"
            "В файловом режиме она переместится в `.trash`."
        )
    if proposal.kind == "note_move":
        return (
            f"Переместить заметку «{proposal.path}» → "
            f"«{proposal.destination}»?"
        )
    if proposal.kind == "note_copy":
        return (
            f"Скопировать заметку «{proposal.path}» → "
            f"«{proposal.destination}»?"
        )
    if proposal.kind == "text_remove":
        return (
            f"Удалить из «{proposal.path}»:\n\n"
            f"{_preview(proposal.content)}\n\nПодтвердить?"
        )
    if proposal.kind == "create":
        folder = f"\n\nПапка: {proposal.folder}" if proposal.folder else ""
        return (
            f"Предлагаю заметку «{proposal.title}»:\n\n"
            f"{_preview(proposal.content)}{folder}{hint}\n\nКуда сохранить?"
        )
    if proposal.kind == "append":
        where = (
            f"раздел «{proposal.heading}» в «{proposal.path}»"
            if proposal.heading
            else f"конец заметки «{proposal.path}»"
        )
        return (
            f"Дописать в {where}:\n\n{_preview(proposal.content)}{hint}"
            "\n\nПодтвердить?"
        )
    verb = "Заменить" if proposal.mode == "replace" else "Дополнить"
    return (
        f"{verb} раздел «{proposal.heading}» в «{proposal.path}»:\n\n"
        f"{_preview(proposal.content)}{hint}\n\nПодтвердить?"
    )


def _proposal_title(proposal: Proposal) -> str:
    """Короткое название действия для сообщений об ошибках и dry-run."""
    return {
        "section_create": "создание раздела",
        "text_remove": "удаление текста",
        "folder_create": "создание папки",
        "folder_rename": "переименование папки",
        "folder_delete": "удаление папки",
        "note_move": "перемещение заметки",
        "note_copy": "копирование заметки",
        "note_delete": "удаление заметки",
    }.get(proposal.kind, proposal.kind)


def _links_line(links: tuple[str, ...]) -> str:
    """Одна строка со ссылками — для превью и для отчёта о записи."""
    if not links:
        return ""
    return "\n\nСвязи: " + ", ".join(f"[[{link}]]" for link in links)


def _links_text(path: str, links: tuple[str, ...]) -> str:
    """Список предложенных связей перед записью."""
    lines = "\n".join(f"- [[{link}]]" for link in links)
    return f"Связи для «{path}»:\n\n{lines}\n\nДобавить их в раздел «Связи»?"


def _folder_keyboard(
    settings: Settings, suggestions: tuple[str, ...] | list[str] = ()
) -> InlineKeyboardMarkup:
    """Кнопки папок: подсказки сразу, затем стандартные варианты."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=_button_label(folder), callback_data=f"act:folder:{index}"
            )
        ]
        for index, folder in enumerate(suggestions[:MAX_FOLDER_CHOICES])
    ]
    folder = settings.agent_default_folder
    label = "Inbox" if folder == "Inbox" else f"Inbox ({_button_label(folder)})"
    rows.append([InlineKeyboardButton(text=label, callback_data="act:inbox")])
    if settings.agent_auto_folder:
        rows.append(
            [InlineKeyboardButton(text="Автоопределение", callback_data="act:auto")]
        )
    rows.append([InlineKeyboardButton(text="Другая папка", callback_data="act:other")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="act:no")])
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


def _links_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Добавить связи", callback_data="act:links:yes"),
                InlineKeyboardButton(text="Без связей", callback_data="act:links:no"),
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="act:no")],
        ]
    )


def _bulk_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Применить", callback_data="act:bulk:yes"),
                InlineKeyboardButton(text="Отмена", callback_data="act:no"),
            ]
        ]
    )


def _button_label(text: str) -> str:
    return text if len(text) <= MAX_BUTTON_CHARS else text[: MAX_BUTTON_CHARS - 1] + "…"


def _guess_folders(title: str, content: str, folders: list[str]) -> list[str]:
    """Запасной вариант без LLM: папки, чьё имя встречается в тексте заметки."""
    text = f"{title} {content}".lower()
    found = [
        folder
        for folder in folders
        if folder.rsplit("/", 1)[-1].lower() in text
    ]
    return found[:MAX_FOLDER_CHOICES]


def _llm_status_text(status: LLMStatus) -> str:
    """Короткая сводка пула для команды /llm."""
    mode = "заглушки" if status.stub else "реальный API"
    lines = [
        f"Режим LLM: {mode}",
        f"Сейчас: {status.active_model} · {status.active_account}",
        f"Доступно пар «модель — аккаунт»: "
        f"{status.available_slots} из {status.total_slots}",
    ]
    if status.reset_at is not None:
        lines.append(f"Автосброс: {time.strftime('%d.%m %H:%M', time.localtime(status.reset_at))}")
    else:
        lines.append("Автосброс: не требуется")

    lines.append("Модели (от лучшей к запасной):")
    for index, model in enumerate(status.models, start=1):
        lines.append(f"{index}. {model}")

    lines.append("Аккаунты: " + (", ".join(status.accounts) or "<нет>"))

    if status.disabled:
        lines.append("Отключены (ключ отклонён): " + ", ".join(status.disabled))
    if status.exhausted:
        shown = status.exhausted[:8]
        lines.append("Лимиты до сброса:")
        for model, account, until in shown:
            lines.append(
                f"• {account} / {model} — "
                f"{time.strftime('%d.%m %H:%M', time.localtime(until))}"
            )
        if len(status.exhausted) > len(shown):
            lines.append(f"… и ещё {len(status.exhausted) - len(shown)}")

    return "\n".join(lines)


def _llm_error_text(error: LLMError) -> str:
    if isinstance(error, LLMAllExhaustedError):
        return f"{error}. Можно подождать или выполнить /llm_reset."
    message = str(error)
    if error.status in (401, 403) or "HTTP 401" in message or "HTTP 403" in message:
        return "Модель отклонила ключ. Проверьте LLM_API_*_KEY в .env."
    if error.status in (402, 429) or "HTTP 429" in message or "HTTP 402" in message:
        return (
            "У модели закончился лимит запросов. Пул уже переключает аккаунты "
            "и модели; состояние — /llm."
        )
    return "Не удалось получить ответ от модели. Попробуйте позже."


def _obsidian_error_text(error: Exception) -> str:
    status = getattr(error, "status", None)
    if status in (401, 403):
        return "Obsidian отклонил ключ. Проверьте OBSIDIAN_API_KEY в .env."
    if status == 404:
        return "Заметка или папка не найдена."
    if status == 409:
        return "Объект уже существует: перезапись запрещена."
    if status == 400:
        return f"Недопустимая операция: {error}"
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


async def _load_template_files(
    obsidian: ObsidianBackend, settings: Settings
) -> tuple[str, ...]:
    """Запоминает шаблоны при старте: кнопка создания не ждёт сеть."""
    try:
        notes = await obsidian.list_notes()
    except (ObsidianError, aiohttp.ClientError) as error:
        log.warning("Не удалось получить список шаблонов: %s", error)
        return ()

    paths = template_paths(
        notes, settings.agent_template_folder, exclude=settings.agent_style_note
    )
    if paths:
        log.info(
            "Шаблоны заметок (%s): %s",
            settings.agent_template_folder,
            ", ".join(paths),
        )
    return tuple(paths[:MAX_TEMPLATE_CHOICES])


async def _load_style(obsidian: ObsidianBackend, settings: Settings) -> str:
    """Читает заметку-описание стиля; без неё бот работает как раньше."""
    if not settings.agent_style_note:
        return ""
    try:
        style = await obsidian.read_note(settings.agent_style_note)
    except (ObsidianError, aiohttp.ClientError) as error:
        log.warning(
            "Не удалось прочитать стиль %s: %s", settings.agent_style_note, error
        )
        return ""
    log.info(
        "Стиль пользователя загружен: %s (%d симв.)",
        settings.agent_style_note,
        len(style),
    )
    return style


def _create_semantic(settings: Settings) -> Any:
    """Семантический поиск; без RAG-зависимостей — None, работает как раньше."""
    if not settings.rag_enabled:
        log.info("RAG выключен: RAG_ENABLED=false")
        return None

    try:
        from app.rag import LocalEmbedder, SemanticSearch, store_dir
    except ImportError as error:
        log.warning(
            "RAG выключен: %s. Установите зависимости: "
            "pip install -r requirements-rag.txt",
            error,
        )
        return None

    target = settings.obsidian_vault_path or settings.obsidian_base_url
    index_dir = store_dir(PROJECT_ROOT, target)
    semantic = SemanticSearch(
        LocalEmbedder(settings.rag_model),
        index_dir,
        exclude_folder=settings.agent_template_folder,
        exclude_note=settings.agent_style_note,
    )
    log.info("RAG: модель %s, индекс %s", settings.rag_model, index_dir)
    return semantic


async def _refresh_rag(semantic: Any, obsidian: ObsidianBackend) -> None:
    """Фоновое обновление индекса при старте: запуск бота не задерживается."""
    try:
        await semantic.refresh(obsidian)
    except Exception as error:  # индекс необязателен: бот продолжает работать
        log.warning("RAG: индекс не обновлён: %s", error)


async def run_bot(settings: Settings) -> None:
    """Запускает бота и блокируется до остановки (Ctrl+C)."""
    pool = LLMPool(settings)
    audit = AuditLog(settings.audit_root, settings.audit_enabled)
    obsidian = AuditedBackend(create_obsidian_client(settings), audit)
    semantic = _create_semantic(settings)
    agent = Agent(
        pool,
        obsidian,
        settings,
        style=await _load_style(obsidian, settings),
        semantic=semantic,
    )
    template_files = await _load_template_files(obsidian, settings)
    rag_task = (
        asyncio.create_task(_refresh_rag(semantic, obsidian))
        if semantic is not None
        else None
    )
    memory = ChatMemory()
    bot = Bot(token=settings.telegram_bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    try:
        me = await bot.get_me()
        log.info("Бот @%s запущен, ожидаю сообщения", me.username)
        # Меню команд у поля ввода: те же три команды, что и на клавиатуре.
        await bot.set_my_commands(COMMANDS)
        # На случай, если для токена ранее был настроен webhook.
        await bot.delete_webhook(drop_pending_updates=True)
        pool.start_auto_reset()
        # Объекты попадают в обработчики как keyword-аргументы (workflow data).
        await dispatcher.start_polling(
            bot,
            agent=agent,
            obsidian=obsidian,
            memory=memory,
            settings=settings,
            llm=pool,
            template_files=template_files,
            semantic=semantic,
            audit=audit,
        )
    finally:
        if rag_task is not None:
            rag_task.cancel()
            with suppress(asyncio.CancelledError):
                await rag_task
        await pool.close()
        await obsidian.close()
        await bot.session.close()
        log.info("Бот остановлен")
