"""RAG interfaces.

Two swappable abstractions:

* :class:`Embedder`   - text -> vector
* :class:`VectorStore` - persist vectors and run similarity search

Concrete implementations live next to this module. Everything else in the
codebase depends on these interfaces only, so replacing the local JSON store
with Chroma (or later pgvector) is a one-line factory change.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Any

from src.core.models import RetrievedChunk, VectorRecord


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Cosine similarity for two equal-length vectors (0 when either is empty)."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    norm_l = 0.0
    norm_r = 0.0
    for a, b in zip(left, right, strict=True):
        dot += a * b
        norm_l += a * a
        norm_r += b * b
    if norm_l <= 0.0 or norm_r <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_l * norm_r)


def l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0:
        return vector
    return [value / norm for value in vector]


class Embedder(ABC):
    """Turns text into vectors."""

    name: str = "embedder"
    dimension: int = 0

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts (order preserved)."""

    def embed_one(self, text: str) -> list[float]:
        vectors = self.embed([text])
        return vectors[0] if vectors else []


class VectorStore(ABC):
    """Persistent vector index, partitioned into named collections."""

    name: str = "vectorstore"

    @abstractmethod
    def upsert(self, collection: str, records: list[VectorRecord]) -> None:
        """Insert or replace records by id."""

    @abstractmethod
    def query(
        self,
        collection: str,
        embedding: list[float],
        *,
        top_k: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        """Return the ``top_k`` most similar chunks, highest score first."""

    @abstractmethod
    def delete(self, collection: str, ids: list[str]) -> None:
        """Remove records by id."""

    @abstractmethod
    def count(self, collection: str) -> int:
        """Number of records in a collection."""

    def persist(self) -> None:
        """Flush to disk. No-op for stores that write eagerly."""

    def collections(self) -> list[str]:
        return []
