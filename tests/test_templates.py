"""Тесты шаблонов заметок и подстановки значений."""

from __future__ import annotations

import asyncio
import re
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.agent import Agent
from app.config import Settings, load_settings
from app.llm import LLMResponse
from app.templates import render, template_name, template_paths


class RenderTests(unittest.TestCase):
    def test_replaces_title_date_and_time(self) -> None:
        now = datetime(2026, 10, 28, 9, 5)
        text = "# {{title}}\n\n## Дата\n\n{{date}} в {{time}}"
        self.assertEqual(
            render(text, "Встреча", now),
            "# Встреча\n\n## Дата\n\n2026-10-28 в 09:05",
        )

    def test_unknown_placeholders_are_kept(self) -> None:
        text = "{{author}} — {{date}}"
        result = render(text, "Заметка", datetime(2026, 1, 2, 3, 4))
        self.assertIn("{{author}}", result)
        self.assertIn("2026-01-02", result)

    def test_text_without_placeholders_is_unchanged(self) -> None:
        text = "# Заметка\n\nОбычный текст."
        self.assertEqual(render(text, "Заметка"), text)

    def test_custom_date_format(self) -> None:
        now = datetime(2026, 10, 28, 9, 5)
        self.assertEqual(render("# {{date:DD.MM.YY}}", "День", now), "# 28.10.26")

    def test_custom_time_format(self) -> None:
        now = datetime(2026, 10, 28, 9, 5, 7)
        self.assertEqual(render("{{time:HH:mm:ss}}", "День", now), "09:05:07")

    def test_single_digit_tokens_are_not_padded(self) -> None:
        now = datetime(2026, 1, 2, 3, 4)
        self.assertEqual(render("{{date:D.M.YYYY}}", "День", now), "2.1.2026")

    def test_empty_format_falls_back_to_default(self) -> None:
        now = datetime(2026, 10, 28, 9, 5)
        self.assertEqual(render("{{date:}}", "День", now), "2026-10-28")


class TemplatePathsTests(unittest.TestCase):
    def test_takes_only_markdown_directly_in_folder(self) -> None:
        notes = [
            "Templates/Встреча.md",
            "Templates/Дневник.md",
            "Templates/Вложенный/Идея.md",
            "Notes/Встреча.md",
            "Templates/Карта.canvas",
        ]
        self.assertEqual(
            template_paths(notes, "Templates"),
            ["Templates/Встреча.md", "Templates/Дневник.md"],
        )

    def test_folder_name_is_case_insensitive(self) -> None:
        self.assertEqual(
            template_paths(["templates/Идея.md"], "Templates"),
            ["templates/Идея.md"],
        )

    def test_empty_folder_returns_nothing(self) -> None:
        self.assertEqual(template_paths(["Inbox/Заметка.md"], "Templates"), [])

    def test_excluded_style_note_is_not_a_template(self) -> None:
        notes = ["Templates/Стиль.md", "Templates/Встреча.md"]
        self.assertEqual(
            template_paths(notes, "Templates", exclude="Templates/Стиль.md"),
            ["Templates/Встреча.md"],
        )

    def test_name_without_folder_and_extension(self) -> None:
        self.assertEqual(template_name("Templates/Шаблон встречи.md"), "Шаблон встречи")


def make_settings(style_note: str = "") -> Settings:
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
        agent_style_note=style_note,
    )


class FakeLLM:
    """Возвращает заранее заданный Markdown и запоминает промпты."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def chat(self, messages, *, tools=None, max_tokens=0) -> LLMResponse:
        self.prompts.append(messages[-1]["content"])
        return LLMResponse(text=self.reply)

    async def close(self) -> None:
        return None


class ComposeWithTemplateTests(unittest.TestCase):
    def test_template_prompt_and_placeholders(self) -> None:
        llm = FakeLLM("# Встреча\n\n## Дата\n\n{{date}}\n\n## Итоги\n\nОбсудили план.")
        agent = Agent(llm, None, make_settings(), style="Пиши кратко.")
        title, content = asyncio.run(
            agent.compose_note(
                "Встреча по проекту", "# {{title}}\n\n## Дата\n\n{{date}}\n\n## Итоги"
            )
        )

        self.assertEqual(title, "Встреча")
        self.assertRegex(content, re.compile(r"\d{4}-\d{2}-\d{2}"))
        self.assertIn("## Итоги", content)
        self.assertIn("Шаблон:", llm.prompts[0])
        self.assertIn("Пиши кратко.", llm.prompts[0])

    def test_style_in_system_prompt(self) -> None:
        agent = Agent(FakeLLM(""), None, make_settings(), style="Короткие абзацы.")
        self.assertIn("Короткие абзацы.", agent.system_prompt)


class TemplateSettingsTests(unittest.TestCase):
    def load(self, **env: str) -> Settings:
        with patch.dict("os.environ", env, clear=True), patch(
            "app.config.ENV_FILE", Path("no-such.env")
        ):
            return load_settings()

    def test_defaults(self) -> None:
        settings = self.load()
        self.assertEqual(settings.agent_template_folder, "Templates")
        self.assertEqual(settings.agent_style_note, "")

    def test_style_note_must_be_markdown(self) -> None:
        with self.assertRaises(ValueError):
            self.load(AGENT_STYLE_NOTE="Templates/Стиль.txt")

    def test_style_note_rejects_parent_dir(self) -> None:
        with self.assertRaises(ValueError):
            self.load(AGENT_STYLE_NOTE="../Стиль.md")


if __name__ == "__main__":
    unittest.main()
