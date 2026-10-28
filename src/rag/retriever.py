"""Retrieval: similarity search over articles, past posts and knowledge."""

from __future__ import annotations

from src.core.config import AppConfig
from src.core.errors import VectorStoreError
from src.core.logging_setup import get_logger
from src.core.models import Article, RetrievedChunk
from src.rag.base import Embedder, VectorStore

logger = get_logger(__name__)


class RagRetriever:
    """Thin query layer over the vector store.

    Also used by the dedup stage: :meth:`best_match` answers "have we seen
    something like this before?".
    """

    def __init__(
        self, *, config: AppConfig, embedder: Embedder, store: VectorStore
    ) -> None:
        self.config = config
        self.embedder = embedder
        self.store = store
        self.articles_collection = str(config.get("rag.collection_articles", "articles"))
        self.posts_collection = str(config.get("rag.collection_posts", "posts"))
        self.knowledge_collection = str(config.get("rag.collection_knowledge", "knowledge"))
        self.top_k = int(config.get("rag.retrieve_top_k", 5))
        self.min_similarity = float(config.get("rag.min_similarity", 0.25))

    # ------------------------------------------------------------------ search
    def search(
        self,
        query: str,
        *,
        collection: str | None = None,
        top_k: int | None = None,
        min_similarity: float | None = None,
        exclude_doc_keys: set[str] | None = None,
    ) -> list[RetrievedChunk]:
        """Return chunks similar to ``query``, best first."""
        if not query.strip():
            return []
        target = collection or self.articles_collection
        limit = top_k or self.top_k
        floor = self.min_similarity if min_similarity is None else min_similarity

        try:
            embedding = self.embedder.embed_one(query)
            # Over-fetch so exclusions/thresholds still leave enough results.
            hits = self.store.query(target, embedding, top_k=limit * 3)
        except VectorStoreError as exc:
            logger.warning("[rag] retrieval failed on %s: %s", target, exc)
            return []

        results: list[RetrievedChunk] = []
        seen_docs: set[str] = set()
        for hit in hits:
            doc_key = str(hit.metadata.get("doc_key") or hit.id)
            if exclude_doc_keys and doc_key in exclude_doc_keys:
                continue
            if hit.score < floor:
                continue
            if doc_key in seen_docs:  # one chunk per document keeps prompts diverse
                continue
            seen_docs.add(doc_key)
            results.append(hit)
            if len(results) >= limit:
                break
        return results

    def best_match(
        self, text: str, *, collection: str, exclude_doc_keys: set[str] | None = None
    ) -> RetrievedChunk | None:
        """Highest-similarity chunk in a collection, ignoring the threshold."""
        hits = self.search(
            text,
            collection=collection,
            top_k=1,
            min_similarity=0.0,
            exclude_doc_keys=exclude_doc_keys,
        )
        return hits[0] if hits else None

    # ----------------------------------------------------------------- context
    def context_for_article(
        self, article: Article, *, top_k: int | None = None
    ) -> list[RetrievedChunk]:
        """Prior context for one article: past articles, our posts, knowledge notes."""
        limit = top_k or self.top_k
        query = f"{article.title}\n{article.summary}"[:2000]
        exclude = {article.key}

        prior_articles = self.search(
            query, collection=self.articles_collection, top_k=limit, exclude_doc_keys=exclude
        )
        prior_posts = self.search(
            query, collection=self.posts_collection, top_k=max(limit // 2, 1)
        )
        knowledge = self.search(
            query, collection=self.knowledge_collection, top_k=max(limit // 2, 1)
        )

        combined = [*prior_articles, *prior_posts, *knowledge]
        combined.sort(key=lambda chunk: chunk.score, reverse=True)
        return combined[:limit]

    def similarity(self, left: str, right: str) -> float:
        """Cosine similarity between two raw texts (no store lookup).

        Used by the selector to detect two items covering the same story.
        """
        if not left.strip() or not right.strip():
            return 0.0
        try:
            vectors = self.embedder.embed([left[:2000], right[:2000]])
        except VectorStoreError as exc:
            logger.warning("[rag] similarity failed: %s", exc)
            return 0.0
        if len(vectors) != 2:
            return 0.0
        from src.rag.base import cosine_similarity

        return cosine_similarity(vectors[0], vectors[1])

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for name in (
            self.articles_collection,
            self.posts_collection,
            self.knowledge_collection,
        ):
            try:
                result[name] = self.store.count(name)
            except VectorStoreError:
                result[name] = 0
        return result
