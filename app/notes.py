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
    inline: str = ""


def _split_heading_title(title: str) -> tuple[str, str]:
    """Делит заголовок на название и текст после двоеточия.

    `### Братья/сестры : Тетя Оксана` → («Братья/сестры», «Тетя Оксана»).
    Двоеточие в самом названии сохраняется, если после него ничего нет.
    """
    cleaned = title.strip()
    match = re.match(r"^(.*?)\s*[:：]\s*(.*)$", cleaned)
    if not match:
        return cleaned, ""
    name = match.group(1).strip()
    tail = match.group(2).strip()
    if not name or name.casefold() in ("http", "https"):
        return cleaned, ""
    return name, tail


def _display_heading(title: str) -> str:
    """Название раздела для кнопок: без решёток, ссылок и разметки."""
    name, _tail = _split_heading_title(title)
    name = re.sub(r"[*_`]+", "", name)
    name = name.replace("[[", " ").replace("]]", " ")
    return re.sub(r"\s+", " ", name).strip()


def _normalize_heading(value: str) -> str:
    """Нормализует заголовок для сравнения: текст, а не разметка."""
    text = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    text = re.sub(r"[*_`]+", "", text)
    text = text.replace("[[", " ").replace("]]", " ")
    text = re.sub(r"\s*/\s*", "/", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t#:;,.!?—–-")


def _heading_match_kind(actual: str, wanted: str) -> int:
    """0 — не совпало, 1 — совпало название, 2 — совпал заголовок целиком."""
    wanted_key = _normalize_heading(wanted)
    if not wanted_key:
        return 0
    if _normalize_heading(actual) == wanted_key:
        return 2
    if _normalize_heading(_split_heading_title(actual)[0]) == wanted_key:
        return 1
    return 0


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


def _missing_section_message(text: str, heading: str) -> str:
    names = sections(text)
    if not names:
        return f"раздел «{heading}» не найден"
    shown = ", ".join(f"«{name}»" for name in names[:6])
    return f"раздел «{heading}» не найден. Есть: {shown}"


def replace_section(text: str, heading: str, body: str) -> str:
    """Заменяет тело раздела, сохраняя frontmatter и остальные разделы.

    Пустое тело очищает раздел: заголовок остаётся, тело исчезает.
    """
    section = find_section(text, heading)
    if section is None:
        raise ValueError(_missing_section_message(text, heading))

    lines = text.splitlines(keepends=True)
    if section.inline:
        heading_line = lines[section.start - 1]
        match = _HEADING_RE.match(heading_line.rstrip("\r\n"))
        if match is not None:
            name = _display_heading(match.group(2))
            ending = "\r\n" if heading_line.endswith("\r\n") else "\n"
            lines[section.start - 1] = f"{match.group(1)} {name}{ending}"

    has_next = section.end < len(lines)
    clean = body.strip("\n")
    if clean:
        block = "\n" + clean + ("\n\n" if has_next else "\n")
    else:
        block = "\n" if has_next else ""
    return "".join(lines[: section.start] + [block] + lines[section.end :])


def clear_section(text: str, heading: str) -> str:
    """Убирает тело раздела, оставляя заголовок и остальную заметку."""
    return replace_section(text, heading, "")


def append_to_section(text: str, heading: str, body: str) -> str:
    """Дописывает текст в конец существующего раздела."""
    section = find_section(text, heading)
    if section is None:
        raise ValueError(_missing_section_message(text, heading))

    lines = text.splitlines(keepends=True)
    body_text = "".join(lines[section.start : section.end]).strip("\n")
    existing = "\n".join(
        part for part in (section.inline, body_text) if part
    )
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
_CONJUNCTIONS = frozenset({"и", "или", "а", "но", "да", "с", "со"})


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
            name = _display_heading(match.group(2))
            if name:
                result.append(name)
    return result


def contains_text(note: str, fragment: str) -> bool:
    """Проверяет, есть ли фрагмент в заметке (без учёта регистра и пробелов)."""
    def normalize(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().lower()

    needle = normalize(fragment)
    return bool(needle) and needle in normalize(note)


def remove_text(note: str, fragment: str) -> tuple[str, int]:
    """Удаляет фрагмент из тела заметки: строку, ссылку или абзац.

    Frontmatter не трогается. Строка удаляется целиком, если совпадает с
    фрагментом (с учётом маркера списка); внутри строки фрагмент вырезается,
    после чего чистятся лишние пробелы. Ссылку можно передать как `[[Имя]]`,
    так и просто именем — ссылки на это имя тоже находятся.

    Возвращает (новый текст, сколько вхождений удалено); если ничего не
    найдено, поднимает `ValueError`.
    """
    needle = fragment.strip()
    if not needle:
        raise ValueError("пустой фрагмент")

    head, body = _split_frontmatter(note)
    target = _link_target(needle)
    link_pattern, text_pattern = _removal_patterns(needle, target)

    removed = 0
    kept: list[str] = []
    for line in body.splitlines(keepends=True):
        content = _line_content(line)
        is_heading = bool(_HEADING_RE.match(line.rstrip("\r\n")))
        if is_heading and not needle.startswith("#"):
            # Заголовки не чистим по словам: иначе раздел останется без названия.
            kept.append(line)
            continue
        if _same_text(content, needle) or (target and _same_text(content, target)):
            removed += 1
            continue
        if is_heading:
            kept.append(line)
            continue
        updated, count = _remove_in_line(line, link_pattern, text_pattern)
        if not count:
            kept.append(line)
            continue
        removed += count
        if _line_is_empty(updated):
            continue
        kept.append(updated)

    if not removed and "\n" in needle:
        # Абзац из нескольких строк ищем подстрокой целиком.
        pattern = re.compile(re.escape(needle), re.IGNORECASE)
        body, removed = pattern.subn("", body)
        if not removed:
            raise ValueError("такой текст в заметке не найден")
        return head + _collapse_blank_lines(body), removed

    if not removed:
        raise ValueError("такой текст в заметке не найден")
    return head + _collapse_blank_lines("".join(kept)), removed


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Отделяет YAML-frontmatter в начале заметки: его удалять нельзя."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[: index + 1]), "".join(lines[index + 1 :])
    return "", text


def _link_target(fragment: str) -> str:
    """Имя внутри ссылки: `[[Путь/Имя|альяс#якорь]]` → `Имя` или путь."""
    raw = fragment.strip()
    if raw.startswith("[[") and raw.endswith("]]"):
        raw = raw[2:-2]
    return raw.split("|", 1)[0].split("#", 1)[0].strip().strip("/")


def _removal_patterns(
    needle: str, target: str
) -> tuple[re.Pattern[str] | None, re.Pattern[str]]:
    """Шаблон ссылок на имя и шаблон самого текста.

    Ссылки на другое имя (`[[Александр Шевчук]]` при удалении «Шевчук») не
    считаются совпадением: иначе в заметке осталась бы битая ссылка.
    """
    link_pattern = None
    if target:
        link_pattern = re.compile(
            rf"\[\[(?:[^\]|]*/)?{re.escape(target)}(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]",
            re.IGNORECASE,
        )
    return link_pattern, re.compile(re.escape(needle), re.IGNORECASE)


def _line_content(line: str) -> str:
    """Строка без маркера списка и крайних пробелов."""
    return re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line).strip()


def _same_text(left: str, right: str) -> bool:
    """Сравнение текста без ссылочных скобок, регистра и лишних пробелов."""
    def normalize(value: str) -> str:
        value = value.casefold().replace("ё", "е")
        value = re.sub(r"[\[\]|#]", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    return bool(normalize(right)) and normalize(left) == normalize(right)


def _remove_in_line(
    line: str,
    link_pattern: re.Pattern[str] | None,
    text_pattern: re.Pattern[str],
) -> tuple[str, int]:
    """Вырезает ссылки на имя и сам текст вне ссылок; прибирает пробелы."""
    updated = line
    count = 0
    if link_pattern is not None:
        updated, found = link_pattern.subn("", updated)
        count += found
    updated, found = _sub_outside_links(text_pattern, updated)
    count += found
    if not count:
        return line, 0
    return _tidy_line(updated), count


_LINK_SPAN_RE = re.compile(r"\[\[[^\[\]]*\]\]")


def _sub_outside_links(
    pattern: re.Pattern[str], text: str
) -> tuple[str, int]:
    """Заменяет только вне `[[...]]`: содержимое чужих ссылок не трогаем."""
    parts: list[str] = []
    count = 0
    last = 0
    for match in _LINK_SPAN_RE.finditer(text):
        chunk, found = pattern.subn("", text[last : match.start()])
        parts.append(chunk)
        parts.append(match.group(0))
        count += found
        last = match.end()
    chunk, found = pattern.subn("", text[last:])
    parts.append(chunk)
    count += found
    return "".join(parts), count


def _tidy_line(line: str) -> str:
    """Убирает лишние пробелы и опустевшие скобки, сохраняя перевод строки."""
    body = line.rstrip("\r\n")
    ending = line[len(body) :]
    body = re.sub(r"[ \t]{2,}", " ", body)
    body = re.sub(r"[ \t]+([,.;:!?])", r"\1", body)
    body = re.sub(r"\(\s*\)|\[\s*\]", "", body)
    return body.rstrip() + ending


def _line_is_empty(line: str) -> bool:
    """Строка осталась пустой: маркер списка, знаки и союзы без слов."""
    content = _line_content(line).strip(" -–—•*·,.;:!?()[]")
    words = [word for word in re.split(r"[\s,;:]+", content) if word]
    if not words:
        return True
    return all(word.casefold() in _CONJUNCTIONS for word in words)


def _collapse_blank_lines(text: str) -> str:
    """Три и больше переводов строки — это лишние пустые строки после удаления."""
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text)


def _sections(text: str, heading: str) -> list[Section]:
    lines = text.splitlines(keepends=True)
    headings: list[tuple[int, int, str, str]] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line.rstrip("\n"))
        if not match:
            continue
        title = match.group(2).strip()
        _name, inline = _split_heading_title(title)
        headings.append((index, len(match.group(1)), title, inline))

    matches: list[tuple[int, int, int, int, str]] = []
    for position, (index, level, title, inline) in enumerate(headings):
        kind = _heading_match_kind(title, heading)
        if kind:
            matches.append((kind, position, index, level, inline))
    if not matches:
        return []
    best = max(kind for kind, _, _, _, _ in matches)

    result: list[Section] = []
    for kind, position, index, level, inline in matches:
        if kind != best:
            continue
        end = len(lines)
        for next_index, next_level, _, _ in headings[position + 1 :]:
            if next_level <= level:
                end = next_index
                break
        result.append(
            Section(level=level, start=index + 1, end=end, inline=inline)
        )
    return result
