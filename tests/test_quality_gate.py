"""품질 게이트 강화: semantic duplicate, 재료 게이트, 문체, content_quality_score."""

from __future__ import annotations

import json
import pathlib

import pytest

from channel.src.config import load_channel_config
from channel.src.material import assess_material, enforce_template
from channel.src.quality import analyze_tone, compute_quality_score
from channel.src.schema import Carousel, Source, VerifiedTopic
from channel.src.similarity import (
    anchor_tokens,
    bullet_similarity,
    content_tokens,
    find_duplicates,
    used_facts,
)
from channel.src.validator import CarouselValidator
from tests.test_ra_glance import make_carousel, make_topic

CHANNEL_ROOT = pathlib.Path(__file__).resolve().parents[1] / "channel"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="module")
def channel():
    return load_channel_config()


class TestBulletSimilarity:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            # 실측 사례 (case4 deep_dive)
            (
                "Mainnet feature gate 3건이 이번 업데이트의 중심",
                "Mainnet feature gate 3건과 신규 클라이언트를 지금 확인",
            ),
            (
                "BPF Stake Program을 v5.1.0으로 업그레이드",
                "BPF Stake Program v5.1.0 마이그레이션 절차 확인",
            ),
            (
                "votor_transport: 마이그레이션 전 client 미활성화",
                "votor_transport의 마이그레이션 전 client 미활성화는 배포 순서에 영향",
            ),
        ],
    )
    def test_same_fact_different_wording_is_detected(self, a, b):
        assert bullet_similarity(a, b) >= 0.62, bullet_similarity(a, b)

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Rent를 바이트당 5080 lamports로 인하", "Slot time을 250ms로 단축"),
            ("lamports per byte를 6333으로 설정", "BPF Stake Program을 v5.1.0으로 상향"),
            ("대화 도중 97개 언어를 자동 감지해 전환", "준실시간 시각 입력으로 대화 맥락 보강"),
            ("ARM Validator 실행 지원", "program cache를 class 기반 메모리 할당으로 전환"),
        ],
    )
    def test_different_facts_are_not_flagged(self, a, b):
        assert bullet_similarity(a, b) < 0.62, bullet_similarity(a, b)

    def test_shared_numbers_raise_score(self):
        with_anchor = bullet_similarity(
            "lamports per byte 6333 적용", "lamports per byte 6333 변경 영향"
        )
        without = bullet_similarity(
            "lamports per byte 6333 적용", "lamports per byte 9999 변경 영향"
        )
        assert with_anchor > without

    def test_empty_input(self):
        assert bullet_similarity("", "무언가") == 0.0

    def test_token_helpers(self):
        assert "gemini" in content_tokens("Gemini 3.8 Live 공개")
        assert "v4.4.0-alpha.4" in anchor_tokens("Agave v4.4.0-alpha.4 배포")
        # 기능어는 제외
        assert "그리고" not in content_tokens("그리고 변경 사항")


class TestDuplicateReport:
    def test_detects_cross_slide_repetition(self):
        carousel = make_carousel()
        carousel.slides[2].body = list(carousel.slides[1].body)  # 같은 사실 복제
        report = find_duplicates(carousel)
        assert report.pairs
        assert report.blocks, "완전 동일 문장은 block 이어야 합니다"

    def test_same_slide_pairs_are_ignored(self):
        carousel = make_carousel()
        carousel.slides[1].body = ["동일한 문장 반복", "동일한 문장 반복"]
        report = find_duplicates(carousel)
        assert all(p.slide_a != p.slide_b for p in report.pairs)

    def test_clean_carousel_has_no_duplicates(self):
        report = find_duplicates(make_carousel())
        assert report.pairs == []
        assert report.uniqueness(10) == 1.0

    def test_uniqueness_drops_with_duplicates(self):
        carousel = make_carousel()
        carousel.slides[2].body = list(carousel.slides[1].body)
        report = find_duplicates(carousel)
        assert report.uniqueness(8) < 1.0

    def test_used_facts_collects_previous_slides(self):
        carousel = make_carousel()
        facts = used_facts(carousel, up_to_index=3)
        assert carousel.slides[1].body[0] in facts
        assert carousel.slides[3].body[0] not in facts

    def test_real_case4_duplicates_are_found(self):
        """이전 실측에서 6건이 보고된 deep_dive 케이스."""
        path = FIXTURES / "channel" / "2026-09-21-case4-bc-deepdive" / "carousel.json"
        if not path.exists():
            pytest.skip("case4 산출물이 없습니다")
        carousel = Carousel.from_dict(json.loads(path.read_text(encoding="utf-8")))
        report = find_duplicates(carousel)
        assert len(report.pairs) >= 6
        assert report.blocks


