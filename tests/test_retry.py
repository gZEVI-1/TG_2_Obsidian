"""Тесты повторов временных сбоев."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import aiohttp

from app.obsidian import ObsidianError
from app.retry import is_transient, retry_async


def _run(coro):
    return asyncio.run(coro)


class TransientTests(unittest.TestCase):
    def test_server_and_rate_limit_errors_are_transient(self) -> None:
        for status in (429, 500, 502, 503, 504):
            self.assertTrue(is_transient(ObsidianError("x", status)))

    def test_client_errors_are_not_retried(self) -> None:
        for status in (400, 401, 403, 404, 409):
            self.assertFalse(is_transient(ObsidianError("x", status)))

    def test_network_errors_are_transient(self) -> None:
        self.assertTrue(is_transient(aiohttp.ClientError("нет сети")))
        self.assertTrue(is_transient(asyncio.TimeoutError()))


class RetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sleeps: list[float] = []
        self._patch = patch("app.retry.asyncio.sleep", new=self._sleep)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def test_retries_transient_and_returns_result(self) -> None:
        calls = {"count": 0}

        async def operation() -> str:
            calls["count"] += 1
            if calls["count"] < 3:
                raise ObsidianError("HTTP 503", 503)
            return "ok"

        self.assertEqual(_run(retry_async(operation, attempts=3, delay=0.1)), "ok")
        self.assertEqual(calls["count"], 3)
        self.assertEqual(len(self.sleeps), 2)
        self.assertLess(self.sleeps[0], self.sleeps[1])

    def test_not_found_is_not_retried(self) -> None:
        calls = {"count": 0}

        async def operation() -> str:
            calls["count"] += 1
            raise ObsidianError("HTTP 404", 404)

        with self.assertRaises(ObsidianError):
            _run(retry_async(operation, attempts=3))
        self.assertEqual(calls["count"], 1)
        self.assertEqual(self.sleeps, [])

    def test_raises_after_all_attempts(self) -> None:
        calls = {"count": 0}

        async def operation() -> str:
            calls["count"] += 1
            raise ObsidianError("HTTP 500", 500)

        with self.assertRaises(ObsidianError):
            _run(retry_async(operation, attempts=2))
        self.assertEqual(calls["count"], 2)

    def test_retry_after_is_respected(self) -> None:
        calls = {"count": 0}
        error = ObsidianError("HTTP 429", 429)
        error.retry_after = 2.0

        async def operation() -> str:
            calls["count"] += 1
            if calls["count"] == 1:
                raise error
            return "ok"

        _run(retry_async(operation, attempts=2, delay=0.1))
        self.assertEqual(self.sleeps, [2.0])

    def test_custom_filter(self) -> None:
        calls = {"count": 0}

        async def operation() -> str:
            calls["count"] += 1
            if calls["count"] == 1:
                raise ObsidianError("HTTP 404", 404)
            return "ok"

        result = _run(
            retry_async(operation, attempts=2, retry_if=lambda error: True)
        )
        self.assertEqual(result, "ok")
        self.assertEqual(calls["count"], 2)


if __name__ == "__main__":
    unittest.main()
