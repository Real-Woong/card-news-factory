"""문장별 근거 추적: 심사 입력, 심사관 출력 검증, 게이트 반영."""

from __future__ import annotations

import pytest

from channel.src.config import load_channel_config
from channel.src.grounding import (
    PARTIAL,
    STATUS_CHECKED,
    STATUS_UNCHECKED,
    SUPPORTED,
    UNCHECKED,
    UNSUPPORTED,
    ClaimVerdict,
    GroundingResult,
    InstagramGroundingChecker,
    build_evidence,
    collect_claims,
    parse_verdicts,
)
from channel.src.schema import SlideType
from channel.src.validator import CarouselValidator
from src.core.errors import AgentError
from tests.test_ra_glance import make_carousel, make_topic


@pytest.fixture(scope="module")
def channel():
    return load_channel_config()


def checker(channel, response=None, error: Exception | None = None):
    agent = object.__new__(InstagramGroundingChecker)
    agent.channel = channel
    agent.config = None
    calls: list[dict] = []

    def ask_json(payload, *, instruction=""):
        calls.append(payload)
        if error is not None:
            raise error
        return response(payload) if callable(response) else response

    agent.ask_json = ask_json
    return agent, calls


def judged(verdict: str, evidence=("E1",), note=""):
    """Judge stub that gives every claim the same verdict."""

    def build(payload):
        return {
            "claims": [
                {"id": c["id"], "verdict": verdict, "evidence": list(evidence), "note": note}
                for c in payload["claims"]
            ]
        }

    return build


# --------------------------------------------------------------------------- #
# 입력 구성
# --------------------------------------------------------------------------- #
def test_evidence_numbers_facts_and_marks_open_questions():
    topic = make_topic()
    topic.research["open_questions"] = ["가격은 미공개"]
    evidence = build_evidence(topic)

    assert [e["id"] for e in evidence] == [f"E{i}" for i in range(1, len(evidence) + 1)]
    kinds = {e["kind"] for e in evidence}
    assert {"key_fact", "technical_detail", "what_changed", "why_it_matters", "open_question"} <= kinds
    assert any(e["text"] == "97개 언어 자동 전환" for e in evidence)


def test_evidence_excludes_unverified_claims():
    topic = make_topic()
    topic.fact_check["unverified_claims"] = ["성능이 2배 향상"]
    assert all("2배" not in e["text"] for e in build_evidence(topic))


def test_claims_cover_assertions_not_labels():
    carousel = make_carousel()
    claims = collect_claims(carousel)
    texts = {c.text for c in claims}

    assert "Gemini 3.8 Live 공개" in texts  # cover headline
    assert "라이브 대화 모델 두 종 동시 공개." in texts  # cover subtitle
    assert "97개 언어 자동 전환" in texts  # content bullet
    assert "가용성 범위 사전 확인" in texts  # closing bullet
    assert "AI / GOOGLE DEEPMIND" not in texts  # eyebrow is a label
    assert "Takeaway" not in texts
    assert [c.id for c in claims] == [f"c{i}" for i in range(1, len(claims) + 1)]


def test_closing_extra_body_is_checked():
    carousel = make_carousel()
    closing = next(s for s in carousel.slides if s.type is SlideType.CLOSING)
    closing.extra_body = ["Mainnet 일정은 미공개"]
    assert "Mainnet 일정은 미공개" in {c.text for c in collect_claims(carousel)}


# --------------------------------------------------------------------------- #
# 심사관 출력 검증 (심사관을 그대로 믿지 않는다)
# --------------------------------------------------------------------------- #
def two_claims():
    return collect_claims(make_carousel())[:2]


def test_supported_requires_a_valid_evidence_id():
    claims = two_claims()
    raw = {
        "claims": [
            {"id": claims[0].id, "verdict": "supported", "evidence": []},
            {"id": claims[1].id, "verdict": "supported", "evidence": ["E99"]},
        ]
    }
    verdicts = parse_verdicts(raw, claims, {"E1", "E2"})

    assert [v.verdict for v in verdicts] == [UNSUPPORTED, UNSUPPORTED]
    assert all("근거 id" in v.note for v in verdicts)


def test_missing_and_unknown_verdicts_are_unchecked_not_passed():
    claims = two_claims()
    raw = {"claims": [{"id": claims[0].id, "verdict": "looks fine", "evidence": ["E1"]}]}
    verdicts = parse_verdicts(raw, claims, {"E1"})

    assert [v.verdict for v in verdicts] == [UNCHECKED, UNCHECKED]


def test_valid_verdict_keeps_only_real_evidence_ids_capped_at_three():
    claims = two_claims()[:1]
    raw = {
        "claims": [
            {
                "id": claims[0].id,
                "verdict": "partial",
                "evidence": ["E1", "E404", "E2", "E3", "E4"],
                "note": "범위 초과",
            }
        ]
    }
    (verdict,) = parse_verdicts(raw, claims, {"E1", "E2", "E3", "E4"})

    assert verdict.verdict == PARTIAL
    assert verdict.evidence == ["E1", "E2", "E3"]
    assert verdict.note == "범위 초과"


def test_judge_output_with_unknown_claim_ids_is_ignored():
    claims = two_claims()
    raw = {"claims": [{"id": "c999", "verdict": "supported", "evidence": ["E1"]}]}
    assert all(v.verdict == UNCHECKED for v in parse_verdicts(raw, claims, {"E1"}))


