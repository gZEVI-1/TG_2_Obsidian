"""Тесты выбора папки: разбор ответа, папка в заголовке, очистка пути."""

from __future__ import annotations

import unittest

from app.notes import clean_folder, folder_from_text, split_folder_title


class CleanFolderTests(unittest.TestCase):
    def test_keeps_name(self) -> None:
        self.assertEqual(clean_folder("Люди"), "Люди")

    def test_removes_parent_segments(self) -> None:
        self.assertEqual(clean_folder("../evil"), "evil")

    def test_collapses_empty_segments(self) -> None:
        self.assertEqual(clean_folder("Проекты//2026"), "Проекты/2026")

    def test_backslashes_become_separators(self) -> None:
        self.assertEqual(clean_folder("Проекты\\2026"), "Проекты/2026")


class FolderFromTextTests(unittest.TestCase):
    def test_command_with_folder(self) -> None:
        self.assertEqual(folder_from_text("Создай папку люди и добавь туда"), "люди")

    def test_matches_existing_folder_spelling(self) -> None:
        self.assertEqual(folder_from_text("создай папку люди", ["Люди"]), "Люди")

    def test_keeps_nested_path(self) -> None:
        self.assertEqual(folder_from_text("Проекты/2026"), "Проекты/2026")

    def test_sentence_is_not_a_folder(self) -> None:
        self.assertEqual(folder_from_text("привет как дела"), "")

    def test_two_word_folder(self) -> None:
        self.assertEqual(folder_from_text("Рабочие заметки"), "Рабочие заметки")


class SplitFolderTitleTests(unittest.TestCase):
    def test_single_word_prefix_is_folder(self) -> None:
        self.assertEqual(
            split_folder_title("Люди/101 Александр Шевчук"),
            ("101 Александр Шевчук", "Люди"),
        )

    def test_known_folder_prefix(self) -> None:
        self.assertEqual(
            split_folder_title("Проект Го/документация", ["Проект Го"]),
            ("документация", "Проект Го"),
        )

    def test_title_with_digits_is_not_folder(self) -> None:
        self.assertEqual(split_folder_title("Q3/Q4 итоги"), ("Q3/Q4 итоги", ""))

    def test_title_without_slash(self) -> None:
        self.assertEqual(
            split_folder_title("Обычная заметка"), ("Обычная заметка", "")
        )


if __name__ == "__main__":
    unittest.main()
