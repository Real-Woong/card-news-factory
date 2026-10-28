"""슬라이드 문구에서 숫자를 뽑아 stat·전/후 비교 차트 블록(``Slide.visual``)을 만듭니다.

설계 원칙 — **새 사실을 만들지 않는다.**

* 입력은 이미 확정된(근거 심사를 거친) 슬라이드 문구뿐입니다. LLM 을 호출하지 않으므로
  credits 가 들지 않고, 문구에 없는 숫자가 차트에 나올 수 없습니다.
* 단위가 붙은 숫자만 봅니다(``3.13x``, ``8.9%``, ``120ms``, ``280M``, ``2.5배`` …).
  정책·규제 소식은 성능 수치가 없는 대신 기간(``5년``, ``30일``, ``24시간``)이 핵심이라
  기간 단위도 후보에 넣되 점수는 가장 낮게 둡니다. 날짜(``2026년``, ``9월 23일``)·버전·
  ``GPT-5`` 같은 이름은 대상이 아닙니다.
* 차트에 쓰이는 값은 모두 슬라이드 문구의 부분 문자열입니다. validator 가 이를 다시 확인합니다
  (:func:`ungrounded_values`).
* 후보가 없으면 아무것도 붙이지 않습니다(빈 공간을 채우려고 차트를 만들지 않음).

visual 종류

* ``stat``    : 큰 숫자 하나 (``{"type": "stat", "value": "3.13x", "number": "3.13", "unit": "x"}``)
* ``compare`` : "A에서 B로" 전/후 막대 두 개 (같은 단위일 때만)
"""

from __future__ import annotations

import re
from typing import Any

from src.core.logging_setup import get_logger

from channel.src.config import ChannelConfig
from channel.src.schema import Carousel, Slide, SlideType

logger = get_logger(__name__)

# 숫자 + 바로 붙은 단위. 한글 단위만 한 칸 공백을 허용합니다.
# 앞에 영문자·숫자·점·쉼표·$·하이픈이 붙은 경우(버전 v0.40, GPT-5 …)는 제외합니다.
_UNIT = (
    r"(?:%|[xX×](?![A-Za-z])|ms|GB|MB|TB|[KMB](?![A-Za-z0-9])"
    r"|\s?(?:배|달러|억|만|조)|개월|시간|년|일)"
)
_NUMBER = re.compile(
    rf"(?<![A-Za-z0-9.,$_-])(\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?|\d+(?:\.\d+)?)({_UNIT})"
)

# "A에서 B로", "A → B" 처럼 전/후를 잇는 표현
_CONNECTORS = ("→", "->", "에서")
_SALIENT_UNITS = {"%", "x", "배"}
_DURATION_UNITS = {"년", "개월", "시간", "일"}

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "max_per_carousel": 2,
    "max_body_lines": 4,
    "max_body_chars": 240,
}


def _normalize_unit(unit: str) -> str:
    unit = unit.strip()
    return "x" if unit in {"x", "X", "×"} else unit


def _to_number(text: str) -> float:
    return float(text.replace(",", ""))


def _numbers(line: str) -> list[re.Match[str]]:
    """줄에서 차트 후보 숫자만 추린다. 연도(2026년)와 날짜의 일(9월 23일)은 제외."""
    kept: list[re.Match[str]] = []
    for match in _NUMBER.finditer(line):
        unit = match.group(2).strip()
        if unit == "년" and _to_number(match.group(1)) >= 1900:
            continue
        if unit == "일" and re.search(r"월\s*$", line[: match.start()]):
            continue
        kept.append(match)
    return kept


def _score(match: re.Match[str]) -> int:
    """눈에 띄는 단위(%, x, 배) 3 > 일반 단위 2 > 기간 1."""
    unit = _normalize_unit(match.group(2))
    return 3 if unit in _SALIENT_UNITS else 1 if unit in _DURATION_UNITS else 2


def number_spans(line: str) -> list[tuple[int, int]]:
    """본문 줄에서 강조할 숫자(단위 포함)의 (시작, 끝) 위치. 날짜·버전은 포함하지 않는다."""
    return [(match.start(), match.end()) for match in _numbers(line)]


def _stat(match: re.Match[str]) -> dict[str, Any]:
    return {
        "type": "stat",
        "value": match.group(0).strip(),
        "number": match.group(1),
        "unit": match.group(2).strip(),
    }


