"""Клиент OpenAI-совместимого API и пул с ротацией аккаунтов и моделей."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Protocol, Sequence

import aiohttp

from app.config import (
    DEFAULT_STUB_MODELS,
    LLMAccount,
    Settings,
    build_stub_accounts,
)

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 60
MAX_TOKENS = 800
# Страховка от случайно огромного пула: модели × аккаунты обычно меньше.
MAX_LLM_ATTEMPTS = 256
# Как часто фоновая задача проверяет, не пора ли вернуться к началу.
AUTO_RESET_TICK_SECONDS = 5.0


class LLMError(RuntimeError):
    """Ошибка при обращении к LLM."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class LLMRateLimitError(LLMError):
    """Лимит запросов или кредитов исчерпан (HTTP 402/429)."""


class LLMAuthError(LLMError):
    """Аккаунт отклонил ключ (HTTP 401/403)."""


class LLMAllExhaustedError(LLMError):
    """Лимиты исчерпаны на всех доступных парах «модель — аккаунт»."""

    def __init__(self, reset_at: float, details: str = "") -> None:
        self.reset_at = reset_at
        message = (
            "Лимиты исчерпаны на всех LLM-аккаунтах и моделях. "
            f"Автосброс в {_format_time(reset_at)}"
        )
        if details:
            message += f". Попытки: {details}"
        super().__init__(message, status=429)


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


@dataclass(frozen=True)
class LLMSlot:
    """Пара «модель — аккаунт», которая сейчас обслуживает запросы."""

    model: str
    account: str


@dataclass(frozen=True)
class LLMStatus:
    """Снимок состояния пула для логов и команды `/llm`."""

    active_model: str
    active_account: str
    models: tuple[str, ...]
    accounts: tuple[str, ...]
    total_slots: int
    available_slots: int
    exhausted: tuple[tuple[str, str, float], ...]
    disabled: tuple[str, ...]
    reset_at: float | None
    stub: bool = False


class ChatClient(Protocol):
    """Минимальный контракт клиента, который умеет один чат-запрос."""

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        max_tokens: int = MAX_TOKENS,
    ) -> LLMResponse: ...

    async def close(self) -> None: ...


