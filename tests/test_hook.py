"""표지 제목 후보(channel/src/hook.py)와 표지 디자인 테스트.

핵심 보장: 어그로는 사실의 범위 안에서만 — 근거에 없는 숫자·과장 표현은 후보에서 탈락하고,
생성이 실패해도 파이프라인은 멈추지 않으며, 고른 제목은 carousel.json 에만 반영된다.
"""

from __future__ import annotations

import json

import pytest

from src.agents.llm import LLMClient, LLMRequest, LLMResponse
from src.core.errors import ContentOpsError

from channel.src.config import load_channel_config
from channel.src.hook import (
    HookOption,
    InstagramHookWriter,
    apply_choice,
    check_option,
    clean_options,
    preview_html,
    review_lines,
)
from channel.src.renderer import CarouselRenderer
from channel.src.schema import Carousel, Slide, SlideType


@pytest.fixture
def channel():
    return load_channel_config()


def make_carousel(**meta) -> Carousel:
    slides = [
        Slide(
            index=1,
            type=SlideType.COVER,
            headline="주식이 이제\n블록체인에서 거래된다",
            subtitle="부제",
        ),
        Slide(
            index=2,
            type=SlideType.CONTENT,
            title="무엇이 바뀌었나",
            body=["2026년 9월 23일 발효된 SEC의 5년 order", "거래소 등록 없이 거래"],
        ),
        Slide(index=3, type=SlideType.CLOSING, title="Takeaway", body=["핵심"]),
    ]
    return Carousel(
        template="news_short", topic="BLOCKCHAIN", headline="주식이 이제 블록체인에서 거래된다",
        slides=slides, meta=meta,
    )


EVIDENCE = "SEC order 5년 발효 2026년 9월 23일 거래소 등록 없이 24시간 거래"


class TestCheckOption:
    def check(self, channel, headline):
        return check_option(HookOption("question", headline), channel=channel, evidence=EVIDENCE)

    def test_clean_option_passes(self, channel):
        assert self.check(channel, "거래소 없이 주식을 산다고?") == []

    def test_number_must_be_in_evidence(self, channel):
        assert self.check(channel, "5년짜리 허가: 주식이 블록체인으로") == []
        issues = self.check(channel, "9년짜리 허가: 주식이 블록체인으로")
        assert any("근거에 없는 숫자" in issue for issue in issues)

    @pytest.mark.parametrize("word", ["충격", "폭등", "대박", "무조건", "지금 사야", "100%"])
    def test_hype_words_are_rejected(self, channel, word):
        assert any("금지어" in issue for issue in self.check(channel, f"{word} 주식 거래 시작"))

    def test_length_lines_url_and_internal_terms(self, channel):
        assert any("자를 넘습니다" in i for i in self.check(channel, "가" * 90))
        assert any("3줄" in i for i in self.check(channel, "가\n나\n다\n라"))
        assert any("URL" in i for i in self.check(channel, "자세히 https://x.com"))
        marker = channel.internal_markers[0]
        assert any("내부 표현" in i for i in self.check(channel, f"주식 {marker} 거래"))

    def test_empty_headline(self, channel):
        assert self.check(channel, "") == ["제목이 비어 있습니다"]


class TestCleanOptions:
    def clean(self, channel, raw):
        return clean_options(raw, channel=channel, carousel=make_carousel(), topic=None)

    def test_keeps_valid_one_per_style(self, channel):
        raw = [
            {"style": "question", "headline": "거래소 없이 주식을 산다고?"},
            {"style": "question", "headline": "중복 스타일은 버린다?"},
            {"style": "number", "headline": "5년짜리 허가: 주식이 블록체인으로"},
            {"style": "contrast", "headline": "거래소 없이도 주식이 거래된다"},
            {"style": "weird", "headline": "알 수 없는 스타일"},
        ]
        kept = self.clean(channel, raw)
        assert [o.style for o in kept] == ["question", "number", "contrast"]

    def test_invalid_options_are_dropped_not_fixed(self, channel):
        raw = [
            {"style": "number", "headline": "77년짜리 허가"},  # 근거에 없는 숫자
            {"style": "question", "headline": "충격! 주식 거래?"},  # 금지어
            {"style": "contrast", "headline": "주식이 이제 블록체인에서 거래된다"},  # 현재 제목과 같음
        ]
        assert self.clean(channel, raw) == []

    @pytest.mark.parametrize("raw", [None, "text", {"a": 1}, [1, "x", None]])
    def test_garbage_is_ignored(self, channel, raw):
        assert self.clean(channel, raw) == []


