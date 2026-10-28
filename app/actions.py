"""Реестр команд (действий) агента: схемы, описания и текст для промпта.

Чтобы добавить команду:
1. добавьте `Action` в `ACTIONS` — имя, заголовок, подсказку и JSON-схему;
2. обработайте имя в `Agent` (метод-исполнитель или терминальное предложение);
3. если команда меняет заметки, поставьте `writes=True` — она станет
   терминальной: запись выполнится только после подтверждения пользователем.

Список инструментов для модели и блок промпта собираются из реестра, поэтому
новая команда автоматически появляется и там, и там.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Action:
    """Одна команда агента."""

    name: str
    title: str
    hint: str
    description: str
    parameters: dict[str, Any]
    writes: bool = False


ACTIONS: tuple[Action, ...] = (
    Action(
        name="search_notes",
        title="Найти заметки",
        hint="когда нужно найти заметку по словам, имени или опечатке",
        description=(
            "Найти заметки: сначала имена файлов (в том числе с опечатками), "
            "затем содержимое. Возвращает пути и фрагменты текста."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Ключевые слова"}
            },
            "required": ["query"],
        },
    ),
    Action(
        name="list_folders",
        title="Папки хранилища",
        hint="когда нужно понять, куда положить новую заметку",
        description="Показать папки хранилища Obsidian.",
        parameters={"type": "object", "properties": {}},
    ),
    Action(
        name="read_note",
        title="Прочитать заметку",
        hint="когда нужно ответить по содержанию или подготовить правку",
        description="Прочитать заметку по пути (длинная обрезается).",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Путь относительно корня хранилища, например «Проекты/Идея.md»",
                }
            },
            "required": ["path"],
        },
    ),
    Action(
        name="create_note",
        title="Создать заметку",
        hint="когда пользователь делится мыслью или текстом для новой заметки",
        description=(
            "Создать новую заметку. title — заголовок, content — полный текст "
            "в Markdown. Папку выберет пользователь, подтверждение — тоже. "
            "Текст перепиши и разверни, не копируй сообщение дословно."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Заголовок и имя файла"},
                "content": {"type": "string", "description": "Полный текст в Markdown"},
            },
            "required": ["title", "content"],
        },
        writes=True,
    ),
    Action(
        name="append_note",
        title="Дописать в заметку",
        hint="когда нужно добавить текст в уже существующую заметку",
        description=(
            "Дописать текст в существующую заметку. section — раздел, к которому "
            "текст относится по смыслу (пусто — в конец). Перед вызовом прочитай "
            "заметку и перепиши текст так, чтобы он хорошо ложился в этот раздел "
            "и не повторял уже написанное."
        ),
        parameters={
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
        writes=True,
    ),
    Action(
        name="edit_section",
        title="Править раздел",
        hint="когда нужно изменить существующий раздел заметки",
        description=(
            "Заменить (mode=replace) или дополнить (mode=append) раздел заметки. "
            "heading — заголовок раздела без решёток. Перепиши текст раздела "
            "целиком, сохранив смысл и стиль заметки."
        ),
        parameters={
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
        writes=True,
    ),
    Action(
        name="clarify",
        title="Уточнить",
        hint="когда запрос не подходит ни под одну команду или непонятно, что менять",
        description=(
            "Задать пользователю один короткий вопрос и предложить выбрать "
            "действие кнопками."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Что именно уточнить"}
            },
            "required": ["question"],
        },
    ),
)


def tools() -> list[dict[str, Any]]:
    """Схемы инструментов в формате OpenAI для всех команд."""
    return [
        {
            "type": "function",
            "function": {
                "name": action.name,
                "description": action.description,
                "parameters": action.parameters,
            },
        }
        for action in ACTIONS
    ]


def prompt_block() -> str:
    """Блок промпта со списком команд — собирается из реестра."""
    lines = ["КОМАНДЫ (пользователь может называть их по-своему):"]
    for action in ACTIONS:
        lines.append(f"- {action.name} — {action.title}: {action.hint}.")
    return "\n".join(lines)


def write_actions() -> tuple[str, ...]:
    """Имена команд, которые меняют заметки."""
    return tuple(action.name for action in ACTIONS if action.writes)
