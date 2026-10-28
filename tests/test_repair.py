"""자동 수정(channel/src/repair.py) 테스트.

핵심 보장: 모델이 새 사실·숫자를 넣거나 엉뚱한 곳을 고치려 하면 코드가 거절하고(문장은 그대로 남아
게이트가 보류), 고칠 대상만 정확히 고친다.
"""

from __future__ import annotations

import json

import pytest

from src.agents.llm import LLMClient, LLMRequest, LLMResponse

from channel.src.config import load_channel_config
from channel.src.grounding import InstagramGroundingChecker
from channel.src.repair import (
    InstagramIssueRepairer,
    InstagramRepairer,
    apply_fixes,
    collect_targets,
    issue_target,
    locate_quote,
    repair_loop,
    review_repair,
)
from channel.src.schema import Carousel, Slide, SlideType, VerifiedTopic

EVIDENCE = [
    {"id": "E1", "kind": "key_fact", "text": "Pocket TTS runs on CPU with 100M parameters"},
    {"id": "E2", "kind": "key_fact", "text": "Flint MoE model released"},
]
OLD = "CPU 구동 on-device TTS 3종 추가"


@pytest.fixture
def channel():
    return load_channel_config()


def carousel(grounding=None, title="Flint MoE 모델 공개"):
    slides = [
        Slide(index=1, type=SlideType.COVER, headline="표지", subtitle="부제"),
        Slide(index=2, type=SlideType.CONTENT, title=title,
              body=[OLD, "100M 파라미터 모델", "세 번째 줄"]),
        Slide(index=3, type=SlideType.CLOSING, title="Takeaway", body=["결론"]),
    ]
    meta = {"grounding": grounding or {
        "status": "checked",
        "summary": {"supported": 2, "partial": 1, "unsupported": 0, "unchecked": 0},
        "verdicts": [
            {"id": "c1", "slide": 2, "text": OLD, "verdict": "partial",
             "evidence": ["E1"], "note": "Pocket TTS만 근거"},
            {"id": "c2", "slide": 2, "text": "100M 파라미터 모델", "verdict": "supported",
             "evidence": ["E1"], "note": ""},
        ],
        "evidence": EVIDENCE,
    }}
    return Carousel(template="news_short", topic="AI", headline="표지", slides=slides, meta=meta)


BLOB = " ".join(e["text"] for e in EVIDENCE)


def fix(channel, c, payload):
    targets = collect_targets(c, channel)
    return targets, apply_fixes(c, targets, payload, channel, evidence_blob=BLOB)


class TestCollectTargets:
    def test_partial_claims_and_jargon_titles_are_targets(self, channel):
        targets = collect_targets(carousel(), channel)
        assert [(t.field, t.slide) for t in targets] == [("body", 2), ("title", 2)]
        assert "일부만" in targets[0].reason and targets[0].evidence == [EVIDENCE[0]["text"]]

    def test_supported_claims_and_plain_titles_are_left_alone(self, channel):
        c = carousel(title="새 모델 공개")
        c.meta["grounding"]["verdicts"][0]["verdict"] = "supported"
        assert collect_targets(c, channel) == []

    def test_text_not_found_on_the_slide_is_skipped(self, channel):
        c = carousel(title="새 모델 공개")
        c.meta["grounding"]["verdicts"][0]["text"] = "슬라이드에 없는 문장"
        assert collect_targets(c, channel) == []


