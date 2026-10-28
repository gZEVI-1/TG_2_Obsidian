"""Семантический поиск по заметкам: локальные эмбеддинги и кэш индекса.

Индекс — векторы фрагментов заметок (numpy) и их тексты; лежит в
`state/rag/<хранилище>/`. Модель эмбеддингов считается локально
(`sentence-transformers`), первый запуск скачивает её один раз.

RAG необязателен: без `numpy` и `sentence-transformers` модуль можно
импортировать, а бот работает с обычным полнотекстовым поиском.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.obsidian import ObsidianBackend, ObsidianError

try:  # pragma: no cover - зависит от окружения
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

CHUNK_CHARS = 900
EMBED_BATCH = 16
MAX_INDEX_NOTES = 500
EXCERPT_CHARS = 300
QUERY_PREFIX = ""  # место под префикс модели, если понадобится


def available() -> bool:
    """Можно ли строить индекс: нужен numpy."""
    return np is not None


def store_dir(root: Path, vault_key: str) -> Path:
    """Папка индекса для хранилища: у каждого своя, чтобы не смешивать."""
    key = hashlib.sha1(vault_key.encode("utf-8")).hexdigest()[:12]
    return root / "state" / "rag" / key


@dataclass(frozen=True)
class EmbeddingStats:
    """Итог обновления индекса — для лога и сообщения пользователю."""

    notes: int = 0
    chunks: int = 0
    updated: int = 0
    skipped: int = 0
    removed: int = 0
    failed: int = 0


def chunk_text(text: str, *, limit: int = CHUNK_CHARS) -> list[str]:
    """Делит заметку на куски не длиннее `limit` по границам абзацев."""
    blocks = [
        block.strip()
        for block in text.replace("\r\n", "\n").split("\n\n")
        if block.strip()
    ]
    chunks: list[str] = []
    current = ""
    for block in blocks:
        while len(block) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(block[:limit])
            block = block[limit:]
        if not block:
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = block
    if current:
        chunks.append(current)
    return chunks


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    """Пишет файл через временный, чтобы не оставить половину индекса."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "wb", dir=path.parent, prefix=".rag-", suffix=".tmp", delete=False
    )
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


class LocalEmbedder:
    """Модель sentence-transformers, загружается лениво и один раз."""

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model: Any = None
        self._lock = asyncio.Lock()

    @property
    def model_name(self) -> str:
        return self._model_name

    async def embed(self, texts: list[str]) -> Any:
        """Векторы с единичной нормой: косинус считается обычным скаляром."""
        if np is None:
            raise RuntimeError("RAG: numpy не установлен")
        model = await self._ensure_model()
        vectors = await asyncio.to_thread(
            model.encode,
            texts,
            batch_size=EMBED_BATCH,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)

    async def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        async with self._lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load)
        return self._model

    def _load(self) -> Any:
        from sentence_transformers import SentenceTransformer

        log.info("Загружаю модель эмбеддингов: %s", self._model_name)
        return SentenceTransformer(self._model_name)


