"""Тесты лимитов: одна долгая операция на чат."""

from __future__ import annotations

import unittest

from app.bot import ChatMemory


class OperationLimitTests(unittest.TestCase):
    def test_one_operation_per_chat(self) -> None:
        memory = ChatMemory()
        self.assertTrue(memory.begin_operation(1, "связи"))
        self.assertFalse(memory.begin_operation(1, "индексация"))
        self.assertEqual(memory.current_operation(1), "связи")

        memory.end_operation(1)
        self.assertEqual(memory.current_operation(1), "")
        self.assertTrue(memory.begin_operation(1, "индексация"))

    def test_different_chats_are_independent(self) -> None:
        memory = ChatMemory()
        self.assertTrue(memory.begin_operation(1, "связи"))
        self.assertTrue(memory.begin_operation(2, "связи"))
        memory.end_operation(1)
        self.assertFalse(memory.begin_operation(2, "индексация"))

    def test_new_dialog_does_not_cancel_running_operation(self) -> None:
        memory = ChatMemory()
        self.assertTrue(memory.begin_operation(1, "связи"))
        memory.clear(1)
        self.assertFalse(memory.begin_operation(1, "индексация"))


if __name__ == "__main__":
    unittest.main()
