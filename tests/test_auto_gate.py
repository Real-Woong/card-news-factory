"""자동 승인 게이트(channel/src/auto_gate.py) 테스트.

핵심 보장: 하나라도 실패·불확실하면 승인하지 않는다(통과 쪽으로 폴백 금지), 사람이 내린 결정은
덮어쓰지 않는다, 통과하면 auto-gate 로 승인 기록이 현재 콘텐츠에 유효하다.
"""

from __future__ import annotations

import json

import pytest

from src.agents.llm import LLMClient, LLMRequest, LLMResponse

from channel.src.approval import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    decide,
    init_record,
    load_record,
)
from channel.src.auto_gate import (
    GATE_FILENAME,
    InstagramReviewer,
    code_checks,
    evaluate,
    parse_review,
    run_gate,
)
from channel.src.config import load_channel_config
from channel.src.schema import Carousel, Slide, SlideType, Source, VerifiedTopic

GOOD_SOURCES = [
    {"name": "SEC", "url": "https://www.sec.gov/rules/order-tokenized", "verdict": "verified",
     "authority": "primary", "source_relation": "primary_announcement", "supports_event": True},
    {"name": "Law", "url": "https://www.davispolk.com/insights/tokenized-order", "verdict": "verified",
     "authority": "secondary", "source_relation": "news", "supports_event": True},
]
GOOD_GROUNDING = {
    "status": "checked",
    "summary": {"supported": 5, "partial": 0, "unsupported": 0, "unchecked": 0},
    "verdicts": [],
    "evidence": [],
}


class FakeLLM(LLMClient):
    provider = "fake"

    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text=self.text, provider=self.provider)


@pytest.fixture
def channel():
    return load_channel_config()


