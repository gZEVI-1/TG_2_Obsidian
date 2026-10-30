"""Тесты новых операций с разделами, папками и путями заметок."""

from __future__ import annotations

import unittest

from app.notes import (
    append_to_section,
    clear_section,
    create_section,
    destination_path,
    find_section,
    replace_section,
    sections,
    validate_folder,
)


NOTE_WITH_INLINE = """# Заметка

### Братья/сестры : Тетя Оксана

### Дети :[[!03 Даша]], Я
"""


class SectionMatchingTests(unittest.TestCase):
    """Заголовки — просто строки с #; текст после двоеточия им не мешает."""

    def test_finds_heading_with_inline_content(self) -> None:
        section = find_section(NOTE_WITH_INLINE, "Братья/сестры")
        self.assertIsNotNone(section)
        assert section is not None
        self.assertEqual(section.inline, "Тетя Оксана")

    def test_ignores_spaces_around_slash_and_yo(self) -> None:
        text = "# Заметка\n\n## Братья / сёстры : Тётя\n"
        self.assertIsNotNone(find_section(text, "братья/сестры"))
        self.assertEqual(sections(text), ["Братья / сёстры"])

    def test_ignores_markdown_emphasis(self) -> None:
        text = "# Заметка\n\n## **Итоги** : всё готово\n"
        self.assertIsNotNone(find_section(text, "Итоги"))

    def test_exact_heading_wins_over_prefix(self) -> None:
        text = "# Заметка\n\n## Итоги\n\nОбщие.\n\n## Итоги: 2026\n\nГод.\n"
        section = find_section(text, "Итоги")
        self.assertIsNotNone(section)
        assert section is not None
        self.assertEqual(section.inline, "")

    def test_replace_removes_inline_content(self) -> None:
        result = replace_section(NOTE_WITH_INLINE, "Братья/сестры", "Брат Иван.")
        self.assertIn("### Братья/сестры\n\nБрат Иван.", result)
        self.assertNotIn("Тетя Оксана", result)

    def test_clear_removes_inline_content_but_keeps_heading(self) -> None:
        result = clear_section(NOTE_WITH_INLINE, "Братья/сестры")
        self.assertIn("### Братья/сестры\n", result)
        self.assertNotIn("Тетя Оксана", result)

    def test_append_keeps_inline_content_as_body(self) -> None:
        result = append_to_section(NOTE_WITH_INLINE, "Братья/сестры", "Ещё брат.")
        self.assertIn("### Братья/сестры\n", result)
        self.assertIn("Тетя Оксана", result)
        self.assertIn("Ещё брат.", result)

    def test_missing_section_lists_available_names(self) -> None:
        with self.assertRaises(ValueError) as context:
            clear_section(NOTE_WITH_INLINE, "Нет такого")
        self.assertIn("Есть:", str(context.exception))
        self.assertIn("Братья/сестры", str(context.exception))


class CreateSectionTests(unittest.TestCase):
    def test_adds_section_at_end(self) -> None:
        text = "# Заметка\n\nВступление.\n"
        result = create_section(text, "Итоги", "Всё хорошо.")
        self.assertEqual(
            result,
            "# Заметка\n\nВступление.\n\n## Итоги\n\nВсё хорошо.\n",
        )

    def test_empty_body_creates_heading(self) -> None:
        result = create_section("# Заметка\n", "Новый раздел")
        self.assertTrue(result.endswith("## Новый раздел\n"))

    def test_custom_level(self) -> None:
        result = create_section("# Заметка\n", "Детали", level=3)
        self.assertIn("### Детали", result)

    def test_refuses_duplicate_case_insensitive(self) -> None:
        text = "# Заметка\n\n## Итоги\n\nТекст.\n"
        with self.assertRaises(ValueError):
            create_section(text, "ИТОГИ")

    def test_empty_heading_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            create_section("# Заметка\n", "  ")

    def test_frontmatter_is_preserved(self) -> None:
        text = "---\ntitle: Тест\n---\n\n# Тест\n"
        result = create_section(text, "Раздел")
        self.assertTrue(result.startswith("---\ntitle: Тест\n---"))
        self.assertIn("## Раздел", result)


class FolderValidationTests(unittest.TestCase):
    def test_normalizes_backslashes(self) -> None:
        self.assertEqual(validate_folder("Проекты\\2026"), "Проекты/2026")

    def test_rejects_parent_segments(self) -> None:
        with self.assertRaises(ValueError):
            validate_folder("Проекты/../Секрет")

    def test_rejects_service_folders(self) -> None:
        with self.assertRaises(ValueError):
            validate_folder(".obsidian")
        with self.assertRaises(ValueError):
            validate_folder(".trash")

    def test_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            validate_folder("  ")


class DestinationPathTests(unittest.TestCase):
    def test_folder_keeps_filename(self) -> None:
        self.assertEqual(
            destination_path("Projects/Альфа.md", "Архив"),
            "Архив/Альфа.md",
        )

    def test_full_path_with_md(self) -> None:
        self.assertEqual(
            destination_path("Projects/Альфа.md", "Архив/Альфа-2024.md"),
            "Архив/Альфа-2024.md",
        )

    def test_same_path_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            destination_path("Projects/Альфа.md", "Projects/Альфа.md")

    def test_requires_destination(self) -> None:
        with self.assertRaises(ValueError):
            destination_path("Projects/Альфа.md", " ")


if __name__ == "__main__":
    unittest.main()
