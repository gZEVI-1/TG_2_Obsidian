"""Агент: цикл вызова инструментов поверх LLMClient."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.actions import prompt_block, tools as build_tools, write_actions
from app.config import Settings
from app.links import (
    duplicate_titles,
    existing_links,
    is_linked,
    link_label,
    mention_paths,
    note_title,
)
from app.llm import ChatClient, LLMError, ToolCall
from app.notes import slugify, validate_path
from app.obsidian import ObsidianBackend, ObsidianError
from app.templates import render, template_paths

log = logging.getLogger(__name__)

MAX_STEPS = 6
MAX_NOTE_CHARS = 4000
MAX_SUGGESTED_FOLDERS = 3
MAX_LINK_CANDIDATES = 20
LINK_FALLBACK_CHOICES = 3

BASE_PROMPT = (
    "Ты живой и дружелюбный помощник в Telegram. Работаешь с хранилищем Obsidian "
    "пользователя и говоришь по-русски просто, по-человечески: без канцелярита, "
    "длинных списков и служебных фраз вроде «выполняю запрос»."
)

PARSE_RULES = (
    "\n\nКАК РАЗБИРАТЬ ПРОСЬБУ:\n"
    "- Всегда сам определи команду по смыслу: пользователь не обязан её называть. "
    "«запиши…», «добавь…», «поправь…», «что там про…» — сопоставь с командами "
    "из списка выше.\n"
    "- Раздели просьбу на три части: ЧТО сделать, ГДЕ (заметка, папка, раздел) "
    "и КАКОЙ текст.\n"
    "- Помни контекст диалога: «сохрани это», «допиши туда», «поправь его» "
    "относятся к сказанному выше.\n"
    "- Если подходят несколько команд или неясно место — вызови clarify, задай "
    "один короткий вопрос и ничего не меняй.\n"
    "- Если запрос не подходит ни под одну команду — тоже clarify: попроси "
    "указать действие явно.\n"
    "- Не выдумывай пути и содержимое: не знаешь — сначала search_notes или "
    "list_folders."
)

WRITE_RULES = (
    "\n\nКАК ПИСАТЬ ТЕКСТ ЗАМЕТКИ (всегда — и при создании, и при дописывании):\n"
    "- Продумай, как текст ляжет в заметку: место, раздел, формат (абзац, "
    "список, таблица).\n"
    "- Перепиши сообщение: убери разговорность, добавь структуру и ясность, "
    "сохранив смысл. Не вставляй сообщение дословно.\n"
    "- При дописывании учитывай уже написанное: не повторяй, продолжай мысль "
    "в том же тоне и формате.\n"
    "- Расширяй за счёт ясности, а не выдуманных фактов.\n"
    "- Если пользователь дал готовый текст и просит сохранить как есть — "
    "сохрани как есть.\n\n"
    "ПРАВИЛА РАБОТЫ:\n"
    "1. Перед дописыванием или правкой прочитай заметку (read_note).\n"
    "2. Не дублируй: если такой текст уже есть, скажи об этом.\n"
    "3. Создание и изменение подтверждает пользователь: готовь текст и коротко "
    "объясни, что предлагаешь.\n"
    "4. Пути — относительно корня хранилища, с .md.\n"
    "5. Если просят оформить заметку по шаблону: вызови list_templates, прочитай "
    "подходящий через read_note и сохрани все его разделы и их порядок.\n"
    "6. Отвечай коротко и по делу."
)

STYLE_RULES = (
    "\n\nСТИЛЬ ПОЛЬЗОВАТЕЛЯ (из его заметки-описания):\n{style}\n"
    "- Соблюдай тон, структуру и оформление из этого описания.\n"
    "- При дописывании в существующую заметку следуй её собственному стилю, "
    "если он отличается."
)


def build_system_prompt(style: str = "") -> str:
    """Системный промпт: команды, правила разбора и письма, стиль пользователя."""
    prompt = BASE_PROMPT + "\n\n" + prompt_block() + PARSE_RULES + WRITE_RULES
    if style:
        prompt += STYLE_RULES.format(style=style)
    return prompt


COMPOSE_NOTE_PROMPT = (
    "Оформи заметку для Obsidian по сообщению пользователя.\n"
    "Верни только Markdown: первая строка — заголовок вида «# ...», дальше текст. "
    "Сохрани смысл, разверни формулировки и добавь структуру, если уместно. "
    "Не выдумывай факты.\n\n"
    "Сообщение:\n{text}"
)

COMPOSE_TEMPLATE_PROMPT = (
    "Оформи заметку для Obsidian по шаблону.\n"
    "Верни только Markdown: заметку целиком, начиная с заголовка «# ...».\n"
    "Сохрани все разделы шаблона, их заголовки и порядок. Заполни разделы по "
    "смыслу сообщения; пустые оставь пустыми, факты не выдумывай. Лишних "
    "разделов не добавляй.\n"
    "Заголовок заметки — тема из сообщения, а не имя шаблона. Если в шаблоне "
    "есть frontmatter, сохрани его поля и подставь подходящие значения; "
    "служебные пометки самого шаблона (например, тег template) не переноси.\n\n"
    "Шаблон:\n{template}\n\n"
    "Сообщение:\n{text}"
)

COMPOSE_APPEND_PROMPT = (
    "Допиши фрагмент в раздел «{section}» заметки «{path}».\n\n"
    "Сейчас в разделе:\n{section_text}\n\n"
    "Сообщение пользователя:\n{text}\n\n"
    "Верни только текст для добавления: в том же стиле и формате, без заголовка "
    "раздела и без повторения уже написанного."
)

COMPOSE_SECTION_PROMPT = (
    "Замени содержимое раздела «{section}» заметки «{path}».\n\n"
    "Сейчас в разделе:\n{section_text}\n\n"
    "Пожелание пользователя:\n{text}\n\n"
    "Верни только новое содержимое раздела, без строки заголовка."
)

TOOLS: list[dict[str, Any]] = build_tools()

TERMINAL_TOOLS = write_actions()

FOLDER_PROMPT = (
    "Разложи заметку по папкам хранилища Obsidian.\n\n"
    "Существующие папки: {folders}\n\n"
    "Заметка «{title}»:\n{content}\n\n"
    "Выбери до трёх подходящих папок. Если ни одна не подходит, предложи "
    "короткие имена новых папок (1–2 слова).\n"
    "Ответь только именами папок, каждое с новой строки, без пояснений."
)


LINK_PROMPT = (
    "Выбери, с какими существующими заметками связать эту заметку.\n\n"
    "Заметка «{title}»:\n{content}\n\n"
    "Кандидаты:\n{candidates}\n\n"
    "Выбери до {limit} самых релевантных. Ответь только путями из списка, "
    "каждый с новой строки, без пояснений. Если ничего не подходит — ответь «нет»."
)


@dataclass(frozen=True)
class Proposal:
    """Изменение, которое ждёт решения пользователя."""

    kind: str  # create | append | section | links | bulk
    title: str = ""
    path: str = ""
    heading: str = ""
    mode: str = ""
    content: str = ""
    folder: str = ""


@dataclass(frozen=True)
class AgentResult:
    """Итог хода агента: ответ пользователю и, возможно, предложение."""

    text: str
    proposal: Proposal | None = None
    clarify: bool = False


class Agent:
    """Один ход диалога: модель ищет, читает и предлагает изменения."""

    def __init__(
        self,
        llm: ChatClient,
        obsidian: ObsidianBackend,
        settings: Settings,
        *,
        style: str = "",
    ) -> None:
        self._llm = llm
        self._obsidian = obsidian
        self._search_limit = settings.agent_search_limit
        self._template_folder = settings.agent_template_folder
        self._style_note = settings.agent_style_note
        self._link_limit = settings.agent_link_limit
        self._style = style.strip()[:4000]
        self.system_prompt = build_system_prompt(self._style)

    async def handle(self, messages: list[dict[str, Any]]) -> AgentResult:
        """Крутит цикл инструментов, пока модель не ответит текстом.

        Инструменты изменения завершают ход: запись выполняет бот после
        решения пользователя. `clarify` тоже завершает ход — бот показывает
        вопрос и кнопки с командами.
        """
        for _ in range(MAX_STEPS):
            response = await self._llm.chat(messages, tools=TOOLS)

            if not response.tool_calls:
                return AgentResult(text=response.text)

            clarify = next(
                (call for call in response.tool_calls if call.name == "clarify"),
                None,
            )
            if clarify is not None:
                question = _argument(clarify.arguments, "question") or (
                    "Уточните, пожалуйста, что нужно сделать."
                )
                return AgentResult(text=question, clarify=True)

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

    async def suggest_links(
        self, text: str, *, title: str = "", exclude_path: str = ""
    ) -> list[str]:
        """Подбирает связи для заметки (FR-05): упоминания, поиск и модель.

        Возвращает готовые строки для `[[...]]` (имя или путь), исключая то,
        что в заметке уже связано. Пустой список — предлагать нечего.
        Автолинковку включает `AGENT_AUTO_LINK`; ручная команда вызывает
        этот метод независимо от настройки.
        """
        if self._obsidian is None:
            return []

        try:
            notes = await self._obsidian.list_notes()
        except ObsidianError as error:
            log.warning("Не удалось получить список заметок для связей: %s", error)
            return []
        if not notes:
            return []

        duplicates = duplicate_titles(notes)
        linked = existing_links(text)
        candidates = await self._link_candidates(text, title, exclude_path, notes)
        candidates = [
            (path, excerpt)
            for path, excerpt in candidates
            if not is_linked(link_label(path, duplicates), linked)
        ][:MAX_LINK_CANDIDATES]
        if not candidates:
            return []

        prompt = LINK_PROMPT.format(
            title=title or "Без названия",
            content=text[:1500],
            candidates="\n".join(
                f"- {path}: {excerpt}" if excerpt else f"- {path}"
                for path, excerpt in candidates
            ),
            limit=self._link_limit,
        )
        try:
            response = await self._llm.chat(
                [{"role": "user", "content": prompt}], max_tokens=200
            )
        except LLMError as error:
            log.warning("Модель не выбрала связи (%s), беру упоминания", error)
            fallback = candidates[: min(LINK_FALLBACK_CHOICES, self._link_limit)]
            return [link_label(path, duplicates) for path, _ in fallback]

        chosen = _pick_paths(response.text, candidates, self._link_limit)
        return [link_label(path, duplicates) for path in chosen]

    async def _link_candidates(
        self, text: str, title: str, exclude_path: str, notes: list[str]
    ) -> list[tuple[str, str]]:
        """Кандидаты (путь, фрагмент): упоминания названий и полнотекстовый поиск.

        Упоминания дают точные совпадения имён, поиск — совпадения по тексту,
        алиасам и тегам (их ищет хранилище, а не бот).
        """
        found: list[tuple[str, str]] = []
        seen: set[str] = set()
        for path in mention_paths(text, notes, exclude=exclude_path):
            seen.add(path)
            found.append((path, ""))

        query = title.strip() or " ".join(text.split()[:8])
        if not query:
            return found
        try:
            hits = await self._obsidian.search_notes(query, limit=self._search_limit)
        except ObsidianError as error:
            log.warning("Поиск кандидатов для связей не удался: %s", error)
            return found

        excluded = exclude_path.strip("/").casefold()
        for hit in hits:
            path = hit.get("path", "")
            if not path or path.strip("/").casefold() == excluded or path in seen:
                continue
            seen.add(path)
            found.append((path, (hit.get("excerpt") or "").strip()[:160]))
        return found

    async def compose_note(self, text: str, template: str = "") -> tuple[str, str]:
        """Разворачивает сообщение в заметку: (заголовок, полный текст).

        Если передан шаблон, модель заполняет его разделы, сохраняя структуру.
        Плейсхолдеры {{title}}, {{date}}, {{time}} подставляет бот.
        """
        prompt = (
            COMPOSE_TEMPLATE_PROMPT.format(template=template, text=text)
            if template
            else COMPOSE_NOTE_PROMPT.format(text=text)
        )
        response = await self._llm.chat(
            [{"role": "user", "content": self._with_style(prompt)}],
            max_tokens=900,
        )
        content = response.text.strip()
        if not content:
            raise LLMError("Модель вернула пустой текст заметки.")
        title, content = _split_note(content)
        if "{{" in title:
            title = _title_from_text(text)
        return title, render(content, title)

    async def compose_append(
        self, path: str, section: str, section_text: str, text: str
    ) -> str:
        """Переписывает фрагмент так, чтобы он хорошо лёг в раздел заметки."""
        prompt = COMPOSE_APPEND_PROMPT.format(
            path=path,
            section=section or "конец заметки",
            section_text=section_text[-2000:],
            text=text,
        )
        return await self._compose(prompt)

    async def compose_section(
        self, path: str, section: str, section_text: str, text: str
    ) -> str:
        """Пишет новое содержимое раздела по пожеланию пользователя."""
        prompt = COMPOSE_SECTION_PROMPT.format(
            path=path,
            section=section,
            section_text=section_text[-2000:],
            text=text,
        )
        return await self._compose(prompt)

    async def _compose(self, prompt: str) -> str:
        response = await self._llm.chat(
            [{"role": "user", "content": self._with_style(prompt)}], max_tokens=700
        )
        result = response.text.strip()
        if not result:
            raise LLMError("Модель вернула пустой текст.")
        return result

    def _with_style(self, prompt: str) -> str:
        """Добавляет описание стиля пользователя, если оно задано."""
        if not self._style:
            return prompt
        return f"{prompt}\n\nСТИЛЬ ПОЛЬЗОВАТЕЛЯ (соблюдай):\n{self._style}"

    def _proposal(self, call: ToolCall) -> AgentResult:
        if call.name == "create_note":
            title = _argument(call.arguments, "title")
            content = _argument(call.arguments, "content", strip=False)
            folder = _argument(call.arguments, "folder")
            if not title or not content.strip():
                return AgentResult(
                    text="Модель предложила заметку без заголовка или текста."
                )
            content = render(content, title)
            log.info(
                "Предложение создать заметку: %s (%d симв.)%s",
                title,
                len(content),
                f", папка «{folder}»" if folder else "",
            )
            return AgentResult(
                text="",
                proposal=Proposal(
                    kind="create", title=title, content=content, folder=folder
                ),
            )

        if call.name == "link_all":
            log.info("Предложение проставить связи во всех заметках")
            return AgentResult(text="", proposal=Proposal(kind="bulk"))

        path = _argument(call.arguments, "path")
        content = _argument(call.arguments, "content", strip=False)
        try:
            path = validate_path(path)
        except ValueError as error:
            return AgentResult(text=f"Некорректный путь заметки: {error}.")
        if call.name == "add_links":
            log.info("Предложение связать заметку: %s", path)
            return AgentResult(text="", proposal=Proposal(kind="links", path=path))
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
        if call.name == "list_templates":
            return await self._templates()
        if call.name == "read_note":
            return await self._read(_argument(call.arguments, "path"))
        return f"Неизвестный инструмент: {call.name}"

    async def _search(self, query: str) -> str:
        if not query:
            return "Ошибка: не указан запрос."
        log.info("Инструмент search_notes: %s", query)
        try:
            hits = await self._obsidian.search_notes(
                query, limit=self._search_limit
            )
        except ObsidianError as error:
            return f"Ошибка поиска: {error}"

        if not hits:
            return "Ничего не найдено."

        lines = []
        for hit in hits:
            if hit["match"] == "имя":
                lines.append(f"- {hit['path']} (совпадение в названии)")
            elif hit["match"] == "похоже":
                lines.append(f"- {hit['path']} (похожее название)")
            else:
                lines.append(f"- {hit['path']}: {hit['excerpt'][:160]}")
        return "\n".join(lines)

    async def _folders(self) -> str:
        try:
            notes = await self._obsidian.list_notes()
        except ObsidianError as error:
            return f"Ошибка списка файлов: {error}"

        folders = sorted({note.rsplit("/", 1)[0] for note in notes if "/" in note})
        return ", ".join(folders) if folders else "Папок нет."

    async def _templates(self) -> str:
        try:
            notes = await self._obsidian.list_notes()
        except ObsidianError as error:
            return f"Ошибка списка шаблонов: {error}"

        paths = template_paths(notes, self._template_folder, exclude=self._style_note)
        if not paths:
            return f"Шаблонов нет: папка «{self._template_folder}» пуста."
        return "\n".join(f"- {path}" for path in paths)

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


def _split_note(content: str) -> tuple[str, str]:
    """Заголовок заметки — первый «# ...», иначе первая строка."""
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:80] or "Без названия", content
    first = next((line.strip() for line in content.splitlines() if line.strip()), "")
    if not first:
        return "Без названия", content
    return first[:80], f"# {first}\n\n{content}"


