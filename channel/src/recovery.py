"""중복 수리 후 슬라이드 밀도 회복과 안전한 템플릿 축소 정책."""

from __future__ import annotations

from dataclasses import dataclass, field

from channel.src.config import ChannelConfig
from channel.src.schema import Carousel, SlideType
from channel.src.similarity import DuplicatePair, bullet_similarity

REASON_THIN_AFTER_REPAIR = "thin_slide_after_repair"
REASON_DENSITY_RECOVERED = "density_recovered"
REASON_TEMPLATE_DOWNGRADED = "template_downgraded"
REASON_INSUFFICIENT_UNIQUE_MATERIAL = "insufficient_unique_material"


@dataclass
class DensityRecoveryState:
    thin_before: list[int] = field(default_factory=list)
    thin_after: list[int] = field(default_factory=list)
    unused_material_count: int = 0
    unique_material_count: int = 0
    unique_material_chars: int = 0
    downgraded_from: str | None = None
    downgraded_to: str | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def recovered(self) -> bool:
        return bool(self.thin_before) and not self.thin_after

    def to_dict(self) -> dict[str, object]:
        return {
            "thin_before": self.thin_before,
            "thin_after": self.thin_after,
            "unused_material_count": self.unused_material_count,
            "unique_material_count": self.unique_material_count,
            "unique_material_chars": self.unique_material_chars,
            "recovered": self.recovered,
            "downgraded_from": self.downgraded_from,
            "downgraded_to": self.downgraded_to,
            "reasons": self.reasons,
        }


def thin_content_slides(carousel: Carousel, channel: ChannelConfig) -> list[int]:
    """본문 항목 수 또는 글자 수 하한을 넘지 못한 content slide 번호."""
    budget = channel.text_budget("content")
    min_items = int(budget.get("body_min_items", 2))
    min_chars = int(budget.get("body_total_min_chars", 90))
    return [
        slide.index
        for slide in carousel.slides
        if slide.type is SlideType.CONTENT
        and (
            len(slide.body) < min_items
            or sum(len(line) for line in slide.body) < min_chars
        )
    ]


def unique_verified_material(
    facts: list[str], *, duplicate_threshold: float = 0.75
) -> list[str]:
    """검증 재료를 의미 중복 제거해 원본 순서로 반환합니다."""
    unique: list[str] = []
    for raw in facts:
        fact = raw.strip()
        if not fact:
            continue
        if any(bullet_similarity(fact, existing) >= duplicate_threshold for existing in unique):
            continue
        unique.append(fact)
    return unique


def unused_verified_material(
    facts: list[str], carousel: Carousel, *, duplicate_threshold: float = 0.75
) -> list[str]:
    """현재 슬라이드에 이미 등장한 사실과 겹치지 않는 검증 재료."""
    used = [
        text
        for slide in carousel.slides
        for text in [slide.headline, slide.subtitle, *slide.body, *slide.extra_body]
        if text
    ]
    return [
        fact
        for fact in unique_verified_material(
            facts, duplicate_threshold=duplicate_threshold
        )
        if not any(
            bullet_similarity(fact, current) >= duplicate_threshold for current in used
        )
    ]


def _meets_template_material(
    template: str, facts: list[str], channel: ChannelConfig
) -> bool:
    gate = channel.get("content.material_gate", {}) or {}
    requirements = {
        "news_short": (
            int(gate.get("min_usable_facts", 4)),
            int(gate.get("min_material_chars", 450)),
        ),
        "news_standard": (
            int(gate.get("standard_min_facts", 6)),
            int(gate.get("standard_min_chars", 700)),
        ),
        "deep_dive": (
            int(gate.get("deep_dive_min_facts", 10)),
            int(gate.get("deep_dive_min_chars", 1500)),
        ),
    }
    minimum = requirements.get(template)
    if minimum is None:
        return False
    return len(facts) >= minimum[0] and sum(len(fact) for fact in facts) >= minimum[1]


def safe_downgrade_target(
    current_template: str, facts: list[str], channel: ChannelConfig
) -> tuple[str | None, list[str]]:
    """유일 검증 재료로 다음 템플릿을 안전하게 채울 수 있을 때만 축소."""
    threshold = float(channel.get("content.duplicate.block_threshold", 0.75))
    unique = unique_verified_material(facts, duplicate_threshold=threshold)
    next_template = {
        "deep_dive": "news_standard",
        "news_standard": "news_short",
    }.get(current_template)
    if next_template is None:
        return None, unique
    # 현재 길이를 감당할 유일 재료가 충분하면 정보를 버리는 축소를 금지.
    if _meets_template_material(current_template, unique, channel):
        return None, unique
    if _meets_template_material(next_template, unique, channel):
        return next_template, unique
    return None, unique


def cover_body_duplicates(
    carousel: Carousel, channel: ChannelConfig
) -> list[DuplicatePair]:
    """cover ↔ body 중복 경고 기준을 넘는 쌍만 반환합니다."""
    cover = carousel.cover
    if cover is None:
        return []
    cfg = channel.get("content.duplicate", {}) or {}
    warn = float(cfg.get("cover_warn_threshold", 0.72))
    block = float(cfg.get("cover_block_threshold", 0.85))
    cover_texts = [text for text in (cover.headline, cover.subtitle) if text]
    body_texts = [
        (slide.index, text)
        for slide in carousel.slides
        if slide.index != cover.index
        for text in [*slide.body, *slide.extra_body]
        if text
    ]
    pairs: list[DuplicatePair] = []
    for cover_text in cover_texts:
        for slide_index, body_text in body_texts:
            score = bullet_similarity(cover_text, body_text)
            if score < warn:
                continue
            pairs.append(
                DuplicatePair(
                    score=score,
                    slide_a=cover.index,
                    slide_b=slide_index,
                    text_a=cover_text,
                    text_b=body_text,
                    severity="block" if score >= block else "warn",
                )
            )
    return sorted(pairs, key=lambda pair: pair.score, reverse=True)
