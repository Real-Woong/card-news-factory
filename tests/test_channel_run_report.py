"""R_A_Glance 실행 보고서 회귀 테스트."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.errors import AgentError

from channel.src.config import load_channel_config
from channel.src.material import MaterialAssessment
from channel.src.pipeline import run_candidates
from channel.src.run_report import (
    REASON_MATERIAL_TOO_THIN,
    REASON_QUALITY_GATE_FAILED,
    RESULT_GENERATED,
    RESULT_FAILED,
    ChannelRunReport,
    resolve_run_report,
    write_run_report,
)
from channel.src.schema import VerifiedTopic


def make_topic(topic_id: str) -> VerifiedTopic:
    return VerifiedTopic(
        topic_id=topic_id,
        category="ai",
        headline=f"Topic {topic_id}",
        fact_check={"verdict": "verified"},
    )


@dataclass
class FakeResult:
    material: MaterialAssessment
    publishable: bool
    report: object | None = None
    files: list[Path] | None = None
    carousel: object | None = None
    gate: str | None = None  # 자동 게이트 결과(approved|held). None 이면 게시 가능 결과에 approved 를 둔다.

    def __post_init__(self):
        if self.files is None:
            self.files = []
        outcome = self.gate or ("approved" if self.publishable else "held")
        self.carousel = SimpleNamespace(meta={"auto_gate": {"outcome": outcome}})


def test_run_report_persists_skips_and_generated_candidate(tmp_path):
    topics = [make_topic("thin"), make_topic("good")]

    def runner(**kwargs):
        if kwargs["topic"].topic_id == "thin":
            return FakeResult(
                material=MaterialAssessment(
                    sufficient=False,
                    reason="fact 2개 / 120자 — 최소 기준 미달",
                ),
                publishable=False,
            )
        return FakeResult(
            material=MaterialAssessment(sufficient=True),
            publishable=True,
        )

    output_dir = tmp_path / "2026-09-21-10"
    result, report, path = run_candidates(
        topics=topics,
        channel=load_channel_config(),
        app_config=object(),
        llm=object(),
        output_dir=output_dir,
        render=False,
        runner=runner,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert result.publishable is True
    assert report.completed_at is not None
    assert payload["candidates_seen"] == 2
    assert payload["generated"] == 1
    assert payload["skipped"] == 1
    assert payload["failed"] == 0
    assert payload["skip_reasons"][REASON_MATERIAL_TOO_THIN] == 1
    assert payload["skip_reasons"][REASON_QUALITY_GATE_FAILED] == 0
    assert payload["topics"][0]["reason"] == REASON_MATERIAL_TOO_THIN
    assert payload["topics"][1]["result"] == RESULT_GENERATED
    assert payload["topics"][1]["output_id"] == output_dir.name


def test_latest_run_report_uses_natural_run_order(tmp_path):
    for name in ("2026-09-21", "2026-09-21-2", "2026-09-21-10"):
        report = ChannelRunReport(run_id=name)
        report.complete()
        write_run_report(report, tmp_path / name)

    latest = resolve_run_report("latest", tmp_path)

    assert latest.parent.name == "2026-09-21-10"
    assert json.loads(latest.read_text(encoding="utf-8"))["run_id"] == "2026-09-21-10"


def test_quality_gate_failure_is_counted(tmp_path):
    def runner(**_kwargs):
        return FakeResult(
            material=MaterialAssessment(sufficient=True),
            publishable=False,
        )

    _result, _report, path = run_candidates(
        topics=[make_topic("blocked")],
        channel=load_channel_config(),
        app_config=object(),
        llm=object(),
        output_dir=tmp_path / "blocked-run",
        render=False,
        runner=runner,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["failed"] == 1
    assert payload["skip_reasons"][REASON_QUALITY_GATE_FAILED] == 1


def test_recovery_failure_has_separate_quality_metric():
    report = ChannelRunReport(run_id="recovery-failed")
    report.add(
        topic_id="topic-1",
        title="Blocked recovery",
        result=RESULT_FAILED,
        reason=REASON_QUALITY_GATE_FAILED,
        detail="llm_recovery_failed; cover_rewrite_failed",
    )

    assert report.to_dict()["quality_metrics"]["recovery_failed"] == 1


def test_approval_cli_reads_latest_run_report(tmp_path, monkeypatch, capsys):
    from channel.src import approval

    report = ChannelRunReport(run_id="2026-09-21")
    report.complete()
    write_run_report(report, tmp_path / report.run_id)
    monkeypatch.setattr(approval, "OUTPUTS_DIR", tmp_path)

    assert approval.main(["run-report", "latest"]) == 0
    assert '"run_id": "2026-09-21"' in capsys.readouterr().out


def test_failed_generation_is_persisted_before_error_propagates(tmp_path):
    def runner(**_kwargs):
        raise AgentError("fixture generation failure", agent="ig_write")

    output_dir = tmp_path / "failed-run"
    with pytest.raises(AgentError):
        run_candidates(
            topics=[make_topic("failed")],
            channel=load_channel_config(),
            app_config=object(),
            llm=object(),
            output_dir=output_dir,
            render=False,
            runner=runner,
        )

    payload = json.loads((output_dir / "run_report.json").read_text(encoding="utf-8"))
    assert payload["completed_at"] is not None
    assert payload["failed"] == 1
    assert payload["skip_reasons"]["generation_failed"] == 1


class TestNextTopicFallback:
    """승인되지 못한 후보(BLOCK·게이트 보류)가 나오면 같은 실행에서 다음 토픽을 시도한다."""

    def run(self, tmp_path, outcomes, *, max_attempts=3, name="2026-10-28"):
        channel = load_channel_config()
        channel.instagram["posting"]["max_attempts"] = max_attempts
        topics = [make_topic(t) for t in outcomes]
        seen: list[tuple[str, str]] = []

        def runner(**kwargs):
            tid = kwargs["topic"].topic_id
            seen.append((tid, kwargs["output_dir"].name))
            kind = outcomes[tid]
            return FakeResult(
                material=MaterialAssessment(sufficient=True),
                publishable=kind != "block",
                gate="held" if kind == "held" else None,
            )

        result, report, path = run_candidates(
            topics=topics, channel=channel, app_config=None, llm=None,
            output_dir=tmp_path / name, runner=runner,
        )
        return seen, report, path, result

    def test_held_candidate_falls_back_to_the_next_topic_in_its_own_folder(self, tmp_path):
        seen, report, path, result = self.run(tmp_path, {"a": "held", "b": "ok"})
        assert seen == [("a", "2026-10-28"), ("b", "2026-10-28-2")]  # 보류된 후보 폴더는 보존
        assert result.publishable and path.parent.name == "2026-10-28-2"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["generated"] == 1 and payload["failed"] == 1

    def test_blocked_candidate_also_falls_back(self, tmp_path):
        seen, *_ = self.run(tmp_path, {"a": "block", "b": "ok"})
        assert [t for t, _ in seen] == ["a", "b"]

    def test_attempts_are_capped(self, tmp_path):
        seen, report, *_ = self.run(tmp_path, {"a": "held", "b": "held", "c": "held", "d": "ok"},
                                    max_attempts=2)
        assert [t for t, _ in seen] == ["a", "b"]  # 비용 상한: d 는 시도하지 않는다

    def test_approved_first_candidate_stops_immediately(self, tmp_path):
        seen, *_ = self.run(tmp_path, {"a": "ok", "b": "ok"})
        assert [t for t, _ in seen] == ["a"]

    def test_explicit_output_dir_never_falls_back(self, tmp_path):
        seen, *_ = self.run(tmp_path, {"a": "held", "b": "ok"}, name="custom-review")
        assert [t for t, _ in seen] == ["a"]  # 날짜 형식이 아니면 사용자가 지정한 것으로 본다

    def test_missing_gate_info_is_not_approval(self, tmp_path):
        channel = load_channel_config()
        result = SimpleNamespace(publishable=True, carousel=SimpleNamespace(meta={}))
        from channel.src.pipeline import _approved

        assert _approved(result, channel) is False
        channel.content["auto_gate"]["enabled"] = False
        assert _approved(result, channel) is True


class TestSkipExistingTopics:
    """무인 실행은 최근 후보가 이미 있는 토픽을 다시 만들지 않는다."""

    def setup(self, tmp_path, existing):
        from datetime import date

        channel = load_channel_config()
        channel.instagram["output"]["dir"] = str(tmp_path / "review")
        today = date.today().isoformat()
        for name, topic_id in existing.items():
            folder = tmp_path / "review" / name.replace("TODAY", today)
            folder.mkdir(parents=True)
            (folder / "carousel.json").write_text(
                json.dumps({"meta": {"topic_id": topic_id}}), encoding="utf-8")
        return channel, today

    def run(self, channel, tmp_path, topics, today, *, skip_existing):
        seen = []

        def runner(**kwargs):
            seen.append(kwargs["topic"].topic_id)
            return FakeResult(material=MaterialAssessment(sufficient=True), publishable=True)

        out = tmp_path / "review" / f"{today}-9"
        result, report, _ = run_candidates(
            topics=[make_topic(t) for t in topics], channel=channel, app_config=None, llm=None,
            output_dir=out, runner=runner, skip_existing=skip_existing)
        return seen, report

    def test_existing_topic_is_skipped_and_reported(self, tmp_path):
        channel, today = self.setup(tmp_path, {"TODAY": "a"})
        seen, report = self.run(channel, tmp_path, ["a", "b"], today, skip_existing=True)
        assert seen == ["b"]
        assert [t.reason for t in report.topics][:1] == ["duplicate_topic"]

    def test_explicit_selection_ignores_existing_candidates(self, tmp_path):
        channel, today = self.setup(tmp_path, {"TODAY": "a"})
        seen, _ = self.run(channel, tmp_path, ["a", "b"], today, skip_existing=False)
        assert seen == ["a"]

    def test_old_candidates_do_not_count(self, tmp_path):
        channel, today = self.setup(tmp_path, {"2020-01-01": "a"})
        seen, _ = self.run(channel, tmp_path, ["a"], today, skip_existing=True)
        assert seen == ["a"]

    def test_held_and_rejected_candidates_count_too(self, tmp_path):
        from channel.src.pipeline import existing_topic_ids

        channel, _ = self.setup(tmp_path, {"TODAY": "held-one", "TODAY-2": "other"})
        assert existing_topic_ids(channel, 14) == {"held-one", "other"}
        assert existing_topic_ids(channel, 0) == set()
