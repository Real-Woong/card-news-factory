"""재료 게이트 — 생산 **전에** 토픽이 캐러셀을 채울 만한지 판단합니다.

빈 슬라이드를 만드는 근본 원인은 렌더러가 아니라 재료 부족입니다. LLM 호출 전에
검증된 fact 의 개수와 분량을 세어, 부족하면 토픽을 건너뜁니다.

**금지: 재료가 부족할 때 문장을 만들어 채우는 것.** 이 모듈은 "만들지 않을 이유"를
제공합니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.core.logging_setup import get_logger

from channel.src.config import ChannelConfig
from channel.src.schema import VerifiedTopic

logger = get_logger(__name__)


@dataclass
class MaterialAssessment:
    """토픽 하나의 재료 평가 결과."""

    usable_facts: int = 0
    material_chars: int = 0
    max_template: str | None = None
    sufficient: bool = False
    reason: str = ""
    facts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "usable_facts": self.usable_facts,
            "material_chars": self.material_chars,
            "max_template": self.max_template,
            "sufficient": self.sufficient,
            "reason": self.reason,
        }


def _usable_material(topic: VerifiedTopic) -> list[str]:
    """슬라이드에 쓸 수 있는 문장만 모읍니다(미검증 항목 제외)."""
    avoid = {c.strip().lower() for c in topic.unverified_claims}
    material: list[str] = []
    for line in [*topic.key_facts, *topic.technical_details]:
        text = line.strip()
        if not text or len(text) < 12:
            continue
        lowered = text.lower()
        if any(claim and claim in lowered for claim in avoid):
            continue
        if text in material:
            continue
        material.append(text)
    # what_changed / why_it_matters 도 재료입니다.
    for extra in (topic.what_changed, topic.why_it_matters):
        if extra and len(extra.strip()) >= 20 and extra.strip() not in material:
            material.append(extra.strip())
    return material


def assess_material(topic: VerifiedTopic, channel: ChannelConfig) -> MaterialAssessment:
    """토픽의 재료가 어느 템플릿까지 감당할 수 있는지 판단합니다."""
    gate = channel.get("content.material_gate", {}) or {}
    material = _usable_material(topic)
    facts = len(material)
    chars = sum(len(line) for line in material)

    min_facts = int(gate.get("min_usable_facts", 3))
    min_chars = int(gate.get("min_material_chars", 260))
    std_facts = int(gate.get("standard_min_facts", 4))
    std_chars = int(gate.get("standard_min_chars", 420))
    deep_facts = int(gate.get("deep_dive_min_facts", 6))
    deep_chars = int(gate.get("deep_dive_min_chars", 700))

    assessment = MaterialAssessment(
        usable_facts=facts, material_chars=chars, facts=material
    )

    if facts < min_facts or chars < min_chars:
        assessment.sufficient = False
        assessment.max_template = None
        assessment.reason = (
            f"사용 가능한 fact {facts}개 / {chars}자 — 최소 기준 "
            f"{min_facts}개 / {min_chars}자 미달 (3장도 채울 수 없음)"
        )
        return assessment

    assessment.sufficient = True
    if facts >= deep_facts and chars >= deep_chars:
        assessment.max_template = "deep_dive"
    elif facts >= std_facts and chars >= std_chars:
        assessment.max_template = "news_standard"
    else:
        assessment.max_template = "news_short"
    assessment.reason = (
        f"fact {facts}개 / {chars}자 → 최대 {assessment.max_template}"
    )
    return assessment


def allowed_templates(assessment: MaterialAssessment, channel: ChannelConfig) -> list[str]:
    """재료로 감당 가능한 템플릿 목록 (짧은 것부터)."""
    if not assessment.sufficient or assessment.max_template is None:
        return []
    order = sorted(channel.templates.items(), key=lambda item: item[1].slides)
    cap = channel.templates[assessment.max_template].slides
    return [name for name, spec in order if spec.slides <= cap]


def enforce_template(
    template: str, assessment: MaterialAssessment, channel: ChannelConfig
) -> tuple[str, str | None]:
    """planner 선택을 재료로 검증합니다.

    Returns:
        ``(사용할 템플릿, 조정 이유 또는 None)``. 재료가 부족하면 템플릿을
        줄이고, 재료가 충분한데 너무 짧게 골랐다면 그대로 둡니다(planner 판단 존중).
    """
    allowed = allowed_templates(assessment, channel)
    if not allowed:
        return template, "재료 부족 — 생산 불가"
    if template in allowed:
        return template, None

    # planner 가 재료보다 큰 템플릿을 골랐다면 감당 가능한 최대치로 내립니다.
    fallback = allowed[-1]
    reason = (
        f"재료가 {template} 를 채우지 못해 {fallback} 로 축소 "
        f"({assessment.usable_facts}개 fact / {assessment.material_chars}자)"
    )
    logger.info("[material] %s", reason)
    return fallback, reason
