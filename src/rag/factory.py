"""Factory that assembles the RAG stack from env + config."""

from __future__ import annotations

import os

from src.core.config import AppConfig
from src.core.errors import VectorStoreError
from src.core.logging_setup import get_logger
from src.rag.base import Embedder, VectorStore
from src.rag.embeddings import build_embedder
from src.rag.local_store import LocalJsonVectorStore

logger = get_logger(__name__)


def build_vector_store(config: AppConfig, *, ephemeral: bool = False) -> VectorStore:
    """Create the configured vector store.

    ``VECTOR_STORE=chroma`` uses ChromaDB; anything else (or an import failure)
    falls back to the built-in JSON store so a run never fails because of an
    optional dependency.
    """
    directory = config.vectorstore_path()
    backend = (os.environ.get("VECTOR_STORE") or "local").strip().lower()

    if backend in {"chroma", "chromadb"}:
        try:
            from src.rag.chroma_store import ChromaVectorStore

            store = ChromaVectorStore(directory / "chroma")
            logger.info("Vector store: chroma (%s)", store.directory)
            return store
        except VectorStoreError as exc:
            logger.warning("Chroma unavailable (%s); falling back to the local JSON store", exc)
    elif backend not in {"local", "json", ""}:
        logger.warning("Unknown VECTOR_STORE=%r; using the local JSON store", backend)

    store = LocalJsonVectorStore(directory / "local", ephemeral=ephemeral)
    logger.info("Vector store: local%s (%s)", " [ephemeral]" if ephemeral else "", store.directory)
    return store


def build_rag(config: AppConfig, *, ephemeral: bool = False) -> tuple[Embedder, VectorStore]:
    """Convenience: embedder + vector store."""
    return build_embedder(config), build_vector_store(config, ephemeral=ephemeral)
