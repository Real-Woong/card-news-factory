"""Importance scoring.

``score = (w_trust*trust + w_recency*recency + w_topic*topic + w_signal*signal
           + w_novelty*novelty + type_boost) * (1 - noise_penalty)``

Every component is 0..1 and every weight comes from ``config/ranking.yaml``, so
tuning behaviour never requires a code change. ``components`` and ``penalties``
are stored on the :class:`ScoredArticle` and written to
``selected_topics.json``, which makes each decision auditable.
"""

from __future__ import annotations

from src.core.config import AppConfig
from src.core.logging_setup import get_logger
from src.core.models import Article, ScoredArticle
from src.core.text import age_days, utcnow
from src.ranking.topics import TopicTagger

logger = get_logger(__name__)


class ArticleScorer:
    """Computes the weighted importance score for an article."""

    def __init__(self, config: AppConfig, *, tagger: TopicTagger | None = None) -> None:
        self.config = config
        self.ranking = config.ranking
        self.tagger = tagger or TopicTagger(config)

    # --------------------------------------------------------------- components
    def _trust(self, article: Article) -> float:
        maximum = max(self.ranking.trust_max, 1)
        return min(article.trust_score / maximum, 1.0)

    def _recency(self, article: Article, *, now=None) -> float:
        reference = now or utcnow()
        age = age_days(article.published_at or article.collected_at, now=reference)
        span = max(self.ranking.recency_half_life_days * 2.0, 0.5)
        return max(0.0, min(1.0, 1.0 - (age / span)))

    def _topic(self, weight: float) -> float:
        # Two strong topic matches saturate the component.
        return min(weight / 2.0, 1.0)

    def _signal(self, hits: int) -> float:
        return min(hits / 3.0, 1.0)

    # -------------------------------------------------------------------- score
    def score(
        self, article: Article, *, novelty: float = 1.0, now=None
    ) -> ScoredArticle:
        """Score one article. ``novelty`` is ``1 - max_similarity`` from RAG."""
        match = self.tagger.tag(article)
        weights = self.ranking.weights

        components = {
            "trust": self._trust(article),
            "recency": self._recency(article, now=now),
            "topic": self._topic(match.weight),
            "signal": self._signal(len(match.signals)),
            "novelty": max(0.0, min(novelty, 1.0)),
        }
        weighted = sum(components[name] * float(weights.get(name, 0.0)) for name in components)

        boost = float(self.ranking.type_boosts.get(article.source_type, 0.0))
        penalties: dict[str, float] = {}
        if match.noise:
            penalties["noise"] = self.tagger.noise_penalty

        raw = (weighted + boost) * (1.0 - penalties.get("noise", 0.0))
        final = max(0.0, min(raw, 1.0))

        reasons: list[str] = []
        if article.trust_tier in {"official", "github"}:
            reasons.append(f"primary source ({article.trust_tier})")
        if match.signals:
            reasons.append("concrete change: " + ", ".join(match.signals[:3]))
        if match.topic_labels:
            reasons.append("topics: " + ", ".join(match.topic_labels[:3]))
        if components["recency"] > 0.7:
            reasons.append("fresh")
        if components["novelty"] < 0.5:
            reasons.append("overlaps earlier coverage")
        if match.noise:
            reasons.append("noise keywords: " + ", ".join(match.noise[:2]))

        return ScoredArticle(
            article=article,
            score=final,
            components=components,
            penalties=penalties,
            novelty=components["novelty"],
            reasons=reasons,
        )

    def score_all(
        self, articles: list[Article], *, novelty_by_key: dict[str, float] | None = None
    ) -> list[ScoredArticle]:
        now = utcnow()
        novelty_by_key = novelty_by_key or {}
        scored = [
            self.score(article, novelty=novelty_by_key.get(article.key, 1.0), now=now)
            for article in articles
        ]
        scored.sort(key=lambda item: item.score, reverse=True)
        logger.info(
            "[rank] scored %d article(s); top score %.3f",
            len(scored),
            scored[0].score if scored else 0.0,
        )
        return scored
