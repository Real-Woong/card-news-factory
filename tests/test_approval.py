"""Phase 2 승인 레이어 — 상태 전이, 승인 차단, 통계."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from channel.src.approval import (
    PHASE3_CONSECUTIVE_TARGET,
    STATUS_APPROVED,
    STATUS_NEEDS_EDIT,
    STATUS_PENDING,
    STATUS_REJECTED,
    ApprovalError,
    decide,
    init_record,
    latest_output,
    load_record,
    metrics,
    output_dirs,
    resolve_output,
    update_preview,
)

CHANNEL_ROOT = Path(__file__).resolve().parents[1] / "channel"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def make_output(root: Path, name: str, *, publishable: bool = True, score: float = 0.84) -> Path:
    """최소 산출물 디렉터리 (LLM/렌더 없이 fixture 로만 구성)."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "carousel.json").write_text(
        json.dumps({"template": "news_standard", "slides": [], "caption": "x"}),
        encoding="utf-8",
    )
    (directory / "review.md").write_text(
        f"- 판정: **게시 가능** (publishable = {str(publishable).lower()}, score = {score:.2f})\n",
        encoding="utf-8",
    )
    init_record(
        directory,
        publishable=publishable,
        score=score,
        warnings=[] if publishable else ["[BLOCK] 재료 부족"],
    )
    return directory


@pytest.fixture
def outputs(tmp_path) -> Path:
    return tmp_path / "outputs"


class TestStateTransitions:
    def test_pending_to_approved(self, outputs):
        directory = make_output(outputs, "run-1")
        assert load_record(directory).status == STATUS_PENDING
        record = decide(directory, STATUS_APPROVED, reviewer="jin")
        assert record.status == STATUS_APPROVED
        assert record.decision == STATUS_APPROVED
        assert record.reviewed_at and record.reviewer == "jin"
        assert record.modified is False
        assert record.approval_valid_for_current_content is True
        assert record.approved_content_hash == record.content_hash
        # 파일에 유지되는지
        assert load_record(directory).status == STATUS_APPROVED

    def test_pending_to_rejected(self, outputs):
        directory = make_output(outputs, "run-1")
        record = decide(directory, STATUS_REJECTED, note="수치 오류")
        assert record.status == STATUS_REJECTED
        assert "수치 오류" in record.notes
        assert load_record(directory).status == STATUS_REJECTED

    def test_pending_to_needs_edit(self, outputs):
        directory = make_output(outputs, "run-1")
        record = decide(directory, STATUS_NEEDS_EDIT, note="cover 중복")
        assert record.status == STATUS_NEEDS_EDIT
        # needs_edit 은 사람이 손대야 하므로 modified 로 표시됩니다
        assert record.modified is True
        assert "cover 중복" in record.notes

    def test_notes_accumulate(self, outputs):
        directory = make_output(outputs, "run-1")
        decide(directory, STATUS_NEEDS_EDIT, note="첫 지적")
        record = decide(directory, STATUS_APPROVED, note="수정 확인", force=True)
        assert "첫 지적" in record.notes and "수정 확인" in record.notes

    def test_unknown_status_raises(self, outputs):
        directory = make_output(outputs, "run-1")
        with pytest.raises(ApprovalError):
            decide(directory, "published")


