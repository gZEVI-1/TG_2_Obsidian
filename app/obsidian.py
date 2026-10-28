"""Клиенты хранилища Obsidian: Local REST API и прямой доступ к файлам.

Режим выбирается настройкой `OBSIDIAN_VAULT_PATH`:
- пусто — Local REST API (нужен запущенный Obsidian с плагином);
- задан путь к папке vault — чтение и запись .md-файлов напрямую,
  Obsidian при этом может быть закрыт.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

from app.config import Settings
from app.search import rank

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10
MAX_LISTED_NOTES = 500
MAX_DEPTH = 3

# Служебные папки Obsidian и систем контроля версий: в поиск не попадают.
IGNORED_DIRS = frozenset({".obsidian", ".trash", ".git", ".stfolder", ".sync"})
MAX_FILE_BYTES = 2 * 1024 * 1024


class ObsidianError(RuntimeError):
    """Ошибка при обращении к хранилищу Obsidian."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ObsidianBackend(ABC):
    """Общий интерфейс хранилища: проверка, поиск, чтение и запись заметок.

    Запись по умолчанию не выполняется, если включён dry-run.
    """

    async def __aenter__(self) -> ObsidianBackend:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Освобождает ресурсы; для файлового режима делать нечего."""

    @abstractmethod
    async def health(self) -> dict[str, Any]:
        """Проверяет, что хранилище доступно."""

    @abstractmethod
    async def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Полнотекстовый поиск по хранилищу (FR-09)."""

    @abstractmethod
    async def list_notes(self) -> list[str]:
        """Все .md-файлы хранилища, рекурсивно; глубина и число ограничены."""

    @abstractmethod
    async def folder_exists(self, folder: str) -> bool:
        """Проверяет, существует ли папка в хранилище."""

    @abstractmethod
    async def note_exists(self, path: str) -> bool:
        """Проверяет, существует ли заметка."""

    @abstractmethod
    async def read_note(self, path: str) -> str:
        """Читает заметку целиком."""

    @abstractmethod
    async def write_note(self, path: str, content: str, *, overwrite: bool = False) -> bool:
        """Создаёт или перезаписывает заметку. True — файл реально записан."""

    async def search_notes(self, query: str, limit: int = 5) -> list[dict[str, str]]:
        """Поиск по именам (точно и нечётко) и содержимому: имена — первыми.

        Штатный поиск Obsidian ранжирует совпадения по тексту и ссылкам,
        поэтому заметка с именем «Мария» может стоять ниже чужих упоминаний.
        Здесь сначала идут имена: точное, с этого слова, содержащее слово,
        затем похожие (опечатки, другой порядок слов), и только потом
        результаты полнотекстового поиска.
        """
        if not query.strip():
            return []

        hits: list[dict[str, str]] = []
        seen: set[str] = set()

        try:
            names = await self.list_notes()
        except (ObsidianError, aiohttp.ClientError):
            names = []

        for path, _score, kind in rank(query, names):
            hits.append(
                {
                    "path": path,
                    "match": kind,
                    "excerpt": (
                        "совпадение в названии"
                        if kind == "имя"
                        else "похожее название"
                    ),
                }
            )
            seen.add(path)
            if len(hits) >= limit:
                return hits[:limit]

        if len(hits) < limit:
            try:
                raw = await self.search(query, limit=limit * 2)
            except (ObsidianError, aiohttp.ClientError):
                raw = []

            for item in raw:
                path = str(item.get("filename", ""))
                if not path or path in seen:
                    continue
                context = " ".join(
                    (item.get("matches") or [{}])[0].get("context", "").split()
                )
                hits.append(
                    {"path": path, "match": "текст", "excerpt": context[:160]}
                )
                seen.add(path)
                if len(hits) >= limit:
                    break

        return hits[:limit]