class RagIndex:
    """Векторы фрагментов и их тексты; сохранение и загрузка без сети."""

    def __init__(self, store_dir: Path) -> None:
        self._dir = store_dir
        self._vectors: Any = None
        self._chunks: list[dict[str, str]] = []
        self._hashes: dict[str, str] = {}
        self._updated_at = ""

    @property
    def notes(self) -> int:
        return len(self._hashes)

    @property
    def chunks(self) -> int:
        return len(self._chunks)

    @property
    def updated_at(self) -> str:
        return self._updated_at

    @property
    def ready(self) -> bool:
        return self._vectors is not None and bool(self._chunks)

    def load(self) -> None:
        """Читает индекс с диска; битый индекс — как пустой, без падения."""
        if np is None:
            return
        meta_path = self._dir / "index.json"
        vectors_path = self._dir / "vectors.npy"
        if not meta_path.is_file() or not vectors_path.is_file():
            return
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            vectors = np.load(vectors_path)
            chunks = list(meta.get("chunks", []))
            hashes = dict(meta.get("hashes", {}))
            if len(chunks) != len(vectors):
                raise ValueError("число чанков не совпадает с числом векторов")
        except (OSError, ValueError, TypeError) as error:
            log.warning("RAG: индекс не прочитан, пересоберу: %s", error)
            return

        self._vectors = vectors
        self._chunks = chunks
        self._hashes = hashes
        self._updated_at = str(meta.get("updated_at", ""))

    def save(self) -> None:
        meta = {
            "chunks": self._chunks,
            "hashes": self._hashes,
            "updated_at": self._updated_at,
        }
        _write_atomic(
            self._dir / "index.json",
            json.dumps(meta, ensure_ascii=False).encode("utf-8"),
        )
        if self._vectors is not None:
            payload = tempfile.NamedTemporaryFile(
                "wb", dir=self._dir, prefix=".rag-", suffix=".npy", delete=False
            )
            try:
                with payload:
                    np.save(payload, self._vectors)
                os.replace(payload.name, self._dir / "vectors.npy")
            except BaseException:
                try:
                    os.unlink(payload.name)
                except OSError:
                    pass
                raise

    async def update(
        self,
        obsidian: ObsidianBackend,
        embedder: LocalEmbedder,
        *,
        exclude: Any = None,
    ) -> EmbeddingStats:
        """Дообновляет индекс: меняет только новые и изменённые заметки."""
        if np is None:
            raise RuntimeError("RAG: numpy не установлен")

        notes = (await obsidian.list_notes())[:MAX_INDEX_NOTES]
        if exclude is not None:
            notes = [note for note in notes if not exclude(note)]
        old_chunks = self._chunks
        old_vectors = self._vectors
        old_hashes = self._hashes

        positions: dict[str, list[int]] = {}
        for index, chunk in enumerate(old_chunks):
            positions.setdefault(chunk["path"], []).append(index)

        chunks: list[dict[str, str]] = []
        vectors: list[Any] = []
        hashes: dict[str, str] = {}
        updated = 0
        skipped = 0
        failed = 0

        for path in notes:
            try:
                text = await obsidian.read_note(path)
            except ObsidianError as error:
                failed += 1
                log.warning("RAG: заметка не прочитана (%s): %s", path, error)
                for index in positions.get(path, []):
                    chunks.append(old_chunks[index])
                    vectors.append(old_vectors[index])
                if path in old_hashes:
                    hashes[path] = old_hashes[path]
                continue

            digest = _digest(text)
            hashes[path] = digest
            if old_hashes.get(path) == digest and positions.get(path):
                skipped += 1
                for index in positions[path]:
                    chunks.append(old_chunks[index])
                    vectors.append(old_vectors[index])
                continue

            pieces = chunk_text(text)
            if not pieces:
                continue
            title = path.rsplit("/", 1)[-1].removesuffix(".md")
            embedded = await embedder.embed([f"{title}\n{piece}" for piece in pieces])
            for piece, vector in zip(pieces, embedded):
                chunks.append({"path": path, "text": piece})
                vectors.append(vector)
            updated += 1

        removed = len(set(old_hashes) - set(hashes))
        self._chunks = chunks
        self._hashes = hashes
        self._vectors = (
            np.vstack(vectors) if vectors else np.zeros((0, 0), dtype=np.float32)
        )
        self._updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.save()
        return EmbeddingStats(
            notes=len(hashes),
            chunks=len(chunks),
            updated=updated,
            skipped=skipped,
            removed=removed,
            failed=failed,
        )

    def search(self, vector: Any, limit: int) -> list[tuple[str, float, str]]:
        """Лучшие заметки по косинусу: (путь, оценка, фрагмент)."""
        if not self.ready:
            return []
        scores = self._vectors @ vector
        best: dict[str, tuple[float, str]] = {}
        for chunk, score in zip(self._chunks, scores):
            path = chunk["path"]
            value = float(score)
            current = best.get(path)
            if current is None or value > current[0]:
                best[path] = (value, chunk["text"])
        ranked = sorted(best.items(), key=lambda item: item[1][0], reverse=True)
        return [
            (path, score, text[:EXCERPT_CHARS])
            for path, (score, text) in ranked[:limit]
        ]


class SemanticSearch:
    """Поиск по смыслу: индекс заметок и локальные эмбеддинги."""

    def __init__(
        self,
        embedder: LocalEmbedder,
        store_dir: Path,
        *,
        exclude_folder: str = "",
        exclude_note: str = "",
    ) -> None:
        self._embedder = embedder
        self._index = RagIndex(store_dir)
        self._loaded = False
        self._exclude_folder = exclude_folder.strip("/").casefold()
        self._exclude_note = exclude_note.strip("/").casefold()

    def _skip(self, path: str) -> bool:
        """Шаблоны и заметка стиля не индексируются: это не личный контент."""
        value = path.strip("/").casefold()
        if self._exclude_note and value == self._exclude_note:
            return True
        return bool(self._exclude_folder) and value.startswith(
            f"{self._exclude_folder}/"
        )

    @property
    def model_name(self) -> str:
        return self._embedder.model_name

    @property
    def notes(self) -> int:
        return self._index.notes

    @property
    def chunks(self) -> int:
        return self._index.chunks

    @property
    def updated_at(self) -> str:
        return self._index.updated_at

    @property
    def ready(self) -> bool:
        return self._index.ready

    def load(self) -> None:
        if not self._loaded:
            self._index.load()
            self._loaded = True

    async def refresh(self, obsidian: ObsidianBackend) -> EmbeddingStats:
        """Обновляет индекс; первый запуск строит его с нуля."""
        self.load()
        stats = await self._index.update(
            obsidian, self._embedder, exclude=self._skip
        )
        log.info(
            "RAG-индекс: заметок %d, чанков %d, обновлено %d, без изменений %d, "
            "удалено %d, ошибок %d",
            stats.notes,
            stats.chunks,
            stats.updated,
            stats.skipped,
            stats.removed,
            stats.failed,
        )
        return stats

    async def search(self, query: str, limit: int = 5) -> list[dict[str, str]]:
        """Заметки, близкие по смыслу; пустой индекс — пустой результат."""
        if not query.strip() or np is None:
            return []
        self.load()
        if not self._index.ready:
            return []
        vector = await self._embedder.embed([QUERY_PREFIX + query.strip()])
        return [
            {
                "path": path,
                "match": "смысл",
                "excerpt": excerpt,
                "score": f"{score:.2f}",
            }
            for path, score, excerpt in self._index.search(vector[0], limit)
        ]
