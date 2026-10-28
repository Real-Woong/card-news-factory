"""Indexing: articles, past posts and curated knowledge -> vector store.

Chunk ids are deterministic (``<doc_type>:<doc_key>:<n>``) so re-indexing the
same document replaces its chunks instead of duplicating them. The
``vector_index`` table records the content hash of what was indexed, so
unchanged documents are skipped.
"""

from __future__ import annotations

from pathlib import Path

from src.core.config import AppConfig
from src.core.errors import VectorStoreError
from src.core.logging_setup import get_logger
from src.core.models import Article, PublishedPost, VectorRecord
from src.core.text import chunk_text, content_hash, to_iso
from src.rag.base import Embedder, VectorStore
from src.storage.repository import ArticleRepository

logger = get_logger(__name__)


class RagIndexer:
    """Writes embeddings for articles, posts and knowledge files."""

    def __init__(
        self,
        *,
        config: AppConfig,
        embedder: Embedder,
        store: VectorStore,
        repo: ArticleRepository | None = None,
        dry_run: bool = False,
    ) -> None:
        self.config = config
        self.embedder = embedder
        self.store = store
        self.repo = repo
        self.dry_run = dry_run
        self.chunk_size = int(config.get("rag.chunk_size", 1200))
        self.overlap = int(config.get("rag.chunk_overlap", 150))
        self.articles_collection = str(config.get("rag.collection_articles", "articles"))
        self.posts_collection = str(config.get("rag.collection_posts", "posts"))
        self.knowledge_collection = str(config.get("rag.collection_knowledge", "knowledge"))

    # ---------------------------------------------------------------- internals
    def _already_indexed(self, collection: str, doc_key: str, digest: str) -> bool:
        if self.repo is None:
            return False
        return self.repo.indexed_content_hash(collection=collection, doc_key=doc_key) == digest

    def _index_document(
        self,
        *,
        collection: str,
        doc_key: str,
        doc_type: str,
        text: str,
        metadata: dict[str, object],
        digest: str,
    ) -> int:
        """Embed and upsert one document. Returns the number of chunks written."""
        if self._already_indexed(collection, doc_key, digest):
            logger.debug("[rag] %s/%s unchanged; skipping", collection, doc_key[:12])
            return 0

        chunks = chunk_text(text, chunk_size=self.chunk_size, overlap=self.overlap)
        if not chunks:
            return 0

        try:
            embeddings = self.embedder.embed(chunks)
        except VectorStoreError as exc:
            logger.warning("[rag] embedding failed for %s (%s)", doc_key[:12], exc)
            return 0

        records = [
            VectorRecord(
                id=f"{doc_type}:{doc_key}:{index}",
                text=chunk,
                embedding=embedding,
                metadata={
                    **metadata,
                    "doc_key": doc_key,
                    "doc_type": doc_type,
                    "chunk_index": index,
                    "chunk_total": len(chunks),
                },
            )
            for index, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=True))
        ]

        self.store.upsert(collection, records)
        if self.repo is not None and not self.dry_run:
            self.repo.record_vector_chunks(
                collection=collection,
                doc_key=doc_key,
                doc_type=doc_type,
                chunk_ids=[record.id for record in records],
                content_hash=digest,
            )
        return len(records)

    # ------------------------------------------------------------------ public
    def index_articles(self, articles: list[Article]) -> int:
        total = 0
        indexed_keys: list[str] = []
        for article in articles:
            written = self._index_document(
                collection=self.articles_collection,
                doc_key=article.key,
                doc_type="article",
                text=article.embedding_text(),
                metadata={
                    "title": article.title,
                    "url": article.url,
                    "source": article.source_name,
                    "source_type": article.source_type,
                    "category": article.category,
                    "trust_tier": article.trust_tier,
                    "published_at": to_iso(article.published_at),
                },
                digest=article.content_hash_value,
            )
            total += written
            if written:
                indexed_keys.append(article.key)
        if self.repo is not None and not self.dry_run:
            self.repo.mark_indexed(indexed_keys or [a.key for a in articles])
        logger.info("[rag] indexed %d chunk(s) from %d article(s)", total, len(articles))
        return total

    def index_posts(self, posts: list[PublishedPost]) -> int:
        total = 0
        for post in posts:
            doc_key = str(post.id or f"{post.run_id}:{post.channel_id}")
            total += self._index_document(
                collection=self.posts_collection,
                doc_key=doc_key,
                doc_type="post",
                text=f"{post.title}\n\n{post.body}",
                metadata={
                    "title": post.title or f"{post.channel_id} draft",
                    "url": "",
                    "channel_id": post.channel_id,
                    "format": post.format,
                    "run_id": post.run_id,
                    "status": post.status,
                    "created_at": to_iso(post.created_at),
                    "article_keys": ", ".join(post.article_keys),
                },
                digest=content_hash(post.title, post.body[:2000]),
            )
        if total:
            logger.info("[rag] indexed %d chunk(s) from %d post(s)", total, len(posts))
        return total

    def index_knowledge(self, directory: Path | None = None) -> int:
        """Index ``knowledge/**/*.md`` (evergreen context the writer can reuse)."""
        root = directory or self.config.paths.knowledge
        if not root.exists():
            return 0

        total = 0
        files = sorted(p for p in root.rglob("*.md") if p.is_file())
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("[rag] cannot read %s: %s", path, exc)
                continue
            if not text.strip():
                continue
            relative = path.relative_to(self.config.paths.root).as_posix()
            total += self._index_document(
                collection=self.knowledge_collection,
                doc_key=relative,
                doc_type="knowledge",
                text=text,
                metadata={
                    "title": path.stem.replace("-", " ").replace("_", " "),
                    "url": "",
                    "path": relative,
                    "topic": path.parent.name,
                },
                digest=content_hash(relative, text[:4000]),
            )
        if total:
            logger.info("[rag] indexed %d chunk(s) from %d knowledge file(s)", total, len(files))
        return total
