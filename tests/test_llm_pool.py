"""Тесты пула LLM: ротация аккаунтов/моделей, заглушки и автосброс."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import (
    DEFAULT_STUB_MODELS,
    LLMAccount,
    Settings,
    load_settings,
)
from app.llm import (
    LLMAllExhaustedError,
    LLMAuthError,
    LLMPool,
    LLMRateLimitError,
    LLMResponse,
)


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "telegram_bot_token": "test-token",
        "llm_base_url": "",
        "llm_api_key": "",
        "llm_model": "",
        "llm_accounts": (
            LLMAccount("account-1", "https://api.example/v1", "key-1"),
            LLMAccount("account-2", "https://api.example/v1", "key-2"),
        ),
        "llm_models": ("best-model", "backup-model"),
        "llm_cooldown_seconds": 60,
        "llm_stub": False,
        "llm_stub_accounts": 2,
        "llm_stub_quota": 1,
        "llm_stub_reset_seconds": 60,
        "obsidian_base_url": "http://127.0.0.1:27123",
        "obsidian_api_key": "",
        "obsidian_dry_run": True,
        "agent_confirmation": "auto",
        "agent_default_folder": "Inbox",
        "agent_auto_folder": True,
        "agent_search_limit": 5,
        "log_level": "INFO",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


class ScriptedClient:
    """Клиент для теста: отвечает по сценарию и записывает порядок попыток."""

    def __init__(self, account: LLMAccount, model: str, handler, calls: list) -> None:
        self._account = account
        self._model = model
        self._handler = handler
        self._calls = calls

    async def chat(self, messages, *, tools=None, max_tokens=800) -> LLMResponse:
        self._calls.append((self._model, self._account.name))
        result = self._handler(self._model, self._account.name)
        if isinstance(result, Exception):
            raise result
        return result

    async def close(self) -> None:
        return None


def make_factory(handler, calls: list):
    def factory(account: LLMAccount, model: str) -> ScriptedClient:
        return ScriptedClient(account, model, handler, calls)

    return factory


def ok(model: str) -> LLMResponse:
    return LLMResponse(text="ok", model=model, finish_reason="stop")


def rate_limit() -> LLMRateLimitError:
    return LLMRateLimitError("лимит", status=429, retry_after=10)


class LLMPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_rotates_within_one_model(self) -> None:
        calls: list[tuple[str, str]] = []

        def handler(model: str, account: str):
            if (model, account) == ("best-model", "account-1"):
                return rate_limit()
            return ok(model)

        pool = LLMPool(make_settings(), client_factory=make_factory(handler, calls))
        try:
            response = await pool.chat([{"role": "user", "content": "привет"}])

            self.assertEqual(response.model, "best-model")
            self.assertEqual(
                calls,
                [("best-model", "account-1"), ("best-model", "account-2")],
            )
            status = pool.status()
            self.assertEqual(status.active_account, "account-2")
            self.assertEqual(status.available_slots, 3)
        finally:
            await pool.close()

    async def test_model_changes_only_after_all_accounts(self) -> None:
        calls: list[tuple[str, str]] = []

        def handler(model: str, account: str):
            if model == "best-model":
                return rate_limit()
            return ok(model)

        pool = LLMPool(make_settings(), client_factory=make_factory(handler, calls))
        try:
            response = await pool.chat([{"role": "user", "content": "привет"}])

            self.assertEqual(response.model, "backup-model")
            self.assertEqual(
                calls,
                [
                    ("best-model", "account-1"),
                    ("best-model", "account-2"),
                    ("backup-model", "account-1"),
                ],
            )
            status = pool.status()
            self.assertEqual(status.active_model, "backup-model")
            self.assertEqual(status.active_account, "account-1")
            self.assertEqual(len(status.exhausted), 2)
        finally:
            await pool.close()

    async def test_all_exhausted_then_auto_reset_to_initial_state(self) -> None:
        now = [1_000.0]
        calls: list[tuple[str, str]] = []

        def handler(model: str, account: str):
            return rate_limit()

        pool = LLMPool(
            make_settings(),
            client_factory=make_factory(handler, calls),
            clock=lambda: now[0],
        )
        try:
            with self.assertRaises(LLMAllExhaustedError) as caught:
                await pool.chat([{"role": "user", "content": "привет"}])
            self.assertEqual(caught.exception.reset_at, 1_010.0)

            status = pool.status()
            self.assertEqual(status.available_slots, 0)
            self.assertEqual(status.reset_at, 1_010.0)

            now[0] = 1_011.0
            pool._maybe_auto_reset()

            status = pool.status()
            self.assertEqual(status.active_model, "best-model")
            self.assertEqual(status.active_account, "account-1")
            self.assertEqual(status.available_slots, 4)
            self.assertIsNone(status.reset_at)
        finally:
            await pool.close()

    async def test_auth_error_disables_account_until_reset(self) -> None:
        calls: list[tuple[str, str]] = []

        def handler(model: str, account: str):
            if account == "account-1":
                return LLMAuthError("ключ отклонён", status=401)
            return ok(model)

        pool = LLMPool(make_settings(), client_factory=make_factory(handler, calls))
        try:
            await pool.chat([{"role": "user", "content": "первый"}])
            await pool.chat([{"role": "user", "content": "второй"}])

            self.assertEqual(
                calls,
                [
                    ("best-model", "account-1"),
                    ("best-model", "account-2"),
                    ("best-model", "account-2"),
                ],
            )
            self.assertEqual(pool.status().disabled, ("account-1",))

            pool.reset(reason="тест")
            self.assertEqual(pool.status().disabled, ())
        finally:
            await pool.close()

    async def test_stub_mode_rotates_and_exhausts(self) -> None:
        clock = lambda: 500.0
        settings = make_settings(
            llm_stub=True,
            llm_accounts=(),
            llm_models=("m1", "m2"),
            llm_stub_accounts=2,
            llm_stub_quota=1,
            llm_stub_reset_seconds=30,
        )
        pool = LLMPool(settings, clock=clock)
        try:
            models = []
            for _ in range(4):
                response = await pool.chat([{"role": "user", "content": "тест"}])
                models.append(response.model)
                self.assertIn("Заглушка LLM", response.text)

            self.assertEqual(models, ["m1", "m1", "m2", "m2"])
            # Последняя пара ещё не помечена: квота кончится на следующем запросе.
            self.assertEqual(pool.status().available_slots, 1)
            with self.assertRaises(LLMAllExhaustedError):
                await pool.chat([{"role": "user", "content": "тест"}])
            self.assertEqual(pool.status().available_slots, 0)

            pool.reset(reason="тест")
            status = pool.status()
            self.assertEqual(status.active_model, "m1")
            self.assertEqual(status.active_account, "stub-1")
            self.assertEqual(status.available_slots, 4)
        finally:
            await pool.close()


class SettingsTests(unittest.TestCase):
    def test_stub_mode_needs_no_real_accounts(self) -> None:
        with patch.dict(os.environ, {"LLM_STUB": "true"}, clear=True), patch(
            "app.config.ENV_FILE", Path("no-such.env")
        ):
            settings = load_settings()

        self.assertTrue(settings.llm_stub)
        self.assertEqual(settings.llm_models, DEFAULT_STUB_MODELS)
        self.assertEqual(
            [account.name for account in settings.llm_accounts],
            ["stub-1", "stub-2", "stub-3"],
        )

    def test_real_mode_reads_multiple_accounts(self) -> None:
        env = {
            "LLM_STUB": "false",
            "LLM_MODELS": "best, backup",
            "LLM_API_1_NAME": "first",
            "LLM_API_1_BASE_URL": "https://api.example/v1",
            "LLM_API_1_KEY": "key-1",
            "LLM_API_2_BASE_URL": "https://api.example/v1",
            "LLM_API_2_KEY": "key-2",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "app.config.ENV_FILE", Path("no-such.env")
        ):
            settings = load_settings()

        self.assertFalse(settings.llm_stub)
        self.assertEqual(settings.llm_models, ("best", "backup"))
        self.assertEqual(
            [account.name for account in settings.llm_accounts],
            ["first", "account-2"],
        )


if __name__ == "__main__":
    unittest.main()
