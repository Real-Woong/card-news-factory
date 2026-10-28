"""Deduplication strategies.

Three independent strategies, applied cheapest-first by
:class:`src.dedup.service.DedupService`:

1. :class:`UrlDedup`       - exact canonical URL / content hash (free)
2. :class:`TitleDedup`     - normalised title similarity (cheap, no embeddings)
3. :class:`EmbeddingDedup` - cosine similarity against past articles and posts

Each strategy implements :class:`DedupStrategy`, so a stage can be disabled in
``pipeline.yaml`` or replaced without touching the others.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.core.logging_setup import get_logger
from src.core.models import Article, DedupReason
from src.core.text import title_similarity
from src.rag.retriever import RagRetriever
from src.storage.repository import ArticleRepository

logger = get_logger(__name__)


@dataclass
class Match:
    """A duplicate/near-duplicate hit."""

    reason: DedupReason
    score: float
    matched_key: str | None = None
    matched_title: str | None = None
    is_duplicate: bool = True

    @property
    def note(self) -> str:
        target = self.matched_title or self.matched_key or "?"
        return f"{self.reason} {self.score:.2f} vs {target}"


class DedupStrategy(ABC):
    """Checks one article against history."""

    name: str = "strategy"

    @abstractmethod
    def check(self, article: Article, *, seen_in_run: dict[str, Article]) -> Match | None:
        """Return a :class:`Match` when the article is a duplicate, else ``None``."""


class UrlDedup(DedupStrategy):
    """Exact match on canonical URL or content hash.

    ``known_keys`` are the article keys that existed *before* this run started.
    The store stage runs before dedup (per the configured stage order), so a
    plain "is it in the database?" check would flag every item as a duplicate of
    itself; comparing against ``known_keys`` keeps the check meaningful.
    """

    name = "url"

    def __init__(
        self, repo: ArticleRepository | None, *, known_keys: set[str] | None = None
    ) -> None:
        self.repo = repo
        # None  -> no snapshot available, fall back to a direct DB lookup
        # set() -> valid snapshot that happens to be empty (first run)
        self.known_keys = known_keys
        self.has_snapshot = known_keys is not None

    def check(self, article: Article, *, seen_in_run: dict[str, Article]) -> Match | None:
        if article.key in seen_in_run:
            return Match(
                reason=DedupReason.URL,
                score=1.0,
                matched_key=article.key,
                matched_title=seen_in_run[article.key].title,
            )
        for other in seen_in_run.values():
            if other.canonical_url and other.canonical_url == article.canonical_url:
                return Match(
                    reason=DedupReason.URL,
                    score=1.0,
                    matched_key=other.key,
                    matched_title=other.title,
                )
            if other.content_hash_value == article.content_hash_value:
                return Match(
                    reason=DedupReason.CONTENT_HASH,
                    score=1.0,
                    matched_key=other.key,
                    matched_title=other.title,
                )

        if self.has_snapshot and self.known_keys and article.key in self.known_keys:
            return Match(reason=DedupReason.URL, score=1.0, matched_key=article.key)
        if self.repo is None:
            return None
        if not self.has_snapshot and self.repo.exists_url_hash(article.key):
            # No snapshot (dedup used standalone, or nothing was stored yet):
            # a row with this key can only come from an earlier run.
            return Match(reason=DedupReason.URL, score=1.0, matched_key=article.key)

        matched = self.repo.find_by_canonical_url(article.canonical_url)
        if matched and matched != article.key:
            return Match(reason=DedupReason.URL, score=1.0, matched_key=matched)
        matched = self.repo.find_by_content_hash(article.content_hash_value)
        if matched and matched != article.key:
            return Match(reason=DedupReason.CONTENT_HASH, score=1.0, matched_key=matched)
        return None


class TitleDedup(DedupStrategy):
    """Normalised-title similarity against recent history and the current batch."""

    name = "title"

    def __init__(
        self,
        repo: ArticleRepository | None,
        *,
        threshold: float = 0.82,
        lookback_days: int = 45,
    ) -> None:
        self.repo = repo
        self.threshold = threshold
        self.lookback_days = lookback_days
        self._history: list[dict[str, str]] | None = None

    def _load_history(self) -> list[dict[str, str]]:
        if self._history is None:
            self._history = (
                self.repo.recent_title_index(days=self.lookback_days) if self.repo else []
            )
        return self._history

    def check(self, article: Article, *, seen_in_run: dict[str, Article]) -> Match | None:
        best: Match | None = None

        for other in seen_in_run.values():
            score = title_similarity(article.title, other.title)
            if score >= self.threshold and (best is None or score > best.score):
                best = Match(
                    reason=DedupReason.TITLE,
                    score=score,
                    matched_key=other.key,
                    matched_title=other.title,
                )

        for row in self._load_history():
            if row["url_hash"] == article.key:
                continue
            score = title_similarity(article.title, row.get("title", ""))
            if score >= self.threshold and (best is None or score > best.score):
                best = Match(
                    reason=DedupReason.TITLE,
                    score=score,
                    matched_key=row["url_hash"],
                    matched_title=row.get("title"),
                )
        return best


class EmbeddingDedup(DedupStrategy):
    """Cosine similarity against indexed articles and previously generated posts.

    Two thresholds:

    * ``article_threshold``  - same news reported twice
    * ``post_threshold``     - we already published about this (stricter)

    Scores between ``warn_margin`` and the threshold are returned with
    ``is_duplicate=False`` so they show up in ``review.md`` as a duplicate risk
    instead of silently dropping the item.
    """

    name = "embedding"

    def __init__(
        self,
        retriever: RagRetriever | None,
        *,
        article_threshold: float = 0.90,
        post_threshold: float = 0.84,
        warn_margin: float = 0.12,
    ) -> None:
        self.retriever = retriever
        self.article_threshold = article_threshold
        self.post_threshold = post_threshold
        self.warn_margin = warn_margin

    def check(self, article: Article, *, seen_in_run: dict[str, Article]) -> Match | None:
        if self.retriever is None:
            return None

        query = f"{article.title}\n{article.summary}"[:2000]
        candidates: list[Match] = []

        post_hit = self.retriever.best_match(
            query, collection=self.retriever.posts_collection
        )
        if post_hit is not None:
            candidates.append(
                Match(
                    reason=DedupReason.EMBEDDING_POST,
                    score=post_hit.score,
                    matched_key=str(post_hit.metadata.get("doc_key") or post_hit.id),
                    matched_title=post_hit.source_title,
                    is_duplicate=post_hit.score >= self.post_threshold,
                )
            )

        article_hit = self.retriever.best_match(
            query,
            collection=self.retriever.articles_collection,
            exclude_doc_keys={article.key},
        )
        if article_hit is not None:
            candidates.append(
                Match(
                    reason=DedupReason.EMBEDDING_ARTICLE,
                    score=article_hit.score,
                    matched_key=str(article_hit.metadata.get("doc_key") or article_hit.id),
                    matched_title=article_hit.source_title,
                    is_duplicate=article_hit.score >= self.article_threshold,
                )
            )

        duplicates = [c for c in candidates if c.is_duplicate]
        if duplicates:
            return max(duplicates, key=lambda c: c.score)

        # Near-miss: report as a warning, not a duplicate.
        near = [
            c
            for c in candidates
            if c.score
            >= (
                (self.post_threshold if c.reason == DedupReason.EMBEDDING_POST
                 else self.article_threshold)
                - self.warn_margin
            )
        ]
        return max(near, key=lambda c: c.score) if near else None
