"""자동 승인 게이트 — 사람 검수를 대신하는 코드 + 독립 심사관.

운영자는 사람이 카드를 확인하지 않고 파이프라인이 자동으로 운영하길 원합니다(HANDOFF 결정 26).
그래서 승인 결정을 **측정 가능한 게이트**로 바꿉니다. 하나라도 실패하면 승인하지 않고 보류합니다
(게이트를 낮춰서 통과시키지 않습니다).

1. 코드 검사(결정적): 엔진 판정 ``verified``, 미검증 주장 0, 근거 추적 통과, 무관한 출처 없음,
   품질 점수, BLOCK 없음, 최신성(validator).
2. 독립 심사관(LLM ``ig_review``): 작성자와 다른 프롬프트로 카드·캡션을 근거와 대조. 호출 실패·
   파싱 실패·high 이슈가 하나라도 있으면 보류(통과 쪽으로 폴백하지 않음).

통과하면 ``approval.json`` 을 ``approved`` / reviewer ``auto-gate`` 로 기록합니다. 사람이 이미 내린
결정(승인·거절·수정필요)은 덮어쓰지 않습니다. 게시(publisher)는 이 모듈이 하지 않습니다.

CLI::

    python -m channel.src.auto_gate <output-id|latest> [--no-llm] [--research path]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.agents.base import Agent
from src.core.errors import AgentError, ContentOpsError
from src.core.evidence import is_related_doc
from src.core.logging_setup import get_logger

from channel.src.approval import (
    STATUS_APPROVED,
    STATUS_PENDING,
    ApprovalError,
    decide,
    latest_output,
    load_record,
    resolve_output,
    save_record,
)
from channel.src.config import ChannelConfig, load_channel_config
from channel.src.grounding import STATUS_CHECKED, build_evidence
from channel.src.schema import Carousel, SlideType, VerifiedTopic

logger = get_logger(__name__)

REVIEWER = "auto-gate"
GATE_FILENAME = "auto_gate.json"

INSTRUCTION = (
    "Review this carousel against the evidence like an independent fact-checking editor. "
    "Use ONLY the evidence. If anything is doubtful, answer hold. "
    "Think first: write the note, THEN decide the kind; if on reflection it is not a real "
    'problem, leave the issue out entirely. Return JSON: {"decision": "pass|hold", "issues": '
    '[{"where": "slide N|caption|sources|cover", "quote": "exact card text", "evidence": ["E1"], '
    '"note": "short Korean explanation", "kind": "contradicts_evidence|inconsistent|overclaim|'
    'unclear|style"}]}'
)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class GateResult:
    checks: list[Check] = field(default_factory=list)
    issues: list[dict[str, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [check.to_dict() for check in self.checks],
            "reviewer_issues": self.issues,
        }


def _gate_settings(channel: ChannelConfig) -> dict[str, Any]:
    return channel.get("content.auto_gate", {}) or {}


# --------------------------------------------------------------------------- #
# 1. 코드 검사
# --------------------------------------------------------------------------- #
def code_checks(directory: Path, channel: ChannelConfig) -> list[Check]:
    """산출물 폴더만 보고 판정하는 결정적 검사(LLM 없음)."""
    settings = _gate_settings(channel)
    record = load_record(directory)
    data = json.loads((directory / "carousel.json").read_text(encoding="utf-8"))
    meta = data.get("meta") or {}
    checks: list[Check] = []

    required = str(settings.get("required_verdict", "verified")).lower()
    verdict = str(meta.get("fact_check_verdict") or "").lower()
    checks.append(
        Check("verdict", verdict == required, f"엔진 판정 {verdict or '없음'} (필요: {required})")
    )

    unverified = meta.get("unverified_claims") or []
    checks.append(
        Check("unverified_claims", not unverified, f"검증되지 않은 주장 {len(unverified)}건")
    )

    grounding = meta.get("grounding") or {}
    summary = grounding.get("summary") or {}
    max_partial = int(settings.get("max_partial_claims", 0))
    grounded = (
        grounding.get("status") == STATUS_CHECKED
        and int(summary.get("unsupported", 0)) == 0
        and int(summary.get("unchecked", 0)) == 0
        and int(summary.get("partial", 0)) <= max_partial
    )
    checks.append(
        Check(
            "grounding",
            grounded,
            "근거 추적 기록 없음"
            if not grounding
            else f"status={grounding.get('status')} {summary}",
        )
    )

    sources = [s for s in (meta.get("sources") or []) if isinstance(s, dict)]
    checks.append(_source_check(sources))

    minimum = float(settings.get("min_quality", 0.75))
    checks.append(
        Check(
            "quality",
            record.publishable and record.content_quality_score >= minimum,
            f"publishable={record.publishable} score={record.content_quality_score} "
            f"(기준 {minimum})",
        )
    )

    blocks = [w for w in record.warnings if w.startswith("[BLOCK]")]
    checks.append(Check("no_block", not blocks, f"BLOCK {len(blocks)}건"))
    return checks


def _source_check(sources: list[dict[str, Any]]) -> Check:
    """사건과 무관한 문서 링크가 출처로 올라오지 않았는지, 출처가 하나라도 있는지."""
    supporting = [s for s in sources if s.get("supports_event", True)]
    if not supporting:
        return Check("sources", False, "사건을 뒷받침하는 출처가 없음")
    primary = next(
        (str(s.get("url") or "") for s in supporting if s.get("source_relation") != "documentation"),
        "",
    )
    bad: list[str] = []
    for source in supporting:
        url = str(source.get("url") or "")
        if url.endswith("\\") or not urlparse(url).netloc:
            bad.append(url or "(빈 URL)")
        elif source.get("source_relation") == "documentation" and primary:
            if not is_related_doc(url, primary):
                bad.append(url)
    if bad:
        return Check("sources", False, f"무관하거나 깨진 출처 {len(bad)}건: {bad[:3]}")
    return Check("sources", True, f"출처 {len(supporting)}건")


# --------------------------------------------------------------------------- #
# 2. 독립 심사관
# --------------------------------------------------------------------------- #
class InstagramReviewer(Agent):
    """게시 직전 독립 팩트체크 편집자(LLM 1회, ``ig_review``)."""

    name = "instagram_reviewer"
    task = "ig_review"
    prompt_file = "instagram_reviewer.md"

    def __init__(self, *, config, llm, channel: ChannelConfig) -> None:  # noqa: ANN001
        super().__init__(config=config, llm=llm, prompt_dir=channel.prompts_dir)
        self.channel = channel

    def setting(self, key: str, default: Any = None) -> Any:
        # 같은 카드는 같은 판정이어야 한다(실측: 문구가 그대로인데 보류와 통과가 갈렸다).
        if key == "temperature":
            return 0.0
        if key == "max_tokens":
            return 3000
        return super().setting(key, default)

    def build_payload(self, topic: VerifiedTopic, carousel: Carousel) -> dict[str, Any]:
        slides = []
        for slide in carousel.slides:
            slides.append(
                {
                    "index": slide.index,
                    "type": str(slide.type),
                    "headline": slide.headline,
                    "subtitle": slide.subtitle,
                    "title": slide.title,
                    "body": slide.body,
                    "extra_body": slide.extra_body,
                }
            )
        return {
            "slides": slides,
            "caption": carousel.caption,
            "sources": carousel.meta.get("sources") or [s.to_dict() for s in topic.sources],
            # 근거 추적(grounding)과 같은 근거 목록(key_facts·technical_details·what_changed 등)을 써야
            # 한다. 일부만 주면 근거에 있는 내용을 "근거 없음"으로 오판한다.
            "evidence": build_evidence(topic),
            "engine_verdict": topic.verdict,
        }

    def run(self, topic: VerifiedTopic, carousel: Carousel) -> tuple[bool, list[dict[str, str]]]:
        """(통과 여부, 이슈 목록). 실패는 통과가 아니라 보류다."""
        try:
            data = self.ask_json(self.build_payload(topic, carousel), instruction=INSTRUCTION)
        except AgentError as exc:
            return False, [{"severity": "high", "where": "reviewer", "note": f"심사 실패: {exc}"}]
        haystack = "\n".join(
            [carousel.caption or "", *(_slide_texts(carousel))]
        )
        return parse_review(data, haystack)


def _slide_texts(carousel: Carousel) -> list[str]:
    texts: list[str] = []
    for slide in carousel.slides:
        texts += [slide.headline, slide.subtitle, slide.title, *slide.body, *slide.extra_body]
    return [t for t in texts if t]


# 심사관이 붙이는 이슈 종류. 심각도는 모델이 아니라 **코드가 종류로 정한다**(모델이 "문제 없음"이라 쓰면서
# high 로 표시하던 오탐을 막는다). 여기에 없는 종류(예: "ok")는 이슈로 세지 않는다.
# 모델이 이슈를 적다가 스스로 문제가 아니라고 철회하는 문구(실측: "이 항목은 철회", "문제 없음").
_RETRACTED = ("철회", "문제 없음", "문제없음", "모순 없음", "충돌 없음", "이슈 아님", "오탐", "일치하여 문제")
HIGH_KINDS = frozenset({"contradicts_evidence", "inconsistent", "overclaim"})
LOW_KINDS = frozenset({"unclear", "style"})


def parse_review(data: Any, haystack: str | None = None) -> tuple[bool, list[dict[str, str]]]:
    """심사 결과를 해석한다.

    * 형식이 틀리면 보류. 이슈마다 ``kind``·``quote``(카드 문구 그대로)가 필요하며, 문구가 카드에 실제로
      없으면(지어낸 지적) 버린다. 심각도는 종류로 정한다.
    * high 가 하나라도 있으면 보류. 사유 없이 ``hold`` 만 말하면 그것도 보류.
    """
    if not isinstance(data, dict) or data.get("decision") not in {"pass", "hold"}:
        return False, [
            {"severity": "high", "where": "reviewer", "note": "심사 결과 형식이 올바르지 않음"}
        ]
    raw_items = [item for item in (data.get("issues") or []) if isinstance(item, dict)]
    issues: list[dict[str, str]] = []
    for item in raw_items:
        kind = str(item.get("kind") or "").strip().lower()
        quote = str(item.get("quote") or "").strip()
        if kind not in HIGH_KINDS | LOW_KINDS:
            continue
        if any(phrase in str(item.get("note") or "") for phrase in _RETRACTED):
            continue  # 스스로 철회한 지적
        if haystack is not None and (not quote or quote not in haystack):
            continue  # 카드에 없는 문구를 근거로 한 지적은 버린다
        issues.append(
            {
                "severity": "high" if kind in HIGH_KINDS else "low",
                "kind": kind,
                "where": str(item.get("where") or ""),
                "quote": quote,
                "note": str(item.get("note") or ""),
            }
        )
    if any(issue["severity"] == "high" for issue in issues):
        return False, issues
    if data["decision"] == "hold" and not raw_items:
        return False, [
            {"severity": "high", "where": "reviewer", "note": "심사관이 사유 없이 보류를 요청함"}
        ]
    return True, issues


# --------------------------------------------------------------------------- #
# 3. 판정과 기록
# --------------------------------------------------------------------------- #
def evaluate(
    directory: Path,
    channel: ChannelConfig,
    *,
    reviewer: Any | None = None,
    topic: VerifiedTopic | None = None,
) -> GateResult:
    """코드 검사 후, 모두 통과했을 때만 독립 심사관을 돌린다(보류가 확정이면 LLM 비용을 아낀다)."""
    result = GateResult(checks=code_checks(directory, channel))
    if not result.passed:
        return result
    if reviewer is None or topic is None:
        result.checks.append(Check("reviewer", False, "독립 심사관이 실행되지 않음"))
        return result
    carousel = Carousel.from_dict(
        json.loads((directory / "carousel.json").read_text(encoding="utf-8"))
    )
    ok, issues = reviewer.run(topic, carousel)
    result.issues = issues
    high = [i for i in issues if i["severity"] == "high"]
    result.checks.append(Check("reviewer", ok, f"high 이슈 {len(high)}건" if high else "통과"))
    return result


def apply(directory: Path, result: GateResult) -> str:
    """결과를 기록한다. 통과하면 자동 승인, 아니면 보류 사유를 남긴다. 반환: approved|held|skipped."""
    (directory / GATE_FILENAME).write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    record = load_record(directory)
    if record.status != STATUS_PENDING:
        return "skipped"  # 사람(또는 이전 게이트)이 이미 결정함 — 덮어쓰지 않는다
    if result.passed:
        try:
            decide(directory, STATUS_APPROVED, reviewer=REVIEWER, note="auto-gate: 모든 게이트 통과")
        except ApprovalError as exc:
            logger.warning("[auto_gate] 승인 기록 거부: %s", exc)
            return "held"
        return "approved"
    reasons = "; ".join(f"{c.name}: {c.detail}" for c in result.failures)
    note = f"auto-gate 보류 — {reasons}"
    record.notes = f"{record.notes}\n{note}".strip() if record.notes else note
    save_record(directory, record)
    return "held"


def run_gate(
    directory: Path,
    channel: ChannelConfig,
    *,
    reviewer: Any | None,
    topic: VerifiedTopic | None,
) -> tuple[str, GateResult]:
    """파이프라인·CLI 공용 진입점."""
    result = evaluate(directory, channel, reviewer=reviewer, topic=topic)
    outcome = apply(directory, result)
    logger.info("[auto_gate] %s → %s", directory.name, outcome)
    return outcome, result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="channel.src.auto_gate", description=__doc__)
    parser.add_argument("output_id", help="출력 폴더 이름 또는 latest")
    parser.add_argument("--no-llm", action="store_true", help="코드 검사만(승인하지 않음)")
    parser.add_argument("--research", type=Path, default=None, help="research.json 경로")
    return parser


def main(argv: list[str] | None = None) -> int:
    from src.agents.llm import build_llm
    from src.core.config import load_config, load_dotenv_file

    from channel.src.grounding import _find_topic

    load_dotenv_file()
    args = build_parser().parse_args(argv)
    channel = load_channel_config()
    try:
        directory = (
            latest_output() if args.output_id == "latest" else resolve_output(args.output_id)
        )
        if args.no_llm:
            result = GateResult(checks=code_checks(directory, channel))
            outcome = "dry-run"
        else:
            carousel = Carousel.from_dict(
                json.loads((directory / "carousel.json").read_text(encoding="utf-8"))
            )
            topic = _find_topic(str(carousel.meta.get("topic_id") or ""), args.research)
            reviewer = InstagramReviewer(config=load_config(), llm=build_llm(), channel=channel)
            outcome, result = run_gate(directory, channel, reviewer=reviewer, topic=topic)
    except (ApprovalError, ContentOpsError, OSError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2

    print(f"{directory.name}: {outcome}")
    for check in result.checks:
        print(f"  [{'ok' if check.passed else 'FAIL'}] {check.name}: {check.detail}")
    for issue in result.issues:
        print(f"  ({issue['severity']}) {issue['where']}: {issue['note']}")
    return 0 if outcome == "approved" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
