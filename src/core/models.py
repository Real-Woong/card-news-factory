"""Domain models.

These dataclasses are the contract between stages. Each stage consumes the
previous stage's model and produces the next one:

``RawItem`` -> ``Article`` -> ``ScoredArticle`` -> ``ResearchNote``
-> ``FactCheckReport`` -> ``Draft``
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from src.core.text import (
    canonicalize_url,
    content_hash,
    domain_of,
    normalize_title,
    to_iso,
    url_hash,
    utcnow,
)


class Category(StrEnum):
    AI = "ai"
    BLOCKCHAIN = "blockchain"


class DedupReason(StrEnum):
    NONE = "none"
    URL = "url_exact"
    CONTENT_HASH = "content_hash"
    TITLE = "title_similarity"
    EMBEDDING_ARTICLE = "embedding_article"
    EMBEDDING_POST = "embedding_published_post"


class Verdict(StrEnum):
    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially_verified"
    UNVERIFIED = "unverified"
    CONTRADICTED = "contradicted"


def _dt(value: Any) -> str | None:
    return to_iso(value) if isinstance(value, datetime) else value


# --------------------------------------------------------------------------- #
# collection
# --------------------------------------------------------------------------- #
@dataclass
class RawItem:
    """Whatever a collector managed to pull from a source, before normalisation."""

    source_id: str
    source_name: str
    source_type: str
    trust_tier: str
    category: str
    url: str
    title: str
    summary: str = ""
    content: str = ""
    published_at: datetime | None = None
    author: str | None = None
    tags: list[str] = field(default_factory=list)
    collected_at: datetime = field(default_factory=utcnow)
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["published_at"] = _dt(self.published_at)
        data["collected_at"] = _dt(self.collected_at)
        return data


@dataclass
class Article:
    """A normalised, storable article."""

    url: str
    title: str
    source_id: str
    source_name: str
    source_type: str
    trust_tier: str
    trust_score: int
    category: str
    summary: str = ""
    content: str = ""
    published_at: datetime | None = None
    collected_at: datetime = field(default_factory=utcnow)
    author: str | None = None
    tags: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    language: str | None = None
    canonical_url: str = ""
    url_hash_value: str = ""
    content_hash_value: str = ""
    title_normalized: str = ""
    domain: str = ""
    raw_path: str | None = None
    processed_path: str | None = None
    # Structured evidence preserved before the bounded public body is created.
    # Internal only: links, identifiers, logical chunks and truncation metadata.
    source_metadata: dict[str, Any] = field(default_factory=dict)
    id: int | None = None

    def __post_init__(self) -> None:
        self.canonical_url = canonicalize_url(self.canonical_url or self.url)
        self.url_hash_value = self.url_hash_value or url_hash(self.url)
        self.title_normalized = self.title_normalized or normalize_title(self.title)
        self.content_hash_value = self.content_hash_value or content_hash(
            self.title, (self.content or self.summary)[:2000]
        )
        self.domain = self.domain or domain_of(self.url)

    @property
    def key(self) -> str:
        """Stable identifier usable as a vector-store document id."""
        return self.url_hash_value

    @property
    def body(self) -> str:
        """Best available text for embedding / prompting."""
        return self.content or self.summary or self.title

    def embedding_text(self) -> str:
        return f"{self.title}\n\n{self.summary}\n\n{self.content}".strip()

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        data = asdict(self)
        data["published_at"] = _dt(self.published_at)
        data["collected_at"] = _dt(self.collected_at)
        if not include_content:
            data.pop("content", None)
        return data

    def reference(self) -> dict[str, Any]:
        """Compact form handed to the LLM agents."""
        evidence_summary = {
            key: value
            for key, value in self.source_metadata.items()
            if key not in {"chunks", "list_items", "tables"}
        }
        return {
            "id": self.id,
            "key": self.key,
            "title": self.title,
            "url": self.url,
            "source": self.source_name,
            "source_type": self.source_type,
            "trust_tier": self.trust_tier,
            "trust_score": self.trust_score,
            "category": self.category,
            "published_at": _dt(self.published_at),
            "topics": self.topics,
            "signals": self.signals,
            "source_metadata": evidence_summary,
        }


# --------------------------------------------------------------------------- #
# dedup + ranking
# --------------------------------------------------------------------------- #
@dataclass
class DedupResult:
    """Outcome of the three dedup strategies for one article."""

    article: Article
    is_duplicate: bool = False
    reason: DedupReason = DedupReason.NONE
    score: float = 0.0
    matched_key: str | None = None
    matched_title: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.article.key,
            "title": self.article.title,
            "is_duplicate": self.is_duplicate,
            "reason": str(self.reason),
            "score": round(self.score, 4),
            "matched_key": self.matched_key,
            "matched_title": self.matched_title,
            "notes": self.notes,
        }


@dataclass
class ScoredArticle:
    """An article with its ranking score and component breakdown."""

    article: Article
    score: float
    components: dict[str, float] = field(default_factory=dict)
    penalties: dict[str, float] = field(default_factory=dict)
    novelty: float = 1.0
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.article.key,
            "title": self.article.title,
            "url": self.article.url,
            "source": self.article.source_name,
            "category": self.article.category,
            "trust_tier": self.article.trust_tier,
            "published_at": _dt(self.article.published_at),
            "score": round(self.score, 4),
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "penalties": {k: round(v, 4) for k, v in self.penalties.items()},
            "novelty": round(self.novelty, 4),
            "reasons": self.reasons,
            "topics": self.article.topics,
        }


# --------------------------------------------------------------------------- #
# RAG
# --------------------------------------------------------------------------- #
@dataclass
class VectorRecord:
    """A chunk stored in the vector store."""

    id: str
    text: str
    embedding: list[float]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievedChunk:
    """A similarity hit returned by the vector store."""

    id: str
    text: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def source_title(self) -> str:
        return str(self.metadata.get("title") or self.metadata.get("source") or self.id)

    @property
    def url(self) -> str:
        return str(self.metadata.get("url") or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "score": round(self.score, 4),
            "title": self.source_title,
            "url": self.url,
            "collection": self.metadata.get("collection"),
            "excerpt": self.text[:400],
        }


# --------------------------------------------------------------------------- #
# agents
# --------------------------------------------------------------------------- #
@dataclass
class Claim:
    """A single factual statement extracted by the Research Agent."""

    text: str
    kind: str = "fact"  # fact | number | date | version | product | claim
    source_url: str = ""
    confidence: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResearchNote:
    """Research Agent output for one selected article."""

    article_key: str
    title: str
    url: str
    category: str
    what_changed: str = ""
    why_it_matters: str = ""
    key_facts: list[Claim] = field(default_factory=list)
    technical_details: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    prior_context: list[str] = field(default_factory=list)
    context_chunks: list[RetrievedChunk] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        return {
            "article_key": self.article_key,
            "title": self.title,
            "url": self.url,
            "category": self.category,
            "what_changed": self.what_changed,
            "why_it_matters": self.why_it_matters,
            "key_facts": [c.to_dict() for c in self.key_facts],
            "technical_details": self.technical_details,
            "open_questions": self.open_questions,
            "prior_context": self.prior_context,
            "context_chunks": [c.to_dict() for c in self.context_chunks],
            "sources": self.sources,
            "confidence": round(self.confidence, 3),
        }


@dataclass
class CheckedClaim:
    claim: str
    verdict: Verdict = Verdict.UNVERIFIED
    confidence: float = 0.0
    evidence_url: str = ""
    source_id: str = ""
    source_url: str = ""
    matched_chunk: str = ""
    matched_section: str = ""
    evidence_excerpt: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["verdict"] = str(self.verdict)
        return data


@dataclass
class FactCheckReport:
    """Fact Check Agent output for one research note."""

    article_key: str
    title: str
    verdict: Verdict = Verdict.UNVERIFIED
    confidence: float = 0.0
    checked_claims: list[CheckedClaim] = field(default_factory=list)
    unverified_claims: list[str] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)
    primary_source_found: bool = False
    needs_human_review: bool = True
    notes: list[str] = field(default_factory=list)
    partial_reasons: list[str] = field(default_factory=list)
    quality_metrics: dict[str, bool] = field(default_factory=dict)

    @property
    def is_publishable(self) -> bool:
        return self.verdict in (Verdict.VERIFIED, Verdict.PARTIALLY_VERIFIED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "article_key": self.article_key,
            "title": self.title,
            "verdict": str(self.verdict),
            "confidence": round(self.confidence, 3),
            "checked_claims": [c.to_dict() for c in self.checked_claims],
            "unverified_claims": self.unverified_claims,
            "corrections": self.corrections,
            "primary_source_found": self.primary_source_found,
            "needs_human_review": self.needs_human_review,
            "notes": self.notes,
            "partial_reasons": self.partial_reasons,
            "quality_metrics": self.quality_metrics,
        }


@dataclass
class Draft:
    """A channel-specific post draft."""

    channel_id: str
    format: str
    title: str
    body: str
    hashtags: list[str] = field(default_factory=list)
    thread_parts: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    char_count: int = 0
    over_limit: bool = False
    editor_changes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewItem:
    """One line in ``review.md``."""

    kind: str  # unverified_claim | missing_source | duplicate_risk | tone | number
    message: str
    severity: str = "info"  # info | warn | block
    reference: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewReport:
    """Aggregate human-review checklist for a run."""

    run_id: str
    recommend_publish: bool = False
    confidence: float = 0.0
    items: list[ReviewItem] = field(default_factory=list)
    summary: str = ""

    @property
    def blockers(self) -> list[ReviewItem]:
        return [i for i in self.items if i.severity == "block"]

    def add(self, kind: str, message: str, *, severity: str = "info", reference: str = "") -> None:
        self.items.append(
            ReviewItem(kind=kind, message=message, severity=severity, reference=reference)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "recommend_publish": self.recommend_publish,
            "confidence": round(self.confidence, 3),
            "summary": self.summary,
            "items": [i.to_dict() for i in self.items],
        }


# --------------------------------------------------------------------------- #
# run bookkeeping
# --------------------------------------------------------------------------- #
@dataclass
class RunRecord:
    """One pipeline execution, persisted in the ``runs`` table."""

    run_id: str
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    status: str = "running"  # running | success | failed | partial
    mode: str = "live"  # live | offline | dry-run
    stages_completed: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    output_dir: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": _dt(self.started_at),
            "finished_at": _dt(self.finished_at),
            "status": self.status,
            "mode": self.mode,
            "stages_completed": self.stages_completed,
            "counts": self.counts,
            "output_dir": self.output_dir,
            "error": self.error,
        }


@dataclass
class PublishedPost:
    """A post we already produced - used to avoid repeating ourselves."""

    run_id: str
    channel_id: str
    format: str
    title: str
    body: str
    status: str = "draft"  # draft | approved | published | rejected
    article_keys: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utcnow)
    published_at: datetime | None = None
    external_id: str | None = None
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = _dt(self.created_at)
        data["published_at"] = _dt(self.published_at)
        return data
