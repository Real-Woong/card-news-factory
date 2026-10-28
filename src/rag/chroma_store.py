"""ChromaDB adapter (optional extra ``rag``).

Chroma is used purely as a vector index: we always pass our own embeddings, so
switching embedders does not change which store we use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.errors import VectorStoreError
from src.core.logging_setup import get_logger
from src.core.models import RetrievedChunk, VectorRecord
from src.rag.base import VectorStore

logger = get_logger(__name__)


class ChromaVectorStore(VectorStore):
    """Persistent Chroma client with one Chroma collection per logical collection."""

    name = "chroma"

    def __init__(self, directory: Path) -> None:
        try:
            import chromadb  # type: ignore[import-not-found]
            from chromadb.config import Settings  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise VectorStoreError(
                "chromadb is not installed (pip install -r requirements-optional.txt)"
            ) from exc

        directory.mkdir(parents=True, exist_ok=True)
        try:
            self._client = chromadb.PersistentClient(
                path=str(directory),
                settings=Settings(anonymized_telemetry=False, allow_reset=False),
            )
        except Exception as exc:  # noqa: BLE001 - chroma raises broad errors
            raise VectorStoreError(f"Cannot open Chroma at {directory}: {exc}") from exc
        self.directory = directory
        self._collections: dict[str, Any] = {}

    def _collection(self, name: str) -> Any:
        if name not in self._collections:
            try:
                self._collections[name] = self._client.get_or_create_collection(
                    name=name, metadata={"hnsw:space": "cosine"}
                )
            except Exception as exc:  # noqa: BLE001
                raise VectorStoreError(f"Cannot open Chroma collection {name}: {exc}") from exc
        return self._collections[name]

    @staticmethod
    def _flatten(metadata: dict[str, Any]) -> dict[str, Any]:
        """Chroma only accepts scalar metadata values."""
        flat: dict[str, Any] = {}
        for key, value in metadata.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                flat[key] = value
            elif isinstance(value, (list, tuple)):
                flat[key] = ", ".join(str(v) for v in value)
            else:
                flat[key] = str(value)
        return flat

    def upsert(self, collection: str, records: list[VectorRecord]) -> None:
        usable = [r for r in records if r.embedding]
        if not usable:
            return
        try:
            self._collection(collection).upsert(
                ids=[r.id for r in usable],
                embeddings=[r.embedding for r in usable],
                documents=[r.text for r in usable],
                metadatas=[self._flatten(r.metadata) for r in usable],
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"Chroma upsert failed for {collection}: {exc}") from exc

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
        try:
            result = self._collection(collection).query(
                query_embeddings=[embedding],
                n_results=top_k,
                where=where or None,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"Chroma query failed for {collection}: {exc}") from exc

        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]

        chunks: list[RetrievedChunk] = []
        for index, identifier in enumerate(ids):
            distance = distances[index] if index < len(distances) else 1.0
            metadata = dict(metadatas[index] or {}) if index < len(metadatas) else {}
            metadata["collection"] = collection
            chunks.append(
                RetrievedChunk(
                    id=identifier,
                    text=documents[index] if index < len(documents) else "",
                    # cosine distance -> similarity
                    score=max(0.0, 1.0 - float(distance)),
                    metadata=metadata,
                )
            )
        return chunks

    def delete(self, collection: str, ids: list[str]) -> None:
        if not ids:
            return
        try:
            self._collection(collection).delete(ids=ids)
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"Chroma delete failed for {collection}: {exc}") from exc

    def count(self, collection: str) -> int:
        try:
            return int(self._collection(collection).count())
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"Chroma count failed for {collection}: {exc}") from exc

    def collections(self) -> list[str]:
        try:
            return sorted(c.name for c in self._client.list_collections())
        except Exception:  # noqa: BLE001 - informational only
            return sorted(self._collections)
