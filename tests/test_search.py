"""Тесты поиска: очистка запроса, совпадения по тексту и именам."""

from __future__ import annotations

import unittest

from app.search import content_score, excerpt, normalize, query_terms, rank


class NormalizeTests(unittest.TestCase):
    def test_lowercase_and_yo(self) -> None:
        self.assertEqual(normalize("ЁЖИК"), "ежик")

    def test_punctuation_becomes_spaces(self) -> None:
        self.assertEqual(normalize("  Код — «Заморожен»!  "), "код заморожен")


class QueryTermsTests(unittest.TestCase):
    def test_drops_search_command_and_quotes(self) -> None:
        self.assertEqual(
            query_terms('Найди "код заморожен"'), ["код", "заморожен"]
        )

    def test_drops_prepositions(self) -> None:
        self.assertEqual(query_terms("что там про проект"), ["проект"])

    def test_keeps_meaningful_words(self) -> None:
        self.assertEqual(query_terms("Отчёт Q4"), ["отчет", "q4"])

    def test_only_stop_words_are_kept_as_fallback(self) -> None:
        self.assertEqual(query_terms("найди"), ["найди"])


class ContentScoreTests(unittest.TestCase):
    def test_phrase_with_quotes_and_command(self) -> None:
        score = content_score(
            "# Старый проект\n\n- Код заморожен\n", 'Найди "код заморожен"'
        )
        self.assertIsNotNone(score)
        assert score is not None
        self.assertEqual(score[0], 1.0)

    def test_any_word_order(self) -> None:
        score = content_score("Заморожен код", "код заморожен")
        self.assertIsNotNone(score)
        assert score is not None
        self.assertEqual(score[0], 0.9)

    def test_partial_word(self) -> None:
        score = content_score("Код заморожен", "заморож")
        self.assertIsNotNone(score)

    def test_case_and_yo_are_ignored(self) -> None:
        self.assertIsNotNone(content_score("Код заморожен", "КОД ЗАМОРОЖЕН"))
        self.assertIsNotNone(content_score("Отчёт готов", "отчет"))

    def test_no_match(self) -> None:
        self.assertIsNone(content_score("Код заморожен", "квантовая хромодинамика"))


class ExcerptTests(unittest.TestCase):
    def test_returns_text_around_match(self) -> None:
        text = "Документация передана Ивану.\n- Код заморожен\n- Уроки позже."
        context = excerpt(text, 'Найди "код заморожен"', before=10, after=30)
        self.assertIn("Код заморожен", context)

    def test_marks_cut_edges(self) -> None:
        text = "а" * 100 + " код заморожен " + "б" * 100
        context = excerpt(text, "код заморожен")
        self.assertTrue(context.startswith("…"))
        self.assertTrue(context.endswith("…"))


class RankTests(unittest.TestCase):
    def test_name_wins_over_command_words(self) -> None:
        ranked = rank("Найди Иван", ["People/Иван Петров.md"])
        self.assertEqual(ranked[0][0], "People/Иван Петров.md")
        self.assertEqual(ranked[0][2], "имя")

    def test_exact_name_is_first(self) -> None:
        ranked = rank("Индекс", ["Notes/Индекс.md", "Projects/Индекс.md"])
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0][1], 1.0)


if __name__ == "__main__":
    unittest.main()
