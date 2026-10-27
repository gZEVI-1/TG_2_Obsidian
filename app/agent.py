"""Агент: цикл вызова инструментов поверх LLMClient."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.llm import LLMClient, ToolCall
from app.notes import slugify, validate_path
from app.obsidian import ObsidianClient, ObsidianError

log = logging.getLogger(__name__)

MAX_STEPS = 6
MAX_NOTE_CHARS = 4000
MAX_SUGGESTED_FOLDERS = 3

SYSTEM_PROMPT = (
    "Ты живой и дружелюбный помощник в Telegram. Работаешь с хранилищем Obsidian "
    "пользователя и говоришь по-русски просто, по-человечески: без канцелярита, "
    "длинных списков и служебных фраз вроде «выполняю запрос». Если что-то "
    "непонятно, переспроси своими словами.\n\n"
    "Инструменты:\n"
    "- search_notes(query) — найти заметки по словам;\n"
    "- list_folders() — папки хранилища;\n"
    "- read_note(path) — прочитать заметку;\n"
    "- create_note(title, content) — предложить новую заметку;\n"
    "- append_note(path, content, section) — дописать текст в заметку;\n"
    "- edit_section(path, heading, content, mode) — изменить раздел заметки.\n\n"
    "Как работать:\n"
    "1. Не выдумывай пути: если заметка названа неточно, сначала search_notes.\n"
    "2. Перед дописыванием прочитай заметку (read_note) и выбери раздел, к "
    "которому текст относится по смыслу. Если подходящего раздела нет — оставь "
    "section пустым, текст добавится в конец.\n"
    "3. Не дублируй: если такой текст в заметке уже есть, ничего не добавляй, "
    "а просто скажи об этом.\n"
    "4. Если подходящих заметок несколько или не нашлось ни одной — спроси "
    "пользователя, ничего не меняя молча.\n"
    "5. Создание и изменение подтверждает пользователь: готовь текст и коротко "
    "объясни, что предлагаешь.\n"
    "6. Пути — относительно корня хранилища, с расширением .md.\n"
    "7. Отвечай коротко и по делу."
)

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_notes",
            "description": "Найти заметки по словам: имя файла, заголовки, текст.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Ключевые слова для поиска"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_folders",
            "description": "Список папок хранилища Obsidian.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_note",
            "description": "Прочитать заметку целиком (длинная обрезается).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Путь относительно корня хранилища, например «Проекты/Идея.md»",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_note",
            "description": (
                "Предложить новую заметку. Папку выберет пользователь, поэтому путь "
                "не передавай — только заголовок и текст."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Заголовок и имя файла"},
                    "content": {"type": "string", "description": "Полный текст в Markdown"},
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_note",
            "description": (
                "Предложить дописать текст в существующую заметку. Сначала прочитай "
                "заметку и укажи раздел, к которому текст относится по смыслу."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь существующей заметки"},
                    "content": {"type": "string", "description": "Текст, который нужно дописать"},
                    "section": {
                        "type": "string",
                        "description": (
                            "Заголовок раздела без решёток, в конец которого дописать. "
                            "Пусто — дописать в конец заметки."
                        ),
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_section",
            "description": "Предложить заменить или дополнить раздел заметки по заголовку.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь существующей заметки"},
                    "heading": {"type": "string", "description": "Заголовок раздела без решёток"},
                    "content": {"type": "string", "description": "Новое содержимое раздела"},
                    "mode": {
                        "type": "string",
                        "enum": ["replace", "append"],
                        "description": "replace — заменить раздел, append — дописать в конец",
                    },
                },
                "required": ["path", "heading", "content", "mode"],
            },
        },
    },
]

TERMINAL_TOOLS = ("create_note", "append_note", "edit_section")

FOLDER_PROMPT = (
    "Разложи заметку по папкам хранилища Obsidian.\n\n"
    "Существующие папки: {folders}\n\n"
    "Заметка «{title}»:\n{content}\n\n"
    "Выбери до трёх подходящих папок. Если ни одна не подходит, предложи "
    "короткие имена новых папок (1–2 слова).\n"
    "Ответь только именами папок, каждое с новой строки, без пояснений."
)


@dataclass(frozen=True)
class Proposal:
    """Изменение, которое ждёт решения пользователя."""

    kind: str  # create | append | section
    title: str = ""
    path: str = ""
    heading: str = ""
    mode: str = ""
    content: str = ""


@dataclass(frozen=True)
class AgentResult:
    """Итог хода агента: ответ пользователю и, возможно, предложение."""

    text: str
    proposal: Proposal | None = None


class Agent:
    """Один ход диалога: модель ищет, читает и предлагает изменения."""

    def __init__(self, llm: LLMClient, obsidian: ObsidianClient, settings: Settings) -> None:
        self._llm = llm
        self._obsidian = obsidian
        self._search_limit = settings.agent_search_limit

    async def handle(self, messages: list[dict[str, Any]]) -> AgentResult:
        """Крутит цикл инструментов, пока модель не ответит текстом.

        Инструменты изменения завершают ход: запись выполняет бот после
        решения пользователя.
        """
        for _ in range(MAX_STEPS):
            response = await self._llm.chat(messages, tools=TOOLS)

            if not response.tool_calls:
                return AgentResult(text=response.text)

            change = next(
                (call for call in response.tool_calls if call.name in TERMINAL_TOOLS),
                None,
            )
            if change is not None:
                return self._proposal(change)

            messages.append(_assistant_tool_message(response.tool_calls))
            for call in response.tool_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": await self._run_tool(call),
                    }
                )

        return AgentResult(
            text="Не удалось завершить за допустимое число шагов с инструментами."
        )

    async def suggest_folders(
        self, title: str, content: str, folders: list[str]
    ) -> list[str]:
        """Просит модель выбрать или предложить папки для новой заметки (FR-04)."""
        prompt = FOLDER_PROMPT.format(
            folders=", ".join(folders[:60]) if folders else "папок пока нет",
            title=title,
            content=content[:1500],
        )
        response = await self._llm.chat(
            [{"role": "user", "content": prompt}], max_tokens=80
        )

        known = {folder.lower(): folder for folder in folders}
        result: list[str] = []
        for line in response.text.splitlines():
            name = _clean_folder_line(line)
            if not name:
                continue
            folder = known.get(name.lower()) or _clean_folder_path(name)
            if folder and folder not in result:
                result.append(folder)
            if len(result) >= MAX_SUGGESTED_FOLDERS:
                break
        return result

    def _proposal(self, call: ToolCall) -> AgentResult:
        if call.name == "create_note":
            title = _argument(call.arguments, "title")
            content = _argument(call.arguments, "content", strip=False)
            if not title or not content.strip():
                return AgentResult(
                    text="Модель предложила заметку без заголовка или текста."
                )
            log.info("Предложение создать заметку: %s (%d симв.)", title, len(content))
            return AgentResult(
                text="", proposal=Proposal(kind="create", title=title, content=content)
            )

        path = _argument(call.arguments, "path")
        content = _argument(call.arguments, "content", strip=False)
        try:
            path = validate_path(path)
        except ValueError as error:
            return AgentResult(text=f"Некорректный путь заметки: {error}.")
        if not content.strip():
            return AgentResult(text="Модель предложила пустое изменение.")

        if call.name == "append_note":
            section = _argument(call.arguments, "section")
            log.info("Предложение дописать: %s (%d симв.)", path, len(content))
            return AgentResult(
                text="",
                proposal=Proposal(
                    kind="append",
                    path=path,
                    heading=section,
                    content=content,
                ),
            )

        heading = _argument(call.arguments, "heading")
        mode = _argument(call.arguments, "mode") or "replace"
        if not heading:
            return AgentResult(text="Модель не указала раздел для правки.")
        if mode not in ("replace", "append"):
            return AgentResult(text="Неизвестный режим правки раздела.")

        log.info("Предложение правки раздела «%s» в %s", heading, path)
        return AgentResult(
            text="",
            proposal=Proposal(
                kind="section",
                path=path,
                heading=heading,
                mode=mode,
                content=content,
            ),
        )

    async def _run_tool(self, call: ToolCall) -> str:
        if call.name == "search_notes":
            return await self._search(_argument(call.arguments, "query"))
        if call.name == "list_folders":
            return await self._folders()
        if call.name == "read_note":
            return await self._read(_argument(call.arguments, "path"))
        return f"Неизвестный инструмент: {call.name}"

    async def _search(self, query: str) -> str:
        if not query:
            return "Ошибка: не указан запрос."
        log.info("Инструмент search_notes: %s", query)
        try:
            hits = await self._obsidian.search(query, limit=self._search_limit)
        except ObsidianError as error:
            return f"Ошибка поиска: {error}"

        if not hits:
            return "Ничего не найдено."

        lines = []
        for hit in hits:
            excerpt = " ".join(
                (hit.get("matches") or [{}])[0].get("context", "").split()
            )
            lines.append(f"- {hit.get('filename', '?')}: {excerpt[:160]}")
        return "\n".join(lines)

    async def _folders(self) -> str:
        try:
            notes = await self._obsidian.list_notes()
        except ObsidianError as error:
            return f"Ошибка списка файлов: {error}"

        folders = sorted({note.rsplit("/", 1)[0] for note in notes if "/" in note})
        return ", ".join(folders) if folders else "Папок нет."

    async def _read(self, path: str) -> str:
        if not path:
            return "Ошибка: не указан путь."
        log.info("Инструмент read_note: %s", path)
        try:
            content = await self._obsidian.read_note(path)
        except ObsidianError as error:
            return f"Ошибка чтения: {error}"

        if len(content) > MAX_NOTE_CHARS:
            return content[:MAX_NOTE_CHARS] + "\n\n[текст обрезан]"
        return content


def _clean_folder_line(line: str) -> str:
    """Убирает маркеры списка, нумерацию и кавычки из ответа модели."""
    text = line.strip()
    text = re.sub(r"^[\s\-–—•*·]+", "", text)
    text = re.sub(r"^\d+[.)]\s*", "", text)
    text = text.strip("`\"'«» .:")
    if text.lower() in ("нет", "none", "no", "-"):
        return ""
    return text


def _clean_folder_path(name: str) -> str:
    """Приводит предложенное моделью имя папки к безопасному виду."""
    parts = [slugify(part) for part in name.split("/") if part.strip()]
    return "/".join(part for part in parts if part and part != "Без названия")


def _assistant_tool_message(calls: tuple[ToolCall, ...]) -> dict[str, Any]:
    """Сообщение ассистента с вызовами инструментов — в формате OpenAI API."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in calls
        ],
    }


def _argument(arguments: str, name: str, *, strip: bool = True) -> str:
    """Достаёт строковый аргумент из JSON, который вернула модель."""
    try:
        data = json.loads(arguments)
    except json.JSONDecodeError:
        return ""
    value = data.get(name) if isinstance(data, dict) else None
    if not isinstance(value, str):
        return ""
    return value.strip() if strip else value