def _title_from_text(text: str) -> str:
    """Запасной заголовок, если модель оставила в нём плейсхолдер."""
    first = next((line.strip().lstrip("#").strip() for line in text.splitlines() if line.strip()), "")
    return first[:80] or "Без названия"


def _clean_folder_line(line: str) -> str:
    """Убирает маркеры списка, нумерацию и кавычки из ответа модели."""
    text = line.strip()
    text = re.sub(r"^[\s\-–—•*·]+", "", text)
    text = re.sub(r"^\d+[.)]\s*", "", text)
    text = text.strip("`\"'«» .:")
    if text.lower() in ("нет", "none", "no", "-"):
        return ""
    return text


def _pick_paths(
    answer: str, candidates: list[tuple[str, str]], limit: int
) -> list[str]:
    """Выбирает пути из ответа модели: по пути или по имени, без повторов.

    Незнакомые строки и «нет» игнорируются: модель может выбрать только
    из предложенных кандидатов, иначе появится ссылка на несуществующую заметку.
    """
    known = {
        path.casefold().removesuffix(".md"): path for path, _ in candidates
    }
    by_name = {note_title(path).casefold(): path for path, _ in candidates}
    chosen: list[str] = []
    for line in answer.splitlines():
        value = _clean_folder_line(line)
        if not value:
            continue
        key = value.casefold().removesuffix(".md")
        path = known.get(key) or by_name.get(key)
        if path is None or path in chosen:
            continue
        chosen.append(path)
        if len(chosen) >= limit:
            break
    return chosen


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
