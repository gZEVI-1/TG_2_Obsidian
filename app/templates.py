"""Шаблоны заметок: поиск в хранилище и подстановка значений.

Шаблон — обычная заметка в папке `AGENT_TEMPLATE_FOLDER` (по умолчанию
`Templates`). Поддерживаются плейсхолдеры `{{title}}`, `{{date}}`, `{{time}}`
и формат в стиле Templater: `{{date:DD.MM.YY}}`, `{{time:HH:mm}}`.
Остальные `{{...}}` остаются как есть — их может обработать Obsidian.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import PurePosixPath

PLACEHOLDER_RE = re.compile(r"\{\{\s*(title|date|time)(?::([^{}]*))?\s*\}\}")
FORMAT_TOKEN_RE = re.compile(r"YYYY|YY|MM|DD|HH|mm|ss|M|D")
DATE_FORMAT = "%Y-%m-%d"
TIME_FORMAT = "%H:%M"
MAX_TEMPLATE_CHARS = 4000


def template_paths(notes: list[str], folder: str, *, exclude: str = "") -> list[str]:
    """Шаблоны — `.md`-файлы непосредственно в папке шаблонов, по алфавиту.

    `exclude` — путь, который не считается шаблоном (например, заметка стиля).
    """
    folder = folder.strip().strip("/").casefold()
    prefix = f"{folder}/" if folder else ""
    excluded = exclude.strip().strip("/").casefold()
    result: list[str] = []
    for note in notes:
        normalized = note.strip("/")
        if not normalized.lower().endswith(".md"):
            continue
        if excluded and normalized.casefold() == excluded:
            continue
        if prefix and not normalized.casefold().startswith(prefix):
            continue
        rest = normalized[len(prefix) :]
        if not rest or "/" in rest:
            continue
        result.append(normalized)
    return sorted(result)


def template_name(path: str) -> str:
    """Имя шаблона без папки и расширения — для кнопки."""
    return PurePosixPath(path).stem


def _format_moment(moment: datetime, pattern: str) -> str:
    """Формат в стиле Templater: YYYY, YY, MM, M, DD, D, HH, mm, ss."""
    values = {
        "YYYY": f"{moment.year:04d}",
        "YY": f"{moment.year % 100:02d}",
        "MM": f"{moment.month:02d}",
        "M": str(moment.month),
        "DD": f"{moment.day:02d}",
        "D": str(moment.day),
        "HH": f"{moment.hour:02d}",
        "mm": f"{moment.minute:02d}",
        "ss": f"{moment.second:02d}",
    }
    return FORMAT_TOKEN_RE.sub(lambda match: values[match.group(0)], pattern)


def render(text: str, title: str, now: datetime | None = None) -> str:
    """Подставляет заголовок, дату и время; прочие плейсхолдеры не трогает."""
    moment = now or datetime.now()

    def replace(match: re.Match[str]) -> str:
        name, pattern = match.group(1), match.group(2)
        if name == "title":
            return title
        if pattern:
            return _format_moment(moment, pattern)
        if name == "date":
            return moment.strftime(DATE_FORMAT)
        return moment.strftime(TIME_FORMAT)

    return PLACEHOLDER_RE.sub(replace, text)