class ObsidianClient(ObsidianBackend):
    """Клиент Obsidian Local REST API (Obsidian должен быть запущен)."""

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
                raise ObsidianError(f"GET /: HTTP {response.status}", response.status)
            return await response.json(content_type=None)

    async def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Полнотекстовый поиск по хранилищу (FR-09)."""
        session = await self._ensure_session()
        async with session.post(
            f"{self._base_url}/search/simple/",
            params={"query": query, "contextLength": 80},
        ) as response:
            if response.status != 200:
                raise ObsidianError(
                    f"POST /search/simple/: HTTP {response.status}", response.status
                )
            data = await response.json(content_type=None)
        return list(data)[:limit]

    async def list_notes(self) -> list[str]:
        """Все .md-файлы хранилища, рекурсивно; глубина и число ограничены."""
        notes: list[str] = []
        await self._collect_notes("", 0, notes)
        return sorted(notes)

    async def _collect_notes(self, folder: str, depth: int, out: list[str]) -> None:
        if depth > MAX_DEPTH or len(out) >= MAX_LISTED_NOTES:
            return

        url = f"{self._base_url}/vault/" + (
            quote(folder, safe="/") + "/" if folder else ""
        )
        session = await self._ensure_session()
        async with session.get(url, headers={"Accept": "application/json"}) as response:
            if response.status == 404:
                return
            if response.status != 200:
                raise ObsidianError(
                    f"GET /vault/{folder}: HTTP {response.status}", response.status
                )
            data = await response.json(content_type=None)

        for entry in data.get("files", []):
            if entry.endswith("/"):
                await self._collect_notes(f"{folder}/{entry}".strip("/"), depth + 1, out)
            elif entry.endswith(".md"):
                out.append(f"{folder}/{entry}".strip("/"))
            if len(out) >= MAX_LISTED_NOTES:
                return

    async def folder_exists(self, folder: str) -> bool:
        """Проверяет, существует ли папка в хранилище."""
        folder = folder.strip().strip("/")
        url = f"{self._base_url}/vault/" + (
            quote(folder, safe="/") + "/" if folder else ""
        )
        session = await self._ensure_session()
        async with session.get(url, headers={"Accept": "application/json"}) as response:
            if response.status == 200:
                return True
            if response.status == 404:
                return False
            raise ObsidianError(
                f"GET /vault/{folder}: HTTP {response.status}", response.status
            )

    async def note_exists(self, path: str) -> bool:
        session = await self._ensure_session()
        async with session.get(
            self._note_url(path), headers={"Accept": "application/json"}
        ) as response:
            if response.status == 200:
                return True
            if response.status == 404:
                return False
            raise ObsidianError(
                f"GET {path}: HTTP {response.status}", response.status
            )

    async def read_note(self, path: str) -> str:
        session = await self._ensure_session()
        async with session.get(
            self._note_url(path), headers={"Accept": "text/markdown"}
        ) as response:
            if response.status == 404:
                raise ObsidianError(f"Заметка не найдена: {path}", 404)
            if response.status != 200:
                raise ObsidianError(
                    f"GET {path}: HTTP {response.status}", response.status
                )
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
                raise ObsidianError(
                    f"PUT {path}: HTTP {response.status}: {body[:200]}", response.status
                )

        log.info("Заметка записана: %s", path)
        return True


class VaultClient(ObsidianBackend):
    """Прямая работа с файлами vault: Obsidian может быть не запущен.

    Все файловые операции выполняются в отдельном потоке, чтобы не блокировать
    цикл событий. Путь каждой заметки проверяется: выйти за пределы хранилища
    нельзя.
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.obsidian_vault_path:
            raise ObsidianError("OBSIDIAN_VAULT_PATH не задан в .env")

        root = Path(settings.obsidian_vault_path).expanduser()
        try:
            root = root.resolve(strict=True)
        except OSError as error:
            raise ObsidianError(
                f"Папка хранилища недоступна: {settings.obsidian_vault_path} ({error})"
            ) from error
        if not root.is_dir():
            raise ObsidianError(
                f"OBSIDIAN_VAULT_PATH не является папкой: {settings.obsidian_vault_path}"
            )

        self._root = root
        self._dry_run = settings.obsidian_dry_run

    async def health(self) -> dict[str, Any]:
        def probe() -> dict[str, Any]:
            if not self._root.is_dir():
                raise ObsidianError(f"Папка хранилища исчезла: {self._root}")
            return {
                "service": "Obsidian vault (direct filesystem)",
                "authenticated": True,
                "path": str(self._root),
                "writable": os.access(self._root, os.W_OK),
            }

        return await asyncio.to_thread(probe)

    def _resolve(self, path: str) -> Path:
        """Путь внутри vault; попытка выйти за его пределы — ошибка."""
        target = (self._root / path).resolve()
        if target != self._root and self._root not in target.parents:
            raise ObsidianError(f"Путь вне хранилища: {path}", 400)
        return target

    async def _call(self, func, *args):
        """Выполняет блокирующую файловую операцию в отдельном потоке."""
        try:
            return await asyncio.to_thread(func, *args)
        except ObsidianError:
            raise
        except OSError as error:
            raise ObsidianError(f"Файловая ошибка: {error}") from error

    def _scan_notes(self) -> list[str]:
        """Рекурсивный обход vault без служебных папок и символьных ссылок."""
        notes: list[str] = []

        def walk(folder: Path, depth: int) -> None:
            if depth > MAX_DEPTH or len(notes) >= MAX_LISTED_NOTES:
                return
            try:
                entries = sorted(folder.iterdir(), key=lambda item: item.name.lower())
            except OSError as error:
                raise ObsidianError(f"Не удалось прочитать {folder}: {error}") from error

            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name.startswith(".") or entry.name in IGNORED_DIRS:
                        continue
                    walk(entry, depth + 1)
                elif entry.suffix.lower() == ".md":
                    notes.append(entry.relative_to(self._root).as_posix())
                if len(notes) >= MAX_LISTED_NOTES:
                    return

        walk(self._root, 0)
        return sorted(notes)

    async def list_notes(self) -> list[str]:
        return await self._call(self._scan_notes)

    async def folder_exists(self, folder: str) -> bool:
        folder = folder.strip().strip("/")
        if not folder:
            return self._root.is_dir()
        return await self._call(lambda: self._resolve(folder).is_dir())

    async def note_exists(self, path: str) -> bool:
        return await self._call(lambda: self._resolve(path).is_file())

    async def read_note(self, path: str) -> str:
        def read() -> str:
            target = self._resolve(path)
            if not target.is_file():
                raise ObsidianError(f"Заметка не найдена: {path}", 404)
            return target.read_text(encoding="utf-8-sig", errors="replace")

        return await self._call(read)

    async def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Полнотекстовый поиск чтением файлов: индекса Obsidian не требуется."""
        needle = " ".join(query.split()).lower()
        if not needle:
            return []

        def scan() -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            for path in self._scan_notes():
                target = self._root / path
                try:
                    if target.stat().st_size > MAX_FILE_BYTES:
                        continue
                    text = target.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue
                flat = " ".join(text.split())
                position = flat.lower().find(needle)
                if position < 0:
                    continue
                start = max(0, position - 40)
                end = min(len(flat), position + len(needle) + 80)
                context = flat[start:end]
                if start:
                    context = "…" + context
                if end < len(flat):
                    context = context + "…"
                results.append(
                    {"filename": path, "matches": [{"context": context}]}
                )
                if len(results) >= limit:
                    break
            return results

        return await self._call(scan)

    async def write_note(self, path: str, content: str, *, overwrite: bool = False) -> bool:
        """Пишет заметку через временный файл, чтобы не оставить половину."""
        if self._dry_run:
            log.info(
                "[dry-run] запись заметки %s (%d симв.) пропущена", path, len(content)
            )
            return False

        def write() -> bool:
            target = self._resolve(path)
            if target.exists() and not overwrite:
                raise ObsidianError(
                    f"Заметка уже существует: {path}. Перезапись запрещена.", 409
                )
            target.parent.mkdir(parents=True, exist_ok=True)

            temp_path: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    newline="",
                    dir=target.parent,
                    prefix=".tg-obs-",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temp_path = handle.name
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, target)
            except BaseException:
                if temp_path is not None:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
                raise
            return True

        written = await self._call(write)
        log.info("Заметка записана: %s", path)
        return written


def create_obsidian_client(settings: Settings) -> ObsidianBackend:
    """Выбирает клиент: файловый режим, если задан OBSIDIAN_VAULT_PATH."""
    if settings.obsidian_vault_path:
        log.info(
            "Obsidian: прямой доступ к файлам %s (REST API не используется)",
            settings.obsidian_vault_path,
        )
        return VaultClient(settings)
    return ObsidianClient(settings)