class TestApplyFixes:
    def test_rewrite_narrows_the_claim(self, channel):
        c = carousel(title="새 모델 공개")
        _, report = fix(channel, c, {"fixes": [
            {"id": "t1", "action": "rewrite", "text": "Pocket TTS가 CPU에서 구동"}]})
        assert c.slides[1].body[0] == "Pocket TTS가 CPU에서 구동"
        assert report.applied[0]["action"] == "rewrite" and not report.rejected

    def test_drop_removes_a_body_line(self, channel):
        c = carousel(title="새 모델 공개")
        fix(channel, c, {"fixes": [{"id": "t1", "action": "drop"}]})
        assert OLD not in c.slides[1].body and len(c.slides[1].body) == 2

    def test_new_numbers_are_rejected(self, channel):
        c = carousel(title="새 모델 공개")
        _, report = fix(channel, c, {"fixes": [
            {"id": "t1", "action": "rewrite", "text": "Pocket TTS가 CPU에서 5배 빠르게 구동"}]})
        assert c.slides[1].body[0] == OLD  # 그대로
        assert "숫자" in report.rejected[0]["why"]

    def test_numbers_from_the_original_or_evidence_are_allowed(self, channel):
        c = carousel(title="새 모델 공개")
        _, report = fix(channel, c, {"fixes": [
            {"id": "t1", "action": "rewrite", "text": "100M 파라미터 Pocket TTS가 CPU 구동"}]})
        assert report.applied and not report.rejected

    def test_number_words_in_the_evidence_allow_digits(self, channel):
        c = carousel(title="새 모델 공개")
        blob = BLOB + " two new TTS models"
        targets = collect_targets(c, channel)
        report = apply_fixes(c, targets, {"fixes": [
            {"id": "t1", "action": "rewrite", "text": "신규 TTS 2종 공개"}]}, channel, evidence_blob=blob)
        assert report.applied and not report.rejected
        c2 = carousel(title="새 모델 공개")  # 근거에 숫자 낱말이 없으면 여전히 거절
        report = apply_fixes(c2, collect_targets(c2, channel), {"fixes": [
            {"id": "t1", "action": "rewrite", "text": "신규 TTS 2종 공개"}]}, channel, evidence_blob=BLOB)
        assert report.rejected and report.rejected[0]["new"] == "신규 TTS 2종 공개"

    @pytest.mark.parametrize("text,why", [
        ("", "빈 문장"), (OLD, "바뀐 것이 없음"), ("자세히 https://x.com", "URL"),
        ("가" * 200, "글자 수"),
    ])
    def test_bad_rewrites_are_rejected(self, channel, text, why):
        c = carousel(title="새 모델 공개")
        _, report = fix(channel, c, {"fixes": [{"id": "t1", "action": "rewrite", "text": text}]})
        assert c.slides[1].body[0] == OLD and why in report.rejected[0]["why"]

    def test_product_names_from_the_evidence_are_allowed_when_fixing_accuracy(self, channel):
        from channel.src.repair import Target

        blob = BLOB + " Gradium Phonon TTS"
        c = carousel(title="CPU에서 도는 음성 모델 3종")
        accuracy = Target("t1", 2, "title", c.slides[1].title, "제목이 근거보다 넓게 말함")
        report = apply_fixes(c, [accuracy], {"fixes": [
            {"id": "t1", "action": "rewrite", "text": "CPU에서 도는 건 Pocket TTS"}]}, channel,
            evidence_blob=blob + " Pocket TTS")
        assert not report.rejected and c.slides[1].title == "CPU에서 도는 건 Pocket TTS"

    def test_product_names_do_not_excuse_jargon_when_the_fix_is_about_jargon(self, channel):
        from channel.src.repair import Target

        c = carousel(title="Flint MoE 모델 공개")
        jargon = Target("t1", 2, "title", c.slides[1].title, "제목 전문용어: 쉬운 한국어로")
        report = apply_fixes(c, [jargon], {"fixes": [
            {"id": "t1", "action": "rewrite", "text": "Flint MoE 신모델"}]}, channel,
            evidence_blob=BLOB)
        assert report.rejected and c.slides[1].title == "Flint MoE 모델 공개"

    def test_title_must_lose_the_jargon(self, channel):
        c = carousel()
        _, report = fix(channel, c, {"fixes": [
            {"id": "t1", "action": "drop"},
            {"id": "t2", "action": "rewrite", "text": "Flint MoE 신모델"}]})
        assert c.slides[1].title == "Flint MoE 모델 공개"
        assert any("전문용어" in r["why"] for r in report.rejected)

    def test_title_and_headline_cannot_be_dropped(self, channel):
        c = carousel()
        _, report = fix(channel, c, {"fixes": [
            {"id": "t1", "action": "rewrite", "text": "Pocket TTS가 CPU에서 구동"},
            {"id": "t2", "action": "drop"}]})
        assert c.slides[1].title == "Flint MoE 모델 공개"
        assert any("삭제할 수 없음" in r["why"] for r in report.rejected)

    def test_cannot_drop_below_the_minimum_lines(self, channel):
        c = carousel(title="새 모델 공개")
        c.slides[1].body = [OLD, "둘"]
        _, report = fix(channel, c, {"fixes": [{"id": "t1", "action": "drop"}]})
        assert len(c.slides[1].body) == 2 and "최소 줄 수" in report.rejected[0]["why"]

    def test_missing_unknown_and_garbage_output_change_nothing(self, channel):
        for payload in [{}, None, "x", {"fixes": "no"}, {"fixes": [{"id": "t9", "action": "drop"}]},
                        {"fixes": [{"id": "t1", "action": "delete-all"}]}]:
            c = carousel(title="새 모델 공개")
            before = json.dumps(c.to_dict(), ensure_ascii=False)
            fix(channel, c, payload)
            assert json.dumps(c.to_dict(), ensure_ascii=False) == before