def _compare(line: str, matches: list[re.Match[str]]) -> dict[str, Any] | None:
    """한 줄에 같은 단위의 숫자 두 개가 전/후 표현으로 이어져 있으면 compare."""
    if len(matches) < 2:
        return None
    first, second = matches[0], matches[1]
    if _normalize_unit(first.group(2)) != _normalize_unit(second.group(2)):
        return None
    between = line[first.end() : second.start()]
    if len(between) > 24 or not any(token in between for token in _CONNECTORS):
        return None
    before, after = _to_number(first.group(1)), _to_number(second.group(1))
    if before <= 0 or after <= 0 or before == after:
        return None
    return {
        "type": "compare",
        "unit": first.group(2).strip(),
        "before": {"text": first.group(0).strip(), "number": before},
        "after": {"text": second.group(0).strip(), "number": after},
    }


def _slide_candidate(slide: Slide) -> tuple[int, dict[str, Any]] | None:
    """슬라이드 본문 줄 중 가장 좋은 (점수, visual). compare > 눈에 띄는 단위 stat > 기타 stat."""
    best: tuple[int, dict[str, Any]] | None = None
    for line in slide.body:
        matches = _numbers(line)
        if not matches:
            continue
        compare = _compare(line, matches)
        if compare:
            candidate = (4, compare)
        else:
            top = max(matches, key=_score)  # 동점이면 먼저 나온 숫자
            stat = _stat(top)
            if str(stat.get("value", "")) and str(stat["value"]) in (slide.title or ""):
                continue  # 제목에 이미 있는 수치는 큰 수치 블록으로 한 번 더 반복하지 않는다
            candidate = (_score(top), stat)
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best


def _settings(channel: ChannelConfig) -> dict[str, Any]:
    configured = channel.get("content.visual", {}) or {}
    return {**DEFAULTS, **configured}


def attach_visuals(carousel: Carousel, channel: ChannelConfig) -> int:
    """content 슬라이드에 visual 을 붙입니다(기존 visual 은 문구와 어긋나지 않게 항상 다시 계산).

    cover·closing 에는 붙이지 않고, 본문이 길어 글자가 이미 빽빽한 슬라이드도 건너뜁니다.
    같은 숫자가 반복되면 한 번만 씁니다. 붙인 개수를 반환합니다.
    """
    for slide in carousel.slides:
        slide.visual = {}
    settings = _settings(channel)
    if not bool(settings["enabled"]):
        return 0

    candidates: list[tuple[int, int, Slide, dict[str, Any]]] = []
    for slide in carousel.slides:
        if slide.type is not SlideType.CONTENT:
            continue
        if len(slide.body) > int(settings["max_body_lines"]):
            continue
        if sum(len(line) for line in slide.body) > int(settings["max_body_chars"]):
            continue
        found = _slide_candidate(slide)
        if found:
            candidates.append((found[0], -slide.index, slide, found[1]))

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    attached = 0
    seen: set[str] = set()
    for _, _, slide, visual in candidates:
        if attached >= int(settings["max_per_carousel"]):
            break
        key = visual.get("value") or f"{visual['before']['text']}>{visual['after']['text']}"
        if key in seen:
            continue
        seen.add(key)
        slide.visual = visual
        attached += 1
        logger.info("[visual] slide %02d → %s (%s)", slide.index, visual["type"], key)
    return attached


def visual_values(visual: dict[str, Any]) -> list[str]:
    """visual 이 화면에 그리는 숫자 문자열(슬라이드 문구의 부분 문자열이어야 하는 값)."""
    kind = visual.get("type")
    if kind == "stat":
        return [str(visual.get("value") or "")]
    if kind == "compare":
        return [
            str((visual.get("before") or {}).get("text") or ""),
            str((visual.get("after") or {}).get("text") or ""),
        ]
    return []


def ungrounded_values(slide: Slide) -> list[str]:
    """슬라이드 문구에 없는 visual 값 목록(손으로 고친 문구와 차트가 어긋난 경우를 잡는다)."""
    if not slide.visual:
        return []
    if slide.visual.get("type") not in {"stat", "compare"}:
        return [f"unknown visual type: {slide.visual.get('type')!r}"]
    text = "\n".join(slide.text_parts)
    values = visual_values(slide.visual)
    return [value for value in values if not value or value not in text]
