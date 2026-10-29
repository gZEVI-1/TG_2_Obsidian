"""Аудит изменений хранилища: что записано, когда и чем закончилось.

Пишется только факт операции и путь; содержимое заметок и секреты в журнал
не попадают. Журнал — append-only JSONL в `state/audit/YYYY-MM-DD.jsonl`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from app.obsidian import ObsidianBackend

log = logging.getLogger(__name__)

MAX_DETAIL_CHARS = 200


class AuditLog:
    """Журнал операций: одна строка JSON на действие."""

    def __init__(self, root: Path, enabled: bool = True) -> None:
        self._root = root
        self._enabled = enabled
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def record_sync(
        self,
        action: str,
        *,
        path: str = "",
        status: str = "ok",
        detail: str = "",
    ) -> dict[str, Any]:
        """Возвращает запись (для тестов и для единого формата)."""
        entry = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "action": action,
            "path": path,
            "status": status,
            "detail": (detail or "")[:MAX_DETAIL_CHARS],
        }
        if not self._enabled:
            return entry
        self._write_line(json.dumps(entry, ensure_ascii=False))
        return entry

    async def record(
        self,
        action: str,
        *,
        path: str = "",
        status: str = "ok",
        detail: str = "",
    ) -> None:
        if not self._enabled:
            return
        entry = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "action": action,
            "path": path,
            "status": status,
            "detail": (detail or "")[:MAX_DETAIL_CHARS],
        }
        line = json.dumps(entry, ensure_ascii=False)
        async with self._lock:
            await asyncio.to_thread(self._write_line, line)

    def _write_line(self, line: str) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            target = self._root / f"{datetime.now():%Y-%m-%d}.jsonl"
            with target.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as error:
            # Журнал не должен ломать саму операцию с заметкой.
            log.warning("Аудит: не удалось записать событие: %s", error)

    async def tail(self, limit: int = 5) -> list[dict[str, Any]]:
        """Последние записи журнала, свежие — в конце списка."""
        if not self._enabled:
            return []
        return await asyncio.to_thread(self._read_tail, max(1, limit))

    def _read_tail(self, limit: int) -> list[dict[str, Any]]:
        lines: list[str] = []
        for file in sorted(self._root.glob("*.jsonl"), reverse=True):
            try:
                content = file.read_text(encoding="utf-8").splitlines()
            except OSError as error:
                log.warning("Аудит: не удалось прочитать %s: %s", file, error)
                continue
            lines = content + lines
            if len(lines) >= limit:
                break

        entries: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries


class AuditedBackend:
    """Обёртка клиента хранилища: изменения попадают в журнал, чтения — нет."""

    def __init__(self, backend: ObsidianBackend, audit: AuditLog) -> None:
        self._backend = backend
        self._audit = audit

    async def __aenter__(self) -> "AuditedBackend":
        await self._backend.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._backend.__aexit__(*args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)

    async def close(self) -> None:
        await self._backend.close()

    async def write_note(self, path: str, content: str, *, overwrite: bool = False) -> bool:
        action = "перезапись заметки" if overwrite else "запись заметки"
        try:
            written = await self._backend.write_note(
                path, content, overwrite=overwrite
            )
        except Exception as error:
            await self._audit.record(
                action, path=path, status="error", detail=str(error)
            )
            raise
        await self._audit.record(
            action,
            path=path,
            status="ok" if written else "dry-run",
            detail=f"{len(content)} симв.",
        )
        return written

    async def delete_note(self, path: str) -> bool:
        return await self._mutation("удаление заметки", path, self._backend.delete_note, path)

    async def move_note(self, source: str, destination: str) -> bool:
        return await self._mutation(
            "перенос заметки",
            f"{source} → {destination}",
            self._backend.move_note,
            source,
            destination,
        )

    async def copy_note(self, source: str, destination: str) -> bool:
        return await self._mutation(
            "копирование заметки",
            f"{source} → {destination}",
            self._backend.copy_note,
            source,
            destination,
        )

    async def create_folder(self, folder: str) -> bool:
        return await self._mutation(
            "создание папки", folder, self._backend.create_folder, folder
        )

    async def rename_folder(self, folder: str, new_path: str) -> bool:
        return await self._mutation(
            "переименование папки",
            f"{folder} → {new_path}",
            self._backend.rename_folder,
            folder,
            new_path,
        )

    async def delete_folder(self, folder: str) -> bool:
        return await self._mutation(
            "удаление папки", folder, self._backend.delete_folder, folder
        )

    async def _mutation(self, action: str, label: str, func, *args) -> bool:
        try:
            done = await func(*args)
        except Exception as error:
            await self._audit.record(action, path=label, status="error", detail=str(error))
            raise
        await self._audit.record(
            action, path=label, status="ok" if done else "dry-run"
        )
        return done
