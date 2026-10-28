"""슬라이드 레이아웃·테마 선택(channel/src/layout.py)과 렌더 HTML 테스트.

핵심 보장: 문구는 바뀌지 않고, 규칙이 같은 입력에는 같은 레이아웃을 고르며, 승인 해시에 영향이 없다.
"""

from __future__ import annotations

import copy
import json

import pytest

from channel.src.approval import content_hash
from channel.src.config import load_channel_config
from channel.src.layout import SlideStyle, count_dates, plan_styles
from channel.src.renderer import CarouselRenderer
from channel.src.schema import Carousel, Slide, SlideType


@pytest.fixture
def channel():
    return load_channel_config()


def deck(template: str, *bodies: list[str]) -> Carousel:
    """cover + content(bodies…) + closing 덱. content 슬라이드 수는 bodies 개수."""
    slides = [Slide(index=1, type=SlideType.COVER, headline="표지", subtitle="부제")]
    for position, body in enumerate(bodies, start=2):
        slides.append(Slide(index=position, type=SlideType.CONTENT, title="제목", body=list(body)))
    slides.append(
        Slide(
            index=len(bodies) + 2,
            type=SlideType.CLOSING,
            title="Takeaway",
            body=["핵심 한 줄"],
            extra_title="What to watch next",
            extra_body=["지켜볼 것 하나"],
        )
    )
    return Carousel(template=template, topic="AI", headline="표지", slides=slides)


STANDARD = ["첫째 사실", "둘째 사실", "셋째 사실"]


class TestPlanStyles:
    def test_standard_deck_roles(self, channel):
        # news_standard: 1 cover / 2 What happened / 3 What changed / 4 Why it matters / 5 closing
        carousel = deck("news_standard", STANDARD, STANDARD, STANDARD)
        styles = plan_styles(carousel, channel)
        assert styles[1] == SlideStyle("default", "accent")  # 표지는 포인트 컬러로 꽉 채움
        assert styles[2] == SlideStyle("default", "dark")  # What happened
        assert styles[3] == SlideStyle("numbered", "dark")  # What changed → 번호 카드
        assert styles[4] == SlideStyle("default", "dark")  # Why it matters (표지가 채움이라 dark)
        assert styles[5].layout == "takeaway"

    def test_only_the_cover_is_filled_with_accent(self, channel):
        carousel = deck("news_standard", STANDARD, STANDARD, STANDARD)
        styles = plan_styles(carousel, channel)
        assert [i for i, s in styles.items() if s.theme == "accent"] == [1]

    def test_key_slide_can_be_filled_instead_of_the_cover(self):
        """표지를 채우지 않는 설정이면 예전처럼 Why it matters 한 장을 채운다."""
        channel = load_channel_config()
        channel.content["layout"].update(cover_theme="dark", key_theme="accent")
        carousel = deck("news_standard", STANDARD, STANDARD, STANDARD)
        styles = plan_styles(carousel, channel)
        assert styles[1].theme == "dark" and styles[4].theme == "accent"
        short = deck("news_short", STANDARD)  # 역할에 Why it matters 가 없는 3장 덱 → closing
        assert plan_styles(short, channel)[3] == SlideStyle("takeaway", "accent")

    def test_short_deck_closing_stays_dark_when_cover_is_filled(self, channel):
        carousel = deck("news_short", STANDARD)
        assert plan_styles(carousel, channel)[3] == SlideStyle("takeaway", "dark")

    def test_deep_dive_gets_one_light_slide(self, channel):
        carousel = deck("deep_dive", *[STANDARD] * 5)
        styles = plan_styles(carousel, channel)
        assert [i for i, s in styles.items() if s.theme == "light"] == [4]
        assert styles[6].theme == "dark"  # Why it matters (표지가 채움)

    def test_deep_dive_rhythm_has_no_repeated_layout(self, channel):
        carousel = deck("deep_dive", *[STANDARD] * 5)
        styles = plan_styles(carousel, channel)
        layouts = [styles[i].layout for i in range(2, 7)]
        assert layouts == ["default", "numbered", "default", "numbered", "default"]
        assert all(a != b for a, b in zip(layouts, layouts[1:], strict=False))

    def test_numbered_needs_two_to_four_lines(self, channel):
        carousel = deck("news_standard", ["a", "b"], ["하나"] * 2, ["하나"] * 5)
        styles = plan_styles(carousel, channel)
        assert styles[3].layout == "numbered"
        carousel = deck("news_standard", ["a", "b"], ["하나"] * 5, ["x", "y"])
        assert plan_styles(carousel, channel)[3].layout == "default"  # 5줄은 번호 카드 제외

    def test_timeline_when_three_dated_lines(self, channel):
        dated = ["9월 3일 발표", "9월 10일 발행", "2026-10-06 발효"]
        carousel = deck("news_standard", dated, STANDARD, STANDARD)
        assert plan_styles(carousel, channel)[2].layout == "timeline"
        two = ["9월 3일 발표", "9월 10일 발행", "날짜 없음"]
        carousel = deck("news_standard", two, STANDARD, STANDARD)
        assert plan_styles(carousel, channel)[2].layout == "default"

    def test_count_dates(self):
        assert count_dates(["9월 3일", "Oct 6, 2026", "2026-10-06", "2026년 11월", "없음", "v0.40"]) == 4

    def test_disabled_gives_default_everywhere(self):
        channel = load_channel_config()
        channel.content["layout"]["enabled"] = False
        carousel = deck("news_standard", STANDARD, STANDARD, STANDARD)
        assert set(plan_styles(carousel, channel).values()) == {SlideStyle()}

    def test_cover_theme_is_configurable(self):
        channel = load_channel_config()
        channel.content["layout"]["cover_theme"] = "dark"
        carousel = deck("news_standard", STANDARD, STANDARD, STANDARD)
        assert plan_styles(carousel, channel)[1].theme == "dark"

    def test_deterministic(self, channel):
        carousel = deck("deep_dive", *[STANDARD] * 5)
        assert plan_styles(carousel, channel) == plan_styles(carousel, channel)


