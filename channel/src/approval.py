"""Phase 2 — 사람 승인 레이어.

흐름::

    content generated → quality gate → human review → approval record

자동 게시는 하지 않습니다. 이 모듈이 하는 일은 승인 결정을 파일로 남기고
(``approval.json``) 통계를 계산하는 것뿐입니다.

CLI::

    python -m channel.src.approval list
    python -m channel.src.approval latest
    python -m channel.src.approval run-report latest
    python -m channel.src.approval show <output-id>
    python -m channel.src.approval approve <output-id> [--reviewer NAME] [--force]
    python -m channel.src.approval reject <output-id> --note "..."
    python -m channel.src.approval needs-edit <output-id> --note "..."
    python -m channel.src.approval metrics

``publishable=false`` 또는 BLOCK 이 있으면 승인을 막습니다(``--force`` 로만 우회).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.errors import ContentOpsError
from src.core.text import to_iso, utcnow

CHANNEL_ROOT = Path(__file__).resolve().parents[1]
# 검수·승인 대상 산출물은 프로젝트 최상단 ``For_Review/`` 에 둡니다.
# 파이프라인의 instagram.yaml ``output.dir`` 과 동일해야 합니다.
OUTPUTS_DIR = (CHANNEL_ROOT.parent / "For_Review").resolve()
APPROVAL_FILENAME = "approval.json"

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_NEEDS_EDIT = "needs_edit"
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_NEEDS_EDIT)

# Phase 3 후보 기준 (문서상 목표. 자동 전환하지 않고 사람이 판단합니다.)
PHASE3_CONSECUTIVE_TARGET = 10


@dataclass
class ApprovalRecord:
    """출력 디렉터리 하나의 승인 상태."""

    status: str = STATUS_PENDING
    decision: str | None = None
    reviewed_at: str | None = None
    reviewer: str | None = None
    modified: bool = False
    notes: str = ""
    publishable: bool = False
    content_quality_score: float = 0.0
    warnings: list[str] = field(default_factory=list)
    content_hash: str | None = None
    approved_content_hash: str | None = None

    @property
    def approval_valid_for_current_content(self) -> bool:
        """현재 공개 콘텐츠가 승인받은 버전과 일치하는지 반환합니다."""
        return bool(
            self.status == STATUS_APPROVED
            and self.content_hash
            and self.content_hash == self.approved_content_hash
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "decision": self.decision,
            "reviewed_at": self.reviewed_at,
            "reviewer": self.reviewer,
            "modified": self.modified,
            "notes": self.notes,
            "publishable": self.publishable,
            "content_quality_score": self.content_quality_score,
            "warnings": self.warnings,
            "content_hash": self.content_hash,
            "approved_content_hash": self.approved_content_hash,
            "approval_valid_for_current_content": self.approval_valid_for_current_content,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalRecord:
        status = str(data.get("status") or STATUS_PENDING)
        return cls(
            status=status if status in STATUSES else STATUS_PENDING,
            decision=data.get("decision"),
            reviewed_at=data.get("reviewed_at"),
            reviewer=data.get("reviewer"),
            modified=bool(data.get("modified", False)),
            notes=str(data.get("notes") or ""),
            publishable=bool(data.get("publishable", False)),
            content_quality_score=float(data.get("content_quality_score") or 0.0),
            warnings=[str(w) for w in (data.get("warnings") or [])],
            content_hash=data.get("content_hash"),
            approved_content_hash=data.get("approved_content_hash"),
        )


# --------------------------------------------------------------------------- #
# 파일 I/O
# --------------------------------------------------------------------------- #
def content_hash(directory: Path) -> str | None:
    """승인 대상인 공개 콘텐츠의 안정적 SHA-256 해시를 계산합니다.

    렌더 측정값과 LLM 사용량 같은 내부 metadata는 재렌더마다 달라질 수
    있으므로 제외하고, 슬라이드·캡션·해시태그·출처만 포함합니다.
    """
    path = directory / "carousel.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    public_keys = (
        "channel",
        "brand",
        "template",
        "topic",
        "headline",
        "caption",
        "hashtags",
    )
    slide_keys = (
        "index",
        "type",
        "eyebrow",
        "headline",
        "subtitle",
        "title",
        "body",
        "extra_title",
        "extra_body",
        "sources",
    )
    canonical = {key: payload.get(key) for key in public_keys if key in payload}
    canonical["slides"] = [
        {key: slide.get(key) for key in slide_keys if key in slide}
        for slide in (payload.get("slides") or [])
        if isinstance(slide, dict)
    ]
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_review(directory: Path) -> tuple[bool, float, list[str]]:
    """``review.md`` 에서 publishable / score / 경고를 읽습니다.

    파이프라인이 approval.json 을 만들기 전에 생성된 기존 산출물도 다루기 위한
    폴백입니다.
    """
    review = directory / "review.md"
    if not review.exists():
        return False, 0.0, []
    text = review.read_text(encoding="utf-8")
    publishable = "publishable = true" in text
    score = 0.0
    match = re.search(r"score = ([0-9.]+)", text)
    if match:
        score = float(match.group(1))
    warnings = [
        line[len("- [확인] ") :].strip()
        for line in text.splitlines()
        if line.startswith("- [확인] ")
    ]
    blocks = [
        line[len("- [차단] ") :].strip()
        for line in text.splitlines()
        if line.startswith("- [차단] ")
    ]
    # BLOCK 은 경고 목록 앞에 붙여 눈에 띄게 합니다.
    return publishable, score, [f"[BLOCK] {b}" for b in blocks] + warnings


def load_record(directory: Path) -> ApprovalRecord:
    """approval.json 을 읽습니다. 없으면 review.md 기준으로 pending 레코드 생성."""
    path = directory / APPROVAL_FILENAME
    if path.exists():
        try:
            return ApprovalRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    publishable, score, warnings = _parse_review(directory)
    return ApprovalRecord(
        publishable=publishable, content_quality_score=score, warnings=warnings
    )


def save_record(directory: Path, record: ApprovalRecord) -> Path:
    path = directory / APPROVAL_FILENAME
    path.write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def init_record(
    directory: Path,
    *,
    publishable: bool,
    score: float,
    warnings: list[str],
) -> ApprovalRecord:
    """생성 metadata를 반영하되 기존 사람의 결정은 보존합니다.

    최초 생성이면 pending 레코드를 만들고, 재렌더라면 상태·검수자·
    메모를 그대로 둔 채 현재 콘텐츠 해시와 품질 정보만 갱신합니다.
    """
    path = directory / APPROVAL_FILENAME
    record = load_record(directory) if path.exists() else ApprovalRecord()
    record.publishable = publishable
    record.content_quality_score = round(float(score), 4)
    record.warnings = list(warnings)
    record.content_hash = content_hash(directory)
    save_record(directory, record)
    update_preview(directory, record)
    return record


# --------------------------------------------------------------------------- #
# 결정
# --------------------------------------------------------------------------- #
class ApprovalError(Exception):
    """승인 규칙 위반."""


def _blocked_reason(record: ApprovalRecord) -> str | None:
    if not record.publishable:
        return "publishable=false"
    blocks = [w for w in record.warnings if w.startswith("[BLOCK]")]
    if blocks:
        return f"BLOCK {len(blocks)}건"
    return None


def decide(
    directory: Path,
    status: str,
    *,
    note: str = "",
    reviewer: str | None = None,
    modified: bool = False,
    force: bool = False,
) -> ApprovalRecord:
    """승인/거절/수정필요 결정을 기록합니다.

    Raises:
        ApprovalError: 승인 조건을 만족하지 않고 ``force`` 도 아닌 경우.
    """
    if status not in STATUSES:
        raise ApprovalError(f"알 수 없는 status: {status}")

    record = load_record(directory)
    if status == STATUS_APPROVED and not force:
        reason = _blocked_reason(record)
        if reason:
            raise ApprovalError(
                f"승인할 수 없습니다 ({reason}). review.md 의 차단 항목을 해결하거나 "
                "--force 로 강제하세요."
            )

    record.status = status
    record.decision = status
    record.reviewed_at = to_iso(utcnow())
    record.reviewer = reviewer or record.reviewer
    record.modified = modified or record.modified
    if note:
        record.notes = f"{record.notes}\n{note}".strip() if record.notes else note
    if status == STATUS_NEEDS_EDIT:
        record.modified = True
    if status == STATUS_APPROVED:
        # 승인 시점의 공개 콘텐츠를 고정합니다. 이후 재생성이 상태를
        # 지우지는 않지만, 해시가 달라지면 현재 승인은 무효가 됩니다.
        record.content_hash = content_hash(directory) or record.content_hash
        record.approved_content_hash = record.content_hash

    save_record(directory, record)
    update_preview(directory, record)
    return record


# --------------------------------------------------------------------------- #
# preview.html 상태 표시 (최소 수정)
# --------------------------------------------------------------------------- #
PREVIEW_MARKER = re.compile(
    r'(<div id="approval-status"[^>]*>)(.*?)(</div>)', re.DOTALL
)


def status_line(record: ApprovalRecord) -> str:
    blocks = sum(1 for w in record.warnings if w.startswith("[BLOCK]"))
    warns = len(record.warnings) - blocks
    return (
        f"Approval: <b>{record.status.upper()}</b> &nbsp;·&nbsp; "
        f"publishable: {str(record.publishable).lower()} &nbsp;·&nbsp; "
        f"quality score: {record.content_quality_score:.2f} &nbsp;·&nbsp; "
        f"approval valid: {str(record.approval_valid_for_current_content).lower()} "
        f"&nbsp;·&nbsp; "
        f"warnings: {warns}"
        + (f" &nbsp;·&nbsp; blocks: {blocks}" if blocks else "")
    )


def update_preview(directory: Path, record: ApprovalRecord) -> bool:
    """preview.html 의 상태 줄만 갱신합니다(있을 때)."""
    preview = directory / "preview.html"
    if not preview.exists():
        return False
    text = preview.read_text(encoding="utf-8")
    if not PREVIEW_MARKER.search(text):
        return False
    updated = PREVIEW_MARKER.sub(
        lambda m: m.group(1) + status_line(record) + m.group(3), text, count=1
    )
    preview.write_text(updated, encoding="utf-8")
    return True


# --------------------------------------------------------------------------- #
# 통계
# --------------------------------------------------------------------------- #
def output_dirs(root: Path | None = None) -> list[Path]:
    """운영 산출물 디렉터리 목록 (생성 순서)."""
    base = root or OUTPUTS_DIR
    if not base.exists():
        return []
    return sorted(
        (p for p in base.iterdir() if p.is_dir() and (p / "carousel.json").exists()),
        key=lambda p: [
            int(part) if part.isdigit() else part
            for part in re.split(r"(\d+)", p.name)
        ],
    )


def latest_output(root: Path | None = None) -> Path:
    """가장 최근에 생성된 운영 후보를 반환합니다."""
    dirs = output_dirs(root)
    if not dirs:
        raise ApprovalError(
            "운영 산출물이 없습니다. 먼저 채널 파이프라인을 실행하세요."
        )
    return dirs[-1]


def metrics(root: Path | None = None) -> dict[str, Any]:
    """승인 통계.

    ``consecutive_no_edit_approvals`` 는 최신 산출물부터 거꾸로 세어, 수정 없이
    승인된 건이 연속으로 몇 개인지입니다. Phase 3 판단 재료이며 자동 전환은
    하지 않습니다.
    """
    dirs = output_dirs(root)
    records = [(d, load_record(d)) for d in dirs]
    counts = {status: 0 for status in STATUSES}
    for _d, record in records:
        counts[record.status] += 1

    total = len(records)
    decided = counts[STATUS_APPROVED] + counts[STATUS_REJECTED] + counts[STATUS_NEEDS_EDIT]
    no_edit_approved = sum(
        1
        for _d, r in records
        if r.approval_valid_for_current_content and not r.modified
    )

    consecutive = 0
    for _d, record in reversed(records):
        if record.approval_valid_for_current_content and not record.modified:
            consecutive += 1
        elif record.status == STATUS_PENDING:
            continue  # 아직 검수하지 않은 건은 연속성을 끊지 않습니다
        else:
            break

    return {
        "total_generated": total,
        "approved": counts[STATUS_APPROVED],
        "rejected": counts[STATUS_REJECTED],
        "needs_edit": counts[STATUS_NEEDS_EDIT],
        "pending": counts[STATUS_PENDING],
        "approval_rate": round(counts[STATUS_APPROVED] / decided, 3) if decided else 0.0,
        "no_edit_approval_rate": round(no_edit_approved / decided, 3) if decided else 0.0,
        "consecutive_no_edit_approvals": consecutive,
        "phase3_target": PHASE3_CONSECUTIVE_TARGET,
        "phase3_ready": consecutive >= PHASE3_CONSECUTIVE_TARGET,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def resolve_output(output_id: str, root: Path | None = None) -> Path:
    """디렉터리 이름 또는 접두사로 산출물을 찾습니다."""
    base = root or OUTPUTS_DIR
    direct = base / output_id
    if direct.is_dir():
        return direct
    matches = [d for d in output_dirs(base) if d.name.startswith(output_id)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ApprovalError(f"산출물을 찾을 수 없습니다: {output_id}")
    raise ApprovalError(
        f"`{output_id}` 가 여러 개에 해당합니다: {[m.name for m in matches]}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m channel.src.approval",
        description="R_A_Glance 승인 기록 (Phase 2). 자동 게시는 하지 않습니다.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="산출물과 승인 상태 목록")
    sub.add_parser("metrics", help="승인 통계")
    sub.add_parser("latest", aliases=["show-latest"], help="최신 운영 후보와 preview 경로")
    run_report = sub.add_parser("run-report", help="실행별 생성·스킵·실패 보고서")
    run_report.add_argument("output_id", nargs="?", default="latest")

    show = sub.add_parser("show", help="한 건의 상세")
    show.add_argument("output_id")

    hook = sub.add_parser(
        "choose-hook", help="표지 제목 후보 중 하나로 교체(재렌더·근거 재심사는 따로)"
    )
    hook.add_argument("output_id")
    hook.add_argument("choice", help="후보 스타일(question·number·contrast) 또는 번호")

    for name, help_text in (
        ("approve", "승인"),
        ("reject", "거절"),
        ("needs-edit", "수정 필요"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("output_id")
        cmd.add_argument("--note", default="", help="검수 메모")
        cmd.add_argument("--reviewer", default=None, help="검수자 이름")
        if name == "approve":
            cmd.add_argument(
                "--force",
                action="store_true",
                help="publishable=false / BLOCK 이 있어도 강제 승인",
            )
            cmd.add_argument(
                "--modified",
                action="store_true",
                help="사람이 문장을 고친 뒤 승인한 경우 (no-edit 통계에서 제외)",
            )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.command == "list":
            dirs = output_dirs()
            if not dirs:
                print("산출물이 없습니다.")
                return 0
            print(
                f"{'output-id':34} {'status':11} {'valid':>5} "
                f"{'pub':>5} {'score':>5}  warn"
            )
            for directory in dirs:
                record = load_record(directory)
                blocks = sum(1 for w in record.warnings if w.startswith("[BLOCK]"))
                warn = len(record.warnings) - blocks
                flag = f"{warn}" + (f"+{blocks}B" if blocks else "")
                print(
                    f"{directory.name:34} {record.status:11} "
                    f"{str(record.approval_valid_for_current_content).lower():>5} "
                    f"{str(record.publishable).lower():>5} "
                    f"{record.content_quality_score:5.2f}  {flag}"
                )
            return 0

        if args.command == "metrics":
            print(json.dumps(metrics(), ensure_ascii=False, indent=2))
            return 0

        if args.command in {"latest", "show-latest"}:
            directory = latest_output()
            record = load_record(directory)
            print(directory.name)
            print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
            print(f"preview: {directory / 'preview.html'}")
            print(f"review: {directory / 'review.md'}")
            return 0

        if args.command == "run-report":
            from channel.src.run_report import resolve_run_report

            try:
                path = resolve_run_report(args.output_id, OUTPUTS_DIR)
            except (FileNotFoundError, ValueError) as exc:
                raise ApprovalError(str(exc)) from exc
            print(path.read_text(encoding="utf-8"))
            return 0

        directory = resolve_output(args.output_id)

        if args.command == "choose-hook":
            from channel.src.hook import apply_choice

            try:
                headline = apply_choice(directory / "carousel.json", args.choice)
            except ContentOpsError as exc:
                raise ApprovalError(str(exc)) from exc
            print(f"{directory.name} 표지 제목 → {headline.replace(chr(10), ' / ')}")
            print("다음: 직접 터미널에서 재렌더 후 근거를 다시 심사하세요.")
            print(f"  python -m channel.src.pipeline --render-only {directory / 'carousel.json'}")
            print(f"  python -m channel.src.grounding check {directory.name}")
            return 0

        if args.command == "show":
            record = load_record(directory)
            print(f"{directory.name}")
            print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
            return 0

        status = {
            "approve": STATUS_APPROVED,
            "reject": STATUS_REJECTED,
            "needs-edit": STATUS_NEEDS_EDIT,
        }[args.command]
        record = decide(
            directory,
            status,
            note=getattr(args, "note", ""),
            reviewer=getattr(args, "reviewer", None),
            modified=getattr(args, "modified", False),
            force=getattr(args, "force", False),
        )
        print(f"{directory.name} → {record.status}")
        if record.notes:
            print(f"notes: {record.notes}")
        return 0

    except ApprovalError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
