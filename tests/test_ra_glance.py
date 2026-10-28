"""R_A_Glance 채널 레이어 테스트.

핵심 검증 대상:

1. **의존 방향** — 공통 엔진(``src/``)이 ``R_A_Glance`` 를 import 하지 않는다
2. 채널 설정 일관성 (템플릿 장수 ↔ 허용 목록)
3. 스키마 왕복 변환과 엔진 출력 어댑터
4. 품질 게이트: 3/5/7 제약, 출처·헤드라인·캡션 필수, 내부 표현 유출 차단
5. 렌더러 HTML 조립 (백엔드 없이 검증 가능한 부분)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from channel.src.config import load_channel_config
from channel.src.editor import _strip_internal_language, sanitize_caption
from channel.src.inputs import (
    TopicInputError,
    load_topics,
    select_topic,
    topics_from_research_payload,
)
from channel.src.renderer import CarouselRenderer
from channel.src.schema import Carousel, Slide, SlideType, Source, VerifiedTopic
from channel.src.validator import CarouselValidator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHANNEL_ROOT = PROJECT_ROOT / "channel"


@pytest.fixture(scope="module")
def channel():
    return load_channel_config()


def make_topic(**overrides) -> VerifiedTopic:
    data = {
        "topic_id": "abc123",
        "category": "ai",
        "headline": "Gemini 3.8 Live 공개",
        "research": {
            "what_changed": "대화 중 tool 호출을 백그라운드로 실행한다.",
            "why_it_matters": "음성 Agent가 대기하지 않는다.",
            "key_facts": [{"text": "97개 언어 자동 전환"}, {"text": "Preview 한정 제공"}],
            "technical_details": ["Gemini API 제공"],
        },
        "fact_check": {"verdict": "verified", "unverified_claims": []},
        "sources": [Source(name="Google DeepMind Blog", url="https://deepmind.google/blog/x")],
    }
    data.update(overrides)
    return VerifiedTopic(**data)


def make_carousel(*, slides: int = 5, template: str = "news_standard") -> Carousel:
    """검증용 캐러셀. 슬라이드마다 **서로 다른** 사실을 넣습니다.

    같은 문장을 반복하면 semantic duplicate 검사가(정상적으로) BLOCK 을 냅니다.
    """
    facts = [
        ["Gemini API 제공 시작", "Workspace·Search 동시 반영"],
        ["97개 언어 자동 전환", "준실시간 시각 입력 지원"],
        ["tool call 백그라운드 실행", "응답 중단 감소"],
        ["Preview 티어 한정 제공", "구독 등급별 접근 차이"],
        ["ARM 빌드 추가", "디스크 대역폭 요구 증가"],
    ]
    items: list[Slide] = [
        Slide(
            index=1,
            type=SlideType.COVER,
            eyebrow="AI / GOOGLE DEEPMIND",
            headline="Gemini 3.8 Live 공개",
            subtitle="라이브 대화 모델 두 종 동시 공개.",
        )
    ]
    for position, index in enumerate(range(2, slides)):
        items.append(
            Slide(
                index=index,
                type=SlideType.CONTENT,
                title=f"내용 {index}",
                body=list(facts[position % len(facts)]),
            )
        )
    items.append(
        Slide(
            index=slides,
            type=SlideType.CLOSING,
            title="Takeaway",
            body=["음성 Agent 설계 시 두 변형 비교", "가용성 범위 사전 확인"],
            sources=[Source(name="Google DeepMind Blog", url="https://deepmind.google/blog/x")],
        )
    )
    return Carousel(
        template=template,
        topic="AI",
        headline="Gemini 3.8 Live 공개",
        slides=items,
        caption="음성 Agent가 멈추지 않는다.\n\n세 문장 정도의 본문.\n\nSources: Google DeepMind Blog",
        hashtags=["#AI", "#Gemini", "#LLM"],
        meta={"fact_check_verdict": "verified"},
    )


class TestDependencyDirection:
    """엔진 → 채널 역방향 import 금지 (설계 원칙의 기계적 검증)."""

    def test_engine_never_imports_channel(self):
        offenders: list[str] = []
        for path in (PROJECT_ROOT / "src").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "R_A_Glance" in text or "channel.src" in text:
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
        assert offenders == [], f"공통 엔진이 채널을 참조합니다: {offenders}"

    def test_engine_configs_have_no_brand_settings(self):
        for name in ("channels.yaml", "pipeline.yaml", "ranking.yaml", "sources.yaml"):
            text = (PROJECT_ROOT / "config" / name).read_text(encoding="utf-8")
            assert "R_A_Glance" not in text, f"{name} 에 채널 브랜드 설정이 섞였습니다"

    def test_channel_only_uses_engine_infrastructure(self):
        """채널은 엔진의 도메인 모델을 직접 import 하지 않는다(파일 계약만 사용)."""
        forbidden = ("src.core.models", "src.storage", "src.dedup", "src.ranking", "src.rag")
        offenders: list[str] = []
        for path in (CHANNEL_ROOT / "src").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for module in forbidden:
                if f"from {module}" in text or f"import {module}" in text:
                    offenders.append(f"{path.name} → {module}")
        assert offenders == [], f"채널이 엔진 내부 모듈에 의존합니다: {offenders}"


class TestChannelConfig:
    def test_loads(self, channel):
        assert channel.brand_name == "R_A_Glance"
        assert channel.canvas == (1080, 1350)
        assert channel.allowed_slide_counts == [3, 5, 7]

    def test_base_colors_and_category_accent(self, channel):
        colors = channel.get("brand.colors", {})
        assert colors["background"] == "#0D0D0D"
        assert colors["foreground"] == "#F5F5F5"
        # 포인트 컬러는 카테고리별 1색이며 default 가 반드시 있어야 한다
        assert {"ai", "blockchain", "default"} <= set(colors["accent"])
        # muted/hairline 은 foreground 투명도 변형이어야 하며 새 색이 아니다
        assert "245" in colors["muted"] and "245" in colors["hairline"]

    def test_templates_match_allowed_counts(self, channel):
        counts = sorted(spec.slides for spec in channel.templates.values())
        assert counts == [3, 5, 7]
        assert channel.default_template == "news_standard"

    def test_template_sections_are_complete(self, channel):
        for name, spec in channel.templates.items():
            assert len(spec.sections) == spec.slides, name
            assert spec.slide_type(1) == "cover", name
            assert spec.slide_type(spec.slides) == "closing", name

    def test_publish_switches_are_never_half_on(self, channel):
        """게시 스위치는 소유자가 한꺼번에 켠다(HANDOFF 26). 일부만 켜진 상태로 커밋되면 안 된다."""
        publish = channel.get("instagram.channel.publish") is True
        phase3 = int(channel.get("instagram.channel.phase")) == 3
        auto = channel.get("instagram.publishing.auto_publish") is True
        assert publish == phase3 == auto

    def test_internal_markers_present(self, channel):
        assert "확인 필요" in channel.internal_markers

    def test_invalid_config_raises(self, tmp_path, channel):
        from src.core.errors import ConfigError

        for name in ("brand.yaml", "instagram.yaml"):
            (tmp_path / name).write_text(
                (CHANNEL_ROOT / "config" / name).read_text(encoding="utf-8"), encoding="utf-8"
            )
        # 템플릿 장수를 허용 목록 밖으로 바꾸면 로드 실패
        (tmp_path / "content.yaml").write_text(
            "templates:\n  odd:\n    slides: 4\n    sections:\n"
            "      - {index: 1, type: cover}\n      - {index: 2, type: content}\n"
            "      - {index: 3, type: content}\n      - {index: 4, type: closing}\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            load_channel_config(tmp_path)


class TestSchema:
    def test_roundtrip(self):
        carousel = make_carousel()
        restored = Carousel.from_dict(json.loads(json.dumps(carousel.to_dict())))
        assert restored.slide_count == carousel.slide_count
        assert restored.headline == carousel.headline
        assert restored.sources[0].name == "Google DeepMind Blog"

    def test_cover_and_closing_lookup(self):
        carousel = make_carousel()
        assert carousel.cover is not None and carousel.cover.index == 1
        assert carousel.closing is not None and carousel.closing.index == 5

    def test_all_text_includes_caption_and_slides(self):
        carousel = make_carousel()
        text = carousel.all_text()
        assert "Gemini 3.8 Live 공개" in text
        assert "음성 Agent가 멈추지 않는다" in text

    def test_cover_serialization_omits_content_fields(self):
        data = make_carousel().slides[0].to_dict()
        assert "headline" in data and "title" not in data

    def test_verified_topic_accessors(self):
        topic = make_topic()
        assert topic.verdict == "verified"
        assert topic.key_facts == ["97개 언어 자동 전환", "Preview 한정 제공"]
        assert topic.primary_source.name == "Google DeepMind Blog"


class TestEngineInputAdapter:
    def test_maps_engine_payload(self):
        payload = {
            "items": [
                {
                    "article": {
                        "key": "k1",
                        "title": "엔진 헤드라인",
                        "url": "https://example.com/a",
                        "source": "Example Blog",
                        "category": "blockchain",
                    },
                    "research": {"what_changed": "바뀐 것", "key_facts": ["사실"]},
                    "fact_check": {"verdict": "verified"},
                }
            ]
        }
        topics = topics_from_research_payload(payload)
        assert len(topics) == 1
        topic = topics[0]
        assert topic.topic_id == "k1"
        assert topic.category == "blockchain"
        assert topic.sources[0].name == "Example Blog"

    def test_filters_unverified(self, tmp_path):
        payload = {
            "items": [
                {
                    "article": {"key": "a", "title": "A", "url": "u", "source": "S"},
                    "research": {"what_changed": "x"},
                    "fact_check": {"verdict": "unverified"},
                },
                {
                    "article": {"key": "b", "title": "B", "url": "u2", "source": "S"},
                    "research": {"what_changed": "y"},
                    "fact_check": {"verdict": "verified"},
                },
            ]
        }
        path = tmp_path / "research.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        topics, used = load_topics(path, root=tmp_path)
        assert [t.topic_id for t in topics] == ["b"]
        assert used == path

    def test_no_usable_topic_raises(self, tmp_path):
        path = tmp_path / "research.json"
        path.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "article": {"key": "a", "title": "A", "url": "u", "source": "S"},
                            "research": {},
                            "fact_check": {"verdict": "contradicted"},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(TopicInputError):
            load_topics(path, root=tmp_path)

    def test_select_topic_by_id_and_index(self):
        topics = [make_topic(topic_id="aaa"), make_topic(topic_id="bbb")]
        assert select_topic(topics).topic_id == "aaa"
        assert select_topic(topics, index=1).topic_id == "bbb"
        assert select_topic(topics, topic_id="bb").topic_id == "bbb"
        with pytest.raises(TopicInputError):
            select_topic(topics, topic_id="zzz")


class TestValidatorQualityGate:
    def _validate(self, channel, carousel, **kwargs):
        return CarouselValidator(channel).validate(carousel, **kwargs)

    def test_clean_carousel_only_lacks_render(self, channel):
        report = self._validate(channel, make_carousel(), topic=make_topic())
        codes = {f.code for f in report.blocks}
        # 렌더를 돌리지 않았으므로 이미지 누락만 BLOCK 이어야 한다
        assert codes == {"missing_slide_image"}

    def test_publishable_requires_quality_score(self, channel):
        """BLOCK 이 없어도 content_quality_score 가 낮으면 게시 불가."""
        report = self._validate(channel, make_carousel(), topic=make_topic())
        assert report.quality is not None
        report.findings = [f for f in report.findings if f.severity != "block"]
        report.quality.components["semantic_uniqueness"] = 0.0
        report.quality.components["information_density"] = 0.0
        report.quality.components["source_diversity"] = 0.0
        assert report.quality.score < report.quality.threshold
        assert report.publishable is False

    @pytest.mark.parametrize("count", [3, 5, 7])
    def test_allowed_slide_counts_pass(self, channel, count):
        template = {3: "news_short", 5: "news_standard", 7: "deep_dive"}[count]
        carousel = make_carousel(slides=count, template=template)
        report = self._validate(channel, carousel, topic=make_topic())
        assert "slide_count_not_allowed" not in {f.code for f in report.blocks}

    @pytest.mark.parametrize("count", [2, 4, 6, 8])
    def test_forbidden_slide_counts_block(self, channel, count):
        carousel = make_carousel(slides=count)
        report = self._validate(channel, carousel, topic=make_topic())
        assert "slide_count_not_allowed" in {f.code for f in report.blocks}

    def test_missing_headline_blocks(self, channel):
        carousel = make_carousel()
        carousel.slides[0].headline = ""
        carousel.headline = ""
        report = self._validate(channel, carousel, topic=make_topic())
        assert "missing_headline" in {f.code for f in report.blocks}

    def test_missing_source_blocks(self, channel):
        carousel = make_carousel()
        carousel.slides[-1].sources = []
        report = self._validate(channel, carousel, topic=make_topic())
        assert "missing_source" in {f.code for f in report.blocks}

    def test_missing_caption_blocks(self, channel):
        carousel = make_carousel()
        carousel.caption = ""
        report = self._validate(channel, carousel, topic=make_topic())
        assert "missing_caption" in {f.code for f in report.blocks}

    @pytest.mark.parametrize(
        "leak", ["확인 필요", "source truncated", "fact check needed", "TODO"]
    )
    def test_internal_language_leak_blocks(self, channel, leak):
        carousel = make_carousel()
        carousel.slides[1].body.append(f"이 수치는 {leak}.")
        report = self._validate(channel, carousel, topic=make_topic())
        assert "internal_language_leak" in {f.code for f in report.blocks}

    def test_unverified_verdict_blocks(self, channel):
        carousel = make_carousel()
        topic = make_topic(fact_check={"verdict": "unverified", "unverified_claims": []})
        report = self._validate(channel, carousel, topic=topic)
        assert "unresolved_critical_fact" in {f.code for f in report.blocks}

    def test_partially_verified_warns_only(self, channel):
        topic = make_topic(fact_check={"verdict": "partially_verified", "unverified_claims": []})
        report = self._validate(channel, make_carousel(), topic=topic)
        assert "partially_verified" in {f.code for f in report.warnings}
        assert "unresolved_critical_fact" not in {f.code for f in report.blocks}

    def test_single_source_warns(self, channel):
        report = self._validate(channel, make_carousel(), topic=make_topic())
        assert "single_source" in {f.code for f in report.warnings}

    def test_banned_phrase_warns(self, channel):
        carousel = make_carousel()
        carousel.slides[1].body.append("혁명적 변화입니다.")
        report = self._validate(channel, carousel, topic=make_topic())
        assert "banned_phrase" in {f.code for f in report.warnings}

    def test_url_on_slide_warns(self, channel):
        carousel = make_carousel()
        carousel.slides[1].body.append("https://example.com/detail 참고")
        report = self._validate(channel, carousel, topic=make_topic())
        assert "url_on_slide" in {f.code for f in report.warnings}

    def test_dense_slide_warns(self, channel):
        carousel = make_carousel()
        carousel.slides[1].body = ["가" * 120, "나" * 120, "다" * 120]
        report = self._validate(channel, carousel, topic=make_topic())
        assert "slide_too_dense" in {f.code for f in report.warnings}


class TestEditorSanitization:
    def test_strips_parenthetical_internal_note(self):
        text = "MilleMiglia 공개 (구체 URL은 소스에서 잘려 확인 필요)"
        cleaned = _strip_internal_language(text, ["확인 필요"])
        assert "확인 필요" not in cleaned
        assert "MilleMiglia 공개" in cleaned

    def test_strips_whole_sentence(self):
        text = "첫 문장입니다. 이 수치는 확인 필요. 마지막 문장입니다."
        cleaned = _strip_internal_language(text, ["확인 필요"])
        assert "확인 필요" not in cleaned
        assert "첫 문장입니다" in cleaned and "마지막 문장입니다" in cleaned

    def test_sanitize_caption_removes_banned(self, channel):
        caption, changes = sanitize_caption("혁명적 업데이트입니다. 내용.", channel)
        assert "혁명적" not in caption
        assert changes


class TestRendererHtml:
    def test_slide_html_contains_brand_values(self, channel):
        import re

        renderer = CarouselRenderer(channel)
        carousel = make_carousel()
        html = renderer._slide_html(carousel.slides[0], carousel)
        # 표지는 포인트 컬러 바탕 + 브랜드 검정 글자, 본문 슬라이드는 브랜드 검정 바탕 + 흰 글자
        assert "#0D0D0D" in html
        body = renderer._slide_html(carousel.slides[1], carousel)
        assert "#0D0D0D" in body and "#F5F5F5" in body
        assert "1080px" in html and "1350px" in html
        assert "Gemini 3.8 Live 공개" in html
        assert "R_A_Glance" in html
        # 치환되지 않은 플레이스홀더가 남아 있으면 안 된다
        # (템플릿 주석에는 설명용 {{...}} 표기가 있으므로 주석은 제외하고 검사)
        without_comments = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
        leftovers = re.findall(r"\{\{[A-Z_]+\}\}", without_comments)
        assert leftovers == [], f"치환되지 않은 placeholder: {leftovers}"

    def test_cover_line_break_is_preserved(self, channel):
        renderer = CarouselRenderer(channel)
        carousel = make_carousel()
        carousel.slides[0].headline = "첫 줄\n두 번째 줄"
        html = renderer._slide_html(carousel.slides[0], carousel)
        assert "<br />" in html

    def test_closing_slide_shows_source_names_without_urls(self, channel):
        renderer = CarouselRenderer(channel)
        carousel = make_carousel()
        html = renderer._slide_html(carousel.slides[-1], carousel)
        assert "Google DeepMind Blog" in html
        assert "https://deepmind.google" not in html

    def test_content_slide_has_no_sources_block(self, channel):
        renderer = CarouselRenderer(channel)
        carousel = make_carousel()
        html = renderer._slide_html(carousel.slides[1], carousel)
        # 인라인 CSS 에 .sources 규칙이 있으므로 렌더된 마크업만 확인한다
        assert 'class="slide__sources' not in html
        assert "Google DeepMind Blog" not in html

    def test_closing_sources_are_outside_main(self, channel):
        """출처는 main 밖에 있어야 takeaway 가 짧아도 하단 여백이 생기지 않는다."""
        renderer = CarouselRenderer(channel)
        carousel = make_carousel()
        html = renderer._slide_html(carousel.slides[-1], carousel)
        main_start = html.index('id="fit-target"')
        main_end = html.index("</main>")
        sources_at = html.index('class="slide__sources')
        assert sources_at > main_end, "출처 블록이 main 안에 있습니다"
        assert main_start < main_end

    def test_closing_variant_a_renders_secondary_block(self, channel):
        renderer = CarouselRenderer(channel)
        carousel = make_carousel()
        closing = carousel.slides[-1]
        closing.extra_title = "What to watch next"
        closing.extra_body = ["다음 릴리스의 기본값 변경 여부."]
        html = renderer._slide_html(closing, carousel)
        assert "closing-extra__label" in html
        assert "What to watch next" in html
        assert "다음 릴리스의 기본값 변경 여부." in html

    def test_html_escapes_user_text(self, channel):
        renderer = CarouselRenderer(channel)
        carousel = make_carousel()
        carousel.slides[1].title = "<script>alert(1)</script>"
        html = renderer._slide_html(carousel.slides[1], carousel)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html


class TestSampleOutput:
    """저장소에 커밋된 샘플 산출물이 스키마와 게이트를 통과하는지 확인."""

    @pytest.fixture
    def sample_dir(self) -> Path:
        candidates = sorted(
            path
            for path in (Path(__file__).resolve().parent / "fixtures" / "channel").glob(
                "*/carousel.json"
            )
        )
        if not candidates:
            pytest.skip("샘플 캐러셀이 없습니다")
        return candidates[-1].parent

    def test_sample_is_valid_and_rendered(self, channel, sample_dir):
        carousel = Carousel.from_dict(
            json.loads((sample_dir / "carousel.json").read_text(encoding="utf-8"))
        )
        assert carousel.slide_count in channel.allowed_slide_counts
        assert carousel.caption
        assert carousel.sources

        slides_dir = sample_dir / "slides"
        pngs = sorted(slides_dir.glob("*.png"))
        assert len(pngs) == carousel.slide_count
        for png in pngs:
            assert png.stat().st_size > 5000, f"{png.name} 이 비어 있습니다"

        assert (sample_dir / "preview.html").exists()
        assert (sample_dir / "caption.md").exists()
        assert (sample_dir / "review.md").exists()

    def test_sample_has_no_internal_language(self, channel, sample_dir):
        carousel = Carousel.from_dict(
            json.loads((sample_dir / "carousel.json").read_text(encoding="utf-8"))
        )
        public_text = carousel.all_text().lower()
        for marker in channel.internal_markers:
            assert marker.lower() not in public_text, f"공개 텍스트에 '{marker}' 유출"

    def test_caption_body_has_no_urls(self, sample_dir):
        caption = (sample_dir / "caption.md").read_text(encoding="utf-8")
        body = caption.split("<!-- metadata")[0]
        assert "http" not in body, "캡션 본문에 URL 이 있습니다"


class TestClosingVariants:
    """closing slide A/B 구성 실험의 규칙 검증."""

    def test_variant_a_has_secondary_label(self, channel):
        spec = channel.closing_spec("A")
        assert spec["primary_label"] == "Takeaway"
        assert spec["secondary_label"] == "What to watch next"

    def test_variant_b_is_removed(self, channel):
        """A/B 실측 후 B 는 deprecated — 설정에서 제거되었다."""
        variants = channel.get("content.closing.variants") or {}
        assert list(variants) == ["A"]
        assert channel.closing_variant == "A"
        # 알 수 없는 variant 를 물어도 안전한 기본값을 돌려준다
        fallback = channel.closing_spec("B")
        assert fallback["primary_label"] == "Takeaway"

    def test_sparse_closing_warns(self, channel):
        carousel = make_carousel()
        carousel.slides[-1].body = ["한 줄만 있는 takeaway."]
        carousel.slides[-1].extra_body = []
        report = CarouselValidator(channel).validate(carousel, topic=make_topic())
        assert "closing_sparse" in {f.code for f in report.warnings}

    def test_two_line_closing_does_not_warn(self, channel):
        carousel = make_carousel()
        carousel.slides[-1].body = ["첫 줄 결론.", "둘째 줄 제약."]
        report = CarouselValidator(channel).validate(carousel, topic=make_topic())
        assert "closing_sparse" not in {f.code for f in report.warnings}

    def test_secondary_block_counts_toward_fill(self, channel):
        carousel = make_carousel()
        carousel.slides[-1].body = ["한 줄 결론."]
        carousel.slides[-1].extra_title = "What to watch next"
        carousel.slides[-1].extra_body = ["다음에 볼 것."]
        report = CarouselValidator(channel).validate(carousel, topic=make_topic())
        assert "closing_sparse" not in {f.code for f in report.warnings}

    def test_extra_body_roundtrip(self):
        carousel = make_carousel()
        carousel.slides[-1].extra_title = "What to watch next"
        carousel.slides[-1].extra_body = ["a", "b"]
        restored = Carousel.from_dict(json.loads(json.dumps(carousel.to_dict())))
        assert restored.slides[-1].extra_body == ["a", "b"]
        assert restored.slides[-1].extra_title == "What to watch next"

    def test_extra_text_is_checked_for_internal_language(self, channel):
        carousel = make_carousel()
        carousel.slides[-1].extra_title = "What to watch next"
        carousel.slides[-1].extra_body = ["가격은 확인 필요."]
        report = CarouselValidator(channel).validate(carousel, topic=make_topic())
        assert "internal_language_leak" in {f.code for f in report.blocks}

    def test_sparse_content_slide_warns(self, channel):
        """항목 수는 채웠지만 글자가 적은 슬라이드도 경고해야 한다(실측: 48자)."""
        carousel = make_carousel()
        carousel.slides[1].body = ["Rent 5080 lamports", "Slot time 250ms"]
        report = CarouselValidator(channel).validate(carousel, topic=make_topic())
        sparse = [f for f in report.warnings if f.code == "slide_too_sparse"]
        assert sparse, "짧은 본문이 경고되지 않았습니다"


class TestSupportingSourceRelevance:
    """기사 페이지의 메뉴·푸터 문서 링크가 사건의 근거로 올라오지 않는다."""

    def test_unrelated_docs_links_do_not_support_the_event(self):
        from src.core.evidence import _supporting_sources

        article = "https://solana.com/news/stocks-sec-innovation-exemption"
        links = [
            "https://solana.com/docs/tools/actions\\",  # 메뉴 링크 + 끝의 이스케이프
            "https://solana.com/docs/core/accounts",
            "https://docs.example.com/tokenized-stocks/exemption",  # 주제가 겹침
        ]
        found = {s["url"]: s["supports_event"] for s in _supporting_sources(links, article)}
        assert found["https://solana.com/docs/tools/actions"] is False  # 백슬래시도 제거
        assert found["https://solana.com/docs/core/accounts"] is False
        assert found["https://docs.example.com/tokenized-stocks/exemption"] is True
