"""Selection: turn scored articles into the shortlist for this run.

Rules (all from ``config/ranking.yaml -> selection``):

* drop anything below ``min_score``
* at most ``max_per_source`` items from one source
* skip an item that covers the same series as something already selected
  (``intra_run_series_overlap``; e.g. two releases of the same repo)
* optionally skip semantic near-duplicates when a dense embedder is configured
* fill ``category_quota`` first, then the remaining slots by score
* never exceed ``max_items`` (``pipeline.yaml -> rank.max_selected`` wins if lower)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from src.core.config import AppConfig
from src.core.logging_setup import get_logger
from src.core.models import Article, ScoredArticle
from src.core.text import entity_overlap

logger = get_logger(__name__)

SimilarityFn = Callable[[Article, Article], float]


@dataclass
class OverlapSkip:
    """An item dropped because the run already covers the same story."""

    candidate: ScoredArticle
    kept: ScoredArticle
    score: float
    kind: str  # series | embedding | cluster

    def to_dict(self) -> dict[str, object]:
        return {
            "skipped_title": self.candidate.article.title,
            "skipped_url": self.candidate.article.url,
            "kept_title": self.kept.article.title,
            "kept_url": self.kept.article.url,
            "similarity": round(self.score, 3),
            "kind": self.kind,
        }


class TopicSelector:
    """Applies quotas, source caps and intra-run overlap checks."""

    def __init__(
        self, config: AppConfig, *, embedding_overlap_threshold: float | None = None
    ) -> None:
        self.config = config
        self.ranking = config.ranking
        pipeline_cap = config.get("rank.max_selected")
        self.max_items = min(
            int(pipeline_cap) if pipeline_cap else self.ranking.max_items,
            self.ranking.max_items,
        )
        self.series_overlap = self.ranking.intra_run_series_overlap
        self.embedding_overlap = embedding_overlap_threshold
        self.skipped_overlap: list[OverlapSkip] = []

    # ------------------------------------------------------------------ helpers
    def _overlap(
        self,
        candidate: ScoredArticle,
        taken: list[ScoredArticle],
        similarity_fn: SimilarityFn | None,
    ) -> OverlapSkip | None:
        """Is this candidate the same story as something already selected?

        Two signals:

        * ``series``    - same repo / same changelog series, different version or
          date (deterministic, works with any embedder)
        * ``embedding`` - semantically the same story in different words
          (only meaningful with a dense embedder; see
          ``dedup.embedding_profiles``)
        """
        for other in taken:
            score = entity_overlap(candidate.article.title, other.article.title)
            if score >= self.series_overlap:
                return OverlapSkip(candidate=candidate, kept=other, score=score, kind="series")
            if similarity_fn is not None and self.embedding_overlap is not None:
                score = similarity_fn(candidate.article, other.article)
                if score >= self.embedding_overlap:
                    return OverlapSkip(
                        candidate=candidate, kept=other, score=score, kind="embedding"
                    )
        return None

    # ------------------------------------------------------------------- public
    def select(
        self,
        scored: list[ScoredArticle],
        *,
        similarity_fn: SimilarityFn | None = None,
        cluster_key_fn: Callable[[Article], str | None] | None = None,
    ) -> list[ScoredArticle]:
        """``cluster_key_fn`` 이 주어지면 같은 클러스터(같은 사건)에서 한 건만 고릅니다."""
        self.skipped_overlap = []
        taken_clusters: dict[str, ScoredArticle] = {}
        eligible = [item for item in scored if item.score >= self.ranking.min_score]
        if not eligible:
            logger.warning(
                "[rank] no article reached min_score=%.2f (best %.3f)",
                self.ranking.min_score,
                scored[0].score if scored else 0.0,
            )
            return []

        ordered = sorted(eligible, key=lambda item: item.score, reverse=True)
        per_source: dict[str, int] = {}
        per_category: dict[str, int] = {}
        selected: list[ScoredArticle] = []
        taken_keys: set[str] = set()
        rejected_keys: set[str] = set()

        def can_take(item: ScoredArticle) -> bool:
            if per_source.get(item.article.source_id, 0) >= self.ranking.max_per_source:
                return False
            if cluster_key_fn is not None:
                key = cluster_key_fn(item.article)
                if key and key in taken_clusters:
                    kept = taken_clusters[key]
                    self.skipped_overlap.append(
                        OverlapSkip(candidate=item, kept=kept, score=1.0, kind="cluster")
                    )
                    rejected_keys.add(item.article.key)
                    logger.info(
                        "[rank] skip %r: 같은 사건 클러스터 (선택됨: %r)",
                        item.article.title[:60],
                        kept.article.title[:60],
                    )
                    return False
            overlap = self._overlap(item, selected, similarity_fn)
            if overlap is not None:
                self.skipped_overlap.append(overlap)
                rejected_keys.add(item.article.key)
                logger.info(
                    "[rank] skip %r: same story as %r (%s %.2f)",
                    item.article.title[:60],
                    overlap.kept.article.title[:60],
                    overlap.kind,
                    overlap.score,
                )
                return False
            return True

        def take(item: ScoredArticle) -> None:
            selected.append(item)
            taken_keys.add(item.article.key)
            if cluster_key_fn is not None:
                key = cluster_key_fn(item.article)
                if key:
                    taken_clusters[key] = item
            per_source[item.article.source_id] = per_source.get(item.article.source_id, 0) + 1
            per_category[item.article.category] = (
                per_category.get(item.article.category, 0) + 1
            )

        def skip(item: ScoredArticle) -> bool:
            return item.article.key in taken_keys or item.article.key in rejected_keys

        # Pass 1: honour the per-category quotas.
        for category, quota in self.ranking.category_quota.items():
            for item in ordered:
                if len(selected) >= self.max_items:
                    break
                if per_category.get(category, 0) >= quota:
                    break
                if skip(item) or item.article.category != category:
                    continue
                if can_take(item):
                    take(item)

        # Pass 2: fill remaining slots by score.
        for item in ordered:
            if len(selected) >= self.max_items:
                break
            if skip(item):
                continue
            if can_take(item):
                take(item)

        selected.sort(key=lambda item: item.score, reverse=True)
        logger.info(
            "[rank] selected %d/%d item(s)%s: %s",
            len(selected),
            len(scored),
            f", {len(self.skipped_overlap)} skipped as same-story"
            if self.skipped_overlap
            else "",
            ", ".join(f"{i.article.category}:{i.score:.2f}" for i in selected),
        )
        return selected