def make_output(tmp_path, **meta_overrides):
    meta = {
        "topic_id": "t1",
        "fact_check_verdict": "verified",
        "unverified_claims": [],
        "sources": GOOD_SOURCES,
        "grounding": GOOD_GROUNDING,
    }
    meta.update(meta_overrides)
    slides = [
        Slide(index=1, type=SlideType.COVER, headline="표지", subtitle="부제"),
        Slide(index=2, type=SlideType.CONTENT, title="제목", body=["가", "나"]),
        Slide(index=3, type=SlideType.CLOSING, title="Takeaway", body=["결론"]),
    ]
    carousel = Carousel(template="news_short", topic="BLOCKCHAIN", headline="표지",
                        slides=slides, meta=meta, caption="캡션")
    (tmp_path / "carousel.json").write_text(
        json.dumps(carousel.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "caption.md").write_text("캡션", encoding="utf-8")
    init_record(tmp_path, publishable=True, score=0.9, warnings=[])
    return tmp_path


def topic() -> VerifiedTopic:
    return VerifiedTopic(
        topic_id="t1", category="blockchain", headline="h",
        research={"key_facts": [{"text": "사실"}], "what_changed": "x", "why_it_matters": "y"},
        sources=[Source(name="SEC", url="https://www.sec.gov/x")],
    )


def reviewer(config, channel, text):
    return InstagramReviewer(config=config, llm=FakeLLM(text), channel=channel)


PASS = json.dumps({"decision": "pass", "issues": [
    {"kind": "style", "where": "slide 2", "quote": "가", "note": "어색"}]})


class TestCodeChecks:
    def failed(self, channel, tmp_path, **overrides):
        out = make_output(tmp_path, **overrides)
        return {c.name for c in code_checks(out, channel) if not c.passed}

    def test_clean_output_passes_every_code_check(self, channel, tmp_path):
        assert self.failed(channel, tmp_path) == set()

    def test_partially_verified_is_held(self, channel, tmp_path):
        assert self.failed(channel, tmp_path, fact_check_verdict="partially_verified") == {"verdict"}

    def test_unverified_claims_and_missing_grounding(self, channel, tmp_path):
        assert "unverified_claims" in self.failed(channel, tmp_path, unverified_claims=["주장"])
        other = tmp_path / "a"
        other.mkdir()
        assert "grounding" in self.failed(channel, other, grounding=None)

    @pytest.mark.parametrize("summary", [
        {"supported": 3, "partial": 1, "unsupported": 0, "unchecked": 0},
        {"supported": 3, "partial": 0, "unsupported": 1, "unchecked": 0},
        {"supported": 3, "partial": 0, "unsupported": 0, "unchecked": 1},
    ])
    def test_any_weak_grounding_is_held(self, channel, tmp_path, summary):
        grounding = {**GOOD_GROUNDING, "summary": summary}
        assert self.failed(channel, tmp_path, grounding=grounding) == {"grounding"}

    def test_unrelated_documentation_source_is_held(self, channel, tmp_path):
        sources = [
            {"name": "News", "url": "https://solana.com/news/stocks-sec-innovation-exemption",
             "source_relation": "primary_announcement", "supports_event": True},
            {"name": "solana.com", "url": "https://solana.com/docs/tools/actions",
             "source_relation": "documentation", "supports_event": True},
        ]
        assert self.failed(channel, tmp_path, sources=sources) == {"sources"}

    def test_related_documentation_and_broken_urls(self, channel, tmp_path):
        ok = [
            {"name": "News", "url": "https://x.com/news/stocks-sec-innovation-exemption",
             "source_relation": "primary_announcement", "supports_event": True},
            {"name": "Docs", "url": "https://docs.x.com/innovation-exemption/faq",
             "source_relation": "documentation", "supports_event": True},
        ]
        assert self.failed(channel, tmp_path, sources=ok) == set()
        broken = [{"name": "N", "url": "https://x.com/a\\", "supports_event": True}]
        sub = tmp_path / "b"
        sub.mkdir()
        assert self.failed(channel, sub, sources=broken) == {"sources"}

    def test_no_supporting_source_is_held(self, channel, tmp_path):
        sources = [{"name": "N", "url": "https://x.com/a", "supports_event": False}]
        assert self.failed(channel, tmp_path, sources=sources) == {"sources"}

    def test_low_quality_or_block_is_held(self, channel, tmp_path):
        out = make_output(tmp_path)
        init_record(out, publishable=True, score=0.6, warnings=[])
        assert {c.name for c in code_checks(out, channel) if not c.passed} == {"quality"}
        init_record(out, publishable=True, score=0.9, warnings=["[BLOCK] 날짜 지남"])
        assert {c.name for c in code_checks(out, channel) if not c.passed} == {"no_block"}


class TestParseReview:
    HAY = "카드 문구 가 나 결론 3종 모델"

    def issue(self, kind, quote="3종 모델", **extra):
        return {"kind": kind, "where": "slide 2", "quote": quote, "note": "사유", **extra}

    def parse(self, decision, issues):
        return parse_review({"decision": decision, "issues": issues}, self.HAY)

    def test_pass_and_low_issues(self):
        ok, issues = parse_review(json.loads(PASS), self.HAY)
        assert ok and issues[0]["severity"] == "low"

    @pytest.mark.parametrize("kind", ["contradicts_evidence", "inconsistent", "overclaim"])
    def test_high_kinds_hold_even_if_the_model_says_pass(self, kind):
        ok, issues = self.parse("pass", [self.issue(kind)])
        assert ok is False and issues[0]["severity"] == "high"

    def test_severity_comes_from_the_kind_not_from_the_model(self):
        ok, issues = self.parse("hold", [self.issue("style", severity="high")])
        assert ok is True and issues[0]["severity"] == "low"  # 모델이 high 라 써도 style 은 low

    @pytest.mark.parametrize("kind", ["ok", "none", "supported", ""])
    def test_non_problem_kinds_are_not_issues(self, kind):
        ok, issues = self.parse("hold", [self.issue(kind)])
        assert ok is True and issues == []  # 문제 없다고 쓴 항목은 보류 사유가 아니다

    def test_issue_quoting_text_not_on_the_card_is_dropped(self):
        ok, issues = self.parse("hold", [self.issue("overclaim", quote="카드에 없는 문장")])
        assert ok is True and issues == []
        ok, _ = self.parse("hold", [self.issue("overclaim", quote="")])
        assert ok is True

    @pytest.mark.parametrize("note", [
        "근거와 일치하여 문제 없음(이 항목은 철회).", "모순 없음", "충돌 없음, 오탐",
    ])
    def test_self_retracted_issues_are_dropped(self, note):
        ok, issues = self.parse("hold", [self.issue("overclaim", note=note)])
        assert ok is True and issues == []

    def test_hold_without_any_reason_is_still_a_hold(self):
        assert self.parse("hold", [])[0] is False

    @pytest.mark.parametrize("data", [None, "text", {}, {"decision": "maybe"}])
    def test_bad_format_is_held(self, data):
        assert parse_review(data, self.HAY)[0] is False


class TestReviewerIsDeterministic:
    def test_temperature_is_zero(self, config, channel):
        assert reviewer(config, channel, PASS).setting("temperature") == 0.0


class TestReviewerPayload:
    def test_reviewer_sees_the_same_evidence_list_as_grounding(self, config, channel):
        tp = VerifiedTopic(topic_id="t", category="ai", headline="h", research={
            "key_facts": [{"text": "사실"}], "technical_details": ["RMS normalization detail"],
            "what_changed": "변화", "why_it_matters": "중요"})
        carousel = Carousel(template="news_short", topic="AI", headline="h", slides=[
            Slide(index=1, type=SlideType.COVER, headline="표지", subtitle="부제")], meta={})
        payload = reviewer(config, channel, PASS).build_payload(tp, carousel)
        texts = [e["text"] for e in payload["evidence"]]
        assert "RMS normalization detail" in texts and all(e["id"].startswith("E") for e in payload["evidence"])


class TestRunGate:
    def run(self, config, channel, tmp_path, text=PASS, **meta):
        out = make_output(tmp_path, **meta)
        rv = reviewer(config, channel, text)
        outcome, result = run_gate(out, channel, reviewer=rv, topic=topic())
        return out, outcome, result, rv

    def test_all_gates_pass_records_auto_approval(self, config, channel, tmp_path):
        out, outcome, result, rv = self.run(config, channel, tmp_path)
        assert outcome == "approved" and result.passed
        record = load_record(out)
        assert record.status == STATUS_APPROVED and record.reviewer == "auto-gate"
        assert record.approval_valid_for_current_content
        assert rv.llm.requests[0].task == "ig_review"
        assert json.loads((out / GATE_FILENAME).read_text(encoding="utf-8"))["passed"] is True

    def test_code_failure_holds_without_calling_the_llm(self, config, channel, tmp_path):
        out, outcome, _, rv = self.run(config, channel, tmp_path,
                                       fact_check_verdict="partially_verified")
        assert outcome == "held" and rv.llm.requests == []  # 보류가 확정이면 LLM 비용을 쓰지 않는다
        record = load_record(out)
        assert record.status == STATUS_PENDING and "verdict" in record.notes

    def test_reviewer_hold_blocks_approval(self, config, channel, tmp_path):
        bad = json.dumps({"decision": "hold", "issues": [
            {"kind": "contradicts_evidence", "where": "slide 2", "quote": "가",
             "evidence": ["E1"], "note": "발효일이 근거와 다름"}]})
        out, outcome, result, _ = self.run(config, channel, tmp_path, text=bad)
        assert outcome == "held" and load_record(out).status == STATUS_PENDING
        assert result.issues[0]["note"].startswith("발효일")

    @pytest.mark.parametrize("text", ["json 아님", "{}", '{"decision": "pass"', ""])
    def test_reviewer_garbage_is_held_not_approved(self, config, channel, tmp_path, text):
        out, outcome, _, _ = self.run(config, channel, tmp_path, text=text)
        assert outcome == "held" and load_record(out).status == STATUS_PENDING

    def test_missing_reviewer_is_held(self, channel, tmp_path):
        out = make_output(tmp_path)
        assert not evaluate(out, channel, reviewer=None, topic=None).passed

    def test_human_decision_is_never_overwritten(self, config, channel, tmp_path):
        out = make_output(tmp_path)
        decide(out, STATUS_REJECTED, reviewer="human", note="별로")
        outcome, _ = run_gate(out, channel, reviewer=reviewer(config, channel, PASS), topic=topic())
        assert outcome == "skipped" and load_record(out).status == STATUS_REJECTED

    def test_edit_after_approval_invalidates_it(self, config, channel, tmp_path):
        out, *_ = self.run(config, channel, tmp_path)
        data = json.loads((out / "carousel.json").read_text(encoding="utf-8"))
        data["slides"][1]["title"] = "바뀐 제목"
        (out / "carousel.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        init_record(out, publishable=True, score=0.9, warnings=[])
        assert not load_record(out).approval_valid_for_current_content

    def test_gate_can_be_required_to_be_looser_only_by_config_not_code(self, channel):
        assert channel.get("content.auto_gate.required_verdict") == "verified"
        assert float(channel.get("content.auto_gate.min_quality")) >= 0.70
