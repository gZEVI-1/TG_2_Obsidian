"""Тесты связей: упоминания, раздел «Связи», настройки и отбор моделью."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent import Agent
from app.config import Settings, load_settings
from app.links import (
    add_links,
    duplicate_titles,
    existing_links,
    is_linked,
    link_label,
    mention_paths,
    note_title,
)
from app.llm import LLMError, LLMResponse


class LinkParsingTests(unittest.TestCase):
    def test_name_without_folder_and_extension(self) -> None:
        self.assertEqual(note_title("Projects/Проект Альфа.md"), "Проект Альфа")

    def test_existing_links_ignore_alias_and_anchor(self) -> None:
        text = "См. [[Идеи]] и [[Notes/Проект Альфа|альфа#Раздел]]."
        links = existing_links(text)
        self.assertIn("идеи", links)
        self.assertIn("notes/проект альфа", links)

    def test_link_check_compares_name_and_path(self) -> None:
        links = existing_links("[[Notes/Идеи]]")
        self.assertTrue(is_linked("Идеи", links))
        self.assertTrue(is_linked("Notes/Идеи", links))
        self.assertFalse(is_linked("Notes/Другое", links))

    def test_duplicates_get_path_instead_of_name(self) -> None:
        paths = ["Projects/Индекс.md", "Notes/Индекс.md", "Notes/Идеи.md"]
        duplicates = duplicate_titles(paths)
        self.assertEqual(link_label("Notes/Индекс.md", duplicates), "Notes/Индекс")
        self.assertEqual(link_label("Notes/Идеи.md", duplicates), "Идеи")


class MentionTests(unittest.TestCase):
    def test_finds_mentions_in_order(self) -> None:
        paths = ["Notes/Идеи.md", "Projects/Проект Альфа.md"]
        text = "Начал проект альфа, потом записал идеи."
        self.assertEqual(
            mention_paths(text, paths),
            ["Projects/Проект Альфа.md", "Notes/Идеи.md"],
        )

    def test_skips_self(self) -> None:
        paths = ["Notes/Идеи.md"]
        self.assertEqual(
            mention_paths("Заметка об идеях", paths, exclude="Notes/Идеи.md"), []
        )

    def test_skips_short_titles(self) -> None:
        self.assertEqual(mention_paths("дом и сад", ["Notes/Дом.md"]), [])

    def test_matches_whole_word_only(self) -> None:
        paths = ["Notes/Идея.md"]
        self.assertEqual(mention_paths("придумал идеями", paths), [])
        self.assertEqual(mention_paths("придумал идея", paths), ["Notes/Идея.md"])

    def test_matching_ignores_case_and_yo(self) -> None:
        self.assertEqual(
            mention_paths("Ссылка на лю цысинь", ["People/Лю Цысинь.md"]),
            ["People/Лю Цысинь.md"],
        )

    def test_strict_mode_ignores_lowercase_words(self) -> None:
        paths = ["People/Человек.md"]
        self.assertEqual(
            mention_paths("каждый человек", paths), ["People/Человек.md"]
        )
        self.assertEqual(mention_paths("каждый человек", paths, strict=True), [])
        self.assertEqual(
            mention_paths("Человек ушёл", paths, strict=True),
            ["People/Человек.md"],
        )


class AddLinksTests(unittest.TestCase):
    def test_creates_section_at_end(self) -> None:
        text = "---\ntags:\n  - Идея\n---\n\n# Заметка\n\nТекст заметки.\n"
        result = add_links(text, ["Notes/Идеи"])
        self.assertTrue(result.startswith(text.rstrip("\n")))
        self.assertIn("\n\n## Связи\n\n- [[Notes/Идеи]]\n", result)

    def test_appends_to_existing_section(self) -> None:
        text = "# Заметка\n\n---\n###### Связи\n[[Идеи]]\n---\n"
        result = add_links(text, ["Идеи", "Notes/Другое"])
        self.assertEqual(result.count("[[Идеи]]"), 1)
        self.assertIn("- [[Notes/Другое]]", result)
        # закрывающий разделитель остаётся после списка
        self.assertTrue(result.rstrip("\n").endswith("---"))

    def test_idempotent(self) -> None:
        text = "# Заметка\n\nТекст.\n"
        once = add_links(text, ["Идеи", "Проект Альфа"])
        twice = add_links(once, ["Идеи", "Проект Альфа"])
        self.assertEqual(once, twice)

    def test_skips_existing_alias_link(self) -> None:
        text = "# Заметка\n\n[[Идеи|мысли]] и всё.\n"
        self.assertEqual(add_links(text, ["Идеи"]), text)

    def test_skips_repeated_targets(self) -> None:
        result = add_links("# Заметка\n", ["Идеи", "Идеи"])
        self.assertEqual(result.count("[[Идеи]]"), 1)

    def test_empty_section_gets_blank_line(self) -> None:
        text = "# Заметка\n\n## Связи\n"
        self.assertEqual(
            add_links(text, ["Идеи"]), "# Заметка\n\n## Связи\n\n- [[Идеи]]\n"
        )


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = dict(
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
    values.update(overrides)
    return Settings(**values)


class LinkSettingsTests(unittest.TestCase):
    def load(self, **env: str) -> Settings:
        with patch.dict("os.environ", env, clear=True), patch(
            "app.config.ENV_FILE", Path("no-such.env")
        ):
            return load_settings()

    def test_defaults(self) -> None:
        settings = self.load()
        self.assertTrue(settings.agent_auto_link)
        self.assertEqual(settings.agent_link_limit, 10)

    def test_auto_link_can_be_disabled(self) -> None:
        self.assertFalse(self.load(AGENT_AUTO_LINK="false").agent_auto_link)

    def test_link_limit_is_validated(self) -> None:
        self.assertEqual(self.load(AGENT_LINK_LIMIT="3").agent_link_limit, 3)
        with self.assertRaises(ValueError):
            self.load(AGENT_LINK_LIMIT="51")
        with self.assertRaises(ValueError):
            self.load(AGENT_LINK_LIMIT="много")


class FakeBackend:
    """Хранилище без сети: список заметок и результаты поиска."""

    def __init__(self, notes: list[str], hits: list[dict[str, str]] | None = None) -> None:
        self.notes = notes
        self.hits = hits or []

    async def list_notes(self) -> list[str]:
        return list(self.notes)

    async def search_notes(self, query: str, limit: int = 5) -> list[dict[str, str]]:
        return list(self.hits)


class FakeLLM:
    """Возвращает заданный ответ или падает с LLMError."""

    def __init__(self, reply: str = "", *, fail: bool = False) -> None:
        self.reply = reply
        self.fail = fail
        self.prompts: list[str] = []

    async def chat(self, messages, *, tools=None, max_tokens=0) -> LLMResponse:
        self.prompts.append(messages[-1]["content"])
        if self.fail:
            raise LLMError("модель недоступна")
        return LLMResponse(text=self.reply)

    async def close(self) -> None:
        return None


class SuggestLinksTests(unittest.TestCase):
    def test_model_choice_is_used(self) -> None:
        backend = FakeBackend(["Projects/Проект Альфа.md", "Notes/Идеи.md"])
        llm = FakeLLM("Projects/Проект Альфа.md")
        agent = Agent(llm, backend, make_settings())
        links = asyncio.run(
            agent.suggest_links("Обсудили проект альфа и сроки.", title="Созвон")
        )
        self.assertEqual(links, ["Проект Альфа"])
        self.assertIn("Кандидаты:", llm.prompts[0])

    def test_existing_links_are_not_offered(self) -> None:
        backend = FakeBackend(
            ["Notes/Идеи.md", "Notes/Другое.md"],
            hits=[{"path": "Notes/Другое.md", "excerpt": "похожее"}],
        )
        llm = FakeLLM("Notes/Другое.md")
        agent = Agent(llm, backend, make_settings())
        links = asyncio.run(
            agent.suggest_links("[[Идеи]] уже связаны.", title="Идеи")
        )
        self.assertEqual(links, ["Другое"])

    def test_model_refusal_means_no_links(self) -> None:
        backend = FakeBackend(["Notes/Идеи.md"])
        agent = Agent(FakeLLM("нет"), backend, make_settings())
        self.assertEqual(
            asyncio.run(agent.suggest_links("Болтовня про идеи.")), []
        )

    def test_fallback_uses_mentions_when_model_fails(self) -> None:
        backend = FakeBackend(["Notes/Идеи.md"])
        agent = Agent(FakeLLM(fail=True), backend, make_settings())
        self.assertEqual(
            asyncio.run(agent.suggest_links("Болтовня про идеи.")), ["Идеи"]
        )


if __name__ == "__main__":
    unittest.main()
