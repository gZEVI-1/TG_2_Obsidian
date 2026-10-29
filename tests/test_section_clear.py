"""Тесты очистки раздела и механического удаления связей."""

from __future__ import annotations

import asyncio
import unittest

from app.bot import _apply, _proposal_text
from app.config import Settings
from app.links import remove_links, wants_remove_links
from app.notes import clear_section, replace_section


def make_settings() -> Settings:
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
    )

NOTE = """---
tags: [проект]
---

# Альфа

## Итоги

Сделали первую версию.

## Связи

- [[Проект Бета]]
- [[Мария]]

## Дальше

Ждём проверки.
"""


class RemoveLinksTests(unittest.TestCase):
    def test_understands_russian_wishes(self) -> None:
        for text in (
            "убери все связи",
            "удали все ссылки",
            "очисти связи",
            "Убери, пожалуйста, все связи",
            "remove all links",
        ):
            self.assertTrue(wants_remove_links(text), text)

    def test_does_not_fire_on_other_wishes(self) -> None:
        for text in ("добавь связи", "поставь ссылки", "очисти раздел", "перепиши итоги"):
            self.assertFalse(wants_remove_links(text), text)

    def test_removes_link_lines_and_keeps_text(self) -> None:
        body = "- [[Проект Бета]]\n- [[Мария]]\nСмотри также [[Альфа]] и всё."
        self.assertEqual(remove_links(body), "Смотри также и всё.")

    def test_removes_everything_when_only_links(self) -> None:
        self.assertEqual(remove_links("- [[Проект Бета]]\n- [[Мария]]\n"), "")


class ClearSectionTests(unittest.TestCase):
    def test_clear_keeps_heading_and_other_sections(self) -> None:
        result = clear_section(NOTE, "Связи")
        self.assertIn("## Связи", result)
        self.assertNotIn("[[", result)
        self.assertIn("Сделали первую версию.", result)
        self.assertIn("## Дальше\n\nЖдём проверки.", result)

    def test_clear_keeps_frontmatter(self) -> None:
        self.assertTrue(clear_section(NOTE, "Связи").startswith("---\ntags: [проект]\n---"))

    def test_replace_with_empty_body_clears_section(self) -> None:
        self.assertEqual(
            replace_section(NOTE, "Связи", "\n\n"), clear_section(NOTE, "Связи")
        )

    def test_clear_of_missing_section_fails(self) -> None:
        with self.assertRaises(ValueError):
            clear_section(NOTE, "Нет такого")


class ClearProposalTests(unittest.TestCase):
    def test_preview_says_section_will_be_cleared(self) -> None:
        from app.agent import Proposal

        text = _proposal_text(
            Proposal(
                kind="section",
                path="Projects/Альфа.md",
                heading="Связи",
                mode="clear",
                content="",
            )
        )
        self.assertIn("Очистить раздел «Связи»", text)
        self.assertIn("заголовок останется", text)

    def test_apply_clears_section_and_writes(self) -> None:
        from app.agent import Proposal

        class FakeBackend:
            def __init__(self, text: str) -> None:
                self.text = text
                self.written = ""

            async def read_note(self, path: str) -> str:
                return self.text

            async def write_note(self, path: str, content: str, *, overwrite=False):
                self.written = content
                return True

        backend = FakeBackend(NOTE)
        proposal = Proposal(
            kind="section",
            path="Projects/Альфа.md",
            heading="Связи",
            mode="clear",
            content="",
        )
        message = asyncio.run(_apply(backend, proposal, proposal.path, ()))
        self.assertIn("Изменения сохранены", message)
        self.assertNotIn("[[", backend.written)
        self.assertIn("## Связи", backend.written)
        self.assertIn("## Дальше", backend.written)

    def test_agent_tool_with_empty_content_means_clear(self) -> None:
        import json

        from app.agent import Agent
        from app.llm import ToolCall

        settings = make_settings()
        agent = Agent(None, None, settings)  # type: ignore[arg-type]
        call = ToolCall(
            id="1",
            name="edit_section",
            arguments=json.dumps(
                {"path": "A.md", "heading": "Связи", "content": "  ", "mode": "replace"}
            ),
        )
        result = agent._proposal(call)
        self.assertIsNotNone(result.proposal)
        self.assertEqual(result.proposal.mode, "clear")

    def test_agent_tool_rejects_empty_append(self) -> None:
        import json

        from app.agent import Agent
        from app.llm import ToolCall

        agent = Agent(None, None, make_settings())  # type: ignore[arg-type]
        call = ToolCall(
            id="1",
            name="edit_section",
            arguments=json.dumps(
                {"path": "A.md", "heading": "Связи", "content": "", "mode": "append"}
            ),
        )
        result = agent._proposal(call)
        self.assertIsNone(result.proposal)

    def test_agent_tool_rejects_empty_append_note(self) -> None:
        import json

        from app.agent import Agent
        from app.llm import ToolCall

        agent = Agent(None, None, make_settings())  # type: ignore[arg-type]
        call = ToolCall(
            id="1",
            name="append_note",
            arguments=json.dumps({"path": "A.md", "content": ""}),
        )
        self.assertIsNone(agent._proposal(call).proposal)


if __name__ == "__main__":
    unittest.main()
