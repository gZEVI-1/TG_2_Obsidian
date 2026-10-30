"""Постоянные запреты связей: «никогда не ставь».

Правило — пара «заметка → цель»; источник `*` запрещает цель во всех
заметках. Правила лежат в `state/link_rules.json`, меняются только командой
бота и действуют на все автоматические предложения связей.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.links import link_target

WILDCARD = "*"


def load(path: Path) -> tuple[tuple[str, str], ...]:
    """Читает правила; отсутствующий или испорченный файл — пустой список."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ()
    try:
        data = json.loads(raw)
    except ValueError:
        return ()
    if not isinstance(data, list):
        return ()

    rules: list[tuple[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source", "")).strip()
        target = str(item.get("target", "")).strip()
        if source and target:
            rules.append((source, target))
    return tuple(rules)


def save(path: Path, rules: tuple[tuple[str, str], ...]) -> None:
    """Записывает правила целиком, подменяя файл атомарно."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [{"source": source, "target": target} for source, target in rules]
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def add(
    rules: tuple[tuple[str, str], ...],
    source: str,
    target: str,
) -> tuple[tuple[str, str], ...]:
    """Добавляет правило без дублей; пустое правило не добавляется."""
    source = _clean_note(source)
    target = _clean_target(target)
    if not source or not target:
        return rules
    if any(
        _same_source(rule_source, source) and _same_target(rule_target, target)
        for rule_source, rule_target in rules
    ):
        return rules
    return (*rules, (source, target))


def is_blocked(
    source: str,
    target: str,
    rules: tuple[tuple[str, str], ...],
) -> bool:
    """Запрещена ли связь source → target этими правилами."""
    if not rules:
        return False
    for rule_source, rule_target in rules:
        if not _same_target(rule_target, target):
            continue
        if rule_source.strip() == WILDCARD:
            return True
        if _same_note(rule_source, source):
            return True
    return False


def describe(source: str, target: str) -> str:
    """Правило для сообщения пользователю."""
    if source.strip() == WILDCARD:
        return f"[[{target}]] — из любой заметки"
    return f"[[{target}]] — из «{source}»"


def _clean_note(value: str) -> str:
    value = value.replace("\\", "/").strip().strip("/")
    if value.casefold().endswith(".md"):
        value = value[:-3]
    return value


def _clean_target(value: str) -> str:
    value = value.replace("[[", " ").replace("]]", " ")
    return link_target(value).strip().strip("/")


def _norm_note(value: str) -> str:
    return _clean_note(value).casefold()


def _norm_target(value: str) -> str:
    return _clean_target(value).casefold()


def _same_source(left: str, right: str) -> bool:
    if left.strip() == WILDCARD or right.strip() == WILDCARD:
        return left.strip() == right.strip()
    return _same_note(left, right)


def _same_note(left: str, right: str) -> bool:
    first = _norm_note(left)
    second = _norm_note(right)
    if not first or not second:
        return False
    if "/" in first and "/" in second:
        return first == second
    return (
        first == second
        or first.rsplit("/", 1)[-1] == second.rsplit("/", 1)[-1]
    )


def _same_target(left: str, right: str) -> bool:
    first = _norm_target(left)
    second = _norm_target(right)
    if not first or not second:
        return False
    if "/" in first and "/" in second:
        return first == second
    return (
        first == second
        or first.rsplit("/", 1)[-1] == second.rsplit("/", 1)[-1]
    )
