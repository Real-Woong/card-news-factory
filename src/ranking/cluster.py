"""토픽 클러스터링 — 같은 사건을 다룬 기사를 하나로 묶습니다.

단일 신호로 판정하지 않습니다. 아래 신호를 각각 계산하고, **시간 근접성을 필수
조건으로** 두 개 이상이 동의할 때만 같은 사건으로 봅니다.

=====================  ===========================================
신호                   내용
=====================  ===========================================
``title``              제목 유사도 (버전/에디션 인식, core.text)
``entity``             제목의 비수치 토큰 containment
``version``            동일 버전/날짜 토큰 공유 (release 매칭)
``body``               본문 토큰 Jaccard
``embedding``          임베딩 코사인 (dense embedder 에서만 유효)
=====================  ===========================================

묶인 클러스터에는 authority 순서가 있습니다: official → github → paper →
major_news → community/social. 공식 출처가 하나라도 있으면 그것이 primary 가 되고,
research/fact_check 는 primary 기준으로 수행되며 나머지는 ``sources[]`` 로 함께
넘어갑니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.core.config import AppConfig
from src.core.logging_setup import get_logger
from src.core.models import Article
from src.core.text import (
    VARIANT_TOKENS,
    entity_overlap,
    entity_tokens,
    identifier_tokens,
    jaccard,
    normalize_title,
    title_similarity,
)

logger = get_logger(__name__)

# authority: 낮은 값이 우선(primary). sources[] 정렬에도 사용합니다.
AUTHORITY_ORDER = {
    "official": 0,
    "github": 1,
    "paper": 2,
    "major_news": 3,
    "community": 4,
    "social": 5,
}
AUTHORITY_LABEL = {
    "official": "primary",
    "github": "primary",
    "paper": "primary",
    "major_news": "secondary",
    "community": "secondary",
    "social": "secondary",
}


def authority_rank(trust_tier: str) -> int:
    return AUTHORITY_ORDER.get((trust_tier or "").lower(), 9)


def authority_label(trust_tier: str) -> str:
    return AUTHORITY_LABEL.get((trust_tier or "").lower(), "secondary")


def is_primary_tier(trust_tier: str) -> bool:
    return authority_label(trust_tier) == "primary"


def source_relation(article: Article, *, primary: bool) -> str:
    tier = (article.trust_tier or "").lower()
    source_type = (article.source_type or "").lower()
    url = article.url.lower()
    if tier == "github" or "github.com/" in url:
        return "implementation_artifact"
    if source_type in {"docs", "documentation"} or "/docs/" in url:
        return "documentation"
    if tier in {"major_news", "community"} and not primary:
        return "independent_secondary"
    return "primary_announcement" if primary else "documentation"


def _body_tokens(article: Article, limit: int = 1200) -> set[str]:
    text = f"{article.title} {article.summary} {article.content[:limit]}".lower()
    return {token for token in normalize_title(text).split() if len(token) > 1}


def _hours_between(left: datetime | None, right: datetime | None) -> float:
    """두 시각의 절대 시간차(시간). 하나라도 없으면 무한."""
    if left is None or right is None:
        return float("inf")
    if left.tzinfo is None:
        left = left.replace(tzinfo=UTC)
    if right.tzinfo is None:
        right = right.replace(tzinfo=UTC)
    return abs((left - right).total_seconds()) / 3600.0


# 어느 기사에나 나올 수 있어 "같은 사건" 근거가 되지 못하는 구절
GENERIC_PHRASES = frozenset(
    {
        "open source",
        "release notes",
        "now available",
        "new version",
        "blog post",
        "read more",
        "machine learning",
        "artificial intelligence",
        "generally available",
    }
)


def _version_tokens(title: str) -> set[str]:
    """버전처럼 보이는 토큰만 추출.

    ``v1.80.0``, ``4.4.0`` 은 인정하고 ``GPT-6`` 의 ``6`` 같은 맨 숫자는 제외합니다
    (맨 숫자는 서로 다른 제품에서도 자주 일치해 오탐을 만듭니다).
    """
    tokens = identifier_tokens(normalize_title(title))
    return {
        t
        for t in tokens
        if ("." in t) or (t.startswith("v") and len(t) > 2) or len(t) >= 4
    }


def _content_bigrams(article: Article, limit: int = 600) -> set[str]:
    """제목+요약에서 뽑은 내용어 bigram.

    "structured outputs", "blob throughput" 처럼 사건을 특정하는 구절을 잡습니다.
    단어 하나의 우연한 일치(예: sample)로 묶이는 것을 막기 위해 bigram 을 씁니다.
    """
    text = normalize_title(f"{article.title} {article.summary[:limit]}")
    tokens = [t for t in text.split() if len(t) > 1 and not any(c.isdigit() for c in t)]
    grams = {f"{a} {b}" for a, b in zip(tokens, tokens[1:], strict=False)}
    return {g for g in grams if g not in GENERIC_PHRASES}


@dataclass
class ClusterSignals:
    """두 기사 사이의 신호 값."""

    title: float = 0.0
    entity: float = 0.0
    version: float = 0.0
    body: float = 0.0
    phrase: float = 0.0
    embedding: float = 0.0
    hours_apart: float = 0.0
    agreed: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        """가장 강한 신호 값 (로그/추적용)."""
        return max(
            self.title, self.entity, self.version, self.body, self.phrase, self.embedding
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": round(self.title, 3),
            "entity": round(self.entity, 3),
            "version": round(self.version, 3),
            "body": round(self.body, 3),
            "phrase": round(self.phrase, 3),
            "embedding": round(self.embedding, 3),
            "hours_apart": round(self.hours_apart, 1),
            "agreed": self.agreed,
        }


@dataclass
class TopicCluster:
    """같은 사건을 다룬 기사 묶음."""

    primary: Article
    members: list[Article] = field(default_factory=list)
    signals: dict[str, ClusterSignals] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return 1 + len(self.members)

    @property
    def articles(self) -> list[Article]:
        return [self.primary, *self.members]

    @property
    def has_primary_source(self) -> bool:
        return any(is_primary_tier(a.trust_tier) for a in self.articles)

    @property
    def tiers(self) -> list[str]:
        return [a.trust_tier for a in self.articles]

    def sources(self) -> list[dict[str, Any]]:
        """authority 순으로 정렬된 출처 목록 (research/fact_check 로 전달)."""
        ordered = sorted(
            self.articles,
            key=lambda a: (
                0 if a is self.primary else 1,
                authority_rank(a.trust_tier),
                -a.trust_score,
                a.title,
            ),
        )
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for article in ordered:
            key = article.canonical_url or article.url
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "name": article.source_name,
                    "url": article.url,
                    "type": article.source_type,
                    "authority": authority_label(article.trust_tier),
                    "trust_tier": article.trust_tier,
                    "trust_score": article.trust_score,
                    "title": article.title,
                    "primary": article is self.primary,
                    "source_relation": source_relation(
                        article, primary=article is self.primary
                    ),
                    "supports_event": True,
                }
            )
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary.key,
            "size": self.size,
            "tiers": self.tiers,
            "sources": self.sources(),
            "signals": {key: sig.to_dict() for key, sig in self.signals.items()},
        }


class ClusterService:
    """설정 기반 다중 신호 클러스터링."""

    def __init__(self, config: AppConfig, retriever: Any | None = None) -> None:
        self.config = config
        self.retriever = retriever
        section = config.get("cluster", {}) or {}
        self.enabled = bool(section.get("enabled", True))
        self.window_hours = float(section.get("window_hours", 72))
        self.min_signals = int(section.get("min_signals", 2))
        self.max_size = int(section.get("max_members", 4)) + 1
        thresholds = section.get("thresholds", {}) or {}
        self.threshold_title = float(thresholds.get("title", 0.55))
        self.threshold_entity = float(thresholds.get("entity", 0.50))
        self.threshold_body = float(thresholds.get("body", 0.34))
        self.threshold_phrase = float(thresholds.get("phrase", 0.5))
        self.threshold_embedding = float(thresholds.get("embedding", 0.80))
        # hashing 임베딩은 희소해서 이 신호를 신뢰할 수 없습니다(README 8절).
        self.use_embedding = bool(section.get("use_embedding", False))
        # 같은 사건이라면 제품/변화를 특정하는 공통 구절(bigram)이나 동일 버전이
        # 반드시 있습니다. 이 게이트가 없으면 흔한 단어만으로 묶이는 오탐이 납니다.
        self.require_anchor = bool(section.get("require_anchor", True))
        embedder_name = getattr(getattr(retriever, "embedder", None), "name", "")
        if self.use_embedding and embedder_name == "hashing":
            logger.info("[cluster] hashing embedder → embedding 신호 비활성화")
            self.use_embedding = False

    # ------------------------------------------------------------------ 신호
    def signals(self, left: Article, right: Article) -> ClusterSignals:
        """두 기사의 신호를 계산합니다."""
        result = ClusterSignals()

        left_time = left.published_at or left.collected_at
        right_time = right.published_at or right.collected_at
        # age_days 는 음수를 0 으로 잘라내므로(과거만 가정) 직접 절대값을 계산합니다.
        result.hours_apart = _hours_between(left_time, right_time)

        result.title = title_similarity(left.title, right.title)
        result.entity = entity_overlap(left.title, right.title)

        ids_left = _version_tokens(left.title)
        ids_right = _version_tokens(right.title)
        if ids_left and ids_right:
            shared = ids_left & ids_right
            result.version = len(shared) / min(len(ids_left), len(ids_right))

        result.body = jaccard(_body_tokens(left), _body_tokens(right))

        shared_phrases = _content_bigrams(left) & _content_bigrams(right)
        result.phrase = min(len(shared_phrases) / 2.0, 1.0)
        self._last_shared_phrases = sorted(shared_phrases)[:4]

        if self.use_embedding and self.retriever is not None:
            try:
                result.embedding = self.retriever.similarity(
                    f"{left.title}\n{left.summary}", f"{right.title}\n{right.summary}"
                )
            except Exception:  # noqa: BLE001 - 신호 하나 실패가 런을 막지 않음
                result.embedding = 0.0

        agreed: list[str] = []
        if result.title >= self.threshold_title:
            agreed.append("title")
        if result.entity >= self.threshold_entity:
            agreed.append("entity")
        if result.body >= self.threshold_body:
            agreed.append("body")
        if result.phrase >= self.threshold_phrase:
            agreed.append("phrase")
        if result.version >= 0.99 and ids_left and ids_right:
            agreed.append("version")
        if self.use_embedding and result.embedding >= self.threshold_embedding:
            agreed.append("embedding")
        result.agreed = agreed
        return result

    def same_event(self, left: Article, right: Article) -> tuple[bool, ClusterSignals]:
        """같은 사건인지 판정. 시간 근접성은 필수, 신호 2개 이상 동의 필요."""
        signals = self.signals(left, right)
        if signals.hours_apart > self.window_hours:
            return False, signals
        # 같은 피드가 같은 사건을 두 번 내지는 않습니다. 동일 source 의 두 기사는
        # 서로 다른 아티팩트/논문이며, 재수집 중복은 dedup 단계가 처리합니다.
        # (실측: arxiv_ai 의 서로 다른 논문 2편, openai_news 의 고객 사례 2건이
        #  공통 bigram 때문에 묶이던 오탐을 제거)
        if left.source_id == right.source_id:
            return False, signals

        # 릴리스 채널이 다르면(devnet vs testnet, rc vs stable) 다른 아티팩트입니다.
        channels = (
            entity_tokens(normalize_title(left.title))
            ^ entity_tokens(normalize_title(right.title))
        ) & VARIANT_TOKENS
        if channels:
            return False, signals

        # anchor: 사건을 특정하는 공통 구절, 또는 (동일 버전 + 동일 대상)
        if self.require_anchor:
            has_anchor = "phrase" in signals.agreed or (
                "version" in signals.agreed and "entity" in signals.agreed
            )
            if not has_anchor:
                return False, signals

        return len(signals.agreed) >= self.min_signals, signals

    # --------------------------------------------------------------- 클러스터
    def build(self, articles: list[Article]) -> list[TopicCluster]:
        """기사 목록을 클러스터로 묶습니다 (authority 우선 primary 선정)."""
        if not self.enabled or not articles:
            return [TopicCluster(primary=article) for article in articles]

        # authority → trust_score → 최신순으로 정렬해 앞선 기사가 primary 가 됩니다.
        # primary 선정 순서: authority → trust_score → 먼저 발행된 것(원 발표가
        # 보통 앞선다) → 본문이 긴 것(요약 기사보다 원문이 길다)
        ordered = sorted(
            articles,
            key=lambda a: (
                authority_rank(a.trust_tier),
                -a.trust_score,
                (a.published_at or a.collected_at).timestamp(),
                -len(a.content or ""),
            ),
        )

        clusters: list[TopicCluster] = []
        assigned: set[str] = set()

        for article in ordered:
            if article.key in assigned:
                continue
            cluster = TopicCluster(primary=article)
            assigned.add(article.key)

            for candidate in ordered:
                if candidate.key in assigned or cluster.size >= self.max_size:
                    continue
                matched, signals = self.same_event(article, candidate)
                if not matched:
                    continue
                cluster.members.append(candidate)
                cluster.signals[candidate.key] = signals
                assigned.add(candidate.key)
                logger.info(
                    "[cluster] %r + %r (신호 %s)",
                    article.title[:46],
                    candidate.title[:46],
                    ",".join(signals.agreed),
                )
            clusters.append(cluster)

        multi = [c for c in clusters if c.size > 1]
        logger.info(
            "[cluster] %d개 기사 → %d개 클러스터 (복수 출처 %d개)",
            len(articles),
            len(clusters),
            len(multi),
        )
        return clusters

    def cluster_for(
        self, article: Article, clusters: list[TopicCluster]
    ) -> TopicCluster | None:
        for cluster in clusters:
            if article.key == cluster.primary.key:
                return cluster
            if any(member.key == article.key for member in cluster.members):
                return cluster
        return None