class FakeLLM(LLMClient):
    provider = "fake"

    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text=self.text, provider=self.provider)


class TestWriter:
    def topic(self):
        from channel.src.schema import VerifiedTopic

        return VerifiedTopic(
            topic_id="t",
            category="blockchain",
            headline="h",
            research={
                "key_facts": [{"text": "5년 order"}],
                "what_changed": "x",
                "why_it_matters": "y",
            },
        )

    def test_generates_validated_options(self, config, channel):
        payload = {"options": [
            {"style": "question", "headline": "거래소 없이 주식을 산다고?"},
            {"style": "number", "headline": "88년 허가"},
        ]}
        llm = FakeLLM(json.dumps(payload, ensure_ascii=False))
        writer = InstagramHookWriter(config=config, llm=llm, channel=channel)
        options = writer.run(self.topic(), make_carousel())
        assert [o.style for o in options] == ["question"]  # 근거 없는 숫자 후보는 탈락
        assert llm.requests[0].task == "ig_hook"

    def test_llm_failure_is_not_fatal(self, config, channel):
        llm = FakeLLM("json 이 아닌 응답")
        writer = InstagramHookWriter(config=config, llm=llm, channel=channel)
        assert writer.run(self.topic(), make_carousel()) == []

    def test_prompt_file_exists_and_states_the_rules(self, config, channel):
        writer = InstagramHookWriter(config=config, llm=FakeLLM("{}"), channel=channel)
        prompt = writer.system_prompt
        assert "facts" in prompt and "과장 금지" in prompt and "정확성" in prompt


class TestApplyChoice:
    def write(self, tmp_path, options=True):
        carousel = make_carousel(
            hook_options=[
                {"style": "question", "headline": "거래소 없이\n주식을 산다고?", "issues": []},
                {"style": "contrast", "headline": "거래소 없이도 주식이 거래된다", "issues": []},
            ]
            if options
            else []
        )
        path = tmp_path / "carousel.json"
        path.write_text(json.dumps(carousel.to_dict(), ensure_ascii=False), encoding="utf-8")
        return path

    def test_choose_by_style_and_number(self, tmp_path):
        path = self.write(tmp_path)
        assert apply_choice(path, "question") == "거래소 없이\n주식을 산다고?"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["slides"][0]["headline"] == "거래소 없이\n주식을 산다고?"
        assert data["headline"] == "거래소 없이 주식을 산다고?"
        assert data["meta"]["hook_choice"]["style"] == "question"
        assert data["meta"]["hook_choice"]["previous"].startswith("주식이 이제")
        assert apply_choice(path, "2") == "거래소 없이도 주식이 거래된다"

    def test_only_the_cover_headline_changes(self, tmp_path):
        path = self.write(tmp_path)
        before = json.loads(path.read_text(encoding="utf-8"))
        apply_choice(path, "contrast")
        after = json.loads(path.read_text(encoding="utf-8"))
        assert before["slides"][1:] == after["slides"][1:]
        assert before["caption"] == after["caption"]

    def test_errors(self, tmp_path):
        with pytest.raises(ContentOpsError, match="후보가 없습니다"):
            apply_choice(self.write(tmp_path, options=False), "question")
        with pytest.raises(ContentOpsError, match="알 수 없는 후보"):
            apply_choice(self.write(tmp_path), "nope")
        with pytest.raises(ContentOpsError, match="읽을 수 없습니다"):
            apply_choice(tmp_path / "없음.json", "question")


