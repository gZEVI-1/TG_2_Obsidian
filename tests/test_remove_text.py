"""Тесты удаления части текста из заметки."""

from __future__ import annotations

import unittest

from app.notes import remove_text


class RemoveLineTests(unittest.TestCase):
    def test_removes_link_line_from_relations_block(self) -> None:
        note = (
            "---\ntags:\n  - person\n---\n\n# Сосед\n\nПросто текст.\n\n"
            "###### Связи\n[[Шевчук]]\n[[Петров]]\n"
        )
        updated, removed = remove_text(note, "Шевчук")
        self.assertEqual(removed, 1)
        self.assertNotIn("Шевчук", updated)
        self.assertIn("[[Петров]]", updated)
        self.assertIn("###### Связи", updated)
        self.assertIn("  - person", updated)

    def test_removes_exact_link(self) -> None:
        updated, removed = remove_text("# X\n\n###### Связи\n[[Шевчук]]\n", "[[Шевчук]]")
        self.assertEqual(removed, 1)
        self.assertNotIn("[[Шевчук]]", updated)

    def test_removes_list_item_line(self) -> None:
        note = "# Список\n\n- [[Шевчук]]\n- [[Петров]]\n"
        updated, removed = remove_text(note, "Шевчук")
        self.assertEqual(removed, 1)
        self.assertNotIn("Шевчук", updated)
        self.assertIn("- [[Петров]]", updated)

    def test_removes_whole_line_with_marker(self) -> None:
        note = "# X\n\n- купить хлеб\n- купить чай\n"
        updated, removed = remove_text(note, "- купить хлеб")
        self.assertEqual(removed, 1)
        self.assertNotIn("хлеб", updated)
        self.assertIn("- купить чай", updated)


class RemoveInlineTests(unittest.TestCase):
    def test_removes_inline_link(self) -> None:
        note = "# X\n\nСм. [[Шевчук]] и всё.\n"
        updated, removed = remove_text(note, "[[Шевчук]]")
        self.assertEqual(removed, 1)
        self.assertEqual(updated, "# X\n\nСм. и всё.\n")

    def test_removes_all_occurrences(self) -> None:
        note = "# X\n\n[[Шевчук]]\n\nТекст [[Шевчук]] ещё.\n"
        updated, removed = remove_text(note, "Шевчук")
        self.assertEqual(removed, 2)
        self.assertNotIn("Шевчук", updated)

    def test_line_with_only_links_and_conjunction_is_dropped(self) -> None:
        note = "# X\n\n[[Шевчук]] и [[Петров]]\n\nДальше текст.\n"
        updated, removed = remove_text(note, "Шевчук")
        self.assertEqual(removed, 1)
        self.assertIn("[[Петров]]", updated)
        self.assertNotIn("и \n", updated)

    def test_alias_and_path_links_are_found(self) -> None:
        note = "# X\n\n[[Люди/Шевчук|Александр]] и [[Notes/Шевчук]].\n"
        updated, removed = remove_text(note, "Шевчук")
        self.assertEqual(removed, 2)
        self.assertNotIn("Шевчук", updated)

    def test_case_insensitive(self) -> None:
        note = "# X\n\nТекст про шевчука.\n"
        updated, removed = remove_text(note, "Шевчук")
        self.assertEqual(removed, 1)
        self.assertNotIn("шевчук", updated.casefold())

    def test_link_to_other_name_is_not_broken(self) -> None:
        note = "# X\n\n- [[Александр Шевчук]]\n- [[Шевчук]]\n"
        updated, removed = remove_text(note, "Шевчук")
        self.assertEqual(removed, 1)
        self.assertIn("[[Александр Шевчук]]", updated)
        self.assertNotIn("[[Шевчук]]", updated)

    def test_plain_text_inside_foreign_link_untouched(self) -> None:
        note = "# X\n\nСсылка [[Александр Шевчук]] остаётся.\n"
        with self.assertRaises(ValueError):
            remove_text(note, "Шевчук")

    def test_heading_text_is_not_mangled(self) -> None:
        with self.assertRaises(ValueError):
            remove_text("## Связи\n\n[[Шевчук]]\n", "Связи")

    def test_heading_can_be_removed_explicitly(self) -> None:
        updated, removed = remove_text("## Связи\n\nтекст\n", "## Связи")
        self.assertEqual(removed, 1)
        self.assertNotIn("Связи", updated)


class RemoveParagraphTests(unittest.TestCase):
    def test_multiline_paragraph(self) -> None:
        note = "# X\n\nПервый абзац.\n\nВторой абзац\nс продолжением.\n\nТретий.\n"
        updated, removed = remove_text(note, "Второй абзац\nс продолжением.")
        self.assertEqual(removed, 1)
        self.assertNotIn("Второй абзац", updated)
        self.assertIn("Первый абзац.", updated)
        self.assertIn("Третий.", updated)
        self.assertNotIn("\n\n\n", updated)


class RemoveErrorsTests(unittest.TestCase):
    def test_missing_text_raises(self) -> None:
        with self.assertRaises(ValueError):
            remove_text("# X\n\nТекст.\n", "Шевчук")

    def test_frontmatter_is_protected(self) -> None:
        note = "---\ntags:\n  - Шевчук\n---\n\n# X\n\nТело.\n"
        with self.assertRaises(ValueError):
            remove_text(note, "Шевчук")

    def test_empty_fragment_raises(self) -> None:
        with self.assertRaises(ValueError):
            remove_text("# X\n", "  ")


if __name__ == "__main__":
    unittest.main()
