"""Dependency-free vector store.

One JSONL file per collection under ``data/vectorstore/``. Loaded into memory on
first access and searched with brute-force cosine similarity, which is fine for
the tens of thousands of chunks a personal pipeline produces (roughly 10 ms per
query at 10k chunks x 512 dims).

Swap in :class:`src.rag.chroma_store.ChromaVectorStore` by setting
``VECTOR_STORE=chroma``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.core.errors import VectorStoreError
from src.core.logging_setup import get_logger
from src.core.models import RetrievedChunk, VectorRecord
from src.rag.base import VectorStore, cosine_similarity

logger = get_logger(__name__)


class LocalJsonVectorStore(VectorStore):
    """Brute-force cosine store persisted as JSONL."""

    name = "local"

    def __init__(self, directory: Path, *, ephemeral: bool = False) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, dict[str, VectorRecord]] = {}
        # ephemeral=True keeps reads working but never writes (used by --dry-run)
        self.ephemeral = ephemeral

    # ---------------------------------------------------------------- internals
    def _path(self, collection: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in collection)
        return self.directory / f"{safe}.jsonl"

    def _load(self, collection: str) -> dict[str, VectorRecord]:
        if collection in self._cache:
            return self._cache[collection]

        records: dict[str, VectorRecord] = {}
        path = self._path(collection)
        if path.exists():
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                    records[payload["id"]] = VectorRecord(
                        id=payload["id"],
                        text=payload.get("text", ""),
                        embedding=[float(v) for v in payload.get("embedding", [])],
                        metadata=payload.get("metadata", {}),
                    )
                except (ValueError, KeyError) as exc:
                    logger.warning("Skipping corrupt vector record %s:%d (%s)", path, line_no, exc)
        self._cache[collection] = records
        return records

    def _flush(self, collection: str) -> None:
        if self.ephemeral:
            return
        records = self._cache.get(collection)
        if records is None:
            return
        path = self._path(collection)
        tmp = path.with_suffix(".jsonl.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                for record in records.values():
                    handle.write(
                        json.dumps(
                            {
                                "id": record.id,
                                "text": record.text,
                                "embedding": [round(v, 6) for v in record.embedding],
                                "metadata": record.metadata,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            tmp.replace(path)
        except OSError as exc:
            raise VectorStoreError(f"Cannot persist vector collection {collection}: {exc}") from exc

    # ------------------------------------------------------------------- public
    def upsert(self, collection: str, records: list[VectorRecord]) -> None:
        if not records:
            return
        store = self._load(collection)
        for record in records:
            if not record.embedding:
                logger.debug("Skipping record %s with empty embedding", record.id)
                continue
            store[record.id] = record
        self._flush(collection)

    def query(
        self,
        collection: str,
        embedding: list[float],
        *,
        top_k: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        if not embedding:
            return []
        store = self._load(collection)
        hits: list[RetrievedChunk] = []
        for record in store.values():
            if where and any(record.metadata.get(k) != v for k, v in where.items()):
                continue
            score = cosine_similarity(embedding, record.embedding)
            hits.append(
                RetrievedChunk(
                    id=record.id,
                    text=record.text,
                    score=score,
                    metadata={**record.metadata, "collection": collection},
                )
            )
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:top_k]

    def delete(self, collection: str, ids: list[str]) -> None:
        if not ids:
            return
        store = self._load(collection)
        removed = False
        for identifier in ids:
            removed = store.pop(identifier, None) is not None or removed
        if removed:
            self._flush(collection)

    def count(self, collection: str) -> int:
        return len(self._load(collection))

    def collections(self) -> list[str]:
        return sorted(path.stem for path in self.directory.glob("*.jsonl"))

    def persist(self) -> None:
        for collection in list(self._cache):
            self._flush(collection)