class FakeLLM(LLMClient):
    provider = "fake"

    def __init__(self, by_task):
        self.by_task = by_task
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text=self.by_task[request.task], provider=self.provider)


class TestRepairLoop:
    def topic(self):
        return VerifiedTopic(topic_id="t", category="ai", headline="h", research={
            "key_facts": [{"text": e["text"]} for e in EVIDENCE],
            "what_changed": "x", "why_it_matters": "y"})

    def agents(self, config, channel, llm):
        return (InstagramRepairer(config=config, llm=llm, channel=channel),
                InstagramGroundingChecker(config=config, llm=llm, channel=channel))

    def test_fix_then_regrounding_updates_the_record(self, config, channel):
        fixed = "Pocket TTS가 CPU에서 구동"
        llm = FakeLLM({
            "ig_repair": json.dumps({"fixes": [{"id": "t1", "action": "rewrite", "text": fixed}]},
                                    ensure_ascii=False),
            "ig_ground": json.dumps({"claims": [
                {"id": f"c{i}", "verdict": "supported", "evidence": ["E1"], "note": ""}
                for i in range(1, 12)]}),
        })
        c = carousel(title="새 모델 공개")
        repairer, checker = self.agents(config, channel, llm)
        history = repair_loop(self.topic(), c, channel, repairer=repairer, checker=checker)
        assert c.slides[1].body[0] == fixed and history[0]["applied"]
        assert c.meta["repair"] == history
        assert c.meta["grounding"]["summary"]["partial"] == 0  # 다시 심사한 결과로 교체
        assert [r.task for r in llm.requests] == ["ig_repair", "ig_ground"]

    def test_nothing_to_fix_makes_no_llm_calls(self, config, channel):
        llm = FakeLLM({})
        c = carousel(title="새 모델 공개")
        c.meta["grounding"]["verdicts"][0]["verdict"] = "supported"
        repairer, checker = self.agents(config, channel, llm)
        assert repair_loop(self.topic(), c, channel, repairer=repairer, checker=checker) == []
        assert llm.requests == [] and "repair" not in c.meta

    def test_rejected_fix_keeps_text_and_skips_regrounding(self, config, channel):
        llm = FakeLLM({"ig_repair": json.dumps({"fixes": [
            {"id": "t1", "action": "rewrite", "text": "CPU에서 9배 빠름"}]}, ensure_ascii=False)})
        c = carousel(title="새 모델 공개")
        repairer, checker = self.agents(config, channel, llm)
        history = repair_loop(self.topic(), c, channel, repairer=repairer, checker=checker)
        assert c.slides[1].body[0] == OLD and history[0]["rejected"]
        assert {r.task for r in llm.requests} == {"ig_repair"}  # 적용된 게 없으면 재심사 안 함

    def test_rejection_is_fed_back_and_the_second_try_can_succeed(self, config, channel):
        class Sequenced(FakeLLM):
            def complete(self, request):
                self.requests.append(request)
                if request.task == "ig_repair":
                    n = sum(1 for r in self.requests if r.task == "ig_repair")
                    text = ("CPU에서 9배 빠름" if n == 1 else "Pocket TTS가 CPU에서 구동")
                    return LLMResponse(text=json.dumps({"fixes": [
                        {"id": "t1", "action": "rewrite", "text": text}]}, ensure_ascii=False),
                        provider="fake")
                return LLMResponse(text=json.dumps({"claims": [
                    {"id": f"c{i}", "verdict": "supported", "evidence": ["E1"]} for i in range(1, 12)]}),
                    provider="fake")

        llm = Sequenced({})
        c = carousel(title="새 모델 공개")
        repairer, checker = self.agents(config, channel, llm)
        history = repair_loop(self.topic(), c, channel, repairer=repairer, checker=checker)
        assert len(history) == 2 and c.slides[1].body[0] == "Pocket TTS가 CPU에서 구동"
        second = [r for r in llm.requests if r.task == "ig_repair"][1]
        assert second.payload["previous_rejections"][0]["why"].startswith("원문·근거에 없는 숫자")

    def test_llm_failure_is_not_fatal(self, config, channel):
        llm = FakeLLM({"ig_repair": "json 아님"})
        c = carousel(title="새 모델 공개")
        repairer, checker = self.agents(config, channel, llm)
        repair_loop(self.topic(), c, channel, repairer=repairer, checker=checker)
        assert c.slides[1].body[0] == OLD

    def test_rounds_are_bounded(self, config, channel):
        channel.content["repair"]["max_rounds"] = 0
        llm = FakeLLM({})
        repairer, checker = self.agents(config, channel, llm)
        assert repair_loop(self.topic(), carousel(), channel, repairer=repairer, checker=checker) == []


