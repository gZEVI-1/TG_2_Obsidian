"""Telegram-бот: команды, подтверждения и цикл long polling."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app.agent import Agent, Proposal
from app.config import Settings
from app.llm import LLMAllExhaustedError, LLMError, LLMPool, LLMStatus
from app.notes import (
    append_to_note,
    append_to_section,
    build_path,
    contains_text,
    find_section,
    replace_section,
    sections,
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

BTN_NEW = "🆕 Новый диалог"
BTN_HELP = "📖 Справка"
BTN_START = "🔄 Начать заново"
BTN_STAY = "💬 Остаться в этом чате"
BTN_BACK = "⬅️ Назад"

BTN_NOTE_NEW = "📝 Новая заметка"
BTN_NOTE_APPEND = "➕ Дописать"
BTN_NOTE_EDIT = "✏️ Править раздел"
BTN_NOTE_FIND = "🔎 Найти"

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
        [KeyboardButton(text=BTN_BACK)],
    ],
    resize_keyboard=True,
)

COMMANDS = [
    BotCommand(command="start", description="Начать заново"),
    BotCommand(command="help", description="Справка"),
    BotCommand(command="new", description="Новый диалог"),
    BotCommand(command="llm", description="Состояние LLM-пула"),
    BotCommand(command="llm_reset", description="Сбросить лимиты LLM"),
]

START_TEXT = (
    "Привет! Я ваш помощник по Obsidian.\n\n"
    "Могу найти и прочитать заметку, сохранить новую мысль, дописать в нужный "
    "раздел или поправить его. Говорите обычными словами — разберусь.\n\n"
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
    "• ⬅️ Назад — вернуться на первую страницу.\n\n"
    "Можно и обычными словами: «сохрани мысль…», «допиши в заметку про…».\n\n"
    "Команды: /start, /help, /new, /llm — состояние моделей и лимитов."
)


@dataclass
class PendingAction:
    """Предложение, которое ждёт решения пользователя."""

    proposal: Proposal
    stage: str  # folder | auto | confirm | exists
    path: str = ""
    folders: tuple[str, ...] = ()


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
        await _offer(message, memory, settings, obsidian, result.proposal)
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
    memory: ChatMemory,
    settings: Settings,
    obsidian: ObsidianBackend,
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

        await _offer(message, memory, settings, obsidian, proposal)
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
        "Напишите папку, например «Проекты/2026». Отмена — /start."
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
            question,
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
    obsidian: ObsidianBackend, proposal: Proposal, path: str
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
        where = (
            f"раздел «{proposal.heading}» в «{proposal.path}»"
            if proposal.heading
            else f"конец заметки «{proposal.path}»"
        )
        return f"Дописать в {where}:\n\n{_preview(proposal.content)}\n\nПодтвердить?"
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


async def run_bot(settings: Settings) -> None:
    """Запускает бота и блокируется до остановки (Ctrl+C)."""
    pool = LLMPool(settings)
    obsidian = create_obsidian_client(settings)
    agent = Agent(pool, obsidian, settings, style=await _load_style(obsidian, settings))
    template_files = await _load_template_files(obsidian, settings)
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
        )
    finally:
        await pool.close()
        await obsidian.close()
        await bot.session.close()
        log.info("Бот остановлен")
