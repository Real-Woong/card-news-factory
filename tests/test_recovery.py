"""중복 수리 후 밀도 회복·템플릿 축소·cover 우선 수리 회귀 테스트."""

from __future__ import annotations

import pytest

from channel.src.config import load_channel_config
from channel.src.editor import InstagramEditor
from channel.src.planner import build_template_plan
from channel.src.material import assess_material
from channel.src.pipeline import _recheck_duplicates_after_refill
from channel.src.recovery import (
    DensityRecoveryState,
    REASON_INSUFFICIENT_UNIQUE_MATERIAL,
    REASON_THIN_AFTER_REPAIR,
    cover_body_duplicates,
    safe_downgrade_target,
    thin_content_slides,
)
from channel.src.similarity import DuplicatePair
from channel.src.validator import CarouselValidator
from tests.test_ra_glance import make_carousel, make_topic


@pytest.fixture(scope="module")
def channel():
    return load_channel_config()


def editor_with_response(channel, response) -> InstagramEditor:
    editor = object.__new__(InstagramEditor)
    editor.channel = channel
    editor.ask_json = lambda *_args, **_kwargs: response
    return editor


def distinct_facts(count: int, chars: int) -> list[str]:
    facts = []
    for index in range(count):
        prefix = f"Subsystem{index} verified change {index * 17 + 3} "
        facts.append(prefix + chr(0xAC00 + index) * max(0, chars - len(prefix)))
    return facts


def test_duplicate_repair_can_create_thin_slide(channel):
    carousel = make_carousel()
    pair = DuplicatePair(
        score=0.91,
        slide_a=2,
        slide_b=3,
        text_a=carousel.slides[1].body[0],
        text_b=carousel.slides[2].body[0],
        severity="block",
    )
    editor = editor_with_response(
        channel,
        {"slides": [{"index": 3, "body": ["중복 삭제 후 짧음"]}], "changes": []},
    )

    repaired = editor.repair_duplicates(carousel, [pair], unused_material=[])

    assert 3 in thin_content_slides(repaired, channel)


def test_density_recovery_uses_traced_unused_verified_fact(channel):
    carousel = make_carousel()
    carousel.slides[1].body = ["짧은 문장"]
    for slide in carousel.slides[2:-1]:
        slide.body = [
            "다른 검증 사실을 충분한 분량으로 설명하는 문장 " * 2,
            "운영 영향과 제약 조건을 구체적으로 정리한 문장 " * 2,
        ]
    source_a = (
        "원문에서 검증된 미사용 사실 A — "
        "운영 전 설정 전환 절차와 제약을 구체적으로 설명"
    )
    source_b = (
        "원문에서 검증된 미사용 사실 B — "
        "배포 후 모니터링할 지표와 적용 범위를 구체적으로 설명"
    )
    editor = editor_with_response(
        channel,
        {
            "slides": [
                {
                    "index": 2,
                    "title": "운영 전후 확인 지점",
                    "body": [
                        {
                            "text": (
                                "운영 전 설정 전환 절차와 적용 제약을 원문 기준으로 "
                                "점검하고 배포 순서와 담당 범위를 함께 정리"
                            ),
                            "source_fact": source_a,
                        },
                        {
                            "text": (
                                "배포 후 지표 모니터링과 적용 범위를 함께 확인해 "
                                "변경 영향을 분리하고 회귀 기준을 명시"
                            ),
                            "source_fact": source_b,
                        },
                    ],
                }
            ]
        },
    )

    recovered = editor.recover_thin_slides(
        carousel,
        [2],
        unused_material=[source_a, source_b],
    )

    assert thin_content_slides(recovered, channel) == []
    assert recovered.slides[1].body[0] == "짧은 문장"
    assert recovered.slides[1].body[1].startswith("운영 전 설정")


def test_safe_downgrade_deep_dive_to_standard_builds_clean_plan(channel):
    target, unique = safe_downgrade_target(
        "deep_dive", distinct_facts(8, 100), channel
    )
    plan = build_template_plan(channel, target, reason="template_downgraded")
    assert target == "news_standard"
    assert len(unique) == 8
    assert plan.slides == 5
    assert [entry["index"] for entry in plan.slide_plan] == [1, 2, 3, 4, 5]