class FakeReviewer:
    def __init__(self, ok, issues):
        self.ok, self.issues, self.calls = ok, issues, 0

    def run(self, topic, carousel):
        self.calls += 1
        return self.ok, self.issues


TITLE = "CPU에서 도는 음성 모델 3종"
ISSUE = {"severity": "high", "kind": "overclaim", "where": "slide 2", "quote": TITLE,
         "note": "제목이 근거보다 넓게 말함"}


class TestLocateQuote:
    def test_exact_substring_and_ambiguous(self):
        slide = carousel(title=TITLE).slides[1]
        assert locate_quote(slide, TITLE) == ("title", TITLE)
        assert locate_quote(slide, "100M 파라미터") == ("body", "100M 파라미터 모델")  # 일부 인용도 찾는다
        assert locate_quote(slide, "없는 문구") is None and locate_quote(slide, "") is None
        slide.body = ["같은 말 하나", "같은 말 둘"]
        assert locate_quote(slide, "같은 말") is None  # 모호하면 고치지 않는다

    def test_cover_issues_map_to_slide_one(self):
        from channel.src.repair import _issue_slide

        assert _issue_slide({"where": "cover"}) == 1 and _issue_slide({"where": "slide 4"}) == 4
        assert _issue_slide({"where": "caption"}) == 0 and _issue_slide({"where": "sources"}) == 0


class FakeReviewer:
    def __init__(self, ok, issues):
        self.ok, self.issues, self.calls = ok, issues, 0

    def run(self, topic, carousel):
        self.calls += 1
        return self.ok, self.issues


