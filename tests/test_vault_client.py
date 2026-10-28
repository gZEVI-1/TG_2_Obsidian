"""Тесты файлового режима: vault без запущенного Obsidian."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.obsidian import ObsidianError, VaultClient, create_obsidian_client


def make_settings(vault: Path, *, dry_run: bool = False) -> Settings:
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
        obsidian_dry_run=dry_run,
        agent_confirmation="auto",
        agent_default_folder="Inbox",
        agent_auto_folder=True,
        agent_search_limit=5,
        log_level="INFO",
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


if __name__ == "__main__":
    unittest.main()
