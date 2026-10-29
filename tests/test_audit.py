"""Тесты аудита изменений и настроек надёжности."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.audit import AuditLog, AuditedBackend
from app.config import Settings, load_settings
from app.obsidian import ObsidianError


class FakeBackend:
    """Хранилище без сети: пишет вызовы, умеет падать и «dry-run»."""

    def __init__(self, *, written: bool = True, fail: bool = False) -> None:
        self.written = written
        self.fail = fail
        self.writes: list[tuple[str, str, bool]] = []
        self.deleted: list[str] = []

    async def write_note(
        self, path: str, content: str, *, overwrite: bool = False
    ) -> bool:
        self.writes.append((path, content, overwrite))
        if self.fail:
            raise ObsidianError("HTTP 500", 500)
        return self.written

    async def delete_note(self, path: str) -> bool:
        self.deleted.append(path)
        if self.fail:
            raise ObsidianError("HTTP 500", 500)
        return self.written

    async def health(self) -> dict[str, str]:
        return {"status": "ok"}

    async def close(self) -> None:
        return None


class AuditLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_record_writes_jsonl(self) -> None:
        audit = AuditLog(self.root)
        asyncio.run(
            audit.record(
                "запись заметки", path="Inbox/Идея.md", status="ok", detail="12 симв."
            )
        )
        files = list(self.root.glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        entry = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(entry["action"], "запись заметки")
        self.assertEqual(entry["path"], "Inbox/Идея.md")
        self.assertEqual(entry["status"], "ok")
        self.assertTrue(entry["ts"])

    def test_disabled_audit_writes_nothing(self) -> None:
        audit = AuditLog(self.root, enabled=False)
        asyncio.run(audit.record("запись заметки", path="A.md"))
        self.assertEqual(list(self.root.glob("*.jsonl")), [])
        self.assertEqual(asyncio.run(audit.tail(5)), [])

    def test_tail_returns_last_entries(self) -> None:
        audit = AuditLog(self.root)
        for index in range(3):
            asyncio.run(audit.record(f"действие {index}", path=f"{index}.md"))
        entries = asyncio.run(audit.tail(2))
        self.assertEqual([entry["action"] for entry in entries], ["действие 1", "действие 2"])

    def test_tail_of_empty_directory(self) -> None:
        audit = AuditLog(self.root / "no-such")
        self.assertEqual(asyncio.run(audit.tail(5)), [])


class AuditedBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.audit = AuditLog(self.root)
        self.sink = FakeBackend()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def entries(self) -> list[dict]:
        return asyncio.run(self.audit.tail(10))

    def test_write_is_recorded(self) -> None:
        backend = AuditedBackend(self.sink, self.audit)
        written = asyncio.run(backend.write_note("Inbox/Идея.md", "текст"))
        self.assertTrue(written)
        entries = self.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["action"], "запись заметки")
        self.assertEqual(entries[0]["status"], "ok")

    def test_overwrite_is_recorded(self) -> None:
        backend = AuditedBackend(self.sink, self.audit)
        asyncio.run(backend.write_note("A.md", "текст", overwrite=True))
        self.assertEqual(self.entries()[0]["action"], "перезапись заметки")

    def test_dry_run_status(self) -> None:
        backend = AuditedBackend(FakeBackend(written=False), self.audit)
        self.assertFalse(asyncio.run(backend.write_note("A.md", "текст")))
        self.assertEqual(self.entries()[0]["status"], "dry-run")

    def test_error_is_recorded_and_raised(self) -> None:
        backend = AuditedBackend(FakeBackend(fail=True), self.audit)
        with self.assertRaises(ObsidianError):
            asyncio.run(backend.write_note("A.md", "текст"))
        entry = self.entries()[0]
        self.assertEqual(entry["status"], "error")
        self.assertIn("500", entry["detail"])

    def test_delete_is_recorded(self) -> None:
        backend = AuditedBackend(self.sink, self.audit)
        asyncio.run(backend.delete_note("A.md"))
        entry = self.entries()[0]
        self.assertEqual(entry["action"], "удаление заметки")
        self.assertEqual(entry["path"], "A.md")

    def test_reads_are_not_audited(self) -> None:
        backend = AuditedBackend(self.sink, self.audit)
        asyncio.run(backend.health())
        self.assertEqual(self.entries(), [])


class ReliabilitySettingsTests(unittest.TestCase):
    def load(self, **env: str) -> Settings:
        with patch.dict("os.environ", env, clear=True), patch(
            "app.config.ENV_FILE", Path("no-such.env")
        ):
            return load_settings()

    def test_defaults(self) -> None:
        settings = self.load()
        self.assertEqual(settings.obsidian_retry_attempts, 3)
        self.assertEqual(settings.obsidian_retry_delay, 0.5)
        self.assertTrue(settings.audit_enabled)

    def test_can_override(self) -> None:
        settings = self.load(
            OBSIDIAN_RETRY_ATTEMPTS="5",
            OBSIDIAN_RETRY_DELAY_SECONDS="1.5",
            AUDIT_ENABLED="false",
        )
        self.assertEqual(settings.obsidian_retry_attempts, 5)
        self.assertEqual(settings.obsidian_retry_delay, 1.5)
        self.assertFalse(settings.audit_enabled)

    def test_retry_values_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            self.load(OBSIDIAN_RETRY_ATTEMPTS="0")
        with self.assertRaises(ValueError):
            self.load(OBSIDIAN_RETRY_ATTEMPTS="6")
        with self.assertRaises(ValueError):
            self.load(OBSIDIAN_RETRY_DELAY_SECONDS="11")
        with self.assertRaises(ValueError):
            self.load(OBSIDIAN_RETRY_DELAY_SECONDS="быстро")


if __name__ == "__main__":
    unittest.main()
