"""Тесты доступа: middleware списка id и разбор TELEGRAM_ALLOWED_USERS."""

from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Dispatcher

from app.access import AllowedUsersMiddleware
from app.bot import register_access
from app.config import load_settings


class AllowedUsersTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def call(self, middleware: AllowedUsersMiddleware, user: object) -> tuple:
        handler = AsyncMock(return_value="handled")
        result = self.run_async(
            middleware(handler, object(), {"event_from_user": user})
        )
        return result, handler

    def test_allowed_user_passes(self) -> None:
        middleware = AllowedUsersMiddleware(frozenset({42}))
        result, handler = self.call(middleware, SimpleNamespace(id=42, username="me"))
        self.assertEqual(result, "handled")
        handler.assert_awaited_once()

    def test_stranger_is_silently_ignored(self) -> None:
        middleware = AllowedUsersMiddleware(frozenset({42}))
        result, handler = self.call(middleware, SimpleNamespace(id=7, username="x"))
        self.assertIsNone(result)
        handler.assert_not_awaited()

    def test_empty_list_allows_everyone(self) -> None:
        middleware = AllowedUsersMiddleware(frozenset())
        result, handler = self.call(middleware, SimpleNamespace(id=7, username="x"))
        self.assertEqual(result, "handled")
        handler.assert_awaited_once()

    def test_event_without_user_passes(self) -> None:
        middleware = AllowedUsersMiddleware(frozenset({42}))
        handler = AsyncMock(return_value="handled")
        result = self.run_async(middleware(handler, object(), {}))
        self.assertEqual(result, "handled")

    def test_refusal_does_not_reveal_details(self) -> None:
        middleware = AllowedUsersMiddleware(frozenset({42}))
        user = SimpleNamespace(id=7, username=None)
        result, _handler = self.call(middleware, user)
        self.assertIsNone(result)

    def test_registered_for_messages_and_callbacks(self) -> None:
        dispatcher = Dispatcher()
        register_access(dispatcher, (42,))
        self.assertEqual(len(dispatcher.message.outer_middleware), 1)
        self.assertEqual(len(dispatcher.callback_query.outer_middleware), 1)


class AllowedUsersConfigTests(unittest.TestCase):
    def load(self, **env: str):
        with patch.dict(os.environ, env, clear=True), patch(
            "app.config.ENV_FILE", Path("no-such.env")
        ):
            return load_settings()

    def test_ids_are_parsed_with_commas_and_spaces(self) -> None:
        settings = self.load(TELEGRAM_ALLOWED_USERS="123, 456 789")
        self.assertEqual(settings.telegram_allowed_users, (123, 456, 789))

    def test_duplicates_are_removed(self) -> None:
        settings = self.load(TELEGRAM_ALLOWED_USERS="42,42")
        self.assertEqual(settings.telegram_allowed_users, (42,))

    def test_empty_means_everyone(self) -> None:
        self.assertEqual(self.load().telegram_allowed_users, ())

    def test_non_numeric_value_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.load(TELEGRAM_ALLOWED_USERS="@durov")

    def test_zero_and_negative_are_rejected(self) -> None:
        for raw in ("0", "-5"):
            with self.assertRaises(ValueError):
                self.load(TELEGRAM_ALLOWED_USERS=raw)


if __name__ == "__main__":
    unittest.main()
