"""Тесты связей: упоминания, раздел «Связи», настройки и отбор моделью."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import link_rules
from app.agent import Agent
from app.bot import (
    BULK_TEXT_LIMIT,
    _apply_never_links,
    _bulk_plan,
    _bulk_plan_chunks,
    _exclude_bulk_links,
    _never_rules_from_text,
    _select_bulk_links,
    _wants_bulk_filter,
)
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
    """Хранилище без сети: список заметок, тексты и результаты поиска."""

    def __init__(
        self,
        notes: list[str],
        hits: list[dict[str, str]] | None = None,
        texts: dict[str, str] | None = None,
    ) -> None:
        self.notes = notes
        self.hits = hits or []
        self.texts = texts or {}

    async def list_notes(self) -> list[str]:
        return list(self.notes)

    async def search_notes(self, query: str, limit: int = 5) -> list[dict[str, str]]:
        return list(self.hits)

    async def read_note(self, path: str) -> str:
        return self.texts.get(path, "")


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

    def test_never_rules_are_not_offered(self) -> None:
        backend = FakeBackend(
            ["Projects/Проект Альфа.md", "Notes/Идеи.md"]
        )
        llm = FakeLLM("Projects/Проект Альфа.md")
        agent = Agent(llm, backend, make_settings())
        links = asyncio.run(
            agent.suggest_links(
                "Обсудили Идеи и Проект Альфа.",
                never_rules=((link_rules.WILDCARD, "Идеи"),),
            )
        )
        self.assertEqual(links, ["Проект Альфа"])
        self.assertNotIn("Notes/Идеи.md", llm.prompts[0])

    def test_fallback_uses_mentions_when_model_fails(self) -> None:
        backend = FakeBackend(["Notes/Идеи.md"])
        agent = Agent(FakeLLM(fail=True), backend, make_settings())
        self.assertEqual(
            asyncio.run(agent.suggest_links("Болтовня про идеи.")), ["Идеи"]
        )


class BulkFilterTests(unittest.TestCase):
    """Исключение и выбор связей из плана массовой простановки."""

    plan = (
        ("Notes/Альфа.md", ("Идеи", "Проект Бета")),
        ("Notes/Гамма.md", ("Идеи",)),
    )

    def test_filter_request_is_recognized(self) -> None:
        self.assertTrue(_wants_bulk_filter("не ставь 2, 5"))
        self.assertTrue(_wants_bulk_filter("убери связь [[Идеи]]"))
        self.assertTrue(_wants_bulk_filter("удали связи 2"))
        self.assertTrue(_wants_bulk_filter("поставь только 2, 5"))
        self.assertTrue(_wants_bulk_filter("оставь только [[Идеи]]"))
        self.assertTrue(_wants_bulk_filter("никогда не ставь 1-2"))
        self.assertFalse(_wants_bulk_filter("свяжи заметку Идеи"))
        self.assertFalse(_wants_bulk_filter("проставить все связи"))
        self.assertFalse(_wants_bulk_filter("убери заметку Идеи"))

    def test_excludes_by_number(self) -> None:
        plan, removed, error = _exclude_bulk_links("не ставь 2", self.plan)
        self.assertEqual(error, "")
        self.assertEqual(removed, 1)
        self.assertEqual(
            plan,
            (
                ("Notes/Альфа.md", ("Идеи",)),
                ("Notes/Гамма.md", ("Идеи",)),
            ),
        )

    def test_excludes_several_numbers_with_conjunction(self) -> None:
        plan, removed, _error = _exclude_bulk_links("не ставь 1 и 3", self.plan)
        self.assertEqual(removed, 2)
        self.assertEqual(plan, (("Notes/Альфа.md", ("Проект Бета",)),))

    def test_excludes_all_links(self) -> None:
        plan, removed, _error = _exclude_bulk_links("исключи 1, 2, 3", self.plan)
        self.assertEqual(removed, 3)
        self.assertEqual(plan, ())

    def test_excludes_by_target_name(self) -> None:
        plan, removed, _error = _exclude_bulk_links(
            "не ставь [[Идеи]]", self.plan
        )
        self.assertEqual(removed, 2)
        self.assertEqual(plan, (("Notes/Альфа.md", ("Проект Бета",)),))

    def test_excludes_by_target_without_brackets(self) -> None:
        plan, removed, _error = _exclude_bulk_links("не ставь Идеи", self.plan)
        self.assertEqual(removed, 2)
        self.assertEqual(plan, (("Notes/Альфа.md", ("Проект Бета",)),))

    def test_excludes_pair_by_note_name(self) -> None:
        plan, removed, _error = _exclude_bulk_links(
            "не ставь Альфа → Проект Бета", self.plan
        )
        self.assertEqual(removed, 1)
        self.assertEqual(
            plan,
            (
                ("Notes/Альфа.md", ("Идеи",)),
                ("Notes/Гамма.md", ("Идеи",)),
            ),
        )

    def test_excludes_pair_by_note_path(self) -> None:
        plan, removed, _error = _exclude_bulk_links(
            "не ставь Notes/Гамма.md → [[Идеи]]", self.plan
        )
        self.assertEqual(removed, 1)
        self.assertEqual(
            plan,
            (("Notes/Альфа.md", ("Идеи", "Проект Бета")),),
        )

    def test_unknown_number_keeps_plan(self) -> None:
        plan, removed, error = _exclude_bulk_links("не ставь 99", self.plan)
        self.assertEqual(plan, self.plan)
        self.assertEqual(removed, 0)
        self.assertIn("99", error)

    def test_unknown_target_keeps_plan(self) -> None:
        plan, removed, error = _exclude_bulk_links("не ставь [[Нет такой]]", self.plan)
        self.assertEqual(plan, self.plan)
        self.assertEqual(removed, 0)
        self.assertIn("Нет такой", error)

    def test_only_keeps_selected_number(self) -> None:
        plan, kept, error = _select_bulk_links("поставь только 2", self.plan)
        self.assertEqual(error, "")
        self.assertEqual(kept, 1)
        self.assertEqual(plan, (("Notes/Альфа.md", ("Проект Бета",)),))

    def test_only_keeps_several_numbers(self) -> None:
        plan, kept, _error = _select_bulk_links("поставь только 1 и 3", self.plan)
        self.assertEqual(kept, 2)
        self.assertEqual(
            plan,
            (
                ("Notes/Альфа.md", ("Идеи",)),
                ("Notes/Гамма.md", ("Идеи",)),
            ),
        )

    def test_only_keeps_by_target(self) -> None:
        plan, kept, _error = _select_bulk_links("поставь только [[Идеи]]", self.plan)
        self.assertEqual(kept, 2)
        self.assertEqual(
            plan,
            (
                ("Notes/Альфа.md", ("Идеи",)),
                ("Notes/Гамма.md", ("Идеи",)),
            ),
        )

    def test_only_keeps_pair(self) -> None:
        plan, kept, _error = _select_bulk_links(
            "поставь только Гамма → Идеи", self.plan
        )
        self.assertEqual(kept, 1)
        self.assertEqual(plan, (("Notes/Гамма.md", ("Идеи",)),))

    def test_only_keeps_everything(self) -> None:
        plan, kept, _error = _select_bulk_links("поставь только 1, 2, 3", self.plan)
        self.assertEqual(kept, 3)
        self.assertEqual(plan, self.plan)

    def test_only_unknown_number_keeps_plan(self) -> None:
        plan, kept, error = _select_bulk_links("поставь только 99", self.plan)
        self.assertEqual(plan, self.plan)
        self.assertEqual(kept, 0)
        self.assertIn("99", error)

    def test_excludes_range(self) -> None:
        plan, removed, error = _exclude_bulk_links("не ставь 1-2", self.plan)
        self.assertEqual(error, "")
        self.assertEqual(removed, 2)
        self.assertEqual(plan, (("Notes/Гамма.md", ("Идеи",)),))

    def test_never_range_selects_same_indexes(self) -> None:
        plan, removed, error = _exclude_bulk_links(
            "никогда не ставь 1-2", self.plan
        )
        self.assertEqual(error, "")
        self.assertEqual(removed, 2)
        self.assertEqual(plan, (("Notes/Гамма.md", ("Идеи",)),))

    def test_only_keeps_range(self) -> None:
        plan, kept, error = _select_bulk_links("поставь только 2-3", self.plan)
        self.assertEqual(error, "")
        self.assertEqual(kept, 2)
        self.assertEqual(
            plan,
            (
                ("Notes/Альфа.md", ("Проект Бета",)),
                ("Notes/Гамма.md", ("Идеи",)),
            ),
        )

    def test_reversed_range_keeps_plan(self) -> None:
        plan, removed, error = _exclude_bulk_links("не ставь 3-1", self.plan)
        self.assertEqual(plan, self.plan)
        self.assertEqual(removed, 0)
        self.assertIn("начало больше конца", error)

    def test_never_number_range_becomes_pair_rules(self) -> None:
        rules, error = _never_rules_from_text(
            "никогда не ставь 1-2", self.plan, {0, 1}
        )
        self.assertEqual(error, "")
        self.assertEqual(
            rules,
            (
                ("Notes/Альфа.md", "Идеи"),
                ("Notes/Альфа.md", "Проект Бета"),
            ),
        )

    def test_never_target_becomes_global_rule(self) -> None:
        rules, error = _never_rules_from_text(
            "никогда не ставь [[Идеи]]", self.plan, {0, 2}
        )
        self.assertEqual(error, "")
        self.assertEqual(rules, ((link_rules.WILDCARD, "Идеи"),))

    def test_never_pair_becomes_pair_rule(self) -> None:
        rules, error = _never_rules_from_text(
            "никогда не ставь Гамма → Идеи", self.plan, {2}
        )
        self.assertEqual(error, "")
        self.assertEqual(rules, (("Notes/Гамма.md", "Идеи"),))

    def test_never_rule_is_saved_and_removed_from_plan(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch(
            "app.config.PROJECT_ROOT", Path(folder)
        ):
            plan, removed, saved, error = _apply_never_links(
                make_settings(), self.plan, "никогда не ставь 2"
            )
            self.assertEqual(error, "")
            self.assertEqual(saved, 1)
            self.assertEqual(removed, 1)
            self.assertEqual(
                plan,
                (
                    ("Notes/Альфа.md", ("Идеи",)),
                    ("Notes/Гамма.md", ("Идеи",)),
                ),
            )
            stored = link_rules.load(Path(folder) / "state" / "link_rules.json")
            self.assertEqual(stored, (("Notes/Альфа", "Проект Бета"),))

    def test_chunks_show_all_links_and_hints(self) -> None:
        chunks = _bulk_plan_chunks(list(self.plan), 10, 5, [], hint=True)
        text = "\n".join(chunks)
        self.assertIn("• новых ссылок — 3", text)
        self.assertIn("1. Notes/Альфа.md → [[Идеи]]", text)
        self.assertIn("3. Notes/Гамма.md → [[Идеи]]", text)
        self.assertIn("не ставь 2, 5", text)
        self.assertIn("поставь только 2, 5", text)
        self.assertNotIn("ещё", text)

    def test_chunks_without_hint_have_no_prompt(self) -> None:
        text = "\n".join(_bulk_plan_chunks(list(self.plan), 10, 5, []))
        self.assertNotIn("исключить", text)
        self.assertNotIn("поставь только", text)

    def test_long_plan_is_split_but_not_truncated(self) -> None:
        plan = [
            (
                f"Projects/Очень длинное имя заметки номер {index}.md",
                tuple(f"Цель {index}-{number}" for number in range(4)),
            )
            for index in range(30)
        ]
        chunks = _bulk_plan_chunks(plan, 100, 50, [], hint=True)
        text = "\n".join(chunks)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= BULK_TEXT_LIMIT for chunk in chunks))
        self.assertIn("120. Projects/", text)
        self.assertNotIn("ещё", text)


class LinkRuleTests(unittest.TestCase):
    """Постоянные запреты связей: хранение и применение."""

    def test_roundtrip_and_pair_match(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "link_rules.json"
            rules = link_rules.add((), "Notes/Альфа.md", "[[Идеи|мысли]]")
            self.assertEqual(rules, (("Notes/Альфа", "Идеи"),))
            link_rules.save(path, rules)
            loaded = link_rules.load(path)
            self.assertTrue(link_rules.is_blocked("Notes/Альфа.md", "Идеи", loaded))
            self.assertFalse(
                link_rules.is_blocked("Notes/Другая.md", "Идеи", loaded)
            )

    def test_pair_rule_is_tied_to_path(self) -> None:
        rules = (("Notes/Альфа", "Идеи"),)
        self.assertTrue(link_rules.is_blocked("Notes/Альфа.md", "Идеи", rules))
        self.assertFalse(
            link_rules.is_blocked("Archive/Альфа.md", "Идеи", rules)
        )

    def test_global_rule_blocks_any_source(self) -> None:
        rules = ((link_rules.WILDCARD, "Notes/Идеи"),)
        self.assertTrue(link_rules.is_blocked("Notes/Любая.md", "Идеи", rules))
        self.assertFalse(link_rules.is_blocked("Notes/Любая.md", "Другое", rules))

    def test_duplicate_rule_is_not_added(self) -> None:
        rules = link_rules.add((), "Notes/Альфа.md", "Идеи")
        again = link_rules.add(rules, "Notes/Альфа", "Notes/Идеи")
        self.assertEqual(again, rules)

    def test_broken_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "link_rules.json"
            path.write_text("{oops", encoding="utf-8")
            self.assertEqual(link_rules.load(path), ())


class BulkPlanRulesTests(unittest.TestCase):
    """Массовая простановка не предлагает связи, запрещённые навсегда."""

    def test_never_rule_excludes_target_from_plan(self) -> None:
        backend = FakeBackend(
            ["Notes/Заметка.md", "Notes/Идеи.md", "Projects/Проект Альфа.md"],
            texts={"Notes/Заметка.md": "Упомянул Идеи и Проект Альфа."},
        )
        plan, _errors, _total, _checked = asyncio.run(
            _bulk_plan(
                backend,
                make_settings(),
                ((link_rules.WILDCARD, "Идеи"),),
            )
        )
        self.assertEqual(plan, [("Notes/Заметка.md", ("Проект Альфа",))])


if __name__ == "__main__":
    unittest.main()