class TestRenderedHtml:
    def html(self, channel, carousel, index):
        return CarouselRenderer(channel)._slide_html(carousel.slides[index - 1], carousel)

    def test_style_classes_and_accent_theme_variables(self, channel):
        carousel = deck("news_standard", STANDARD, STANDARD, STANDARD)
        numbered = self.html(channel, carousel, 3)
        assert "slide--layout-numbered" in numbered and "slide--theme-dark" in numbered
        cover = self.html(channel, carousel, 1)
        assert "slide--theme-accent" in cover
        # 표지: 바탕이 포인트 컬러(AI 초록)이고 글자는 대비가 큰 검정
        assert "--bg: #3DDC84" in cover and "--fg: #0D0D0D" in cover

    def test_numbers_are_highlighted_without_changing_text(self, channel):
        carousel = deck("news_standard", ["속도 3.13x 향상, 9월 23일 발표"], STANDARD, STANDARD)
        html = self.html(channel, carousel, 2)
        assert '<mark class="hl">3.13x</mark>' in html
        assert "9월 23일" in html and '<mark class="hl">9월' not in html  # 날짜는 강조 안 함

    def test_highlight_escapes_html(self, channel):
        carousel = deck("news_standard", ["<b>5x</b> & 비교"], STANDARD, STANDARD)
        html = self.html(channel, carousel, 2)
        body = html.split("<main")[1].split("</main>")[0]
        assert "&lt;b&gt;" in body and "<b>" not in body

    def test_ghost_number_only_on_content_slides(self, channel):
        carousel = deck("news_standard", STANDARD, STANDARD, STANDARD)
        assert 'class="slide__ghost"' in self.html(channel, carousel, 2)
        assert 'class="slide__ghost"' not in self.html(channel, carousel, 1)
        assert 'class="slide__ghost"' not in self.html(channel, carousel, 5)

    def test_closing_extra_list_is_a_checklist(self, channel):
        carousel = deck("news_standard", STANDARD, STANDARD, STANDARD)
        html = self.html(channel, carousel, 5)
        assert "body-list--checklist" in html and "slide--layout-takeaway" in html

    def test_override_style_is_honored(self, channel):
        carousel = deck("news_standard", STANDARD, STANDARD, STANDARD)
        renderer = CarouselRenderer(channel)
        html = renderer._slide_html(carousel.slides[2], carousel, SlideStyle("default", "dark"))
        assert "slide--layout-default" in html

    def test_no_unreplaced_placeholders(self, channel):
        carousel = deck("deep_dive", *[STANDARD] * 5)
        for index in range(1, 8):
            html = self.html(channel, carousel, index)
            assert "{{" not in html.split("</style>")[1]


class TestApprovalIsUnaffected:
    def test_layout_and_render_fields_do_not_change_content_hash(self, channel, tmp_path):
        carousel = deck("news_standard", STANDARD, STANDARD, STANDARD)
        (tmp_path / "carousel.json").write_text(
            json.dumps(carousel.to_dict(), ensure_ascii=False), encoding="utf-8"
        )
        before = content_hash(tmp_path)

        decorated = copy.deepcopy(carousel)
        decorated.slides[2].visual = {"type": "stat", "value": "3x", "number": "3", "unit": "x"}
        decorated.slides[2].render = {"layout": "numbered", "theme": "dark", "fit": "ok"}
        (tmp_path / "carousel.json").write_text(
            json.dumps(decorated.to_dict(), ensure_ascii=False), encoding="utf-8"
        )
        assert content_hash(tmp_path) == before


