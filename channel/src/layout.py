"""슬라이드별 레이아웃·배경 테마를 고릅니다(코드 규칙, LLM 호출 없음).

모든 슬라이드가 "제목 + 줄 목록" 한 가지 틀이면 넘길 때 변화가 없어 정적으로 보입니다.
문구는 그대로 두고 **보여주는 방식**만 슬라이드의 역할(role)과 내용의 모양(줄 수·날짜 유무)으로
고릅니다. 결과는 렌더 때마다 다시 계산하며 ``carousel.json`` 의 공개 문구·승인 해시에는 영향을
주지 않습니다(``approval.content_hash`` 는 문구만 해시).

레이아웃 (``Slide`` 한 장의 본문 표현)

* ``default``  : 기존 "제목 + 대시 목록"
* ``numbered`` : 큰 ``01 02 03`` 번호 카드 (변화 목록 2~4줄)
* ``timeline`` : 세로선 + 점 (날짜가 든 줄이 3개 이상)
* ``takeaway`` : closing 의 takeaway 를 크게, "What to watch next" 는 체크 박스로

테마 (배경 리듬)

* ``dark``   : 기본 검정 바탕
* ``accent`` : 포인트 컬러로 한 장을 통째로 채움 — 기본은 표지(``cover_theme``), 표지를 채우지
  않는 설정이면 핵심 장("Why it matters")
* ``light``  : 흰 바탕 반전 — 장수가 많은 덱의 중간 한 장
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.core.errors import ContentOpsError

from channel.src.config import ChannelConfig
from channel.src.schema import Carousel, Slide, SlideType

LAYOUTS = {"default", "numbered", "timeline", "takeaway"}
THEMES = {"dark", "accent", "light"}

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # role(소문자)에 이 문구가 들어 있으면 포인트 컬러로 한 장을 채운다. 없으면 closing 을 채운다.
    "key_role": "why it matters",
    # 연달아 같은 레이아웃이 반복되지 않게 "Key detail 1" 은 기본 목록으로 둔다.
    "numbered_roles": ["what changed", "key detail 2"],
    "numbered_min_lines": 2,
    "numbered_max_lines": 4,
    "timeline_min_dates": 3,
    # 슬라이드 수 → 흰 바탕으로 반전할 슬라이드 번호 목록
    "light_slides": {7: [4]},
    "cover_theme": "dark",
    # 핵심 장("key_role")에 적용할 테마. 표지를 채우면 dark 로 두어 채움이 둘이 되지 않게 한다.
    "key_theme": "accent",
}

_DATE = re.compile(
    r"\d{1,2}월\s?\d{1,2}일"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{4}년\s?\d{1,2}월"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s\d{1,2}\b"
)


@dataclass(frozen=True)
class SlideStyle:
    """슬라이드 한 장의 표현 방식."""

    layout: str = "default"
    theme: str = "dark"


def _settings(channel: ChannelConfig) -> dict[str, Any]:
    configured = channel.get("content.layout", {}) or {}
    return {**DEFAULTS, **configured}


def _role(channel: ChannelConfig, carousel: Carousel, slide: Slide) -> str:
    try:
        return channel.template(carousel.template).role(slide.index).lower()
    except ContentOpsError:
        return ""


def _light_indexes(settings: dict[str, Any], slide_count: int) -> set[int]:
    raw = settings.get("light_slides") or {}
    for key, value in raw.items():
        if int(key) == slide_count:
            return {int(item) for item in value}
    return set()


def count_dates(lines: list[str]) -> int:
    """날짜 표현이 들어 있는 줄 수."""
    return sum(1 for line in lines if _DATE.search(line))


def plan_styles(carousel: Carousel, channel: ChannelConfig) -> dict[int, SlideStyle]:
    """슬라이드 번호 → :class:`SlideStyle`. 비활성이면 모두 기본값."""
    settings = _settings(channel)
    if not bool(settings["enabled"]):
        return {slide.index: SlideStyle() for slide in carousel.slides}

    key_role = str(settings["key_role"]).lower()
    numbered_roles = [str(role).lower() for role in settings["numbered_roles"]]
    light = _light_indexes(settings, carousel.slide_count)

    # 포인트 컬러로 채울 한 장: 역할이 맞는 content 슬라이드, 없으면 closing
    key_index: int | None = None
    for slide in carousel.slides:
        if slide.type is SlideType.CONTENT and key_role in _role(channel, carousel, slide):
            key_index = slide.index
            break
    if key_index is None and carousel.closing is not None:
        key_index = carousel.closing.index

    styles: dict[int, SlideStyle] = {}
    for slide in carousel.slides:
        if slide.type is SlideType.COVER:
            theme = str(settings["cover_theme"])
            styles[slide.index] = SlideStyle("default", theme if theme in THEMES else "dark")
            continue

        theme = "dark"
        if slide.index == key_index:
            key_theme = str(settings["key_theme"])
            theme = key_theme if key_theme in THEMES else "accent"
        elif slide.index in light:
            theme = "light"
        if slide.type is SlideType.CLOSING:
            styles[slide.index] = SlideStyle("takeaway", theme)
            continue

        lines = len(slide.body)
        layout = "default"
        if lines >= 3 and count_dates(slide.body) >= int(settings["timeline_min_dates"]):
            layout = "timeline"
        elif (
            any(role in _role(channel, carousel, slide) for role in numbered_roles)
            and int(settings["numbered_min_lines"]) <= lines <= int(settings["numbered_max_lines"])
        ):
            layout = "numbered"
        styles[slide.index] = SlideStyle(layout, theme)
    return styles
