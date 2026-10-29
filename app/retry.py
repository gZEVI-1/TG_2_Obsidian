"""Повторы временных сбоев: сеть, таймауты, 429 и 5xx.

Повторяются только идемпотентные операции (чтение). Запись не повторяется:
повтор после сбоя мог бы создать дубль заметки или ссылки.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

import aiohttp

log = logging.getLogger(__name__)

T = TypeVar("T")

TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
DEFAULT_ATTEMPTS = 3
DEFAULT_DELAY = 0.5
MAX_DELAY = 5.0


def is_transient(error: BaseException) -> bool:
    """Сбой временный: сеть, таймаут, 429 или 5xx."""
    status = getattr(error, "status", None)
    if status in TRANSIENT_STATUSES:
        return True
    return isinstance(error, (aiohttp.ClientError, asyncio.TimeoutError))


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    delay: float = DEFAULT_DELAY,
    what: str = "запрос",
    retry_if: Callable[[BaseException], bool] = is_transient,
) -> T:
    """Повторяет операцию, пока ошибка временная и попытки не кончились."""
    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as error:  # noqa: BLE001 — фильтр ниже решает, повторять ли
            if not retry_if(error) or attempt == attempts:
                raise
            pause = _pause(error, delay, attempt)
            log.warning(
                "%s: временная ошибка (%s), повтор %d из %d через %.1f с",
                what,
                error,
                attempt,
                attempts - 1,
                pause,
            )
            await asyncio.sleep(pause)

    raise RuntimeError("retry_async: недостижимая точка")  # pragma: no cover


def _pause(error: BaseException, delay: float, attempt: int) -> float:
    """Пауза перед повтором: экспонента, подсказка сервера и небольшой разброс."""
    retry_after = getattr(error, "retry_after", None)
    if isinstance(retry_after, (int, float)) and retry_after > 0:
        return min(float(retry_after), MAX_DELAY * 4)
    pause = min(delay * 2 ** (attempt - 1), MAX_DELAY)
    return pause + random.uniform(0, delay / 2)


def retryable_read(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Декоратор для идемпотентных чтений клиента хранилища.

    Параметры повторов берутся из клиента (`_retry_attempts`, `_retry_delay`),
    поэтому настройки видны в одном месте — в конфиге.
    """

    @functools.wraps(func)
    async def wrapper(self, *args, **kwargs):
        return await retry_async(
            lambda: func(self, *args, **kwargs),
            attempts=getattr(self, "_retry_attempts", DEFAULT_ATTEMPTS),
            delay=getattr(self, "_retry_delay", DEFAULT_DELAY),
            what=f"Obsidian: {func.__name__}",
        )

    return wrapper
