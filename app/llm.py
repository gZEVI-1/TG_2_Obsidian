"""Клиент OpenAI-совместимого API (LLM)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Sequence

import aiohttp

from app.config import Settings

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 60
MAX_TOKENS = 800


class LLMError(RuntimeError):
    """Ошибка при обращении к LLM."""


@dataclass(frozen=True)
class ToolCall:
    """Запрос модели на вызов инструмента."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class LLMResponse:
    """Ответ модели: текст и, если инструменты передавались, вызовы инструментов."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    model: str = ""
    finish_reason: str = ""


class LLMClient:
    """Минимальный клиент: одно сообщение — один ответ, без ретраев.

    Одна сессия aiohttp на клиент; ретраи сознательно не делаются (Этап 9).
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.llm_base_url:
            raise LLMError("LLM_BASE_URL не задан в .env")
        if not settings.llm_api_key:
            raise LLMError("LLM_API_KEY не задан в .env")
        if not settings.llm_model:
            raise LLMError("LLM_MODEL не задан в .env")
        self._url = settings.llm_base_url.rstrip("/") + "/chat/completions"
        self._api_key = settings.llm_api_key
        self._model = settings.llm_model
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> LLMClient:
        await self._ensure_session()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS),
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        max_tokens: int = MAX_TOKENS,
    ) -> LLMResponse:
        """Отправляет сообщения модели и возвращает её ответ."""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": list(messages),
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = list(tools)

        session = await self._ensure_session()
        async with session.post(self._url, json=payload) as response:
            body = await response.text()
            if response.status != 200:
                raise LLMError(
                    f"POST /chat/completions: HTTP {response.status}: {_shorten(body)}"
                )

        try:
            data = json.loads(body)
        except json.JSONDecodeError as error:
            raise LLMError(f"Ответ LLM не является JSON: {_shorten(body)}") from error

        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"LLM не вернула choices: {_shorten(body)}")

        message = choices[0].get("message") or {}
        text = (message.get("content") or "").strip()
        tool_calls = tuple(
            ToolCall(
                id=str(call.get("id", "")),
                name=str((call.get("function") or {}).get("name", "")),
                arguments=str((call.get("function") or {}).get("arguments", "")),
            )
            for call in message.get("tool_calls") or []
        )

        log.debug(
            "LLM: model=%s finish=%s text=%d симв. tool_calls=%d",
            data.get("model", self._model),
            choices[0].get("finish_reason", ""),
            len(text),
            len(tool_calls),
        )
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            model=str(data.get("model", self._model)),
            finish_reason=str(choices[0].get("finish_reason", "")),
        )


def _shorten(text: str, limit: int = 300) -> str:
    """Обрезает тело ответа для сообщения об ошибке."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"
