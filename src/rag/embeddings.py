"""Embedder implementations.

``hashing`` is the default: deterministic, offline, no dependencies. It is good
enough for near-duplicate detection and coarse retrieval over a few thousand
chunks. Swap in ``sentence_transformers`` (local, ~90 MB model) or ``openai``
when quality matters more than setup cost.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Any

from src.core.errors import VectorStoreError
from src.core.logging_setup import get_logger
from src.rag.base import Embedder, l2_normalize

logger = get_logger(__name__)

_TOKEN = re.compile(r"[a-z0-9]+|[\uac00-\ud7a3]+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    """Lowercase word tokens plus Korean syllable bigrams."""
    lowered = (text or "").lower()
    raw = _TOKEN.findall(lowered)
    out: list[str] = []
    for token in raw:
        if "\uac00" <= token[0] <= "\ud7a3" and len(token) > 2:
            out.extend(token[i : i + 2] for i in range(len(token) - 1))
        else:
            out.append(token)
    return out


class HashingEmbedder(Embedder):
    """Hashing-trick bag-of-words embedder (TF with sublinear scaling).

    Deterministic across processes and Python versions: the bucket comes from a
    BLAKE2b digest rather than ``hash()``.
    """

    name = "hashing"

    def __init__(self, dimension: int = 512) -> None:
        self.dimension = dimension

    def _bucket(self, token: str) -> tuple[int, float]:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        index = value % self.dimension
        sign = 1.0 if (value >> 63) & 1 else -1.0
        return index, sign

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimension
            counts: dict[str, int] = {}
            tokens = _tokens(text)
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            # Bigrams give the vector a little word-order awareness.
            for first, second in zip(tokens, tokens[1:], strict=False):
                bigram = f"{first}_{second}"
                counts[bigram] = counts.get(bigram, 0) + 1
            for token, count in counts.items():
                index, sign = self._bucket(token)
                vector[index] += sign * (1.0 + math.log(count))
            vectors.append(l2_normalize(vector))
        return vectors


class SentenceTransformerEmbedder(Embedder):
    """Local transformer embeddings (optional extra ``embeddings``)."""

    name = "sentence_transformers"

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise VectorStoreError(
                "sentence-transformers is not installed "
                "(pip install -r requirements-optional.txt)"
            ) from exc
        self.model_name = model_name
        self._model = SentenceTransformer(model_name)
        self.dimension = int(self._model.get_sentence_embedding_dimension())

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [list(map(float, vector)) for vector in vectors]


class OpenAIEmbedder(Embedder):
    """Hosted embeddings (optional extra ``llm``; requires ``OPENAI_API_KEY``)."""

    name = "openai"
    _DIMENSIONS = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}

    def __init__(self, model: str = "text-embedding-3-small") -> None:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise VectorStoreError("OPENAI_API_KEY is not set; cannot use the openai embedder")
        try:
            from openai import OpenAI  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise VectorStoreError("openai is not installed") from exc
        self._client = OpenAI(api_key=api_key)
        self.model = model
        self.dimension = self._DIMENSIONS.get(model, 1536)

    def embed(self, texts: list[str]) -> list[list[float]]:
        payload = [text or " " for text in texts]
        try:
            response = self._client.embeddings.create(model=self.model, input=payload)
        except Exception as exc:  # noqa: BLE001 - SDK raises many types
            raise VectorStoreError(f"OpenAI embedding request failed: {exc}") from exc
        return [list(map(float, item.embedding)) for item in response.data]


def build_embedder(config: Any | None = None) -> Embedder:
    """Create the configured embedder, falling back to hashing on any problem.

    Provider comes from ``EMBEDDING_PROVIDER`` (see ``.env.example``).
    """
    provider = (os.environ.get("EMBEDDING_PROVIDER") or "hashing").strip().lower()

    if provider in {"", "hashing", "local"}:
        return HashingEmbedder()

    try:
        if provider in {"sentence_transformers", "sentence-transformers", "st"}:
            model = os.environ.get("EMBEDDING_MODEL") or "sentence-transformers/all-MiniLM-L6-v2"
            embedder = SentenceTransformerEmbedder(model)
        elif provider == "openai":
            model = os.environ.get("OPENAI_EMBEDDING_MODEL") or "text-embedding-3-small"
            embedder = OpenAIEmbedder(model)
        else:
            logger.warning("Unknown EMBEDDING_PROVIDER=%r; using hashing", provider)
            return HashingEmbedder()
    except VectorStoreError as exc:
        logger.warning("Embedder `%s` unavailable (%s); falling back to hashing", provider, exc)
        return HashingEmbedder()

    logger.info("Embedder: %s (dim=%d)", embedder.name, embedder.dimension)
    return embedder
