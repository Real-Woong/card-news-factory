"""숫자 차트 블록(channel/src/visual.py) 테스트.

핵심 보장: 차트의 모든 값은 슬라이드 문구의 부분 문자열이고, 문구에 없는 숫자는 만들어지지 않는다.
"""

from __future__ import annotations

import pytest

from channel.src.config import load_channel_config
from channel.src.renderer import CarouselRenderer
from channel.src.schema import Carousel, Slide, SlideType
from channel.src.validator import CarouselValidator
from channel.src.visual import attach_visuals, ungrounded_values


@pytest.fixture(scope="module")
def channel():
    return load_channel_config()


def content(index: int, *body: str, title: str = "제목") -> Slide:
    return Slide(index=index, type=SlideType.CONTENT, title=title, body=list(body))


def carousel_of(*slides: Slide) -> Carousel:
    cover = Slide(index=1, type=SlideType.COVER, headline="표지", subtitle="부제")
    closing = Slide(index=len(slides) + 2, type=SlideType.CLOSING, title="정리", body=["마무리"])
    items = [cover, *slides, closing]
    for position, slide in enumerate(items, start=1):
        slide.index = position
    return Carousel(template="news_standard", topic="AI", headline="표지", slides=items)


class TestExtraction:
    def test_stat_from_speedup(self, channel):
        carousel = carousel_of(content(2, "온디바이스 디코드 최대 3.13x 가속"))
        assert attach_visuals(carousel, channel) == 1
        visual = carousel.slides[1].visual
        assert visual == {"type": "stat", "value": "3.13x", "number": "3.13", "unit": "x"}

    def test_stat_percent_and_korean_unit(self, channel):
        carousel = carousel_of(
            content(2, "파라미터 8.9% 증가"), content(3, "처리량 2.5배 향상")
        )
        attach_visuals(carousel, channel)
        assert carousel.slides[1].visual["value"] == "8.9%"
        assert carousel.slides[2].visual["value"] == "2.5배"

    def test_compare_before_after(self, channel):
        carousel = carousel_of(content(2, "지연 시간 340ms에서 120ms로 단축"))
        attach_visuals(carousel, channel)
        visual = carousel.slides[1].visual
        assert visual["type"] == "compare"
        assert visual["before"] == {"text": "340ms", "number": 340.0}
        assert visual["after"] == {"text": "120ms", "number": 120.0}

    def test_compare_requires_same_unit_and_connector(self, channel):
        mixed = carousel_of(content(2, "지연 340ms에서 3.1x 로 개선"))
        attach_visuals(mixed, channel)
        assert mixed.slides[1].visual["type"] == "stat"  # compare 가 아니라 첫 숫자 stat

        no_connector = carousel_of(content(2, "340ms 그리고 매우 긴 설명이 이어진 뒤 120ms"))
        attach_visuals(no_connector, channel)
        assert no_connector.slides[1].visual["type"] == "stat"

    @pytest.mark.parametrize(
        "line",
        [
            "2026년 9월 23일 발효",
            "9월 3일자 이슈를 9월 10일 발행",
            "Ollama v0.40.0 출시",
            "GPT-5 와 Claude 4 비교",
            "Solana 빌더에게 설계 과제",
            "17명이 참여",
        ],
    )
    def test_dates_versions_and_names_are_ignored(self, channel, line):
        carousel = carousel_of(content(2, line))
        assert attach_visuals(carousel, channel) == 0
        assert carousel.slides[1].visual == {}


class TestDurationsAndPriority:
    def test_duration_is_a_stat_but_dates_are_not(self, channel):
        carousel = carousel_of(content(2, "2026년 9월 23일 발효된 SEC의 5년 order"))
        attach_visuals(carousel, channel)
        assert carousel.slides[1].visual["value"] == "5년"

    @pytest.mark.parametrize("text", ["17개월 논의 반영", "pool은 24시간 가동", "출시 30일 전 공지"])
    def test_other_durations(self, channel, text):
        carousel = carousel_of(content(2, text))
        attach_visuals(carousel, channel)
        assert carousel.slides[1].visual["type"] == "stat"

    def test_salient_unit_wins_within_a_line(self, channel):
        carousel = carousel_of(content(2, "약 280M 파라미터 추가, 타깃 대비 8.9% 증가"))
        attach_visuals(carousel, channel)
        assert carousel.slides[1].visual["value"] == "8.9%"

    def test_performance_number_beats_duration_across_slides(self, channel):
        carousel = carousel_of(
            content(2, "5년 order 발효"),
            content(3, "속도 3x 향상"),
            content(4, "24시간 가동"),
            content(5, "정확도 12% 상승"),
        )
        attach_visuals(carousel, channel)
        values = sorted(s.visual["value"] for s in carousel.slides if s.visual)
        assert values == ["12%", "3x"]


