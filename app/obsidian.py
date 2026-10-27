"""Клиент Obsidian Local REST API."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import aiohttp

from app.config import Settings

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10


class ObsidianError(RuntimeError):
    """Ошибка при обращении к Obsidian Local REST API."""


class ObsidianClient:
    """Минимальный клиент: проверка соединения, чтение и запись заметок.

    Запись по умолчанию не выполняется, если включён dry-run.
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.obsidian_api_key:
            raise ObsidianError("OBSIDIAN_API_KEY не задан в .env")
        self._base_url = settings.obsidian_base_url
        self._api_key = settings.obsidian_api_key
        self._dry_run = settings.obsidian_dry_run
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> ObsidianClient:
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

    def _note_url(self, path: str) -> str:
        return f"{self._base_url}/vault/{quote(path, safe='/')}"

    async def health(self) -> dict[str, Any]:
        """Проверяет, что API доступен и ключ принят."""
        session = await self._ensure_session()
        async with session.get(f"{self._base_url}/") as response:
            if response.status != 200:
                raise ObsidianError(f"GET /: HTTP {response.status}")
            return await response.json(content_type=None)

    async def note_exists(self, path: str) -> bool:
        session = await self._ensure_session()
        async with session.get(
            self._note_url(path), headers={"Accept": "application/json"}
        ) as response:
            if response.status == 200:
                return True
            if response.status == 404:
                return False
            raise ObsidianError(f"GET {path}: HTTP {response.status}")

    async def read_note(self, path: str) -> str:
        session = await self._ensure_session()
        async with session.get(
            self._note_url(path), headers={"Accept": "text/markdown"}
        ) as response:
            if response.status == 404:
                raise ObsidianError(f"Заметка не найдена: {path}")
            if response.status != 200:
                raise ObsidianError(f"GET {path}: HTTP {response.status}")
            return await response.text()

    async def write_note(self, path: str, content: str, *, overwrite: bool = False) -> bool:
        """Создаёт заметку. Возвращает True, если файл реально записан.

        В dry-run ничего не отправляется в Obsidian, возвращается False.
        """
        if self._dry_run:
            log.info(
                "[dry-run] запись заметки %s (%d симв.) пропущена", path, len(content)
            )
            return False

        if not overwrite and await self.note_exists(path):
            raise ObsidianError(f"Заметка уже существует: {path}. Перезапись запрещена.")

        session = await self._ensure_session()
        async with session.put(
            self._note_url(path),
            data=content.encode("utf-8"),
            headers={"Content-Type": "text/markdown; charset=utf-8"},
        ) as response:
            if response.status not in (200, 201, 204):
                body = await response.text()
                raise ObsidianError(f"PUT {path}: HTTP {response.status}: {body[:200]}")

        log.info("Заметка записана: %s", path)
        return True
