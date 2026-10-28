"""Dedup orchestration: run the strategies in order and record the outcome."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.core.config import AppConfig
from src.core.logging_setup import get_logger
from src.core.models import Article, DedupResult
from src.dedup.strategies import (
    DedupStrategy,
    EmbeddingDedup,
    TitleDedup,
    UrlDedup,
)
from src.rag.retriever import RagRetriever
from src.storage.repository import ArticleRepository

logger = get_logger(__name__)

_DENSE_DEFAULTS = {
    "article_threshold": 0.90,
    "published_post_threshold": 0.84,
    "warn_margin": 0.12,
    "intra_run_threshold": 0.80,
}


def resolve_embedding_profile(
    config: AppConfig, retriever: RagRetriever | None = None
) -> dict[str, float]:
    """Pick the similarity thresholds that match the active embedder.

    Sparse hashing vectors and dense transformer vectors live on very different
    scales, so a single threshold cannot serve both. The profile name is the
    embedder name when a matching entry exists, otherwise ``dense``.
    """
    profiles = config.get("dedup.embedding_profiles") or {}
    name = getattr(getattr(retriever, "embedder", None), "name", "dense")
    raw = profiles.get(name) or profiles.get("dense") or {}
    resolved = {**_DENSE_DEFAULTS, **{k: float(v) for k, v in raw.items()}}
    resolved["profile"] = name if name in profiles else "dense"  # type: ignore[assignment]
    return resolved


@dataclass
class DedupSummary:
    """Aggregate view of one dedup pass."""

    kept: list[Article] = field(default_factory=list)
    duplicates: list[DedupResult] = field(default_factory=list)
    warnings: list[DedupResult] = field(default_factory=list)
    results: list[DedupResult] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        by_reason: dict[str, int] = {}
        for duplicate in self.duplicates:
            by_reason[str(duplicate.reason)] = by_reason.get(str(duplicate.reason), 0) + 1
        return {
            "kept": len(self.kept),
            "duplicates": len(self.duplicates),
            "near_duplicates": len(self.warnings),
            **{f"dup_{k}": v for k, v in by_reason.items()},
        }


class DedupService:
    """Applies URL -> title -> embedding checks and keeps an audit trail."""

    def __init__(
        self,
        *,
        config: AppConfig,
        repo: ArticleRepository | None = None,
        retriever: RagRetriever | None = None,
        known_keys: set[str] | None = None,
    ) -> None:
        self.config = config
        self.repo = repo
        self.profile = resolve_embedding_profile(config, retriever)
        self.strategies: list[DedupStrategy] = []

        if config.get("dedup.url_enabled", True):
            self.strategies.append(UrlDedup(repo, known_keys=known_keys))
        if config.get("dedup.title_enabled", True):
            self.strategies.append(
                TitleDedup(
                    repo,
                    threshold=float(config.get("dedup.title_threshold", 0.82)),
                    lookback_days=int(config.get("dedup.title_lookback_days", 45)),
                )
            )
        if config.get("dedup.embedding_enabled", True) and retriever is not None:
            self.strategies.append(
                EmbeddingDedup(
                    retriever,
                    article_threshold=self.profile["article_threshold"],
                    post_threshold=self.profile["published_post_threshold"],
                    warn_margin=self.profile["warn_margin"],
                )
            )

        logger.debug(
            "Dedup strategies: %s (profile=%s)",
            [s.name for s in self.strategies],
            self.profile,
        )

    def run(self, articles: list[Article], *, run_id: str | None = None) -> DedupSummary:
        """Partition articles into kept vs duplicate, recording near-misses."""
        summary = DedupSummary()
        seen_in_run: dict[str, Article] = {}

        for article in articles:
            result = DedupResult(article=article)

            for strategy in self.strategies:
                match = strategy.check(article, seen_in_run=seen_in_run)
                if match is None:
                    continue
                result.notes.append(match.note)
                if match.is_duplicate:
                    result.is_duplicate = True
                    result.reason = match.reason
                    result.score = match.score
                    result.matched_key = match.matched_key
                    result.matched_title = match.matched_title
                    break

            summary.results.append(result)
            if result.is_duplicate:
                summary.duplicates.append(result)
                logger.info(
                    "[dedup] drop %r (%s %.2f)",
                    article.title[:70],
                    result.reason,
                    result.score,
                )
            else:
                if result.notes:
                    summary.warnings.append(result)
                seen_in_run[article.key] = article
                summary.kept.append(article)

        logger.info(
            "[dedup] kept %d, dropped %d, flagged %d",
            len(summary.kept),
            len(summary.duplicates),
            len(summary.warnings),
        )
        return summary

    def persist(self, summary: DedupSummary, *, run_id: str | None = None) -> None:
        """Mark duplicates in the database (no-op without a repository)."""
        if self.repo is None:
            return
        for duplicate in summary.duplicates:
            self.repo.mark_duplicate(duplicate, run_id=run_id)
