"""Тесты файлового режима: vault без запущенного Obsidian."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from app.config import Settings, load_settings
from app.obsidian import (
    FailoverClient,
    ObsidianClient,
    ObsidianError,
    VaultClient,
    create_obsidian_client,
)


def make_settings(
    vault: Path, *, dry_run: bool = False, mode: str = "auto", api_key: str = ""
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
        obsidian_api_key=api_key,
        obsidian_dry_run=dry_run,
        agent_confirmation="auto",
        agent_default_folder="Inbox",
        agent_auto_folder=True,
        agent_search_limit=5,
        log_level="INFO",
        obsidian_mode=mode,
        obsidian_vault_path=str(vault),
    )


class VaultClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.vault = Path(self._temp.name)
        (self.vault / "Projects").mkdir()
        (self.vault / "Projects" / "Альфа.md").write_text(
            "# Альфа\n\nСроки и бюджет проекта.\n", encoding="utf-8"
        )
        (self.vault / "Inbox.md").write_text("# Inbox\n", encoding="utf-8")
        (self.vault / ".obsidian").mkdir()
        (self.vault / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
        (self.vault / ".obsidian" / "hidden.md").write_text(
            "# Служебное\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self._temp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_factory_selects_vault(self) -> None:
        client = create_obsidian_client(make_settings(self.vault))
        self.assertIsInstance(client, VaultClient)

    def test_lists_only_markdown_outside_service_folders(self) -> None:
        client = VaultClient(make_settings(self.vault))
        notes = self.run_async(client.list_notes())
        self.assertEqual(notes, ["Inbox.md", "Projects/Альфа.md"])

    def test_reads_and_writes_note(self) -> None:
        client = VaultClient(make_settings(self.vault))
        self.assertTrue(self.run_async(client.note_exists("Projects/Альфа.md")))
        self.assertIn("бюджет", self.run_async(client.read_note("Projects/Альфа.md")))

        self.assertTrue(
            self.run_async(client.write_note("Projects/Бета.md", "# Бета\n"))
        )
        self.assertEqual(
            (self.vault / "Projects" / "Бета.md").read_text(encoding="utf-8"),
            "# Бета\n",
        )

    def test_refuses_overwrite_without_flag(self) -> None:
        client = VaultClient(make_settings(self.vault))
        with self.assertRaises(ObsidianError) as caught:
            self.run_async(client.write_note("Inbox.md", "# Другое\n"))
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(
            (self.vault / "Inbox.md").read_text(encoding="utf-8"), "# Inbox\n"
        )

    def test_dry_run_writes_nothing(self) -> None:
        client = VaultClient(make_settings(self.vault, dry_run=True))
        self.assertFalse(
            self.run_async(client.write_note("Projects/Гамма.md", "# Гамма\n"))
        )
        self.assertFalse((self.vault / "Projects" / "Гамма.md").exists())

    def test_search_finds_text_and_skips_service_folders(self) -> None:
        client = VaultClient(make_settings(self.vault))
        hits = self.run_async(client.search_notes("бюджет", limit=5))
        self.assertEqual([hit["path"] for hit in hits], ["Projects/Альфа.md"])
        self.assertIn("бюджет", hits[0]["excerpt"].lower())

        hidden = self.run_async(client.search("Служебное", limit=5))
        self.assertEqual(hidden, [])

    def test_search_by_content_ignores_quotes_and_command_words(self) -> None:
        client = VaultClient(make_settings(self.vault))
        hits = self.run_async(
            client.search_notes('Найди "сроки бюджет"', limit=5)
        )
        self.assertEqual([hit["path"] for hit in hits], ["Projects/Альфа.md"])
        self.assertEqual(hits[0]["match"], "текст")
        self.assertIn("бюджет", hits[0]["excerpt"].lower())

    def test_search_by_content_any_word_order(self) -> None:
        client = VaultClient(make_settings(self.vault))
        hits = self.run_async(client.search_notes("проекта бюджет", limit=5))
        self.assertEqual([hit["path"] for hit in hits], ["Projects/Альфа.md"])

    def test_search_by_partial_word(self) -> None:
        client = VaultClient(make_settings(self.vault))
        hits = self.run_async(client.search_notes("бюдж", limit=5))
        self.assertEqual([hit["path"] for hit in hits], ["Projects/Альфа.md"])

    def test_manages_folders(self) -> None:
        client = VaultClient(make_settings(self.vault))
        self.assertTrue(self.run_async(client.create_folder("Projects/2026")))
        self.assertIn("Projects/2026", self.run_async(client.list_folders()))

        self.assertTrue(
            self.run_async(client.rename_folder("Projects/2026", "Projects/2027"))
        )
        self.assertFalse((self.vault / "Projects" / "2026").exists())
        self.assertTrue((self.vault / "Projects" / "2027").is_dir())

        self.assertTrue(self.run_async(client.delete_folder("Projects/2027")))
        self.assertFalse((self.vault / "Projects" / "2027").exists())
        self.assertTrue((self.vault / ".trash" / "Projects" / "2027").is_dir())

    def test_refuses_existing_folder(self) -> None:
        client = VaultClient(make_settings(self.vault))
        with self.assertRaises(ObsidianError) as caught:
            self.run_async(client.create_folder("Projects"))
        self.assertEqual(caught.exception.status, 409)

    def test_copies_moves_and_deletes_note(self) -> None:
        client = VaultClient(make_settings(self.vault))
        source = "Projects/Альфа.md"
        copy_path = "Projects/Альфа-копия.md"

        self.assertTrue(self.run_async(client.copy_note(source, copy_path)))
        self.assertEqual(
            (self.vault / copy_path).read_text(encoding="utf-8"),
            (self.vault / source).read_text(encoding="utf-8"),
        )

        moved_path = "Inbox/Альфа-копия.md"
        self.assertTrue(self.run_async(client.move_note(copy_path, moved_path)))
        self.assertFalse((self.vault / copy_path).exists())
        self.assertTrue((self.vault / moved_path).is_file())

        self.assertTrue(self.run_async(client.delete_note(moved_path)))
        self.assertFalse((self.vault / moved_path).exists())
        self.assertTrue((self.vault / ".trash" / moved_path).is_file())
        self.assertTrue((self.vault / source).is_file())

    def test_note_operations_do_not_overwrite(self) -> None:
        client = VaultClient(make_settings(self.vault))
        with self.assertRaises(ObsidianError) as caught:
            self.run_async(client.copy_note("Projects/Альфа.md", "Inbox.md"))
        self.assertEqual(caught.exception.status, 409)

    def test_dry_run_skips_folder_and_note_operations(self) -> None:
        client = VaultClient(make_settings(self.vault, dry_run=True))
        self.assertFalse(self.run_async(client.create_folder("Архив")))
        self.assertFalse((self.vault / "Архив").exists())
        self.assertFalse(
            self.run_async(client.delete_note("Projects/Альфа.md"))
        )
        self.assertTrue((self.vault / "Projects" / "Альфа.md").exists())
        self.assertFalse(
            self.run_async(
                client.move_note("Projects/Альфа.md", "Архив/Альфа.md")
            )
        )
        self.assertTrue((self.vault / "Projects" / "Альфа.md").exists())

    def test_lists_folders_without_service_directories(self) -> None:
        client = VaultClient(make_settings(self.vault))
        folders = self.run_async(client.list_folders())
        self.assertEqual(folders, ["Projects"])
        self.assertNotIn(".obsidian", folders)

    def test_path_outside_vault_is_rejected(self) -> None:
        client = VaultClient(make_settings(self.vault))
        with self.assertRaises(ObsidianError):
            self.run_async(client.read_note("../секрет.md"))
        with self.assertRaises(ObsidianError):
            self.run_async(client.write_note("../секрет.md", "x"))

    def test_health_reports_vault(self) -> None:
        client = VaultClient(make_settings(self.vault))
        status = self.run_async(client.health())
        self.assertEqual(status["service"], "Obsidian vault (direct filesystem)")
        self.assertTrue(status["authenticated"])


class ModeTests(unittest.TestCase):
    """OBSIDIAN_MODE: auto / rest / vault."""

    def load(self, **env: str) -> Settings:
        with patch.dict(os.environ, env, clear=True), patch(
            "app.config.ENV_FILE", Path("no-such.env")
        ):
            return load_settings()

    def test_auto_uses_vault_without_api_key(self) -> None:
        settings = self.load(OBSIDIAN_VAULT_PATH=r"E:\Vault")
        self.assertEqual(settings.effective_obsidian_mode, "vault")

    def test_auto_uses_rest_without_path(self) -> None:
        self.assertEqual(self.load().effective_obsidian_mode, "rest")

    def test_auto_prefers_rest_with_key(self) -> None:
        settings = self.load(
            OBSIDIAN_VAULT_PATH=r"E:\Vault", OBSIDIAN_API_KEY="test-key"
        )
        self.assertEqual(settings.effective_obsidian_mode, "rest")
        self.assertTrue(settings.obsidian_fallback_enabled)

    def test_rest_ignores_vault_path(self) -> None:
        settings = self.load(
            OBSIDIAN_MODE="rest", OBSIDIAN_VAULT_PATH=r"E:\Vault"
        )
        self.assertEqual(settings.obsidian_mode, "rest")
        self.assertEqual(settings.effective_obsidian_mode, "rest")

    def test_vault_requires_path(self) -> None:
        with self.assertRaises(ValueError):
            self.load(OBSIDIAN_MODE="vault")

    def test_unknown_mode_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.load(OBSIDIAN_MODE="api")

    def test_factory_respects_rest_mode(self) -> None:
        settings = self.load(
            OBSIDIAN_MODE="rest",
            OBSIDIAN_VAULT_PATH=r"E:\Vault",
            OBSIDIAN_API_KEY="test-key",
        )
        self.assertIsInstance(create_obsidian_client(settings), ObsidianClient)

    def test_factory_respects_vault_mode(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = self.load(
                OBSIDIAN_MODE="vault",
                OBSIDIAN_VAULT_PATH=folder,
            )
            self.assertIsInstance(create_obsidian_client(settings), VaultClient)

    def test_factory_builds_failover_in_auto(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = self.load(
                OBSIDIAN_MODE="auto",
                OBSIDIAN_VAULT_PATH=folder,
                OBSIDIAN_API_KEY="test-key",
            )
            self.assertIsInstance(create_obsidian_client(settings), FailoverClient)


class FailoverClientTests(unittest.TestCase):
    """REST первым, файлы vault — только при временном сбое REST."""

    def make(
        self, rest_error: Exception | None = None, *, cooldown: float = 30.0
    ) -> tuple[FailoverClient, MagicMock, MagicMock]:
        rest = MagicMock()
        rest.read_note = (
            AsyncMock(side_effect=rest_error)
            if rest_error is not None
            else AsyncMock(return_value="rest")
        )
        rest.write_note = (
            AsyncMock(side_effect=rest_error)
            if rest_error is not None
            else AsyncMock(return_value=True)
        )
        rest.health = AsyncMock(return_value={"service": "rest", "authenticated": True})
        rest.close = AsyncMock()

        vault = MagicMock()
        vault.read_note = AsyncMock(return_value="vault")
        vault.write_note = AsyncMock(return_value=True)
        vault.health = AsyncMock(return_value={"service": "vault", "authenticated": True})
        vault.close = AsyncMock()

        return FailoverClient(rest, vault, cooldown), rest, vault

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_rest_is_used_first(self) -> None:
        client, _rest, vault = self.make()
        self.assertEqual(self.run_async(client.read_note("Заметка.md")), "rest")
        vault.read_note.assert_not_awaited()

    def test_connection_error_switches_to_vault(self) -> None:
        client, rest, vault = self.make(aiohttp.ClientError("нет соединения"))
        self.assertEqual(self.run_async(client.read_note("Заметка.md")), "vault")
        rest.read_note.assert_awaited_once()
        self.assertEqual(vault.read_note.await_count, 1)

    def test_rest_is_not_retried_during_cooldown(self) -> None:
        client, rest, vault = self.make(aiohttp.ClientError("нет соединения"))
        self.run_async(client.read_note("A.md"))
        self.run_async(client.read_note("B.md"))
        rest.read_note.assert_awaited_once()
        self.assertEqual(vault.read_note.await_count, 2)

    def test_rest_is_probed_again_after_cooldown(self) -> None:
        client, rest, _vault = self.make(aiohttp.ClientError("нет соединения"), cooldown=5)
        with patch("app.obsidian.time.monotonic", return_value=1000.0):
            self.run_async(client.read_note("A.md"))
        with patch("app.obsidian.time.monotonic", return_value=1006.0):
            self.run_async(client.read_note("B.md"))
        self.assertEqual(rest.read_note.await_count, 2)

    def test_not_found_is_not_repeated_on_vault(self) -> None:
        client, _rest, vault = self.make(ObsidianError("нет заметки", 404))
        with self.assertRaises(ObsidianError):
            self.run_async(client.read_note("Нет.md"))
        vault.read_note.assert_not_awaited()

    def test_write_keeps_overwrite_flag(self) -> None:
        client, _rest, vault = self.make(aiohttp.ClientError("нет соединения"))
        self.assertTrue(
            self.run_async(client.write_note("A.md", "текст", overwrite=True))
        )
        vault.write_note.assert_awaited_once_with("A.md", "текст", overwrite=True)

    def test_status_reports_both_backends(self) -> None:
        client, _rest, _vault = self.make(aiohttp.ClientError("нет соединения"))
        states = self.run_async(client.backends_status())
        self.assertIn("доступен", states["rest"])
        self.assertIn("доступен", states["vault"])


if __name__ == "__main__":
    unittest.main()