class TestValidatorGates:
    def _validate(self, channel, carousel, **kwargs):
        return CarouselValidator(channel).validate(carousel, **kwargs)

    def test_semantic_duplicate_blocks(self, channel):
        carousel = make_carousel()
        carousel.slides[2].body = list(carousel.slides[1].body)
        report = self._validate(channel, carousel, topic=make_topic())
        assert "semantic_duplicate" in {f.code for f in report.blocks}
        assert report.publishable is False

    def test_moderate_overlap_only_warns(self, channel):
        """0.62~0.75 구간은 경고, 0.75 이상은 block."""
        from channel.src.similarity import bullet_similarity

        a, b = "Preview 티어 한정 제공", "Preview 단계로 제공 범위 제한"
        score = bullet_similarity(a, b)
        assert 0.62 <= score < 0.75, f"테스트 전제 확인 필요: {score}"

        carousel = make_carousel()
        carousel.slides[1].body = [a, "다른 사실 하나"]
        carousel.slides[2].body = [b, "또 다른 사실"]
        report = self._validate(channel, carousel, topic=make_topic())
        dup = [f for f in report.findings if f.code == "semantic_duplicate"]
        assert dup, "중간 유사도가 보고되지 않았습니다"
        assert all(f.severity == "warn" for f in dup)

    def test_news_short_density_blocks(self, channel):
        carousel = make_carousel(slides=3, template="news_short")
        carousel.slides[1].body = ["Rent 5080 lamports", "Slot time 250ms"]  # 약 34자
        report = self._validate(channel, carousel, topic=make_topic())
        assert "news_short_density_below_minimum" in {f.code for f in report.blocks}

    def test_standard_density_only_warns(self, channel):
        carousel = make_carousel(slides=5, template="news_standard")
        carousel.slides[1].body = ["Rent 5080 lamports", "Slot time 250ms"]
        report = self._validate(channel, carousel, topic=make_topic())
        codes = {f.code for f in report.blocks}
        assert "news_short_density_below_minimum" not in codes
        assert "slide_too_sparse" in {f.code for f in report.warnings}

    def test_tone_inconsistency_warns(self, channel):
        carousel = make_carousel()
        carousel.slides[1].body = [
            "Gemini API로 제공됩니다",
            "Workspace에도 반영할 수 있습니다",
        ]
        report = self._validate(channel, carousel, topic=make_topic())
        assert "tone_inconsistency" in {f.code for f in report.warnings}
        assert report.tone.polite >= 2

    def test_nominal_tone_passes(self, channel):
        report = self._validate(channel, make_carousel(), topic=make_topic())
        assert report.tone.polite == 0
        assert "tone_inconsistency" not in {f.code for f in report.warnings}

    def test_density_variance_warns(self, channel):
        carousel = make_carousel()
        carousel.slides[1].body = ["짧음", "또 짧음"]
        carousel.slides[3].body = ["가" * 120, "나" * 120]
        report = self._validate(channel, carousel, topic=make_topic())
        assert "slide_density_variance" in {f.code for f in report.warnings}


class TestQualityScore:
    def test_components_present(self, channel):
        score = compute_quality_score(
            make_carousel(), channel=channel, topic=make_topic()
        )
        assert set(score.components) == {
            "factual_confidence",
            "source_diversity",
            "semantic_uniqueness",
            "information_density",
            "tone_consistency",
            "render_quality",
        }
        assert 0.0 <= score.score <= 1.0

    def test_verified_scores_higher_than_partially(self, channel):
        good = compute_quality_score(
            make_carousel(), channel=channel, topic=make_topic()
        )
        weak = compute_quality_score(
            make_carousel(),
            channel=channel,
            topic=make_topic(
                fact_check={"verdict": "partially_verified", "unverified_claims": ["a", "b"]}
            ),
        )
        assert good.components["factual_confidence"] > weak.components["factual_confidence"]

    def test_multi_source_scores_higher(self, channel):
        single = make_carousel()
        multi = make_carousel()
        multi.meta["sources"] = [
            {"name": "OpenAI News", "url": "https://openai.com/x", "authority": "primary"},
            {"name": "TechCrunch", "url": "https://tc.com/x", "authority": "secondary"},
        ]
        multi.slides[-1].sources = [
            Source(name="OpenAI News", url="https://openai.com/x"),
            Source(name="TechCrunch", url="https://tc.com/x"),
        ]
        a = compute_quality_score(single, channel=channel, topic=make_topic())
        b = compute_quality_score(multi, channel=channel, topic=make_topic())
        assert b.components["source_diversity"] > a.components["source_diversity"]

    def test_duplicates_lower_uniqueness(self, channel):
        carousel = make_carousel()
        carousel.slides[2].body = list(carousel.slides[1].body)
        duplicates = find_duplicates(carousel)
        score = compute_quality_score(
            carousel, channel=channel, topic=make_topic(), duplicates=duplicates
        )
        assert score.components["semantic_uniqueness"] < 1.0

    def test_polite_tone_lowers_score(self, channel):
        carousel = make_carousel()
        carousel.slides[1].body = ["제공됩니다", "반영할 수 있습니다"]
        tone = analyze_tone(carousel, channel)
        score = compute_quality_score(
            carousel, channel=channel, topic=make_topic(), tone=tone
        )
        assert score.components["tone_consistency"] <= 0.6

    def test_render_quality_zero_without_render(self, channel):
        score = compute_quality_score(make_carousel(), channel=channel, topic=make_topic())
        assert score.components["render_quality"] == 0.0

    def test_threshold_from_config(self, channel):
        score = compute_quality_score(make_carousel(), channel=channel, topic=make_topic())
        assert score.threshold == pytest.approx(
            float(channel.get("content.quality_score.publish_threshold"))
        )