class TestSelection:
    def test_cover_and_closing_never_get_visuals(self, channel):
        carousel = carousel_of(content(2, "속도 3x 향상"))
        carousel.slides[0].subtitle = "최대 9x 빠름"
        carousel.slides[-1].body = ["효율 40% 개선"]
        attach_visuals(carousel, channel)
        assert carousel.slides[0].visual == {} and carousel.slides[-1].visual == {}

    def test_dense_slide_is_skipped(self, channel):
        dense = content(2, "항목 하나 3.13x", "항목 둘", "항목 셋", "항목 넷", "항목 다섯")
        carousel = carousel_of(dense)
        assert attach_visuals(carousel, channel) == 0

    def test_max_per_carousel_and_duplicate_values(self, channel):
        carousel = carousel_of(
            content(2, "속도 3x 향상"),
            content(3, "정확도 12% 상승"),
            content(4, "비용 70% 절감"),
            content(5, "다시 속도 3x 향상"),
        )
        count = attach_visuals(carousel, channel)
        assert count == 2  # content.visual.max_per_carousel
        values = [s.visual["value"] for s in carousel.slides if s.visual]
        assert len(set(values)) == len(values)

    def test_recomputed_each_time_and_clears_stale(self, channel):
        carousel = carousel_of(content(2, "속도 3x 향상"))
        attach_visuals(carousel, channel)
        carousel.slides[1].body = ["숫자 없는 문장"]  # 문구를 고친 뒤 재렌더하는 경우
        attach_visuals(carousel, channel)
        assert carousel.slides[1].visual == {}

    def test_disabled_by_config(self):
        channel = load_channel_config()
        channel.content["visual"]["enabled"] = False
        carousel = carousel_of(content(2, "속도 3x 향상"))
        assert attach_visuals(carousel, channel) == 0


class TestGroundingGuard:
    def test_values_are_substrings_of_slide_text(self, channel):
        carousel = carousel_of(
            content(2, "지연 340ms에서 120ms로 단축"), content(3, "처리량 2.5배 향상")
        )
        attach_visuals(carousel, channel)
        for slide in carousel.slides:
            assert ungrounded_values(slide) == []

    def test_edited_text_makes_visual_ungrounded(self, channel):
        slide = content(2, "속도 3x 향상")
        carousel = carousel_of(slide)
        attach_visuals(carousel, channel)
        slide.body = ["속도 향상"]  # 손으로 문구를 고쳤지만 visual 은 남은 경우
        assert ungrounded_values(slide) == ["3x"]

    def test_validator_blocks_ungrounded_visual(self, channel):
        slide = content(2, "속도 향상")
        slide.visual = {"type": "stat", "value": "99x", "number": "99", "unit": "x"}
        carousel = carousel_of(slide)
        report = CarouselValidator(channel).validate(carousel)
        codes = [finding.code for finding in report.blocks]
        assert "visual_ungrounded" in codes

    def test_unknown_visual_type_is_flagged(self):
        slide = content(2, "문장")
        slide.visual = {"type": "pie"}
        assert ungrounded_values(slide)


class TestRenderAndSchema:
    def test_roundtrip(self, channel):
        carousel = carousel_of(content(2, "속도 3x 향상"))
        attach_visuals(carousel, channel)
        restored = Carousel.from_dict(carousel.to_dict())
        assert restored.slides[1].visual == carousel.slides[1].visual
        assert "visual" not in carousel.slides[0].to_dict()

    def test_stat_html_uses_accent_variable_and_values(self, channel):
        carousel = carousel_of(content(2, "속도 3.13x 향상"))
        attach_visuals(carousel, channel)
        html = CarouselRenderer(channel)._slide_html(carousel.slides[1], carousel)
        assert 'class="visual__number">3.13<' in html
        assert 'class="visual__unit">x<' in html
        assert "--accent: #3DDC84" in html  # AI 카테고리 포인트 컬러(초록)

    def test_compare_html_bar_widths_are_proportional(self, channel):
        carousel = carousel_of(content(2, "지연 340ms에서 120ms로 단축"))
        attach_visuals(carousel, channel)
        html = CarouselRenderer(channel)._slide_html(carousel.slides[1], carousel)
        assert "width:100.0%" in html  # 큰 값이 기준
        assert "width:35.3%" in html  # 120/340
        assert "340ms" in html and "120ms" in html

    def test_no_placeholder_left_and_no_visual_html_without_visual(self, channel):
        carousel = carousel_of(content(2, "숫자 없는 문장"))
        attach_visuals(carousel, channel)
        html = CarouselRenderer(channel)._slide_html(carousel.slides[1], carousel)
        assert '<div class="visual ' not in html
        assert "{{" not in html.split("</style>")[1]


def test_stat_already_in_the_title_is_not_repeated():
    from channel.src.visual import _slide_candidate

    slide = Slide(index=2, type=SlideType.CONTENT, title="배경: 5년 order", body=["SEC의 5년 order"])
    assert _slide_candidate(slide) is None
    slide = Slide(index=2, type=SlideType.CONTENT, title="배경", body=["SEC의 5년 order"])
    assert _slide_candidate(slide) is not None
