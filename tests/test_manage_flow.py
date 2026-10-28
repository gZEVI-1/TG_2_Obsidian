"""Сквозные проверки новых действий: агент → предложение → применение."""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

from app.agent import Agent, Proposal
from app.bot import _apply, _proposal_text
from app.config import Settings
from app.llm import ToolCall
from tests.test_vault_client import make_settings


class FakeBackend:
    """Хранилище в памяти для проверки предложений и применения."""

    def __init__(self, text: str = "# Заметка\n") -> None:
        self.text = text
        self.calls: list[tuple] = []
        self.saved: str = ""
        self.dry_run = False

    async def read_note(self, path: str) -> str:
        return self.text

    async def list_notes(self) -> list[str]:
        return ["Notes/Идеи.md", "Projects/Альфа.md"]

    async def list_folders(self) -> list[str]:
        return ["Notes", "Projects"]

    async def write_note(self, path: str, content: str, *, overwrite: bool = False) -> bool:
        self.calls.append(("write", path, overwrite))
        self.saved = content
        return not self.dry_run

    async def create_folder(self, folder: str) -> bool:
        self.calls.append(("create_folder", folder))
        return not self.dry_run

    async def rename_folder(self, folder: str, new_path: str) -> bool:
        self.calls.append(("rename_folder", folder, new_path))
        return not self.dry_run

    async def delete_folder(self, folder: str) -> bool:
        self.calls.append(("delete_folder", folder))
        return not self.dry_run

    async def move_note(self, source: str, destination: str) -> bool:
        self.calls.append(("move_note", source, destination))
        return not self.dry_run

    async def copy_note(self, source: str, destination: str) -> bool:
        self.calls.append(("copy_note", source, destination))
        return not self.dry_run

    async def delete_note(self, path: str) -> bool:
        self.calls.append(("delete_note", path))
        return not self.dry_run


class FakeChat:
    async def chat(self, messages, **kwargs):
        raise AssertionError("модель в этих тестах не нужна")


def settings() -> Settings:
    return make_settings(Path("."))


class AgentProposalTests(unittest.TestCase):
    def propose(self, name: str, **arguments):
        agent = Agent(FakeChat(), FakeBackend(), settings())
        call = ToolCall(id="1", name=name, arguments=json.dumps(arguments))
        return agent._proposal(call)

    def test_create_section(self) -> None:
        result = self.propose(
            "create_section",
            path="Notes/Идеи.md",
            heading="## Итоги",
            content="- Готово",
            level=3,
        )
        assert result.proposal is not None
        self.assertEqual(result.proposal.kind, "section_create")
        self.assertEqual(result.proposal.heading, "Итоги")
        self.assertEqual(result.proposal.level, 3)

    def test_folder_rename(self) -> None:
        result = self.propose(
            "rename_folder", path="Проекты/2026", new_path="Архив/2026"
        )
        assert result.proposal is not None
        self.assertEqual(result.proposal.kind, "folder_rename")
        self.assertEqual(result.proposal.destination, "Архив/2026")

    def test_move_resolves_folder_destination(self) -> None:
        result = self.propose(
            "move_note", path="Projects/Альфа.md", destination="Архив"
        )
        assert result.proposal is not None
        self.assertEqual(result.proposal.kind, "note_move")
        self.assertEqual(result.proposal.destination, "Архив/Альфа.md")

    def test_invalid_folder_is_rejected(self) -> None:
        result = self.propose("create_folder", folder=".obsidian")
        self.assertIsNone(result.proposal)
        self.assertIn("Некорректный", result.text)

    def test_lists_notes_in_folder(self) -> None:
        agent = Agent(FakeChat(), FakeBackend(), settings())
        result = asyncio.run(agent._notes("Notes"))
        self.assertIn("Notes/Идеи.md", result)
        self.assertNotIn("Projects/Альфа.md", result)


class ApplyTests(unittest.TestCase):
    def apply(self, proposal: Proposal, backend: FakeBackend | None = None):
        backend = backend or FakeBackend()
        result = asyncio.run(_apply(backend, proposal, proposal.path))
        return result, backend

    def test_section_create(self) -> None:
        proposal = Proposal(
            kind="section_create",
            path="Notes/Идеи.md",
            heading="Итоги",
            content="- Готово",
        )
        result, backend = self.apply(proposal)
        self.assertIn("Раздел", result)
        self.assertIn("## Итоги", backend.saved)

    def test_folder_and_note_operations_reach_backend(self) -> None:
        cases = [
            (Proposal(kind="folder_create", path="Архив"), "create_folder"),
            (
                Proposal(
                    kind="folder_rename", path="Архив", destination="Старое"
                ),
                "rename_folder",
            ),
            (Proposal(kind="folder_delete", path="Архив"), "delete_folder"),
            (
                Proposal(
                    kind="note_move",
                    path="Notes/Идеи.md",
                    destination="Архив/Идеи.md",
                ),
                "move_note",
            ),
            (
                Proposal(
                    kind="note_copy",
                    path="Notes/Идеи.md",
                    destination="Архив/Идеи.md",
                ),
                "copy_note",
            ),
            (Proposal(kind="note_delete", path="Notes/Идеи.md"), "delete_note"),
        ]
        for proposal, expected in cases:
            result, backend = self.apply(proposal)
            self.assertEqual(backend.calls[-1][0], expected)
            self.assertNotIn("Ошибка", result)

    def test_dry_run_is_reported(self) -> None:
        backend = FakeBackend()
        backend.dry_run = True
        result, _ = self.apply(
            Proposal(kind="folder_delete", path="Архив"), backend
        )
        self.assertIn("Dry-run", result)

    def test_preview_contains_destination(self) -> None:
        text = _proposal_text(
            Proposal(
                kind="note_move",
                path="Notes/Идеи.md",
                destination="Архив/Идеи.md",
            )
        )
        self.assertIn("Notes/Идеи.md", text)
        self.assertIn("Архив/Идеи.md", text)


if __name__ == "__main__":
    unittest.main()
