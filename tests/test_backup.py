"""Тесты резервной копии: состав архива, ротация и защита от рекурсии."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app.backup import backup_due, backup_if_due, create_backup, last_backup
from app.config import Settings


def make_settings(
    vault: Path, backup_dir: Path, *, keep: int = 7, every_hours: float = 24.0
) -> Settings:
    return Settings(
        telegram_bot_token="test-token",
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
        obsidian_vault_path=str(vault),
        backup_dir=str(backup_dir),
        backup_keep=keep,
        backup_every_hours=every_hours,
    )


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)
        self.vault = root / "vault"
        self.backups = root / "backups"
        self.project = root / "project"
        self.vault.mkdir()
        (self.vault / "Notes").mkdir()
        (self.vault / "Notes" / "Идея.md").write_text("# Идея\n", encoding="utf-8")
        (self.vault / ".obsidian" / "plugins").mkdir(parents=True)
        (self.vault / ".obsidian" / "plugins" / "data.json").write_text(
            '{"apiKey": "secret"}', encoding="utf-8"
        )
        (self.project / "state" / "audit").mkdir(parents=True)
        (self.project / "state" / "audit" / "2026-10-30.jsonl").write_text(
            '{"action": "write_note"}\n', encoding="utf-8"
        )
        self.patch_root = patch("app.config.PROJECT_ROOT", self.project)
        self.patch_root.start()

    def tearDown(self) -> None:
        self.patch_root.stop()
        self._temp.cleanup()

    def test_archive_contains_vault_and_audit(self) -> None:
        archive = create_backup(make_settings(self.vault, self.backups))
        self.assertTrue(archive.is_file())
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
        self.assertIn("vault/Notes/Идея.md", names)
        self.assertIn("state/audit/2026-10-30.jsonl", names)
        self.assertIn("backup-info.txt", names)

    def test_obsidian_settings_are_not_copied(self) -> None:
        archive = create_backup(make_settings(self.vault, self.backups))
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
        self.assertFalse([name for name in names if ".obsidian" in name])

    def test_keeps_only_newest_copies(self) -> None:
        settings = make_settings(self.vault, self.backups, keep=2)
        start = datetime(2026, 10, 30, 12, 0, 0)
        for index in range(3):
            create_backup(settings, now=start + timedelta(minutes=index))
        left = sorted(path.name for path in self.backups.glob("*.zip"))
        self.assertEqual(len(left), 2)
        self.assertNotIn("tg-obs-backup-20261030-120000.zip", left)

    def test_refuses_backup_dir_inside_vault(self) -> None:
        with self.assertRaises(ValueError):
            create_backup(make_settings(self.vault, self.vault / "backups"))

    def test_requires_backup_dir(self) -> None:
        settings = replace(make_settings(self.vault, self.backups), backup_dir="")
        with self.assertRaises(ValueError):
            create_backup(settings)

    def test_missing_vault_is_reported(self) -> None:
        settings = replace(
            make_settings(self.vault, self.backups),
            obsidian_vault_path=str(self.vault / "нет"),
        )
        with self.assertRaises(ValueError):
            create_backup(settings)


class BackupScheduleTests(unittest.TestCase):
    """BACKUP_EVERY_HOURS: когда копия нужна, а когда ещё рано."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.vault = self.root / "vault"
        self.backups = self.root / "backups"
        self.vault.mkdir()
        (self.vault / "Заметка.md").write_text("# Заметка\n", encoding="utf-8")
        self.patch_root = patch("app.config.PROJECT_ROOT", self.root / "project")
        self.patch_root.start()

    def tearDown(self) -> None:
        self.patch_root.stop()
        self._temp.cleanup()

    def test_due_when_no_copies(self) -> None:
        self.assertTrue(backup_due(make_settings(self.vault, self.backups)))
        self.assertIsNone(last_backup(make_settings(self.vault, self.backups)))

    def test_not_due_after_fresh_copy(self) -> None:
        settings = make_settings(self.vault, self.backups)
        create_backup(settings)
        self.assertFalse(backup_due(settings))

    def test_due_after_interval(self) -> None:
        settings = make_settings(self.vault, self.backups, every_hours=5)
        create_backup(settings)
        later = datetime.now() + timedelta(hours=6)
        self.assertTrue(backup_due(settings, now=later))
        self.assertFalse(
            backup_due(settings, now=datetime.now() + timedelta(hours=1))
        )

    def test_zero_interval_never_due(self) -> None:
        settings = make_settings(self.vault, self.backups, every_hours=0)
        self.assertFalse(backup_due(settings))

    def test_no_backup_dir_never_due(self) -> None:
        settings = replace(make_settings(self.vault, self.backups), backup_dir="")
        self.assertFalse(backup_due(settings))

    def test_backup_if_due_creates_once(self) -> None:
        settings = make_settings(self.vault, self.backups)
        first = backup_if_due(settings)
        self.assertIsNotNone(first)
        self.assertIsNone(backup_if_due(settings))
        self.assertEqual(len(list(self.backups.glob("*.zip"))), 1)


if __name__ == "__main__":
    unittest.main()
