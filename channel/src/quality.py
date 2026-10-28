"""content_quality_score — "사람이 그대로 게시해도 되는 수준"의 정량화.

``publishable`` 은 두 조건을 모두 만족해야 합니다.

1. BLOCK 이 0건 (치명적 결함 없음)
2. ``content_quality_score >= quality_score.publish_threshold``

즉 "기술적으로 생성됐다"가 아니라 "게시할 만하다"에 가깝게 만듭니다.

6개 성분 (가중치는 ``content.yaml -> quality_score.weights``)::

    factual_confidence   fact_check verdict + 미검증 항목 수
    source_diversity     출처 개수 + authority 구성
    semantic_uniqueness  슬라이드 간 중복 정도
    information_density   슬라이드가 목표 밀도 구간에 있는지 + 편차
    tone_consistency     명사형/개조식 비율, 금지 표현
    render_quality       overflow, autofit 축소, PNG 존재
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.core.logging_setup import get_logger
from src.core.text import canonicalize_url

from channel.src.config import ChannelConfig
from channel.src.schema import Carousel, SlideType, VerifiedTopic
from channel.src.similarity import DuplicateReport

logger = get_logger(__name__)

# 명사형/개조식 종결로 인정하는 패턴 (한글 명사, 영문/숫자, 괄호 등)
_NOMINAL_END = re.compile(
    r"(?:[가-힣]{1,}(?:음|함|됨|화|성|점|용|량|중|시|형|값|건|개|수|율|비|기|기준|지원|제거|"
    r"추가|변경|도입|적용|공개|제공|축소|확대|인하|인상|단축|전환|폐지|필요|권고|예정)"
    r"|[A-Za-z0-9)\]％%]|[0-9]+(?:\.[0-9]+)*)\s*$"
)
_PLAIN_END = re.compile(r"(?:한다|된다|이다|난다|든다|온다|진다|간다|없다|있다)\.?\s*$")


def _ending_kind(line: str, avoid: list[str]) -> str:
    """``nominal`` | ``plain`` | ``polite``"""
    text = line.rstrip().rstrip(".")
    for ending in avoid:
        if text.endswith(ending.rstrip(".")):
            return "polite"
    if _PLAIN_END.search(line.strip()):
        return "plain"
    if _NOMINAL_END.search(text):
        return "nominal"
    return "nominal" if not text.endswith("다") else "plain"


@dataclass
class ToneReport:
    """문체 분석 결과."""

    total: int = 0
    nominal: int = 0
    plain: int = 0
    polite: int = 0
    polite_lines: list[str] = field(default_factory=list)

    @property
    def nominal_ratio(self) -> float:
        if not self.total:
            return 1.0
        return (self.nominal + self.plain * 0.5) / self.total

    @property
    def consistent(self) -> bool:
        return self.polite == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "nominal": self.nominal,
            "plain": self.plain,
            "polite": self.polite,
            "nominal_ratio": round(self.nominal_ratio, 3),
        }


def analyze_tone(carousel: Carousel, channel: ChannelConfig) -> ToneReport:
    """슬라이드 본문의 종결 방식 분포를 셉니다(캡션은 제외)."""
    avoid = [str(e) for e in (channel.get("content.tone.avoid_endings") or [])]
    report = ToneReport()
    for slide in carousel.slides:
        lines = [*slide.body, *slide.extra_body]
        if slide.type is SlideType.COVER and slide.subtitle:
            lines = [slide.subtitle]
        for line in lines:
            if not line.strip():
                continue
            report.total += 1
            kind = _ending_kind(line, avoid)
            if kind == "polite":
                report.polite += 1
                report.polite_lines.append(line)
            elif kind == "plain":
                report.plain += 1
            else:
                report.nominal += 1
    return report


@dataclass
class QualityScore:
    """content_quality_score 와 성분별 값."""

    components: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    threshold: float = 0.70
    notes: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        if not self.components:
            return 0.0
        total = sum(
            self.components.get(name, 0.0) * weight for name, weight in self.weights.items()
        )
        return round(max(0.0, min(1.0, total)), 4)

    @property
    def passes(self) -> bool:
        return self.score >= self.threshold

    @property
    def weakest(self) -> tuple[str, float] | None:
        if not self.components:
            return None
        name = min(self.components, key=lambda k: self.components[k])
        return name, self.components[name]

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "threshold": self.threshold,
            "passes": self.passes,
            "components": {k: round(v, 3) for k, v in self.components.items()},
            "weights": self.weights,
            "notes": self.notes,
        }


def _factual_confidence(topic: VerifiedTopic | None, carousel: Carousel) -> float:
    verdict = (
        topic.verdict if topic else str(carousel.meta.get("fact_check_verdict") or "")
    ).lower()
    base = {"verified": 1.0, "partially_verified": 0.72}.get(verdict, 0.25)
    unresolved = len(
        topic.unverified_claims if topic else (carousel.meta.get("unverified_claims") or [])
    )
    # 미검증 항목이 많으면 원문 확인 부담이 커집니다(슬라이드에 쓰이지 않아도).
    return max(0.0, base - min(unresolved, 4) * 0.05)


def _source_diversity(carousel: Carousel) -> float:
    meta_sources = carousel.meta.get("sources") or []
    meta_by_url = {
        canonicalize_url(str(source.get("url") or "")): source
        for source in meta_sources
        if isinstance(source, dict) and source.get("url")
    }
    sources = [
        source
        for source in carousel.sources
        if source.name
        and (
            source.source_relation
            or str(
                meta_by_url.get(canonicalize_url(source.url), {}).get(
                    "source_relation"
                )
                or ""
            )
        )
        != "mirror"
        and source.supports_event
        and bool(
            meta_by_url.get(canonicalize_url(source.url), {}).get(
                "supports_event", True
            )
        )
    ]
    if not sources:
        return 0.0
    unique = []
    seen: set[str] = set()
    for source in sources:
        key = canonicalize_url(source.url) or source.name.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(source)

    relations: set[str] = set()
    authorities: set[str] = set()
    for index, source in enumerate(unique):
        metadata = meta_by_url.get(canonicalize_url(source.url), {})
        authority = source.authority or str(metadata.get("authority") or "")
        relation = source.source_relation or str(
            metadata.get("source_relation") or ""
        )
        # Legacy carousel fixtures predate relationship fields. Preserve their
        # former primary/secondary semantics without counting explicit mirrors.
        if not relation:
            relation = (
                "independent_secondary"
                if authority == "secondary"
                else "primary_announcement"
                if index == 0 or authority == "primary"
                else "documentation"
            )
        if authority:
            authorities.add(authority)
        relations.add(relation)
    has_primary = "primary" in authorities or any(
        relation in {"primary_announcement", "implementation_artifact", "documentation"}
        for relation in relations
    )
    if not has_primary:
        return 0.33
    if "independent_secondary" in relations:
        return 0.95
    if "implementation_artifact" in relations and len(unique) >= 2:
        return 0.82
    if "documentation" in relations and len(unique) >= 2:
        return 0.72
    if len(unique) >= 2:
        return 0.70
    return 0.55


def _information_density(carousel: Carousel, channel: ChannelConfig) -> float:
    budget = channel.text_budget("content")
    low = int(budget.get("body_total_min_chars", 90))
    high = int(budget.get("body_total_max_chars", 380))
    target_low, target_high = low * 1.4, high * 0.8

    totals: list[int] = []
    for slide in carousel.slides:
        if slide.type is SlideType.COVER:
            continue
        totals.append(sum(len(line) for line in [*slide.body, *slide.extra_body]))
    if not totals:
        return 0.0

    def slide_score(total: int) -> float:
        if total < low:
            return max(0.0, total / max(low, 1) * 0.6)
        if total < target_low:
            return 0.6 + 0.4 * (total - low) / max(target_low - low, 1)
        if total <= target_high:
            return 1.0
        if total <= high:
            return 1.0 - 0.3 * (total - target_high) / max(high - target_high, 1)
        return 0.6

    base = sum(slide_score(t) for t in totals) / len(totals)

    # 편차 보정: 한 장은 꽉 차고 한 장은 비면 읽는 리듬이 깨집니다.
    variance_limit = float(channel.get("content.density_variance_warn", 3.0))
    spread = max(totals) / max(min(totals), 1)
    if spread > variance_limit:
        base *= 0.85
    return max(0.0, min(1.0, base))


def _render_quality(render: Any | None, carousel: Carousel) -> float:
    if render is None:
        return 0.0
    slides = getattr(render, "slides", [])
    if not slides or len(slides) != carousel.slide_count:
        return 0.3
    if any(getattr(s, "overflow", False) for s in slides):
        return 0.0
    scales = [float(getattr(s, "fit_scale", 1.0)) for s in slides]
    shrunk = [s for s in scales if s < 1.0]
    if not shrunk:
        return 1.0
    # 축소가 있었다면 그만큼 감점 (0.82배까지 축소 = 0.55점)
    worst = min(scales)
    return max(0.4, 1.0 - (1.0 - worst) * 2.5)


def compute_quality_score(
    carousel: Carousel,
    *,
    channel: ChannelConfig,
    topic: VerifiedTopic | None = None,
    duplicates: DuplicateReport | None = None,
    tone: ToneReport | None = None,
    render: Any | None = None,
) -> QualityScore:
    """6개 성분을 계산해 content_quality_score 를 만듭니다."""
    weights = {
        str(k): float(v)
        for k, v in (channel.get("content.quality_score.weights") or {}).items()
    }
    threshold = float(channel.get("content.quality_score.publish_threshold", 0.70))

    total_bullets = sum(
        len(slide.body) + len(slide.extra_body) for slide in carousel.slides
    )
    tone = tone or analyze_tone(carousel, channel)
    min_ratio = float(channel.get("content.tone.min_nominal_ratio", 0.7))

    uniqueness = duplicates.uniqueness(total_bullets) if duplicates else 1.0
    tone_score = min(1.0, tone.nominal_ratio / max(min_ratio, 0.01))
    if tone.polite:
        tone_score = min(tone_score, 0.6)
    banned_hits = [p for p in channel.banned_phrases if p and p in carousel.all_text()]
    if banned_hits:
        tone_score = min(tone_score, 0.5)

    score = QualityScore(
        components={
            "factual_confidence": _factual_confidence(topic, carousel),
            "source_diversity": _source_diversity(carousel),
            "semantic_uniqueness": uniqueness,
            "information_density": _information_density(carousel, channel),
            "tone_consistency": max(0.0, min(1.0, tone_score)),
            "render_quality": _render_quality(render, carousel),
        },
        weights=weights,
        threshold=threshold,
    )

    weakest = score.weakest
    if weakest and weakest[1] < 0.6:
        score.notes.append(f"가장 약한 성분: {weakest[0]} {weakest[1]:.2f}")
    if duplicates and duplicates.pairs:
        score.notes.append(
            f"중복 {len(duplicates.pairs)}쌍 (최대 {duplicates.max_score:.2f})"
        )
    if tone.polite:
        score.notes.append(f"경어체 {tone.polite}줄 — 개조식/명사형으로 통일 필요")

    logger.info(
        "[quality] score=%.3f (%s)",
        score.score,
        ", ".join(f"{k} {v:.2f}" for k, v in score.components.items()),
    )
    return score
