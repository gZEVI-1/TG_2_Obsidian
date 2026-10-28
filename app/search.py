"""Сопоставление запроса с заметками: имена файлов, затем их текст."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

FUZZY_THRESHOLD = 0.78
MIN_FUZZY_LENGTH = 3
MIN_TERM_LENGTH = 2
EXCERPT_BEFORE = 40
EXCERPT_AFTER = 80

# Служебные слова и глаголы поиска: в запросе «Найди "код заморожен"» они
# не должны мешать найти текст заметки. Сами по себе такие слова всё равно
# ищутся — см. query_terms().
_QUERY_STOP_WORDS = frozenset(
    """
    а без более бы был была были было быть в вам вас весь во вот все всего
    всех вы где да даже для до его ее если есть еще же за и из или им их к как
    ко когда кто ли либо мне может мы на надо наш не него нее нет ни них но ну
    о об однако он она они оно от очень по под при про с со так также такой там
    те тем то того тоже той только том ты у уже хотя чего чей чем что чтобы чья
    эта эти это я
    найди найдите найти поищи поищите ищи ищите покажи покажите поиск искать
    ищу ищем ищет
    заметка заметку заметке заметки заметок заметкам заметками заметках заметкой
    текст текста тексту текстом тексте тексты текстов
    содержимое содержимого содержимом
    пожалуйста плиз
    """.split()
)


def normalize(text: str) -> str:
    """Нижний регистр, «ё» → «е», пунктуация — в пробелы."""
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def note_name(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".md")


def query_terms(query: str) -> list[str]:
    """Слова запроса без служебных: «Найди "код заморожен"» → код, заморожен.

    Если после очистки ничего не осталось (запрос — одно служебное слово),
    возвращаются все слова: так поиск по самому слову «найди» продолжает
    работать.
    """
    words = normalize(query).split()
    terms = [
        word
        for word in words
        if word not in _QUERY_STOP_WORDS and len(word) >= MIN_TERM_LENGTH
    ]
    if terms:
        return terms
    return [word for word in words if len(word) >= MIN_TERM_LENGTH]


def content_score(text: str, query: str) -> tuple[float, int] | None:
    """Оценка совпадения запроса с текстом заметки; None — не совпало.

    Фраза целиком (1.0) важнее слов в любом порядке (0.9). Регистр, «ё»,
    кавычки и другая пунктуация не важны. Позиция — в нормализованном тексте.
    """
    haystack = normalize(text)
    terms = query_terms(query)
    if not haystack or not terms:
        return None

    position = haystack.find(" ".join(terms))
    if position >= 0:
        return 1.0, position

    positions = [haystack.find(term) for term in terms]
    if any(found < 0 for found in positions):
        return None
    return 0.9, min(positions)


def excerpt(
    text: str,
    query: str,
    *,
    before: int = EXCERPT_BEFORE,
    after: int = EXCERPT_AFTER,
) -> str:
    """Фрагмент исходного текста вокруг первого слова запроса."""
    flat = " ".join(text.split())
    lowered = flat.lower().replace("ё", "е")
    positions = [
        found for term in query_terms(query) if (found := lowered.find(term)) >= 0
    ]
    if not positions:
        return flat[: before + after]

    position = min(positions)
    start = max(0, position - before)
    end = min(len(flat), position + after)
    context = flat[start:end]
    if start:
        context = "…" + context
    if end < len(flat):
        context = context + "…"
    return context


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

    terms = query_terms(query)
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
        elif terms and all(term in name for term in terms):
            scored.append((path, 0.85, "имя"))
        elif needle in full:
            scored.append((path, 0.8, "имя"))
        else:
            score = _fuzzy_score(needle, terms, name, full)
            if score >= FUZZY_THRESHOLD:
                scored.append((path, 0.5 + score / 2, "похоже"))

    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored


def _fuzzy_score(needle: str, terms: list[str], name: str, full: str) -> float:
    """Нечёткое сходство запроса с именем файла и с сегментами пути."""
    if len(needle) < MIN_FUZZY_LENGTH:
        return 0.0

    best = SequenceMatcher(None, needle, name).ratio()
    path_tokens = full.split()
    for token in terms:
        if len(token) < MIN_FUZZY_LENGTH:
            continue
        for candidate in path_tokens:
            best = max(best, SequenceMatcher(None, token, candidate).ratio())
    return best
