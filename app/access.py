"""Ограничение доступа к боту по списку Telegram id."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

log = logging.getLogger(__name__)


class AllowedUsersMiddleware(BaseMiddleware):
    """Пускает только перечисленных пользователей, остальным не отвечает.

    Пустой список означает «пускать всех»: так прежние `.env` продолжают
    работать, но при старте пишется предупреждение. Молчание вместо ответа
    выбрано намеренно: чужой не узнаёт, что бот жив, и флуд не превращается
    в переписку. Id отказавшегося попадает в лог — его можно скопировать
    в `TELEGRAM_ALLOWED_USERS`.
    """

    def __init__(self, allowed: frozenset[int]) -> None:
        self._allowed = allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if not self._allowed or user is None or user.id in self._allowed:
            return await handler(event, data)

        log.warning(
            "Отказ в доступе: id=%s, username=%s. "
            "Добавьте id в TELEGRAM_ALLOWED_USERS.",
            user.id,
            user.username or "<без username>",
        )
        return None