class TestReviewSurfaces:
    def carousel(self):
        return make_carousel(
            hook_options=[{"style": "question", "headline": "거래소 없이 주식을 산다고?", "issues": []}]
        )

    def test_review_md_lists_options_with_commands(self):
        text = "\n".join(review_lines(self.carousel(), "2026-10-17-9"))
        assert "표지 제목 후보" in text and "질문형" in text
        assert "choose-hook 2026-10-17-9 question" in text and "grounding check" in text

    def test_no_options_means_no_section(self):
        assert review_lines(make_carousel(), "x") == []
        assert preview_html(make_carousel(), "x") == ""

    def test_preview_escapes_html(self):
        options = [{"style": "question", "headline": "<b>x</b>?", "issues": []}]
        carousel = make_carousel(hook_options=options)
        html = preview_html(carousel, "id")
        assert "&lt;b&gt;" in html and "<b>x" not in html


class TestCoverDesign:
    def html(self, channel, carousel=None):
        carousel = carousel or make_carousel()
        return CarouselRenderer(channel)._slide_html(carousel.slides[0], carousel)

    def test_cover_has_logo_art_cue_and_slide_count(self, channel):
        html = self.html(channel)
        main = html.split("<main")[1].split("</main>")[0]
        assert "cover__art--logo" in main and "mask-image:url('data:image/png" in main
        assert "넘겨서 보기 · 3장 →" in main

    def test_cover_cue_can_be_disabled(self, channel):
        channel.content["cover"]["cue"] = ""
        assert "cover__cue" not in self.html(channel).split("</style>")[1]

    def test_only_the_cover_gets_art_and_cue(self, channel):
        carousel = make_carousel()
        body_html = CarouselRenderer(channel)._slide_html(carousel.slides[1], carousel)
        assert "cover__art" not in body_html.split("</style>")[1].split("<main")[1]
        assert "cover__cue" not in body_html.split("</style>")[1]

    def test_cover_subtitle_budget_is_one_line(self, channel):
        assert int(channel.get("content.text_budget.cover.subtitle_max_chars")) <= 40

    def test_art_is_in_flow_before_the_headline_so_text_cannot_overlap(self, channel):
        main = self.html(channel).split("<main")[1].split("</main>")[0]
        assert main.index("cover__art") < main.index("t-headline") < main.index("t-subtitle")
        base = load_channel_config().templates_dir / "shared" / "base.css"
        css = base.read_text(encoding="utf-8")
        art = css[css.index(".cover__art {") : css.index(".cover__art svg")]
        assert "flex: 1 1 0" in art and "min-height" in art  # 남는 높이를 차지, 최소 높이 보장
        assert "position: absolute" not in art  # 글자 위에 겹쳐 그리는 장식이 아니다

    def test_every_category_has_an_existing_cover_logo(self, channel):
        logos = channel.get("brand.cover_logos")
        accents = channel.get("brand.colors.accent")
        for key in accents:
            assert key in logos, key
        for key, rel in logos.items():
            assert (channel.root / rel).is_file(), (key, rel)

    def test_missing_logo_falls_back_to_drawing(self, channel):
        channel.content  # 설정 객체는 그대로 두고 경로만 바꾼다
        channel.brand["cover_logos"] = {"default": "없는파일.png"}
        main = self.html(channel).split("<main")[1].split("</main>")[0]
        assert "<svg" in main and main.count("<path") == 3

    def test_art_uses_theme_color_not_hardcoded_colors(self, channel):
        channel.brand["cover_logos"] = {}
        main = self.html(channel).split("<main")[1].split("</main>")[0]
        art = main[main.index("cover__art") : main.index("</svg>")]
        assert 'stroke="currentColor"' in art and "#" not in art
