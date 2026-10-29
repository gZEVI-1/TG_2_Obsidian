"""Тесты проверки готовности: доступ, LLM, режим записи и общий отчёт."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.backup import create_backup
from app.config import Settings
from app.health import (
    FAIL,
    OK,
    WARN,
    _check_access,
    _check_backup,
    _check_llm,
    _check_write_mode,
    check_health,
)


def make_settings(vault: Path, **changes: object) -> Settings:
    settings = Settings(
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
        obsidian_mode="vault",
        obsidian_vault_path=str(vault),
    )
    return replace(settings, **changes)


class HealthCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.project = Path(self._temp.name) / "project"
        self.vault = Path(self._temp.name) / "vault"
        self.backups = Path(self._temp.name) / "backups"
        self.vault.mkdir()
        (self.vault / "Заметка.md").write_text("# Заметка\n", encoding="utf-8")
        self.patch_root = patch("app.config.PROJECT_ROOT", self.project)
        self.patch_root.start()

    def tearDown(self) -> None:
        self.patch_root.stop()
        self._temp.cleanup()

    def test_local_address_is_ok(self) -> None:
        name, status, _detail = _check_access(make_settings(self.vault))
        self.assertEqual((name, status), ("доступ", OK))

    def test_network_address_warns(self) -> None:
        settings = replace(
            make_settings(self.vault), obsidian_base_url="http://192.168.1.5:27123"
        )
        _name, status, detail = _check_access(settings)
        self.assertEqual(status, WARN)
        self.assertIn("HTTPS", detail)

    def test_https_local_is_ok(self) -> None:
        settings = replace(
            make_settings(self.vault),
            obsidian_base_url="https://127.0.0.1:27124",
        )
        _name, status, detail = _check_access(settings)
        self.assertEqual(status, OK)
        self.assertIn("HTTPS", detail)

    def test_https_without_verify_warns(self) -> None:
        settings = replace(
            make_settings(self.vault),
            obsidian_base_url="https://127.0.0.1:27124",
            obsidian_verify_ssl=False,
        )
        _name, status, detail = _check_access(settings)
        self.assertEqual(status, WARN)
        self.assertIn("без проверки", detail)

    def test_llm_checks(self) -> None:
        stub = make_settings(self.vault)
        self.assertEqual(_check_llm(stub)[1], WARN)
        empty = replace(stub, llm_stub=False)
        self.assertEqual(_check_llm(empty)[1], FAIL)

    def test_dry_run_warns(self) -> None:
        self.assertEqual(_check_write_mode(make_settings(self.vault))[1], WARN)
        live = replace(make_settings(self.vault), obsidian_dry_run=False)
        self.assertEqual(_check_write_mode(live)[1], OK)

    def test_backup_dir_not_set_warns(self) -> None:
        self.assertEqual(_check_backup(make_settings(self.vault))[1], WARN)

    def test_backup_check_reports_fresh_copy(self) -> None:
        settings = replace(make_settings(self.vault), backup_dir=str(self.backups))
        create_backup(settings)
        _name, status, detail = _check_backup(settings)
        self.assertEqual(status, OK)
        self.assertIn("каждые 24 ч", detail)

    def test_backup_check_warns_when_stale(self) -> None:
        settings = replace(make_settings(self.vault), backup_dir=str(self.backups))
        archive = create_backup(settings)
        old = (datetime.now() - timedelta(hours=30)).timestamp()
        os.utime(archive, (old, old))
        _name, status, detail = _check_backup(settings)
        self.assertEqual(status, WARN)
        self.assertIn("просрочена", detail)

    def test_backup_check_manual_mode(self) -> None:
        settings = replace(
            make_settings(self.vault),
            backup_dir=str(self.backups),
            backup_every_hours=0,
        )
        _name, status, detail = _check_backup(settings)
        self.assertEqual(status, OK)
        self.assertIn("вручную", detail)

    def test_health_ok_for_available_vault(self) -> None:
        with patch(
            "app.health._check_telegram",
            AsyncMock(return_value=("Telegram", OK, "test")),
        ):
            healthy = asyncio.run(check_health(make_settings(self.vault)))
        self.assertTrue(healthy)

    def test_health_fails_without_vault(self) -> None:
        settings = replace(
            make_settings(self.vault), obsidian_vault_path=str(self.vault / "нет")
        )
        with patch(
            "app.health._check_telegram",
            AsyncMock(return_value=("Telegram", OK, "test")),
        ):
            self.assertFalse(asyncio.run(check_health(settings)))


if __name__ == "__main__":
    unittest.main()