def test_safe_downgrade_standard_to_short_builds_clean_plan(channel):
    target, unique = safe_downgrade_target(
        "news_standard", distinct_facts(5, 100), channel
    )
    plan = build_template_plan(channel, target, reason="template_downgraded")
    assert target == "news_short"
    assert len(unique) == 5
    assert plan.slides == 3
    assert [entry["type"] for entry in plan.slide_plan] == ["cover", "content", "closing"]


def test_downgrade_refused_when_current_template_material_is_sufficient(channel):
    target, unique = safe_downgrade_target(
        "deep_dive", distinct_facts(10, 160), channel
    )
    assert target is None
    assert len(unique) == 10


def test_unresolved_thin_slide_blocks_publishing(channel):
    carousel = make_carousel()
    carousel.slides[1].body = ["회복 불가"]
    carousel.meta["density_recovery"] = {
        "attempted": True,
        "thin_after": [2],
        "template_downgraded": None,
        "reasons": [
            REASON_THIN_AFTER_REPAIR,
            REASON_INSUFFICIENT_UNIQUE_MATERIAL,
        ],
    }

    report = CarouselValidator(channel).validate(carousel, topic=make_topic())
    block_codes = {finding.code for finding in report.blocks}
    assert "thin_slide_after_repair" in block_codes
    assert "insufficient_unique_material" in block_codes
    assert report.publishable is False


def test_cover_numeric_duplicate_rewrites_cover_and_preserves_body(channel):
    carousel = make_carousel()
    carousel.cover.headline = "Solana lamports per byte 6333 변경"
    carousel.cover.subtitle = "lamports per byte 6333 적용"
    carousel.slides[1].body = [
        "lamports per byte를 6333으로 변경",
        "Stake Program 조정으로 운영 설정 재검토 필요",
    ]
    original_bodies = [list(slide.body) for slide in carousel.slides]
    pairs = cover_body_duplicates(carousel, channel)
    assert pairs
    before = CarouselValidator(channel).validate(carousel, topic=make_topic())
    assert "cover_body_semantic_duplicate" in {
        finding.code for finding in [*before.blocks, *before.warnings]
    }
    editor = editor_with_response(
        channel,
        {
            "cover": {
                "index": 1,
                "type": "cover",
                "eyebrow": "BLOCKCHAIN / SOLANA",
                "headline": "Solana mainnet 비용 구조와 Stake Program 조정",
                "subtitle": "노드 운영 설정과 비용 회계에 동시에 영향",
                "body": ["본문을 바꾸려는 잘못된 응답"],
            }
        },
    )

    repaired = editor.repair_cover_duplicates(carousel, pairs, topic=make_topic())

    assert "6333" not in repaired.cover.headline + repaired.cover.subtitle
    assert [slide.body for slide in repaired.slides] == original_bodies
    assert repaired.slides[1].body[0] == "lamports per byte를 6333으로 변경"
    assert cover_body_duplicates(repaired, channel) == []


def duplicated_body_carousel():
    """Slide 3 re-lists slide 2's bullet, as a density refill can do."""
    carousel = make_carousel()
    carousel.slides[2].body = [carousel.slides[1].body[0], *carousel.slides[2].body[1:]]
    return carousel


def counting_editor(channel):
    calls = []
    editor = object.__new__(InstagramEditor)
    editor.channel = channel

    def ask_json(*_args, **_kwargs):
        calls.append(1)
        return {"slides": [], "changes": []}

    editor.ask_json = ask_json
    return editor, calls


def test_duplicates_are_rechecked_after_density_refill(channel):
    topic = make_topic()
    editor, calls = counting_editor(channel)
    refilled = DensityRecoveryState(thin_before=[3], thin_after=[])

    _, repaired = _recheck_duplicates_after_refill(
        duplicated_body_carousel(),
        refilled,
        topic=topic,
        assessment=assess_material(topic, channel),
        editor=editor,
        channel=channel,
    )

    assert refilled.recovered
    assert calls, "a refill that can reintroduce a restated fact must be re-checked"
    assert repaired >= 1


def test_no_recheck_when_density_was_not_refilled(channel):
    topic = make_topic()
    editor, calls = counting_editor(channel)
    carousel = duplicated_body_carousel()
    untouched = DensityRecoveryState()  # nothing was thin, nothing refilled

    result, repaired = _recheck_duplicates_after_refill(
        carousel,
        untouched,
        topic=topic,
        assessment=assess_material(topic, channel),
        editor=editor,
        channel=channel,
    )

    assert not untouched.recovered
    assert calls == []
    assert repaired == 0
    assert result is carousel