# --------------------------------------------------------------------------- #
# 에이전트
# --------------------------------------------------------------------------- #
def test_checker_judges_every_claim_and_runs_deterministically(channel):
    agent, calls = checker(channel, judged(SUPPORTED))
    carousel = make_carousel()

    result = agent.run(make_topic(), carousel)

    assert result.status == STATUS_CHECKED
    assert len(result.verdicts) == len(collect_claims(carousel))
    assert all(v.verdict == SUPPORTED for v in result.verdicts)
    assert {e["id"] for e in calls[0]["evidence"]} >= {"E1"}
    assert agent.setting("temperature") == 0.0


def test_checker_failure_is_unchecked_never_a_pass(channel):
    agent, _ = checker(channel, error=AgentError("provider down", agent="x"))
    result = agent.run(make_topic(), make_carousel())

    assert result.status == STATUS_UNCHECKED
    assert "provider down" in result.reason
    assert result.verdicts == []


def test_checker_without_evidence_is_unchecked(channel):
    agent, calls = checker(channel, judged(SUPPORTED))
    topic = make_topic(research={})

    result = agent.run(topic, make_carousel())

    assert result.status == STATUS_UNCHECKED
    assert calls == []  # no point asking a judge when there is nothing to judge against


def test_result_round_trips_through_carousel_meta():
    original = GroundingResult(
        status=STATUS_CHECKED,
        verdicts=[ClaimVerdict("c1", 2, "문장", PARTIAL, ["E1"], "메모")],
        evidence=[{"id": "E1", "kind": "key_fact", "text": "x"}],
    )
    restored = GroundingResult.from_dict(original.to_dict())

    assert restored.verdicts[0].verdict == PARTIAL
    assert restored.verdicts[0].evidence == ["E1"]
    assert original.to_dict()["summary"]["partial"] == 1


# --------------------------------------------------------------------------- #
# 게이트
# --------------------------------------------------------------------------- #
def grounded_carousel(channel, verdict_for=None):
    """Carousel whose meta carries a grounding result (default: all supported)."""
    carousel = make_carousel()
    claims = collect_claims(carousel)
    verdicts = []
    for claim in claims:
        verdict = (verdict_for or {}).get(claim.text, SUPPORTED)
        verdicts.append(
            ClaimVerdict(
                claim.id, claim.slide, claim.text, verdict,
                ["E1"] if verdict != UNSUPPORTED else [], "근거 설명",
            )
        )
    carousel.meta["grounding"] = GroundingResult(
        status=STATUS_CHECKED, verdicts=verdicts
    ).to_dict()
    return carousel


def codes(report, severity):
    return {f.code for f in report.findings if f.severity == severity}


def test_unsupported_claim_blocks_publishing(channel):
    carousel = grounded_carousel(channel, {"응답 중단 감소": UNSUPPORTED})
    report = CarouselValidator(channel).validate(carousel)

    assert "unsupported_claim" in codes(report, "block")
    assert not report.publishable
    finding = next(f for f in report.findings if f.code == "unsupported_claim")
    assert "응답 중단 감소" in finding.message
    assert finding.where.startswith("slide ")


def test_partial_support_warns_but_does_not_block(channel):
    carousel = grounded_carousel(channel, {"응답 중단 감소": PARTIAL})
    report = CarouselValidator(channel).validate(carousel)

    assert "partial_claim_support" in codes(report, "warn")
    assert "unsupported_claim" not in codes(report, "block")


def test_all_supported_is_recorded_as_info(channel):
    report = CarouselValidator(channel).validate(grounded_carousel(channel))

    assert "grounding_verified" in codes(report, "info")
    assert not codes(report, "block") & {"unsupported_claim", "grounding_unchecked"}


def test_unchecked_grounding_blocks(channel):
    carousel = make_carousel()
    carousel.meta["grounding"] = GroundingResult(
        status=STATUS_UNCHECKED, reason="심사 호출 실패: timeout"
    ).to_dict()
    report = CarouselValidator(channel).validate(carousel)

    assert "grounding_unchecked" in codes(report, "block")
    assert not report.publishable


def test_unchecked_claim_verdict_blocks(channel):
    carousel = grounded_carousel(channel, {"응답 중단 감소": UNCHECKED})
    report = CarouselValidator(channel).validate(carousel)

    assert "grounding_unchecked" in codes(report, "block")


def test_text_edited_after_grounding_is_flagged_stale(channel):
    carousel = grounded_carousel(channel)
    carousel.slides[2].body[0] = "사람이 나중에 고친 문장"
    report = CarouselValidator(channel).validate(carousel)

    stale = [f for f in report.findings if f.code == "grounding_stale"]
    assert stale and "사람이 나중에 고친 문장" in stale[0].message
    assert stale[0].severity == "warn"


def test_legacy_output_without_record_is_info_only(channel):
    report = CarouselValidator(channel).validate(make_carousel())

    assert "grounding_not_recorded" in codes(report, "info")
    assert not codes(report, "block") & {"unsupported_claim", "grounding_unchecked"}


def test_disabled_grounding_stays_silent(channel):
    import copy

    cfg = copy.deepcopy(channel)
    cfg.content.setdefault("grounding", {})["enabled"] = False
    report = CarouselValidator(cfg).validate(make_carousel())

    assert not any(f.code.startswith("grounding") for f in report.findings)