class TestRerenderPreservesReview:
    def refresh(self, directory: Path, *, score: float = 0.91):
        return init_record(
            directory,
            publishable=True,
            score=score,
            warnings=["재렌더 경고"],
        )

    def test_pending_survives_rerender(self, outputs):
        directory = make_output(outputs, "pending")
        before = load_record(directory)
        after = self.refresh(directory)
        assert after.status == STATUS_PENDING
        assert after.content_hash == before.content_hash

    def test_approved_survives_identical_rerender(self, outputs):
        directory = make_output(outputs, "approved")
        approved = decide(directory, STATUS_APPROVED, reviewer="jin", note="승인")
        after = self.refresh(directory)
        assert after.status == STATUS_APPROVED
        assert after.reviewer == "jin"
        assert after.notes == "승인"
        assert after.reviewed_at == approved.reviewed_at
        assert after.approval_valid_for_current_content is True

    def test_approved_content_change_invalidates_current_approval(self, outputs):
        directory = make_output(outputs, "changed")
        approved = decide(directory, STATUS_APPROVED, reviewer="jin")
        payload = json.loads((directory / "carousel.json").read_text(encoding="utf-8"))
        payload["caption"] = "바뀐 공개 캡션"
        (directory / "carousel.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

        after = self.refresh(directory)

        assert after.status == STATUS_APPROVED
        assert after.reviewer == "jin"
        assert after.approved_content_hash == approved.content_hash
        assert after.content_hash != after.approved_content_hash
        assert after.approval_valid_for_current_content is False

    @pytest.mark.parametrize("status", [STATUS_REJECTED, STATUS_NEEDS_EDIT])
    def test_non_approval_decision_survives_rerender(self, outputs, status):
        directory = make_output(outputs, status)
        decided = decide(directory, status, reviewer="jin", note="검수 메모")
        after = self.refresh(directory)
        assert after.status == status
        assert after.decision == status
        assert after.reviewed_at == decided.reviewed_at
        assert after.reviewer == "jin"
        assert after.notes == "검수 메모"


class TestApprovalBlocking:
    def test_unpublishable_cannot_be_approved(self, outputs):
        directory = make_output(outputs, "run-bad", publishable=False, score=0.41)
        with pytest.raises(ApprovalError) as exc:
            decide(directory, STATUS_APPROVED)
        assert "publishable=false" in str(exc.value)
        assert load_record(directory).status == STATUS_PENDING

    def test_block_warning_prevents_approval(self, outputs):
        directory = make_output(outputs, "run-block")
        record = load_record(directory)
        record.warnings = ["[BLOCK] 텍스트 overflow"]
        from channel.src.approval import save_record

        save_record(directory, record)
        with pytest.raises(ApprovalError):
            decide(directory, STATUS_APPROVED)

    def test_force_overrides(self, outputs):
        directory = make_output(outputs, "run-bad", publishable=False)
        record = decide(directory, STATUS_APPROVED, force=True, note="예외 승인")
        assert record.status == STATUS_APPROVED

    def test_reject_is_always_allowed(self, outputs):
        directory = make_output(outputs, "run-bad", publishable=False)
        assert decide(directory, STATUS_REJECTED).status == STATUS_REJECTED


class TestMetrics:
    def test_counts_and_rates(self, outputs):
        for index in range(4):
            make_output(outputs, f"run-{index}")
        decide(outputs / "run-0", STATUS_APPROVED)
        decide(outputs / "run-1", STATUS_REJECTED)
        decide(outputs / "run-2", STATUS_NEEDS_EDIT)
        data = metrics(outputs)
        assert data["total_generated"] == 4
        assert data["approved"] == 1
        assert data["rejected"] == 1
        assert data["needs_edit"] == 1
        assert data["pending"] == 1
        assert data["approval_rate"] == pytest.approx(1 / 3, abs=0.01)

    def test_consecutive_no_edit_approvals(self, outputs):
        for index in range(5):
            make_output(outputs, f"run-{index}")
        # run-0 은 수정 후 승인, run-1..4 는 무수정 승인
        decide(outputs / "run-0", STATUS_APPROVED, modified=True)
        for index in range(1, 5):
            decide(outputs / f"run-{index}", STATUS_APPROVED)
        data = metrics(outputs)
        assert data["consecutive_no_edit_approvals"] == 4
        assert data["no_edit_approval_rate"] == pytest.approx(0.8, abs=0.01)
        assert data["phase3_target"] == PHASE3_CONSECUTIVE_TARGET
        assert data["phase3_ready"] is False

    def test_rejection_breaks_the_streak(self, outputs):
        for index in range(3):
            make_output(outputs, f"run-{index}")
        decide(outputs / "run-0", STATUS_APPROVED)
        decide(outputs / "run-1", STATUS_REJECTED)
        decide(outputs / "run-2", STATUS_APPROVED)
        assert metrics(outputs)["consecutive_no_edit_approvals"] == 1

    def test_pending_does_not_break_the_streak(self, outputs):
        for index in range(3):
            make_output(outputs, f"run-{index}")
        decide(outputs / "run-0", STATUS_APPROVED)
        decide(outputs / "run-2", STATUS_APPROVED)  # run-1 은 pending 유지
        assert metrics(outputs)["consecutive_no_edit_approvals"] == 2

    def test_phase3_ready_requires_target(self, outputs):
        for index in range(PHASE3_CONSECUTIVE_TARGET):
            directory = make_output(outputs, f"run-{index:02d}")
            decide(directory, STATUS_APPROVED)
        data = metrics(outputs)
        assert data["consecutive_no_edit_approvals"] == PHASE3_CONSECUTIVE_TARGET
        assert data["phase3_ready"] is True

    def test_empty_outputs(self, outputs):
        outputs.mkdir(parents=True, exist_ok=True)
        data = metrics(outputs)
        assert data["total_generated"] == 0
        assert data["approval_rate"] == 0.0


class TestLookupAndPreview:
    def test_resolve_by_prefix(self, outputs):
        make_output(outputs, "2026-09-21-case1")
        assert resolve_output("2026-09-21-case1", outputs).name == "2026-09-21-case1"

    def test_resolve_missing_raises(self, outputs):
        outputs.mkdir(parents=True, exist_ok=True)
        with pytest.raises(ApprovalError):
            resolve_output("nope", outputs)

    def test_output_dirs_requires_carousel(self, outputs):
        (outputs / "empty").mkdir(parents=True)
        make_output(outputs, "real")
        assert [d.name for d in output_dirs(outputs)] == ["real"]

    def test_latest_output_uses_natural_run_order(self, outputs):
        make_output(outputs, "2026-09-21")
        make_output(outputs, "2026-09-21-2")
        latest = make_output(outputs, "2026-09-21-10")
        assert latest_output(outputs) == latest

    def test_latest_output_requires_operational_candidate(self, outputs):
        outputs.mkdir(parents=True)
        with pytest.raises(ApprovalError, match="운영 산출물이 없습니다"):
            latest_output(outputs)

    def test_default_root_is_top_level_for_review(self):
        from channel.src.approval import OUTPUTS_DIR
        from channel.src.config import load_channel_config

        assert OUTPUTS_DIR.name == "For_Review"
        assert OUTPUTS_DIR.parent == CHANNEL_ROOT.parent
        # 승인 CLI 와 파이프라인이 같은 폴더를 봐야 한다
        assert load_channel_config().outputs_dir == OUTPUTS_DIR

    def test_preview_status_is_updated(self, outputs):
        directory = make_output(outputs, "run-1")
        (directory / "preview.html").write_text(
            '<div id="approval-status" class="meta">Approval: <b>PENDING</b></div>',
            encoding="utf-8",
        )
        record = decide(directory, STATUS_APPROVED)
        text = (directory / "preview.html").read_text(encoding="utf-8")
        assert "APPROVED" in text
        assert "quality score" in text
        assert f"{record.content_quality_score:.2f}" in text

    def test_preview_missing_is_not_an_error(self, outputs):
        directory = make_output(outputs, "run-1")
        assert update_preview(directory, load_record(directory)) is False


class TestRealOutputFixture:
    """실제 산출물에서 review.md 폴백이 동작하는지 (LLM/렌더 호출 없음)."""

    def test_reads_existing_output(self, tmp_path):
        source = FIXTURES / "channel" / "v2-case2-ai-standard"
        if not (source / "review.md").exists():
            pytest.skip("v2 산출물이 없습니다")
        target = tmp_path / "outputs" / source.name
        shutil.copytree(source, target)
        (target / "approval.json").unlink(missing_ok=True)
        record = load_record(target)
        assert record.status == STATUS_PENDING
        assert record.publishable is True
        assert record.content_quality_score > 0.5