class TestReviewRepair:
    def agents(self, config, channel, llm):
        return (InstagramIssueRepairer(config=config, llm=llm, channel=channel),
                InstagramGroundingChecker(config=config, llm=llm, channel=channel))

    def topic(self):
        return TestRepairLoop().topic()

    def grounded(self):
        return json.dumps({"claims": [
            {"id": f"c{i}", "verdict": "supported", "evidence": ["E1"]} for i in range(1, 12)]})

    def fix(self, **fix):
        return json.dumps({"fixes": [{"id": "i1", **fix}]}, ensure_ascii=False)

    def run(self, config, channel, llm, reviewer, c=None):
        c = c or carousel(title=TITLE)
        repairer, checker = self.agents(config, channel, llm)
        history = review_repair(self.topic(), c, channel, repairer=repairer,
                                reviewer=reviewer, checker=checker)
        return c, history

    def test_issue_on_a_title_is_narrowed_and_regrounded(self, config, channel):
        llm = FakeLLM({
            "ig_repair_issue": self.fix(action="rewrite", text="CPU에서 구동되는 음성 모델"),
            "ig_ground": self.grounded(),
        })
        c, history = self.run(config, channel, llm, FakeReviewer(False, [ISSUE]))
        assert c.slides[1].title == "CPU에서 구동되는 음성 모델"
        assert history[0]["kind"] == "review" and history[0]["applied"]
        assert c.meta["repair"][-1]["kind"] == "review"
        assert [r.task for r in llm.requests] == ["ig_repair_issue", "ig_ground"]

    def test_the_model_is_told_the_exact_target_found_by_code(self, config, channel):
        llm = FakeLLM({"ig_repair_issue": self.fix(action="rewrite", text="CPU에서 구동되는 음성 모델"),
                       "ig_ground": self.grounded()})
        self.run(config, channel, llm, FakeReviewer(False, [ISSUE]))
        payload = llm.requests[0].payload
        assert payload["target"] == {"field": "title", "text": TITLE}
        assert payload["issue"]["id"] == "i1"

    def test_cover_issue_is_repaired_on_slide_one(self, config, channel):
        c = carousel(title="새 모델 공개")
        c.slides[0].headline = "바로 사용하는 새 모델"
        issue = {**ISSUE, "where": "cover", "quote": "바로 사용하는 새 모델"}
        llm = FakeLLM({"ig_repair_issue": self.fix(action="rewrite", text="기본 지원하는 새 모델"),
                       "ig_ground": self.grounded()})
        c, history = self.run(config, channel, llm, FakeReviewer(False, [issue]), c)
        assert c.slides[0].headline == "기본 지원하는 새 모델" and history[0]["applied"]

    def test_each_issue_gets_its_own_call_and_duplicates_are_merged(self, config, channel):
        other = {**ISSUE, "where": "slide 2", "quote": "세 번째 줄", "kind": "inconsistent"}
        llm = FakeLLM({"ig_repair_issue": self.fix(action="drop"), "ig_ground": self.grounded()})
        self.run(config, channel, llm, FakeReviewer(False, [ISSUE, ISSUE, other]))
        assert [r.task for r in llm.requests].count("ig_repair_issue") == 2

    def test_missing_answer_is_reported_not_silent(self, config, channel):
        llm = FakeLLM({"ig_repair_issue": json.dumps({"fixes": []})})
        c, history = self.run(config, channel, llm, FakeReviewer(False, [ISSUE]))
        assert c.slides[1].title == TITLE
        assert history[0]["rejected"][0]["why"] == "수정 응답 없음"

    def test_passing_review_changes_nothing(self, config, channel):
        llm = FakeLLM({})
        c, history = self.run(config, channel, llm, FakeReviewer(True, []))
        assert history == [] and llm.requests == []

    def test_issues_without_a_slide_and_low_issues_are_skipped(self, config, channel):
        llm = FakeLLM({})
        issues = [{**ISSUE, "where": "caption"}, {**ISSUE, "where": "sources"},
                  {**ISSUE, "severity": "low"}]
        c, history = self.run(config, channel, llm, FakeReviewer(False, issues))
        assert history == [] and llm.requests == []

    def test_quote_that_is_not_on_the_slide_is_not_repaired(self, config, channel):
        llm = FakeLLM({"ig_repair_issue": self.fix(action="rewrite", text="새 제목")})
        c, history = self.run(config, channel, llm,
                              FakeReviewer(False, [{**ISSUE, "quote": "없는 문구"}]))
        assert c.slides[1].title == TITLE and history[0]["rejected"][0]["why"].startswith("인용한")

    def test_new_numbers_are_still_rejected_in_review_repair(self, config, channel):
        llm = FakeLLM({"ig_repair_issue": self.fix(action="rewrite", text="CPU에서 7배 빠른 TTS")})
        c, history = self.run(config, channel, llm, FakeReviewer(False, [ISSUE]))
        assert c.slides[1].title == TITLE and history[0]["rejected"]

    def test_issue_target_ignores_garbage(self):
        c = carousel(title=TITLE)
        issue = {**ISSUE, "id": "i1"}
        for raw in [None, {}, {"fixes": "x"}, {"fixes": [1, {"id": "zz"}]}]:
            assert issue_target(c, issue, raw)[0] is None
