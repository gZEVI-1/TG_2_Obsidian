"""Связи между заметками: разбор ссылок, упоминания и раздел «Связи».

Функции чистые, без сети: связи ставят агент и бот, правила — FR-10.
Ссылка добавляется только на существующую заметку, без самоссылок и дублей;
раздел «Связи» дополняется, авторский текст не меняется.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

LINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
DEFAULT_LINK_HEADING = "Связи"
MIN_MENTION_LENGTH = 4


def note_title(path: str) -> str:
    """Имя заметки без папки и расширения."""
    return path.rsplit("/", 1)[-1].removesuffix(".md")


def link_target(raw: str) -> str:
    """Цель ссылки: `[[Путь/Имя|текст#раздел]]` → `Путь/Имя`."""
    target = raw.split("|", 1)[0].split("#", 1)[0]
    return target.strip().strip("/")


def existing_links(text: str) -> set[str]:
    """Цели всех ссылок заметки: полный путь и имя файла, в нижнем регистре."""
    targets: set[str] = set()
    for raw in LINK_RE.findall(text):
        target = link_target(raw).casefold()
        if not target:
            continue
        targets.add(target)
        targets.add(target.rsplit("/", 1)[-1])
    return targets


def is_linked(target: str, linked: set[str]) -> bool:
    """Есть ли такая ссылка: сравниваем и с путём, и с именем файла."""
    value = target.casefold()
    return value in linked or value.rsplit("/", 1)[-1] in linked


def duplicate_titles(paths: Sequence[str]) -> set[str]:
    """Имена, которые встречаются в разных папках (ссылка пойдёт через путь)."""
    counts: dict[str, int] = {}
    for path in paths:
        key = note_title(path).casefold()
        counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count > 1}


def link_label(path: str, duplicates: set[str]) -> str:
    """Строка для `[[...]]`: имя, а для неуникального имени — путь без `.md`."""
    title = note_title(path)
    return path.removesuffix(".md") if title.casefold() in duplicates else title


def mention_paths(
    text: str,
    paths: Sequence[str],
    *,
    exclude: str = "",
    limit: int = 0,
    strict: bool = False,
) -> list[str]:
    """Заметки, названия которых упомянуты в тексте, в порядке упоминания.

    `exclude` — сама заметка (самоссылки запрещены), `limit` — сколько вернуть.
    Короткие названия (< 4 символов) не ищем: слишком много ложных срабатываний.
    `strict` — считать упоминанием только написание с заглавной буквы или цифры;
    так массовая простановка не цепляется за обычные слова вроде «человек».
    """
    haystack = _caseful(text)
    if not haystack:
        return []

    excluded = exclude.strip("/").casefold()
    found: list[tuple[int, str]] = []
    for path in paths:
        if excluded and path.strip("/").casefold() == excluded:
            continue
        title = note_title(path)
        if len(title.strip()) < MIN_MENTION_LENGTH:
            continue
        position = _mention_position(haystack, _caseful(title), strict=strict)
        if position >= 0:
            found.append((position, path))

    found.sort()
    result = [path for _, path in found]
    return result[:limit] if limit else result


def _caseful(text: str) -> str:
    """Нормализует текст, сохраняя регистр: он нужен для strict-режима."""
    value = text.replace("ё", "е").replace("Ё", "Е")
    value = re.sub(r"[^\w\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _mention_position(haystack: str, needle: str, *, strict: bool) -> int:
    """Позиция упоминания или -1: слово целиком, без учёта регистра."""
    if not needle:
        return -1
    pattern = rf"(?<!\w){re.escape(needle)}(?!\w)"
    for match in re.finditer(pattern, haystack, re.IGNORECASE):
        first = match.group(0)[0]
        if not strict or not first.isalpha() or first.isupper():
            return match.start()
    return -1


def add_links(
    text: str,
    targets: Sequence[str],
    *,
    heading: str = DEFAULT_LINK_HEADING,
) -> str:
    """Добавляет `- [[...]]` в раздел «Связи», не меняя авторский текст.

    Существующий раздел (любого уровня, в том числе `###### Связи`) дополняется;
    если раздела нет — он создаётся в конце заметки. Уже имеющиеся ссылки и
    повторы в `targets` пропускаются, поэтому повторный вызов ничего не меняет.
    """
    linked = existing_links(text)
    lines: list[str] = []
    seen: set[str] = set()
    for target in targets:
        value = target.strip()
        if not value or is_linked(value, linked) or value.casefold() in seen:
            continue
        seen.add(value.casefold())
        lines.append(f"- [[{value}]]")
    if not lines:
        return text

    span = _link_section(text, heading)
    if span is None:
        base = text.rstrip("\n")
        block = f"## {heading}\n\n" + "\n".join(lines)
        return f"{base}\n\n{block}\n" if base else f"{block}\n"

    start, end = span
    body = text.splitlines()[start:end]
    while body and not body[-1].strip():
        body.pop()

    closing: list[str] = []
    if body and body[-1].strip() == "---":
        closing = [body.pop()]
        while body and not body[-1].strip():
            body.pop()

    updated = [*body, ""] if body else [""]
    updated.extend(lines)
    if closing:
        updated.extend(["", *closing])

    tail = text.splitlines()[end:]
    if tail:
        updated.append("")
    head = text.splitlines()[:start]
    return "\n".join([*head, *updated, *tail]).rstrip("\n") + "\n"


def _link_section(text: str, heading: str) -> tuple[int, int] | None:
    """Тело последнего раздела «Связи»: (первая строка тела, строка за ним)."""
    target = heading.strip().casefold()
    lines = text.splitlines()
    headings: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if match:
            headings.append(
                (index, len(match.group(1)), match.group(2).strip().casefold())
            )

    span: tuple[int, int] | None = None
    for position, (index, level, title) in enumerate(headings):
        if title != target:
            continue
        end = len(lines)
        for next_index, next_level, _ in headings[position + 1 :]:
            if next_level <= level:
                end = next_index
                break
        span = (index + 1, end)
    return span
