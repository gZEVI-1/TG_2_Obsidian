"""Тесты защиты от рассуждений модели в тексте заметки."""

from __future__ import annotations

import asyncio
import unittest

from app.agent import Agent, _clean_completion, _looks_like_reasoning
from app.config import Settings
from app.llm import LLMError, LLMResponse

SECTION_TEXT = "## Связи\n\n- [[Проект Альфа]]\n- [[Иван Петров]]\n"
USER_WISH = "убери все связи"


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


class FakeLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def chat(self, messages, *, tools=None, max_tokens=0) -> LLMResponse:
        self.prompts.append(messages[-1]["content"])
        return LLMResponse(text=self.reply)

    async def close(self) -> None:
        return None


class ReasoningGuardTests(unittest.TestCase):
    def test_reasoning_with_instruction_quote_is_rejected(self) -> None:
        reply = (
            "User's wish: \"убрать все связи\".\n\n"
            "Constraints:\n1. Return only the new content of the section.\n\n"
            "Since the user wants to remove all connections, the section should be empty."
        )
        agent = Agent(FakeLLM(reply), None, make_settings())
        with self.assertRaises(LLMError):
            asyncio.run(agent.compose_section("A.md", "Связи", SECTION_TEXT, USER_WISH))

    def test_english_reasoning_without_markers_is_rejected(self) -> None:
        reply = (
            "The section currently holds two references. I think the right move here "
            "is to drop both of them, because the author asked for a clean slate and "
            "nothing else matters in this particular case."
        )
        agent = Agent(FakeLLM(reply), None, make_settings())
        with self.assertRaises(LLMError):
            asyncio.run(agent.compose_section("A.md", "Связи", SECTION_TEXT, USER_WISH))

    def test_empty_section_is_a_valid_result(self) -> None:
        agent = Agent(FakeLLM(""), None, make_settings())
        result = asyncio.run(
            agent.compose_section("A.md", "Связи", SECTION_TEXT, USER_WISH)
        )
        self.assertEqual(result, "")

    def test_thinking_tags_are_stripped(self) -> None:
        reply = "<thinking>надо убрать всё</thinking>\n- [[Осталась]]"
        agent = Agent(FakeLLM(reply), None, make_settings())
        result = asyncio.run(
            agent.compose_section("A.md", "Связи", SECTION_TEXT, "оставь одну")
        )
        self.assertEqual(result, "- [[Осталась]]")

    def test_code_fence_is_stripped(self) -> None:
        reply = "```markdown\n## Связи\n\n- [[Проект Альфа]]\n```"
        agent = Agent(FakeLLM(reply), None, make_settings())
        result = asyncio.run(
            agent.compose_section("A.md", "Связи", SECTION_TEXT, "поправь")
        )
        self.assertTrue(result.startswith("## Связи"))
        self.assertNotIn("```", result)

    def test_append_still_requires_text(self) -> None:
        agent = Agent(FakeLLM(""), None, make_settings())
        with self.assertRaises(LLMError):
            asyncio.run(agent.compose_append("A.md", "Связи", SECTION_TEXT, "допиши"))

    def test_note_reasoning_is_rejected(self) -> None:
        reply = (
            "Let me think about the structure. The user wants a note about the "
            "project, so I will output a heading and a couple of paragraphs below it."
        )
        agent = Agent(FakeLLM(reply), None, make_settings())
        with self.assertRaises(LLMError):
            asyncio.run(agent.compose_note("Заметка про проект"))

    def test_clean_completion_helpers(self) -> None:
        self.assertEqual(_clean_completion("  текст  "), "текст")
        self.assertFalse(_looks_like_reasoning("Обычный текст заметки", SECTION_TEXT))
        self.assertTrue(
            _looks_like_reasoning("Return only the content", "Русский исходный текст")
        )


if __name__ == "__main__":
    unittest.main()
