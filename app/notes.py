"""Текст заметок: имена файлов, пути и разделы Markdown."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_PATH_LENGTH = 200
MAX_TITLE_LENGTH = 80
FORBIDDEN_PATH_CHARS = '\\/:*?"<>|#^[]'

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")


@dataclass(frozen=True)
class Section:
    """Раздел заметки: индексы строк тела (без строки заголовка)."""

    level: int
    start: int
    end: int


def slugify(title: str) -> str:
    """Превращает заголовок в безопасное имя файла."""
    text = unicodedata.normalize("NFKC", title)
    text = "".join(" " if char in FORBIDDEN_PATH_CHARS else char for char in text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = text[:MAX_TITLE_LENGTH].rstrip(" .")
    return text or "Без названия"


def build_path(folder: str, title: str) -> str:
    folder = folder.strip().strip("/")
    name = slugify(title) + ".md"
    return f"{folder}/{name}" if folder else name


def validate_path(path: str) -> str:
    """Проверяет путь заметки и возвращает его без крайних слэшей."""
    path = path.strip().strip("/")
    if not path:
        raise ValueError("путь пуст")
    if len(path) > MAX_PATH_LENGTH:
        raise ValueError(f"путь длиннее {MAX_PATH_LENGTH} символов")
    if not path.endswith(".md"):
        raise ValueError("путь должен оканчиваться на .md")

    for part in path.split("/"):
        if not part.strip():
            raise ValueError("в пути есть пустой сегмент")
        if part in (".", ".."):
            raise ValueError("в пути есть «.» или «..»")
        if any(char in part for char in FORBIDDEN_PATH_CHARS):
            raise ValueError(f"недопустимый символ в «{part}»")
    return path


def find_section(text: str, heading: str) -> Section | None:
    """Находит раздел по заголовку (без учёта регистра и решёток)."""
    sections = _sections(text, heading)
    if len(sections) > 1:
        raise ValueError(f"раздел «{heading}» встречается {len(sections)} раза")
    return sections[0] if sections else None


def replace_section(text: str, heading: str, body: str) -> str:
    """Заменяет тело раздела, сохраняя frontmatter и остальные разделы."""
    section = find_section(text, heading)
    if section is None:
        raise ValueError(f"раздел «{heading}» не найден")

    lines = text.splitlines(keepends=True)
    has_next = section.end < len(lines)
    block = "\n" + body.strip("\n") + ("\n\n" if has_next else "\n")
    return "".join(lines[: section.start] + [block] + lines[section.end :])


def append_to_section(text: str, heading: str, body: str) -> str:
    """Дописывает текст в конец существующего раздела."""
    section = find_section(text, heading)
    if section is None:
        raise ValueError(f"раздел «{heading}» не найден")

    lines = text.splitlines(keepends=True)
    existing = "".join(lines[section.start : section.end]).strip("\n")
    return replace_section(text, heading, _join_blocks(existing, body.strip("\n")))


def append_to_note(text: str, body: str) -> str:
    """Дописывает блок в конец заметки."""
    base = text.rstrip("\n")
    block = body.strip("\n")
    return f"{_join_blocks(base, block)}\n" if base else f"{block}\n"


_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _join_blocks(existing: str, block: str) -> str:
    """Склеивает блоки: элементы списка — без пустой строки, остальное — с ней."""
    if not existing:
        return block
    if not block:
        return existing

    last_line = existing.splitlines()[-1]
    first_line = block.splitlines()[0]
    separator = "\n" if _LIST_ITEM.match(last_line) and _LIST_ITEM.match(first_line) else "\n\n"
    return f"{existing}{separator}{block}"


def sections(text: str) -> list[str]:
    """Заголовки разделов (уровень 2 и глубже) в порядке появления.

    Заголовок первого уровня считается названием заметки и разделом не является.
    """
    result: list[str] = []
    for line in text.splitlines():
        match = _HEADING_RE.match(line.rstrip("\n"))
        if match and len(match.group(1)) >= 2:
            result.append(match.group(2).strip())
    return result


def contains_text(note: str, fragment: str) -> bool:
    """Проверяет, есть ли фрагмент в заметке (без учёта регистра и пробелов)."""
    def normalize(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().lower()

    needle = normalize(fragment)
    return bool(needle) and needle in normalize(note)


def _sections(text: str, heading: str) -> list[Section]:
    target = heading.strip().lower()
    if target.startswith("#"):
        target = target.lstrip("#").strip()

    lines = text.splitlines(keepends=True)
    headings: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line.rstrip("\n"))
        if match:
            headings.append((index, len(match.group(1)), match.group(2).strip().lower()))

    result: list[Section] = []
    for position, (index, level, title) in enumerate(headings):
        if title != target:
            continue
        end = len(lines)
        for next_index, next_level, _ in headings[position + 1 :]:
            if next_level <= level:
                end = next_index
                break
        result.append(Section(level=level, start=index + 1, end=end))
    return result
