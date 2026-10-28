"""Текст заметок: имена файлов, пути и разделы Markdown."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

MAX_PATH_LENGTH = 200
MAX_TITLE_LENGTH = 80
FORBIDDEN_PATH_CHARS = '\\/:*?"<>|#^[]'

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
_FOLDER_TOKEN_RE = re.compile(r"[\w\-/]+", re.UNICODE)
_FOLDER_WORD_RE = re.compile(r"[^\W\d_][^\W\d_\-]*", re.UNICODE)

# Слова, которые в ответе «создай папку люди и добавь туда» не являются папкой.
FOLDER_STOPWORDS = frozenset(
    {
        "создай", "создайте", "создать", "сделай", "сделайте", "сделать",
        "добавь", "добавьте", "добавить", "сохрани", "сохраните", "положи",
        "положите", "запиши", "запишите", "нужно", "надо", "давай",
        "папку", "папка", "папке", "папки", "новую", "новая", "новой",
        "и", "а", "в", "во", "на", "туда", "там", "эту", "это", "её", "ее",
        "заметку", "заметка", "файл", "пожалуйста", "потом", "затем", "сюда",
    }
)


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


def clean_folder(name: str) -> str:
    """Безопасный путь папки: сегменты через `slugify`, без «.» и «..»."""
    parts = [
        slugify(part.strip())
        for part in name.replace("\\", "/").split("/")
        if part.strip() and part.strip() not in (".", "..")
    ]
    return "/".join(part for part in parts if part and part != "Без названия")


def folder_from_text(text: str, folders: Sequence[str] = ()) -> str:
    """Название папки из ответа пользователя.

    «Создай папку люди и добавь туда» → «Люди» (если такая папка есть),
    «Проекты/2026» → «Проекты/2026». Если в тексте нет папки, а есть фраза —
    пустая строка: такой ответ нельзя превращать в путь.
    """
    words = [
        token
        for token in _FOLDER_TOKEN_RE.findall(text)
        if token.casefold() not in FOLDER_STOPWORDS
    ]
    if not words:
        return ""
    if len(words) > 2 and "папк" not in text.casefold():
        return ""

    candidate = clean_folder(" ".join(words[:4]))
    for folder in folders:
        if folder.casefold() == candidate.casefold():
            return folder
    return candidate


def split_folder_title(title: str, folders: Sequence[str] = ()) -> tuple[str, str]:
    """Отделяет папку, если модель положила её в заголовок: «Люди/Имя».

    Папкой считается первый сегмент, если он совпадает с существующей папкой
    или выглядит как одно слово без цифр («Люди»). Заголовки вроде
    «Q3/Q4 итоги» не трогаем: «Q3» папкой не считается.
    """
    head, separator, rest = title.partition("/")
    head, rest = head.strip(), rest.strip()
    if not separator or not head or not rest:
        return title, ""

    known = {folder.casefold() for folder in folders}
    looks_like_folder = bool(_FOLDER_WORD_RE.fullmatch(head)) and len(head) <= 30
    if head.casefold() in known or looks_like_folder:
        return rest, head
    return title, ""


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


def create_section(text: str, heading: str, body: str = "", level: int = 2) -> str:
    """Добавляет новый раздел в конец заметки (FR-03).

    Заголовок не должен уже существовать: иначе получится дубль, и правка
    раздела по имени станет неоднозначной. Frontmatter и остальные разделы
    сохраняются.
    """
    title = heading.strip().lstrip("#").strip()
    if not title:
        raise ValueError("не указан заголовок раздела")
    if level < 1 or level > 6:
        raise ValueError("уровень раздела — от 1 до 6")
    if any(existing.casefold() == title.casefold() for existing in sections(text)):
        raise ValueError(f"раздел «{title}» уже существует")

    block = f"{'#' * level} {title}"
    body = body.strip("\n")
    if body:
        block = f"{block}\n\n{body}"
    base = text.rstrip("\n")
    return f"{base}\n\n{block}\n" if base else f"{block}\n"


def validate_folder(folder: str) -> str:
    """Проверяет путь папки: без выхода вверх и служебных каталогов."""
    value = folder.strip().replace("\\", "/").strip("/")
    if not value:
        raise ValueError("путь папки пуст")
    if len(value) > MAX_PATH_LENGTH:
        raise ValueError(f"путь папки длиннее {MAX_PATH_LENGTH} символов")

    for part in value.split("/"):
        if not part.strip():
            raise ValueError("в пути папки есть пустой сегмент")
        if part in (".", ".."):
            raise ValueError("в пути папки есть «.» или «..»")
        if part.startswith("."):
            raise ValueError(f"служебная папка «{part}»")
        if any(char in part for char in FORBIDDEN_PATH_CHARS):
            raise ValueError(f"недопустимый символ в «{part}»")
    return value


def destination_path(source: str, destination: str) -> str:
    """Куда переносить/копировать заметку.

    Полный путь с «.md» принимается как есть; иначе это папка, и имя файла
    сохраняется. Путь не может совпадать с исходным.
    """
    source = validate_path(source)
    raw = destination.strip().replace("\\", "/").strip("/")
    if not raw:
        raise ValueError("не указано, куда перенести заметку")

    if raw.lower().endswith(".md"):
        target = validate_path(raw)
    else:
        folder = validate_folder(raw)
        name = source.rsplit("/", 1)[-1]
        target = validate_path(f"{folder}/{name}" if folder else name)

    if target == source:
        raise ValueError("новый путь совпадает со старым")
    return target


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
