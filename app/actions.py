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
        name="list_notes",
        title="Заметки в папке",
        hint="когда нужно посмотреть, какие заметки лежат в папке",
        description=(
            "Показать заметки хранилища. folder — папка, если нужно только её "
            "содержимое; пусто — все заметки."
        ),
        parameters={
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": "Папка относительно корня; пусто — всё хранилище",
                }
            },
        },
    ),
    Action(
        name="list_folders",
        title="Папки хранилища",
        hint="когда нужно посмотреть папки или понять, куда положить заметку",
        description="Показать папки хранилища Obsidian, включая пустые.",
        parameters={"type": "object", "properties": {}},
    ),
    Action(
        name="list_templates",
        title="Шаблоны заметок",
        hint="когда пользователь просит оформить заметку по шаблону",
        description=(
            "Показать шаблоны из папки шаблонов. Прочитай подходящий через "
            "read_note и оформи заметку по его разделам."
        ),
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
            "Создать новую заметку. title — заголовок без пути, content — полный "
            "текст в Markdown, folder — папка, если пользователь её назвал "
            "(«создай папку Люди» → folder=«Люди»). Если папка не названа — не "
            "заполняй: бот предложит варианты. Подтверждение и запись — на стороне "
            "бота. Текст перепиши и разверни, не копируй сообщение дословно."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Заголовок и имя файла"},
                "content": {"type": "string", "description": "Полный текст в Markdown"},
                "folder": {
                    "type": "string",
                    "description": (
                        "Папка хранилища, если пользователь её назвал. Пусто — "
                        "пусть бот предложит варианты."
                    ),
                },
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
        name="create_section",
        title="Создать раздел",
        hint="когда в заметке нужен новый раздел с заголовком",
        description=(
            "Добавить новый раздел в конец существующей заметки. heading — "
            "заголовок без решёток, content — текст раздела (может быть пустым). "
            "Если такой раздел уже есть, бот откажется создавать дубль. Сначала "
            "прочитай заметку, чтобы не дублировать существующие разделы."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Путь существующей заметки"},
                "heading": {
                    "type": "string",
                    "description": "Заголовок нового раздела",
                },
                "content": {
                    "type": "string",
                    "description": "Текст раздела в Markdown",
                },
                "level": {
                    "type": "integer",
                    "description": "Уровень заголовка 1–6, по умолчанию 2 (##)",
                },
            },
            "required": ["path", "heading"],
        },
        writes=True,
    ),
    Action(
        name="create_folder",
        title="Создать папку",
        hint="когда просят завести новую папку в хранилище",
        description=(
            "Создать папку по пути относительно корня хранилища, например "
            "«Проекты/2026». Не используй служебные папки с точкой."
        ),
        parameters={
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": (
                        "Путь новой папки, например «Люди» или «Проекты/2026»"
                    ),
                }
            },
            "required": ["folder"],
        },
        writes=True,
    ),
    Action(
        name="rename_folder",
        title="Переименовать папку",
        hint="когда просят переименовать или перенести папку",
        description=(
            "Переименовать папку или перенести её в другое место. path — текущий "
            "путь папки, new_path — новый путь. Содержимое папки сохраняется."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Текущий путь папки"},
                "new_path": {"type": "string", "description": "Новый путь папки"},
            },
            "required": ["path", "new_path"],
        },
        writes=True,
    ),
    Action(
        name="delete_folder",
        title="Удалить папку",
        hint="когда просят удалить папку вместе с содержимым",
        description=(
            "Удалить папку со всем содержимым. Бот покажет подтверждение; "
            "в файловом режиме папка перемещается в `.trash`."
        ),
        parameters={
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "Путь папки"}
            },
            "required": ["folder"],
        },
        writes=True,
    ),
    Action(
        name="move_note",
        title="Переместить заметку",
        hint="когда просят перенести заметку в другую папку или переименовать",
        description=(
            "Переместить или переименовать заметку. path — текущий путь, "
            "destination — полный новый путь с «.md» либо папка (тогда имя файла "
            "сохраняется). Сначала найди точный путь через search_notes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Текущий путь заметки"},
                "destination": {
                    "type": "string",
                    "description": "Новый путь с .md или папка назначения",
                },
            },
            "required": ["path", "destination"],
        },
        writes=True,
    ),
    Action(
        name="copy_note",
        title="Скопировать заметку",
        hint="когда просят сделать копию заметки",
        description=(
            "Скопировать заметку. path — исходный путь, destination — полный "
            "новый путь с «.md» либо папка (тогда имя файла сохраняется). "
            "Существующий файл не перезаписывается."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Путь исходной заметки"},
                "destination": {
                    "type": "string",
                    "description": "Новый путь с .md или папка назначения",
                },
            },
            "required": ["path", "destination"],
        },
        writes=True,
    ),
    Action(
        name="delete_note",
        title="Удалить заметку",
        hint="когда просят удалить заметку",
        description=(
            "Удалить заметку. Бот покажет подтверждение; в файловом режиме "
            "заметка перемещается в `.trash`, в REST-режиме — в корзину Obsidian. "
            "Сначала найди точный путь через search_notes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Путь заметки"}
            },
            "required": ["path"],
        },
        writes=True,
    ),
    Action(
        name="remove_text",
        title="Удалить текст",
        hint="когда просят убрать строку, ссылку, пункт списка или абзац",
        description=(
            "Удалить фрагмент из заметки. text — точный текст из заметки: "
            "строка, пункт списка, ссылка [[Имя]] или абзац. Для ссылки можно "
            "передать и просто имя. Перед вызовом прочитай заметку и передай "
            "текст так, как он там написан. Если просят «удали связь на X» — "
            "передай саму ссылку или имя X, а не придумывай пустой edit_section."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Путь существующей заметки"},
                "text": {
                    "type": "string",
                    "description": "Точный фрагмент, который нужно удалить",
                },
            },
            "required": ["path", "text"],
        },
        writes=True,
    ),
    Action(
        name="add_links",
        title="Связать заметку",
        hint="когда просят поставить или добавить связи в одной заметке",
        description=(
            "Подобрать связи для существующей заметки и добавить их в раздел "
            "«Связи». Кандидатов ищет бот, подтверждение спрашивает тоже бот."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Путь существующей заметки"}
            },
            "required": ["path"],
        },
        writes=True,
    ),
    Action(
        name="link_all",
        title="Связи во всём хранилище",
        hint="когда просят проставить связи во всех заметках сразу",
        description=(
            "Массовая простановка связей: бот строит план по всем заметкам, "
            "показывает сводку, спрашивает подтверждение и отчитывается об ошибках."
        ),
        parameters={"type": "object", "properties": {}},
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