def long_facts(count: int, *, chars: int = 150) -> list[dict[str, str]]:
    """실제 research 의 key_fact 와 비슷한 길이(80~150자)의 서로 다른 문장."""
    base = (
        "검증된 사실 문장으로 슬라이드 본문에 그대로 옮길 수 있을 만큼 구체적인 수치와 "
        "버전 정보를 포함한 설명이며 운영자가 확인해야 하는 내용입니다"
    )
    return [{"text": f"{base} — 항목 {i} ({'가' * max(0, chars - len(base) - 10)})"} for i in range(count)]


class TestMaterialGate:
    def test_rich_topic_allows_deep_dive(self, channel):
        """실측 기준: deep_dive 는 fact 10개 / 1500자 이상."""
        topic = make_topic(
            research={
                "what_changed": "Mainnet feature gate 3건 활성화와 클라이언트 릴리스 동시 반영",
                "why_it_matters": "노드 운영자는 저장 비용 회계와 스테이킹 동작 변경을 확인해야 합니다",
                "key_facts": long_facts(10),
                "technical_details": [f["text"] for f in long_facts(3)],
            }
        )
        assessment = assess_material(topic, channel)
        assert assessment.sufficient
        assert assessment.max_template == "deep_dive", assessment.to_dict()

    def test_thin_topic_is_insufficient(self, channel):
        topic = make_topic(
            research={"what_changed": "짧은 변경", "why_it_matters": "", "key_facts": []}
        )
        assessment = assess_material(topic, channel)
        assert not assessment.sufficient
        assert assessment.max_template is None
        assert "미달" in assessment.reason

    def test_unverified_facts_are_excluded(self, channel):
        claim = "이 수치는 원문에서 확인되지 않았다"
        topic = make_topic(
            research={
                "what_changed": "변경 내용이 충분히 긴 문장으로 작성되어 있습니다.",
                "why_it_matters": "운영자에게 주는 영향도 충분히 긴 문장입니다.",
                "key_facts": [{"text": claim}, {"text": "검증된 사실 하나의 충분히 긴 문장."}],
            },
            fact_check={"verdict": "verified", "unverified_claims": [claim]},
        )
        assessment = assess_material(topic, channel)
        assert all(claim not in fact for fact in assessment.facts)

    def test_enforce_template_shrinks_when_material_thin(self, channel):
        """planner 가 재료보다 큰 템플릿을 고르면 감당 가능한 최대치로 내린다."""
        topic = make_topic(
            research={
                "what_changed": "변경 내용을 담은 충분히 긴 문장으로 작성된 요약입니다",
                "why_it_matters": "영향도를 담은 충분히 긴 문장으로 작성된 설명입니다",
                "key_facts": long_facts(3),
            }
        )
        assessment = assess_material(topic, channel)
        assert assessment.sufficient and assessment.max_template != "deep_dive", assessment.to_dict()
        template, reason = enforce_template("deep_dive", assessment, channel)
        assert template != "deep_dive"
        assert reason and "축소" in reason

    def test_enforce_keeps_valid_choice(self, channel):
        topic = make_topic(
            research={
                "what_changed": "변경 내용을 담은 충분히 긴 문장으로 작성된 요약입니다",
                "why_it_matters": "영향도를 담은 충분히 긴 문장으로 작성된 설명입니다",
                "key_facts": long_facts(3),
            }
        )
        assessment = assess_material(topic, channel)
        assert assessment.sufficient, assessment.to_dict()
        template, reason = enforce_template("news_short", assessment, channel)
        assert template == "news_short"
        assert reason is None
