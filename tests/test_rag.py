"""Тесты RAG: нарезка заметок, индекс, семантический поиск и настройки."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings, load_settings
from app.rag import available, chunk_text, store_dir

if available():
    import numpy as np

    from app.rag import SemanticSearch


class ChunkTests(unittest.TestCase):
    def test_empty_text(self) -> None:
        self.assertEqual(chunk_text(""), [])
        self.assertEqual(chunk_text("\n\n  \n"), [])

    def test_short_note_is_one_chunk(self) -> None:
        text = "# Заметка\n\nПервый абзац.\n\nВторой абзац."
        self.assertEqual(chunk_text(text), [text])

    def test_long_note_is_split_by_paragraphs(self) -> None:
        text = "\n\n".join(["абзац " * 120, "второй " * 120, "третий " * 120])
        chunks = chunk_text(text, limit=500)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 500 for chunk in chunks))

    def test_long_paragraph_is_hard_split(self) -> None:
        text = "слово " * 400
        chunks = chunk_text(text, limit=300)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 300 for chunk in chunks))


class FakeEmbedder:
    """Вектор из долей ключевых слов: детерминированно, без модели."""

    VOCAB = ("кот", "собака", "чай", "релиз")

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]):
        self.calls += 1
        rows = []
        for text in texts:
            lowered = text.lower()
            rows.append([lowered.count(word) for word in self.VOCAB] + [1.0])
        matrix = np.asarray(rows, dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        return matrix / np.maximum(norms, 1e-6)


class FakeVault:
    """Хранилище из словаря: для индекса достаточно двух методов."""

    def __init__(self, notes: dict[str, str]) -> None:
        self.notes = notes

    async def list_notes(self) -> list[str]:
        return list(self.notes)

    async def read_note(self, path: str) -> str:
        return self.notes[path]


@unittest.skipUnless(available(), "нужен numpy")
class SemanticIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make(self, store: str = "index") -> SemanticSearch:
        return SemanticSearch(FakeEmbedder(), self.dir / store)

    def test_builds_index_and_finds_note_by_meaning(self) -> None:
        semantic = self.make()
        vault = FakeVault(
            {
                "Notes/Кот.md": "Кот спит на окне целый день.",
                "Notes/Собака.md": "Собака гуляет во дворе.",
            }
        )
        stats = asyncio.run(semantic.refresh(vault))
        self.assertEqual(stats.notes, 2)
        self.assertEqual(stats.updated, 2)

        hits = asyncio.run(semantic.search("кот", limit=2))
        self.assertEqual(hits[0]["path"], "Notes/Кот.md")
        self.assertEqual(hits[0]["match"], "смысл")
        self.assertIn("Кот", hits[0]["excerpt"])

    def test_second_refresh_reuses_unchanged_notes(self) -> None:
        semantic = self.make()
        vault = FakeVault({"Notes/Кот.md": "Кот спит."})
        asyncio.run(semantic.refresh(vault))
        stats = asyncio.run(semantic.refresh(vault))
        self.assertEqual(stats.updated, 0)
        self.assertEqual(stats.skipped, 1)

    def test_changed_note_is_reembedded(self) -> None:
        semantic = self.make()
        vault = FakeVault({"Notes/Кот.md": "Кот спит."})
        asyncio.run(semantic.refresh(vault))
        vault.notes["Notes/Кот.md"] = "Кот спит и видит чай."
        stats = asyncio.run(semantic.refresh(vault))
        self.assertEqual(stats.updated, 1)
        self.assertEqual(stats.skipped, 0)

    def test_removed_note_disappears_from_index(self) -> None:
        semantic = self.make()
        vault = FakeVault({"Notes/Кот.md": "Кот спит.", "Notes/Чай.md": "Чай на столе."})
        asyncio.run(semantic.refresh(vault))
        del vault.notes["Notes/Чай.md"]
        stats = asyncio.run(semantic.refresh(vault))
        self.assertEqual(stats.removed, 1)
        self.assertEqual(stats.notes, 1)
        hits = asyncio.run(semantic.search("чай", limit=5))
        self.assertNotIn("Notes/Чай.md", [hit["path"] for hit in hits])

    def test_index_survives_restart(self) -> None:
        directory = Path(self.tmp.name)
        store = store_dir(directory, "vault:test")
        first = SemanticSearch(FakeEmbedder(), store)
        asyncio.run(first.refresh(FakeVault({"Notes/Кот.md": "Кот спит."})))

        second = SemanticSearch(FakeEmbedder(), store)
        hits = asyncio.run(second.search("кот", limit=1))
        self.assertEqual([hit["path"] for hit in hits], ["Notes/Кот.md"])

    def test_empty_index_returns_nothing(self) -> None:
        semantic = self.make()
        self.assertEqual(asyncio.run(semantic.search("кот")), [])

    def test_search_by_symbol_query_works(self) -> None:
        semantic = self.make()
        vault = FakeVault(
            {
                "Projects/Релиз.md": "Сроки релиза сдвинулись на неделю.",
                "Notes/Кот.md": "Кот спит.",
            }
        )
        asyncio.run(semantic.refresh(vault))
        hits = asyncio.run(semantic.search("когда выйдет релиз", limit=3))
        self.assertEqual(hits[0]["path"], "Projects/Релиз.md")

    def test_long_note_takes_several_chunks(self) -> None:
        semantic = self.make()
        vault = FakeVault({"Notes/Длинная.md": "\n\n".join(["абзац " * 300] * 3)})
        stats = asyncio.run(semantic.refresh(vault))
        self.assertGreater(stats.chunks, stats.notes)

    def test_store_dir_is_per_vault(self) -> None:
        root = Path(self.tmp.name)
        self.assertNotEqual(store_dir(root, "vault:A"), store_dir(root, "vault:B"))


class RagSettingsTests(unittest.TestCase):
    def load(self, **env: str) -> Settings:
        with patch.dict("os.environ", env, clear=True), patch(
            "app.config.ENV_FILE", Path("no-such.env")
        ):
            return load_settings()

    def test_defaults(self) -> None:
        settings = self.load()
        self.assertTrue(settings.rag_enabled)
        self.assertEqual(settings.rag_top_k, 5)
        self.assertIn("paraphrase-multilingual", settings.rag_model)

    def test_can_disable_and_override(self) -> None:
        settings = self.load(RAG_ENABLED="false", RAG_MODEL="my-model", RAG_TOP_K="3")
        self.assertFalse(settings.rag_enabled)
        self.assertEqual(settings.rag_model, "my-model")
        self.assertEqual(settings.rag_top_k, 3)

    def test_top_k_is_validated(self) -> None:
        with self.assertRaises(ValueError):
            self.load(RAG_TOP_K="21")
        with self.assertRaises(ValueError):
            self.load(RAG_TOP_K="много")


if __name__ == "__main__":
    unittest.main()
