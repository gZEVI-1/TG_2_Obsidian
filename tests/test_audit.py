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


class AuditGuardTests(unittest.TestCase):
    """Агент не читает журнал: правило в промпте и запрет в read_note."""

    def make_agent(self, vault: str = "") -> tuple[object, Settings]:
        from app.agent import Agent

        settings = Settings(
            telegram_bot_token="t",
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            llm_accounts=(),
            llm_models=(),
            llm_cooldown_seconds=60,
            llm_stub=True,
            llm_stub_accounts=1,
            llm_stub_quota=1,
            llm_stub_reset_seconds=60,
            obsidian_base_url="http://127.0.0.1:27123",
            obsidian_api_key="",
            obsidian_dry_run=True,
            agent_confirmation="auto",
            agent_default_folder="Inbox",
            agent_auto_folder=True,
            agent_search_limit=5,
            log_level="INFO",
            obsidian_vault_path=vault,
        )
        return Agent(None, FakeBackend(), settings), settings  # type: ignore[arg-type]

    def test_audit_rule_is_in_system_prompt(self) -> None:
        from app.agent import WRITE_RULES

        self.assertIn("9. Журнал изменений", WRITE_RULES)
        self.assertIn("AuditLog", WRITE_RULES)
        self.assertIn("AuditedBackend", WRITE_RULES)
        self.assertIn("/audit", WRITE_RULES)
        agent, _ = self.make_agent()
        self.assertIn("Журнал изменений", agent.system_prompt)  # type: ignore[union-attr]

    def test_audit_root_is_mapped_when_inside_vault(self) -> None:
        from app.config import PROJECT_ROOT
        from app.agent import _audit_root

        agent, settings = self.make_agent(str(PROJECT_ROOT))
        self.assertTrue(agent._is_audit_path("state/audit/2026-10-30.jsonl"))  # type: ignore[union-attr]
        self.assertTrue(agent._is_audit_path("state/audit"))  # type: ignore[union-attr]
        self.assertFalse(agent._is_audit_path("Notes/state/audit.md"))  # type: ignore[union-attr]
        self.assertEqual(_audit_root(settings), "state/audit")

    def test_read_note_refuses_audit_log(self) -> None:
        from app.config import PROJECT_ROOT

        agent, _ = self.make_agent(str(PROJECT_ROOT))
        result = asyncio.run(  # type: ignore[union-attr]
            agent._read("state/audit/2026-10-30.jsonl")
        )
        self.assertIn("служебный журнал", result)

    def test_audit_path_outside_vault_is_ignored(self) -> None:
        agent, _ = self.make_agent("E:\\some\\vault")
        self.assertFalse(agent._is_audit_path("state/audit/x.jsonl"))  # type: ignore[union-attr]


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
