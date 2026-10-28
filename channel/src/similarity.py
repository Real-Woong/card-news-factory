"""슬라이드 간 semantic duplicate 검사.

같은 사실이 표현만 바뀌어 여러 슬라이드에 반복되는 것을 잡습니다.
(실측 문제: deep_dive 7장에서 Context → Key detail → Why it matters → Takeaway 가
같은 사실을 각도만 바꿔 되풀이함)

**bullet 단위**로 비교합니다. 슬라이드 전체를 합쳐 비교하면 흔한 단어 때문에
오탐이 나고, 어느 문장이 문제인지도 알 수 없습니다.

세 신호를 조합합니다.

``ratio``
    문자열 유사도(대칭). 표현을 조금 바꾼 재사용을 잡습니다.
``content``
    내용어 containment. 조사·어미가 달라도 같은 단어 집합이면 높습니다.
``anchor``
    숫자·버전·영문 고유명사(Firedancer, RPC, v4.4.0) 공유. 같은 *사실*을
    가리키는지 판단하는 핵심 신호입니다.

최종 점수는 ``max(ratio, content)`` 에 anchor 보정을 더한 값입니다. anchor 가 없으면
(= 서로 다른 수치/대상) 점수를 낮춰 오탐을 줄입니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from src.core.text import normalize_title

from channel.src.schema import Carousel, Slide

# 어느 문장에나 나오는 기능어/일반 명사 — 유사도 계산에서 제외
STOP_TOKENS = frozenset(
    {
        "그리고", "또한", "하지만", "이번", "현재", "기존", "관련", "가능", "지원", "제공",
        "사용", "적용", "변경", "추가", "개선", "필요", "경우", "대상", "기준", "이상",
        "이하", "때문", "위해", "통해", "따라", "대해", "수", "것", "등", "및", "중",
        "the", "and", "for", "with", "from", "that", "this", "are", "was", "will",
    }
)

# 사실을 특정하는 anchor 토큰: 숫자/버전/영문 식별자
_ANCHOR = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{2,}|\d+(?:[.,]\d+)*[a-zA-Z%]*")

# containment 신호를 쓰기 위한 최소 내용어 수. 짧은 bullet 은 2~3 단어만 겹쳐도
# containment 가 1.0 이 되어 오탐이 납니다(실측: "다른 사실 하나" ↔ "또 다른 사실").
MIN_TOKENS_FOR_CONTAINMENT = 3


def content_tokens(text: str) -> set[str]:
    """내용어 토큰 (기능어·1글자 제외)."""
    tokens = normalize_title(text).split()
    return {t for t in tokens if len(t) > 1 and t not in STOP_TOKENS}


def anchor_tokens(text: str) -> set[str]:
    """숫자·버전·영문 고유명사 — 같은 사실을 가리키는지 판단하는 신호."""
    return {m.group(0).lower() for m in _ANCHOR.finditer(text or "")}


def _ratio(left: str, right: str) -> float:
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return 0.0
    return max(
        SequenceMatcher(None, a, b).ratio(),
        SequenceMatcher(None, b, a).ratio(),
    )


def _containment(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def bullet_similarity(left: str, right: str) -> float:
    """두 bullet 의 의미 중복 점수 (0..1).

    anchor(수치·버전·고유명사)가 겹치면 같은 사실을 반복하는 것으로 보고 점수를
    올리고, 전혀 겹치지 않으면 낮춥니다.

    짧은 bullet(내용어 3개 미만)은 containment 가 쉽게 1.0 이 되어 오탐을 만들므로
    문자열 유사도만 사용합니다. 공유 내용어가 2개 미만일 때도 같습니다.
    """
    if not left.strip() or not right.strip():
        return 0.0

    tokens_left, tokens_right = content_tokens(left), content_tokens(right)
    shared = tokens_left & tokens_right
    use_containment = (
        len(tokens_left) >= MIN_TOKENS_FOR_CONTAINMENT
        and len(tokens_right) >= MIN_TOKENS_FOR_CONTAINMENT
        and len(shared) >= 2
    )
    base = _ratio(left, right)
    if use_containment:
        base = max(base, _containment(tokens_left, tokens_right))

    anchors_left, anchors_right = anchor_tokens(left), anchor_tokens(right)
    if anchors_left and anchors_right:
        shared_anchors = _containment(anchors_left, anchors_right)
        if shared_anchors >= 0.5:
            # 같은 수치/대상을 두 번 말하고 있다.
            base = min(1.0, base + 0.12)
        elif shared_anchors == 0.0:
            # 서로 다른 수치/대상 → 표현이 닮았어도 다른 사실이다.
            base *= 0.72
    return round(base, 4)


@dataclass
class DuplicatePair:
    """중복으로 판정된 bullet 쌍."""

    score: float
    slide_a: int
    slide_b: int
    text_a: str
    text_b: str
    severity: str = "warn"  # warn | block

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "slides": [self.slide_a, self.slide_b],
            "severity": self.severity,
            "a": self.text_a,
            "b": self.text_b,
        }


@dataclass
class DuplicateReport:
    """캐러셀 전체의 중복 검사 결과."""

    pairs: list[DuplicatePair] = field(default_factory=list)
    warn_threshold: float = 0.62
    block_threshold: float = 0.75

    @property
    def blocks(self) -> list[DuplicatePair]:
        return [p for p in self.pairs if p.severity == "block"]

    @property
    def warnings(self) -> list[DuplicatePair]:
        return [p for p in self.pairs if p.severity == "warn"]

    @property
    def max_score(self) -> float:
        return max((p.score for p in self.pairs), default=0.0)

    def uniqueness(self, total_bullets: int) -> float:
        """0..1. 중복 쌍이 많을수록 낮아집니다(semantic uniqueness 점수 재료)."""
        if total_bullets <= 1:
            return 1.0
        penalty = sum(p.score for p in self.pairs) / total_bullets
        return max(0.0, min(1.0, 1.0 - penalty))

    def to_dict(self) -> dict[str, Any]:
        return {
            "pairs": [p.to_dict() for p in self.pairs],
            "blocks": len(self.blocks),
            "warnings": len(self.warnings),
            "max_score": self.max_score,
        }


def _bullets(carousel: Carousel) -> list[tuple[int, str]]:
    """(슬라이드 번호, bullet) 목록. cover 의 headline/subtitle 도 포함합니다."""
    items: list[tuple[int, str]] = []
    for slide in carousel.slides:
        if slide.subtitle:
            items.append((slide.index, slide.subtitle))
        for line in slide.body:
            items.append((slide.index, line))
        for line in slide.extra_body:
            items.append((slide.index, line))
    return items


def find_duplicates(
    carousel: Carousel, *, warn: float = 0.62, block: float = 0.75
) -> DuplicateReport:
    """슬라이드 간 bullet 중복을 찾습니다(같은 슬라이드 내부는 제외).

    같은 슬라이드 안의 중복은 editor 가 이미 제거하며, 여기서는 "슬라이드를 넘겨도
    같은 말이 또 나오는" 문제만 봅니다.
    """
    report = DuplicateReport(warn_threshold=warn, block_threshold=block)
    items = _bullets(carousel)

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            slide_a, text_a = items[i]
            slide_b, text_b = items[j]
            if slide_a == slide_b:
                continue
            score = bullet_similarity(text_a, text_b)
            if score < warn:
                continue
            report.pairs.append(
                DuplicatePair(
                    score=score,
                    slide_a=slide_a,
                    slide_b=slide_b,
                    text_a=text_a,
                    text_b=text_b,
                    severity="block" if score >= block else "warn",
                )
            )
    report.pairs.sort(key=lambda p: p.score, reverse=True)
    return report


def used_facts(carousel: Carousel, up_to_index: int) -> list[str]:
    """``up_to_index`` 까지의 슬라이드에서 이미 사용한 문장 목록.

    writer 에게 "이미 쓴 사실" 로 전달해 재사용을 막습니다.
    """
    facts: list[str] = []
    for slide in carousel.slides:
        if slide.index > up_to_index:
            break
        facts.extend(slide.body)
        facts.extend(slide.extra_body)
    return facts


def slide_bullets(slide: Slide) -> list[str]:
    return [*slide.body, *slide.extra_body]