class LLMClient:
    """Один эндпоинт: base_url + ключ + модель. Без ретраев внутри себя.

    Ротацию аккаунтов и моделей делает `LLMPool`; клиент лишь выполняет
    один запрос и переводит ответ провайдера в типизированную ошибку.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        if not base_url:
            raise LLMError("LLM: не задан base_url")
        if not api_key:
            raise LLMError("LLM: не задан ключ аккаунта")
        if not model:
            raise LLMError("LLM: не задана модель")
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> LLMClient:
        await self._ensure_session()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
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
        try:
            async with session.post(
                self._url,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            ) as response:
                body = await response.text()
                status = response.status
                retry_after = _parse_retry_after(
                    response.headers.get("Retry-After")
                )
        except (aiohttp.ClientError, TimeoutError) as error:
            # Сетевые ошибки и таймауты не должны всплывать наружу как
            # необработанные исключения: у клиента единый тип ошибок.
            raise LLMError(f"LLM недоступна: {error}") from error

        if status != 200:
            raise _error_type(status)(
                f"POST /chat/completions: HTTP {status}: {_shorten(body)}",
                status=status,
                retry_after=retry_after,
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


class StubLLMProvider:
    """Имитация нескольких API-аккаунтов без сети.

    У каждой пары «модель — аккаунт» своя квота запросов. Когда она
    заканчивается, клиент отвечает `429` с `Retry-After`, как настоящий
    провайдер. Это позволяет проверить ротацию и автосброс, не тратя
    реальные лимиты.
    """

    def __init__(
        self,
        accounts: Sequence[LLMAccount],
        models: Sequence[str],
        *,
        quota: int,
        reset_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._accounts = tuple(accounts)
        self._models = tuple(models)
        self._quota = max(1, quota)
        self._reset_seconds = max(1, reset_seconds)
        self._clock = clock
        self._used: dict[tuple[str, str], int] = {}
        self._blocked_until: dict[tuple[str, str], float] = {}

    def client(self, account: LLMAccount, model: str) -> StubLLMClient:
        return StubLLMClient(self, account.name, model)

    def chat(
        self,
        account: str,
        model: str,
        messages: Sequence[dict[str, Any]],
    ) -> LLMResponse:
        now = self._clock()
        key = (model, account)
        blocked_until = self._blocked_until.get(key)
        if blocked_until is not None:
            if blocked_until > now:
                raise LLMRateLimitError(
                    f"Заглушка: квота {model} / {account} исчерпана",
                    status=429,
                    retry_after=blocked_until - now,
                )
            # Время вышло — начинаем квоту заново.
            self._blocked_until.pop(key, None)
            self._used[key] = 0

        used = self._used.get(key, 0)
        if used >= self._quota:
            until = now + self._reset_seconds
            self._blocked_until[key] = until
            raise LLMRateLimitError(
                f"Заглушка: квота {model} / {account} исчерпана",
                status=429,
                retry_after=self._reset_seconds,
            )

        self._used[key] = used + 1
        prompt = _last_user_text(messages)
        return LLMResponse(
            text=(
                f"Заглушка LLM: модель {model}, аккаунт {account}. "
                f"Реальный API не вызывался. Запрос: {_shorten(prompt, 80)}"
            ),
            model=model,
            finish_reason="stop",
        )

    def reset(self) -> None:
        """Возвращает все заглушки к полной квоте."""
        self._used.clear()
        self._blocked_until.clear()
        log.info(
            "LLM: заглушки сброшены (моделей %d, аккаунтов %d, квота %d)",
            len(self._models),
            len(self._accounts),
            self._quota,
        )


class StubLLMClient:
    """Один клиент заглушки: имя аккаунта + модель."""

    def __init__(self, provider: StubLLMProvider, account: str, model: str) -> None:
        self._provider = provider
        self._account = account
        self._model = model

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        max_tokens: int = MAX_TOKENS,
    ) -> LLMResponse:
        # tools/max_tokens намеренно игнорируются: заглушка не вызывает модель.
        return self._provider.chat(self._account, self._model, messages)

    async def close(self) -> None:
        return None


class LLMPool:
    """Пул LLM: модели по убыванию качества, внутри модели — аккаунты.

    Порядок работы:

    1. Запрос идёт на самую качественную доступную модель и первый
       доступный аккаунт.
    2. Лимит на аккаунте (`429`/`402`) — аккаунт помечается исчерпанным
       для этой модели, запрос повторяется на следующем аккаунте.
    3. Лимиты на всех аккаунтах модели — пул переходит к следующей модели
       (и снова начинает с первого аккаунта).
    4. Когда недоступны все пары «модель — аккаунт», пул запоминает время
       автосброса. По его наступлении все пометки снимаются, и работа
       начинается заново с лучшей модели и первого аккаунта.

    Аккаунт с отклонённым ключом (`401`/`403`) отключается до общего сброса
    (автосброса или `/llm_reset`).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[[LLMAccount, str], ChatClient] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        accounts = tuple(settings.llm_accounts)
        models = tuple(settings.llm_models)
        if settings.llm_stub:
            # В режиме заглушек ключи не нужны: если аккаунты не заданы,
            # создаём их сами. Реальные аккаунты в этом режиме не используются.
            if not accounts:
                accounts = build_stub_accounts(settings.llm_stub_accounts)
            if not models:
                models = DEFAULT_STUB_MODELS

        if not accounts:
            raise LLMError(
                "LLM: не задан ни один аккаунт. Заполните LLM_API_1_BASE_URL "
                "и LLM_API_1_KEY в .env (или LLM_BASE_URL/LLM_API_KEY), "
                "либо включите LLM_STUB=true."
            )
        if not models:
            raise LLMError(
                "LLM: не задан ни одна модель. Заполните LLM_MODELS в .env."
            )

        self._accounts = accounts
        self._models = models
        self._cooldown = float(settings.llm_cooldown_seconds)
        self._clock = clock or time.time
        self._stub = bool(settings.llm_stub)
        self._custom_factory = client_factory
        self._stub_provider: StubLLMProvider | None = None
        if self._stub and client_factory is None:
            self._stub_provider = StubLLMProvider(
                self._accounts,
                self._models,
                quota=settings.llm_stub_quota,
                reset_seconds=settings.llm_stub_reset_seconds,
                clock=self._clock,
            )

        self._session: aiohttp.ClientSession | None = None
        self._exhausted_until: dict[tuple[str, str], float] = {}
        self._disabled: set[str] = set()
        self._reset_at: float | None = None
        self._active = LLMSlot(model=self._models[0], account=self._accounts[0].name)
        self._closed = False
        self._reset_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> LLMPool:
        await self._ensure_session()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @property
    def uses_stub(self) -> bool:
        return self._stub

    async def _ensure_session(self) -> aiohttp.ClientSession | None:
        """Создаёт HTTP-сессию только для настоящих API."""
        if self._stub or self._custom_factory is not None:
            return None
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
            )
        return self._session

    def _build_client(self, account: LLMAccount, model: str) -> ChatClient:
        if self._custom_factory is not None:
            return self._custom_factory(account, model)
        if self._stub_provider is not None:
            return self._stub_provider.client(account, model)
        return LLMClient(
            account.base_url,
            account.api_key,
            model,
            session=self._session,
        )

    async def close(self) -> None:
        self._closed = True
        if self._reset_task is not None:
            self._reset_task.cancel()
            try:
                await self._reset_task
            except asyncio.CancelledError:
                pass
            self._reset_task = None
        if self._session is not None and not self._session.closed:
            await self._session.close()

    def start_auto_reset(self) -> None:
        """Запускает фоновый автосброс (нужен работающий event loop)."""
        if self._reset_task is None or self._reset_task.done():
            self._reset_task = asyncio.create_task(
                self._auto_reset_loop(), name="llm-auto-reset"
            )

    async def _auto_reset_loop(self) -> None:
        while not self._closed:
            delay = AUTO_RESET_TICK_SECONDS
            if self._reset_at is not None:
                delay = min(delay, max(0.05, self._reset_at - self._clock()))
            await asyncio.sleep(delay)
            if self._closed:
                break
            self._maybe_auto_reset()

    def reset(self, *, reason: str = "вручную") -> None:
        """Возвращает пул к начальным условиям: лучшая модель, первый аккаунт."""
        self._exhausted_until.clear()
        self._disabled.clear()
        self._reset_at = None
        self._active = LLMSlot(model=self._models[0], account=self._accounts[0].name)
        if self._stub_provider is not None:
            self._stub_provider.reset()
        log.warning(
            "LLM: сброс к начальным условиям (%s): модель %s, аккаунт %s",
            reason,
            self._active.model,
            self._active.account,
        )

    def status(self) -> LLMStatus:
        """Текущее состояние пула без секретов."""
        self._maybe_auto_reset()
        now = self._clock()
        exhausted = tuple(
            sorted(
                (model, account, until)
                for (model, account), until in self._exhausted_until.items()
                if until > now
            )
        )
        available = self._available_count(now)
        return LLMStatus(
            active_model=self._active.model,
            active_account=self._active.account,
            models=self._models,
            accounts=tuple(account.name for account in self._accounts),
            total_slots=len(self._models) * len(self._accounts),
            available_slots=available,
            exhausted=exhausted,
            disabled=tuple(sorted(self._disabled)),
            reset_at=self._reset_at,
            stub=self._stub,
        )

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        max_tokens: int = MAX_TOKENS,
    ) -> LLMResponse:
        """Пробует пары «модель — аккаунт», пока какая-нибудь не ответит."""
        async with self._lock:
            self._maybe_auto_reset()
            if self._reset_at is not None:
                raise LLMAllExhaustedError(
                    self._reset_at, "лимиты ещё не истекли, ждём автосброса"
                )

            await self._ensure_session()
            failures: list[str] = []
            attempts = 0

            for model in self._models:
                for account in self._accounts:
                    if attempts >= MAX_LLM_ATTEMPTS:
                        break
                    if account.name in self._disabled:
                        continue
                    key = (model, account.name)
                    now = self._clock()
                    until = self._exhausted_until.get(key)
                    if until is not None and until > now:
                        continue

                    attempts += 1
                    client = self._build_client(account, model)
                    try:
                        response = await client.chat(
                            messages, tools=tools, max_tokens=max_tokens
                        )
                    except LLMRateLimitError as error:
                        until = self._clock() + (
                            error.retry_after or self._cooldown
                        )
                        self._exhausted_until[key] = until
                        failures.append(
                            f"{account.name}/{model}: лимит до "
                            f"{_format_time(until)}"
                        )
                        log.warning(
                            "LLM: лимит на модели %s (аккаунт %s), переключаюсь: %s",
                            model,
                            account.name,
                            error,
                        )
                        continue
                    except LLMAuthError as error:
                        self._disabled.add(account.name)
                        failures.append(f"{account.name}: ключ отклонён")
                        log.error(
                            "LLM: аккаунт %s отключён до сброса, ключ отклонён: %s",
                            account.name,
                            error,
                        )
                        continue
                    except LLMError as error:
                        failures.append(
                            f"{account.name}/{model}: {_shorten(str(error), 80)}"
                        )
                        log.warning(
                            "LLM: попытка %s/%s не удалась: %s",
                            account.name,
                            model,
                            error,
                        )
                        continue

                    if (
                        self._active.model != model
                        or self._active.account != account.name
                    ):
                        log.warning(
                            "LLM: переключение на модель %s, аккаунт %s",
                            model,
                            account.name,
                        )
                    self._active = LLMSlot(model=model, account=account.name)
                    return response

                if attempts >= MAX_LLM_ATTEMPTS:
                    break

            now = self._clock()
            details = _join_failures(failures)

            if self._available_count(now) > 0:
                # Часть пар осталась доступной: это не общий лимит, а сбой
                # конкретных попыток. Следующий запрос попробует их снова.
                raise LLMError("LLM не ответила. " + details)

            future = [
                until for until in self._exhausted_until.values() if until > now
            ]
            if future:
                self._reset_at = min(future)
                raise LLMAllExhaustedError(self._reset_at, details)

            raise LLMAuthError(
                "Все LLM-аккаунты отключены: ключи отклонены. "
                "Проверьте LLM_API_*_KEY в .env и перезапустите бота. " + details
            )

    def _available_count(self, now: float) -> int:
        return sum(
            1
            for model in self._models
            for account in self._accounts
            if account.name not in self._disabled
            and self._exhausted_until.get((model, account.name), 0.0) <= now
        )

    def _maybe_auto_reset(self) -> None:
        now = self._clock()
        for key, until in list(self._exhausted_until.items()):
            if until <= now:
                del self._exhausted_until[key]
                log.info(
                    "LLM: лимит снова доступен: модель %s, аккаунт %s",
                    key[0],
                    key[1],
                )
        if self._reset_at is None or now < self._reset_at:
            return
        self.reset(reason="истекло время лимитов")


def _error_type(status: int) -> type[LLMError]:
    """Сопоставляет HTTP-код провайдера типу ошибки пула."""
    if status in (401, 403):
        return LLMAuthError
    if status in (402, 429):
        return LLMRateLimitError
    return LLMError


def _parse_retry_after(value: str | None) -> float | None:
    """Разбирает Retry-After: секунды или HTTP-дата."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment is None:
        return None
    return max(0.0, moment.timestamp() - time.time())


def _format_time(timestamp: float) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(timestamp))


def _join_failures(failures: list[str], limit: int = 4) -> str:
    """Короткая сводка попыток без секретов."""
    if not failures:
        return "попыток не было"
    shown = failures[:limit]
    text = "; ".join(shown)
    if len(failures) > limit:
        text += f"; ещё {len(failures) - limit}"
    return text


def _last_user_text(messages: Sequence[dict[str, Any]]) -> str:
    """Последнее сообщение пользователя: для текста заглушки."""
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
    return ""


def _shorten(text: str, limit: int = 300) -> str:
    """Обрезает тело ответа для сообщения об ошибке."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"
