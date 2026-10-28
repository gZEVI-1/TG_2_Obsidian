"""Сопоставление запроса с именами заметок: точное и нечёткое."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

FUZZY_THRESHOLD = 0.78
MIN_FUZZY_LENGTH = 3


def normalize(text: str) -> str:
    """Нижний регистр, «ё» → «е», пунктуация — в пробелы."""
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def note_name(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".md")


def rank(query: str, paths: list[str]) -> list[tuple[str, float, str]]:
    """Оценивает имена файлов по запросу, лучшие — первыми.

    Возвращает (путь, оценка, тип): «имя» — совпадение по названию,
    «похоже» — нечёткое (опечатки, другой порядок слов).
    Оценка: 1.0 — точное имя, 0.95 — имя начинается с запроса, 0.9 — запрос
    внутри имени, 0.85 — все слова запроса есть в имени, 0.8 — запрос в пути.
    Ниже 0.8 — нечёткое совпадение по порогу.
    """
    needle = normalize(query)
    if not needle:
        return []

    tokens = needle.split()
    scored: list[tuple[str, float, str]] = []

    for path in paths:
        name = normalize(note_name(path))
        full = normalize(path)

        if name == needle:
            scored.append((path, 1.0, "имя"))
        elif name.startswith(needle):
            scored.append((path, 0.95, "имя"))
        elif needle in name:
            scored.append((path, 0.9, "имя"))
        elif tokens and all(token in name for token in tokens):
            scored.append((path, 0.85, "имя"))
        elif needle in full:
            scored.append((path, 0.8, "имя"))
        else:
            score = _fuzzy_score(needle, tokens, name, full)
            if score >= FUZZY_THRESHOLD:
                scored.append((path, 0.5 + score / 2, "похоже"))

    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored


def _fuzzy_score(needle: str, tokens: list[str], name: str, full: str) -> float:
    """Нечёткое сходство запроса с именем файла и с сегментами пути."""
    if len(needle) < MIN_FUZZY_LENGTH:
        return 0.0

    best = SequenceMatcher(None, needle, name).ratio()
    path_tokens = full.split()
    for token in tokens:
        if len(token) < MIN_FUZZY_LENGTH:
            continue
        for candidate in path_tokens:
            best = max(best, SequenceMatcher(None, token, candidate).ratio())
    return best
