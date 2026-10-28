"""Topic tagging.

Turns ``config/topics.yaml`` into per-article tags:

* ``topics``  - matched topic ids (used by ranking and shown in drafts)
* ``signals`` - "something changed" keywords (release, GA, breaking change, ...)
* ``noise``   - low-value keywords that penalise the score

Used by the normalize stage (to persist tags) and by the scorer (to weight them).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.core.config import AppConfig
from src.core.models import Article
from src.core.text import count_keyword_hits


@dataclass
class TopicMatch:
    """Result of tagging one article."""

    topics: list[str] = field(default_factory=list)
    topic_labels: list[str] = field(default_factory=list)
    weight: float = 0.0
    signals: list[str] = field(default_factory=list)
    noise: list[str] = field(default_factory=list)


class TopicTagger:
    """Keyword-based tagger. Cheap, deterministic, easy to audit."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.signal_terms: list[str] = list(
            (config.topics.get("signal_keywords") or {}).get("terms") or []
        )
        self.noise_terms: list[str] = list(
            (config.topics.get("noise_keywords") or {}).get("terms") or []
        )
        self.noise_penalty = float(
            (config.topics.get("noise_keywords") or {}).get("penalty", 0.35)
        )
        self._topics: dict[str, list[dict]] = {}
        for category in config.categories:
            self._topics[category] = list(config.topic_section(category).get("topics") or [])

    def topics_for(self, category: str) -> list[dict]:
        return self._topics.get(category.lower(), [])

    def tag_text(self, text: str, *, category: str) -> TopicMatch:
        match = TopicMatch()
        haystack = (text or "").lower()

        for topic in self.topics_for(category):
            keywords = [str(k) for k in (topic.get("keywords") or [])]
            hits = count_keyword_hits(haystack, keywords)
            if hits:
                match.topics.append(str(topic.get("id")))
                match.topic_labels.append(str(topic.get("label") or topic.get("id")))
                match.weight += float(topic.get("weight", 1.0))

        match.signals = count_keyword_hits(haystack, self.signal_terms)
        match.noise = count_keyword_hits(haystack, self.noise_terms)
        return match

    def tag(self, article: Article) -> TopicMatch:
        """Tag an article in place (``topics``/``signals``) and return the match."""
        haystack = f"{article.title}\n{article.summary}\n{article.content[:4000]}"
        match = self.tag_text(haystack, category=article.category)
        article.topics = match.topics
        article.signals = match.signals
        return match