class TestReaderAids:
    def html(self, channel, carousel, index):
        return CarouselRenderer(channel)._slide_html(carousel.slides[index - 1], carousel)

    def test_glossary_only_for_terms_present_in_the_slide(self, channel):
        carousel = deck("news_standard", ["allowlist 기반 접근 통제", "permissionless ledger"], STANDARD, STANDARD)
        main = self.html(channel, carousel, 2).split("<main")[1].split("</main>")[0]
        assert "<b>allowlist</b> =" in main and "<b>permissionless</b> =" in main
        assert "<b>venue</b>" not in main
        plain = self.html(channel, carousel, 3).split("<main")[1].split("</main>")[0]
        assert "aid--gloss" not in plain  # 용어가 없는 장에는 붙이지 않는다

    def test_glossary_matches_whole_words_and_is_capped(self, channel):
        carousel = deck("news_standard", ["permissioned permissionless allowlist venue"], STANDARD, STANDARD)
        main = self.html(channel, carousel, 2).split("<main")[1].split("</main>")[0]
        assert main.count("aid--gloss") == 2  # max_per_slide
        carousel = deck("news_standard", ["permissionlessness"], STANDARD, STANDARD)
        assert "aid--gloss" not in self.html(channel, carousel, 2).split("<main")[1]

    def test_glossary_can_be_disabled(self, channel):
        channel.content["glossary"]["enabled"] = False
        carousel = deck("news_standard", ["allowlist"], STANDARD, STANDARD)
        assert "aid--gloss" not in self.html(channel, carousel, 2).split("<main")[1]

    def test_save_cue_only_on_closing(self, channel):
        carousel = deck("news_standard", STANDARD, STANDARD, STANDARD)
        assert "aid--cta" in self.html(channel, carousel, 5).split("<main")[1]
        assert "aid--cta" not in self.html(channel, carousel, 2).split("<main")[1]

    def test_aids_do_not_change_content_hash_inputs(self, channel):
        carousel = deck("news_standard", ["allowlist"], STANDARD, STANDARD)
        before = carousel.to_dict()
        self.html(channel, carousel, 2)
        assert carousel.to_dict() == before  # 렌더가 carousel 을 바꾸지 않는다

    def test_closing_title_not_repeated_when_eyebrow_says_it(self, channel):
        carousel = deck("news_standard", STANDARD, STANDARD, STANDARD)
        # news_standard 5번 역할이 "Takeaway + Sources" 라 제목 "Takeaway" 는 eyebrow 와 겹친다.
        main = self.html(channel, carousel, 5).split("<main")[1].split("</main>")[0]
        assert 'class="t-title"' not in main
        carousel.slides[-1].title = "마무리 한 줄"
        assert 'class="t-title"' in self.html(channel, carousel, 5).split("<main")[1]


class TestTitleStyle:
    def issues(self, channel, titles):
        from channel.src.validator import CarouselValidator, ValidationReport

        slides = [Slide(index=1, type=SlideType.COVER, headline="표지", subtitle="부제")]
        for position, title in enumerate(titles, start=2):
            slides.append(Slide(index=position, type=SlideType.CONTENT, title=title, body=["a", "b"]))
        carousel = Carousel(template="news_short", topic="AI", headline="표지", slides=slides)
        report = ValidationReport()
        CarouselValidator(channel)._check_title_plain(carousel, report)
        return [f.code for f in report.findings]

    def test_plain_titles_pass_and_jargon_warns(self, channel):
        assert self.issues(channel, ["거래소 등록이 필요 없는 새 거래 장소", "SEC의 5년 조치"]) == []
        assert self.issues(channel, ["새 venue 유형, Tokenized Securities Venue"]) == [
            "title_jargon",
            "title_long",  # 31자
        ]
        assert self.issues(channel, ["allowlist 가 하는 일"]) == ["title_jargon"]  # 풀이 대상 용어

    def test_long_title_warns_and_switch_disables(self, channel):
        assert self.issues(channel, ["가" * 31]) == ["title_long"]
        channel.content["title_style"]["enabled"] = False
        assert self.issues(channel, ["새 venue 유형, Tokenized Securities Venue"]) == []


class TestSourcesBlock:
    def test_duplicate_source_names_are_shown_once(self, channel):
        from channel.src.schema import Source

        carousel = deck("news_standard", STANDARD, STANDARD, STANDARD)
        carousel.slides[-1].sources = [Source("Releases"), Source("huggingface.co"),
                                       Source("HuggingFace.co"), Source("huggingface.co")]
        html = CarouselRenderer(channel)._slide_html(carousel.slides[-1], carousel)
        assert html.count("<span>huggingface.co</span>") == 1 and "<span>Releases</span>" in html
        assert html.lower().count("huggingface.co</span>") == 1
