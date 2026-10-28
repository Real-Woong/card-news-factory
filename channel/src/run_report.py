"""채널 실행 단위 결과와 스킵 이유를 남기는 내구성 JSON 보고서."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.text import to_iso, utcnow

RUN_REPORT_FILENAME = "run_report.json"

RESULT_GENERATED = "generated"
RESULT_SKIPPED = "skipped"
RESULT_FAILED = "failed"

REASON_MATERIAL_TOO_THIN = "material_too_thin"
REASON_DUPLICATE_TOPIC = "duplicate_topic"
REASON_FACT_CHECK_FAILED = "fact_check_failed"
REASON_QUALITY_GATE_FAILED = "quality_gate_failed"
REASON_GENERATION_FAILED = "generation_failed"

REASON_CODES = (
    REASON_MATERIAL_TOO_THIN,
    REASON_DUPLICATE_TOPIC,
    REASON_FACT_CHECK_FAILED,
    REASON_QUALITY_GATE_FAILED,
    REASON_GENERATION_FAILED,
)


@dataclass
class TopicRunResult:
    topic_id: str
    title: str
    result: str
    reason: str = ""
    output_id: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "topic_id": self.topic_id,
            "title": self.title,
            "result": self.result,
            "reason": self.reason,
            "output_id": self.output_id,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass
class ChannelRunReport:
    run_id: str
    started_at: str = field(default_factory=lambda: to_iso(utcnow()))
    completed_at: str | None = None
    topics: list[TopicRunResult] = field(default_factory=list)

    def add(
        self,
        *,
        topic_id: str,
        title: str,
        result: str,
        reason: str = "",
        output_id: str | None = None,
        detail: str = "",
    ) -> None:
        if result not in {RESULT_GENERATED, RESULT_SKIPPED, RESULT_FAILED}:
            raise ValueError(f"알 수 없는 topic result: {result}")
        if reason and reason not in REASON_CODES:
            raise ValueError(f"알 수 없는 reason code: {reason}")
        self.topics.append(
            TopicRunResult(
                topic_id=topic_id,
                title=title,
                result=result,
                reason=reason,
                output_id=output_id,
                detail=detail,
            )
        )

    def complete(self) -> None:
        self.completed_at = to_iso(utcnow())

    def to_dict(self) -> dict[str, Any]:
        counts = {
            result: sum(1 for topic in self.topics if topic.result == result)
            for result in (RESULT_GENERATED, RESULT_SKIPPED, RESULT_FAILED)
        }
        reasons = {
            reason: sum(1 for topic in self.topics if topic.reason == reason)
            for reason in REASON_CODES
        }
        recovery_codes = {
            "llm_recovery_failed",
            "thin_slide_unresolved",
            "duplicate_unresolved",
            "cover_rewrite_failed",
            "thin_slide_after_repair",
            "semantic_duplicate",
            "cover_body_semantic_duplicate",
        }
        recovery_failed = sum(
            1
            for topic in self.topics
            if any(code in topic.detail for code in recovery_codes)
        )
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "candidates_seen": len(self.topics),
            "generated": counts[RESULT_GENERATED],
            "skipped": counts[RESULT_SKIPPED],
            "failed": counts[RESULT_FAILED],
            "skip_reasons": reasons,
            "quality_metrics": {"recovery_failed": recovery_failed},
            "topics": [topic.to_dict() for topic in self.topics],
        }


def write_run_report(report: ChannelRunReport, output_dir: Path) -> Path:
    """보고서를 임시 파일 후 replace해 중간 쓰기 상태를 노출하지 않습니다."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / RUN_REPORT_FILENAME
    temporary = output_dir / f".{RUN_REPORT_FILENAME}.tmp"
    temporary.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)
    return path


def run_report_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        (
            path
            for path in root.iterdir()
            if path.is_dir() and (path / RUN_REPORT_FILENAME).exists()
        ),
        key=lambda path: [
            int(part) if part.isdigit() else part
            for part in re.split(r"(\d+)", path.name)
        ],
    )


def resolve_run_report(output_id: str, root: Path) -> Path:
    if output_id == "latest":
        directories = run_report_dirs(root)
        if not directories:
            raise FileNotFoundError("운영 run report가 없습니다.")
        return directories[-1] / RUN_REPORT_FILENAME

    direct = root / output_id / RUN_REPORT_FILENAME
    if direct.exists():
        return direct
    matches = [path for path in run_report_dirs(root) if path.name.startswith(output_id)]
    if len(matches) == 1:
        return matches[0] / RUN_REPORT_FILENAME
    if not matches:
        raise FileNotFoundError(f"run report를 찾을 수 없습니다: {output_id}")
    raise ValueError(f"`{output_id}` 가 여러 run report에 해당합니다.")
