"""슬라이드 문장별 근거 추적 — 근거 없는 주장을 게시 전에 잡습니다.

중복·문체·내부 표현 검사는 기계적이지만, "원문에 없는 사실"은 그런 검사로 잡히지
않습니다(실측: 원문에 없는 폴백 동작을 서술한 문장이 모든 게이트를 통과). 슬라이드는
한국어이고 근거는 영어라서 문자열 유사도로는 대조할 수 없으므로, LLM 심사관이
문장마다 판정하고 **그 판정을 코드가 다시 검증**합니다.

* 심사관은 근거 id 를 반드시 인용해야 합니다. 존재하지 않는 id 를 인용하거나 비워
  두면 ``unsupported`` 로 강등합니다(심사관 자신의 환각 차단).
* 판정이 누락되거나 심사 호출이 실패하면 통과가 아니라 ``unchecked`` 입니다.
  검증하지 못한 콘텐츠는 게시 대상이 아니기 때문에 validator 가 BLOCK 합니다.
* 문구를 자동으로 고치지 않습니다. 판정만 기록하고 사람이 결정합니다.

CLI (기존 산출물 재검사, 파일은 수정하지 않음)::

    python -m channel.src.grounding check <output-id> [--research path]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.agents.base import Agent
from src.core.errors import AgentError, ContentOpsError
from src.core.logging_setup import get_logger

from channel.src.approval import ApprovalError, latest_output, resolve_output
from channel.src.config import ChannelConfig, load_channel_config
from channel.src.schema import Carousel, SlideType, VerifiedTopic

logger = get_logger(__name__)

SUPPORTED = "supported"
PARTIAL = "partial"
UNSUPPORTED = "unsupported"
UNCHECKED = "unchecked"
VERDICTS = (SUPPORTED, PARTIAL, UNSUPPORTED)

STATUS_CHECKED = "checked"
STATUS_UNCHECKED = "unchecked"

INSTRUCTION = (
    "Judge whether each claim is grounded in the evidence. Use ONLY the evidence; never "
    "your own knowledge. Plausible inference that the evidence does not state is "
    "unsupported. Cite evidence ids for supported/partial. Answer every claim id. "
    'Return JSON: {"claims": [{"id": "c1", "verdict": "supported|partial|unsupported", '
    '"evidence": ["E1"], "note": "short Korean reason or empty"}]}'
)


@dataclass
class Claim:
    id: str
    slide: int
    field: str
    text: str


@dataclass
class ClaimVerdict:
    id: str
    slide: int
    text: str
    verdict: str
    evidence: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slide": self.slide,
            "text": self.text,
            "verdict": self.verdict,
            "evidence": self.evidence,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClaimVerdict:
        return cls(
            id=str(data.get("id") or ""),
            slide=int(data.get("slide") or 0),
            text=str(data.get("text") or ""),
            verdict=str(data.get("verdict") or UNCHECKED),
            evidence=[str(e) for e in (data.get("evidence") or [])],
            note=str(data.get("note") or ""),
        )


@dataclass
class GroundingResult:
    status: str = STATUS_CHECKED
    reason: str = ""
    verdicts: list[ClaimVerdict] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)

    def count(self, verdict: str) -> int:
        return sum(1 for item in self.verdicts if item.verdict == verdict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "summary": {
                name: self.count(name) for name in (*VERDICTS, UNCHECKED)
            },
            "verdicts": [item.to_dict() for item in self.verdicts],
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GroundingResult:
        return cls(
            status=str(data.get("status") or STATUS_UNCHECKED),
            reason=str(data.get("reason") or ""),
            verdicts=[
                ClaimVerdict.from_dict(item)
                for item in (data.get("verdicts") or [])
                if isinstance(item, dict)
            ],
            evidence=[
                {str(k): str(v) for k, v in item.items()}
                for item in (data.get("evidence") or [])
                if isinstance(item, dict)
            ],
        )


# --------------------------------------------------------------------------- #
# 입력 구성
# --------------------------------------------------------------------------- #
def build_evidence(topic: VerifiedTopic) -> list[dict[str, str]]:
    """검증된 research 에서 근거 문장을 번호와 함께 모읍니다.

    ``unverified_claims`` 는 일부러 넣지 않습니다. 검증되지 않은 내용에 기댄 문장은
    근거 없음으로 판정되어야 합니다.
    """
    items: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(kind: str, text: str) -> None:
        cleaned = (text or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            items.append({"id": f"E{len(items) + 1}", "kind": kind, "text": cleaned})

    for text in topic.key_facts:
        add("key_fact", text)
    for text in topic.technical_details:
        add("technical_detail", text)
    add("what_changed", topic.what_changed)
    add("why_it_matters", topic.why_it_matters)
    for text in topic.open_questions:
        add("open_question", text)
    return items


def collect_claims(carousel: Carousel) -> list[Claim]:
    """판정 대상 문장. 라벨(eyebrow/title)이 아니라 사실을 단언하는 문장만 봅니다."""
    claims: list[Claim] = []

    def add(slide: int, name: str, text: str | None) -> None:
        cleaned = (text or "").strip()
        if cleaned:
            claims.append(Claim(f"c{len(claims) + 1}", slide, name, cleaned))

    for slide in carousel.slides:
        if slide.type is SlideType.COVER:
            add(slide.index, "headline", slide.headline)
            add(slide.index, "subtitle", slide.subtitle)
            continue
        for line in slide.body:
            add(slide.index, "body", line)
        for line in slide.extra_body:
            add(slide.index, "extra_body", line)
    return claims


# --------------------------------------------------------------------------- #
# 판정 검증
# --------------------------------------------------------------------------- #
def parse_verdicts(
    raw: dict[str, Any], claims: list[Claim], evidence_ids: set[str]
) -> list[ClaimVerdict]:
    """심사관 출력을 검증해 확정합니다. 심사관의 판정을 그대로 믿지 않습니다."""
    by_id: dict[str, dict[str, Any]] = {}
    for item in raw.get("claims") or []:
        if isinstance(item, dict) and item.get("id") is not None:
            by_id[str(item["id"])] = item

    verdicts: list[ClaimVerdict] = []
    for claim in claims:
        item = by_id.get(claim.id)
        base = {"id": claim.id, "slide": claim.slide, "text": claim.text}
        if item is None:
            verdicts.append(ClaimVerdict(**base, verdict=UNCHECKED, note="심사 결과 누락"))
            continue
        verdict = str(item.get("verdict") or "").strip().lower()
        note = str(item.get("note") or "").strip()
        if verdict not in VERDICTS:
            verdicts.append(
                ClaimVerdict(**base, verdict=UNCHECKED, note=f"알 수 없는 판정 `{verdict}`")
            )
            continue
        cited = [str(e) for e in (item.get("evidence") or []) if str(e) in evidence_ids]
        if verdict in (SUPPORTED, PARTIAL) and not cited:
            verdicts.append(
                ClaimVerdict(
                    **base,
                    verdict=UNSUPPORTED,
                    note="인용한 근거 id 가 없거나 존재하지 않아 근거 없음으로 처리",
                )
            )
            continue
        verdicts.append(
            ClaimVerdict(**base, verdict=verdict, evidence=cited[:3], note=note)
        )
    return verdicts


class InstagramGroundingChecker(Agent):
    """슬라이드 문장을 research 근거와 대조하는 심사 에이전트."""

    name = "instagram_grounding"
    task = "ig_ground"
    prompt_file = "instagram_grounding.md"

    def __init__(self, *, config, llm, channel: ChannelConfig) -> None:  # noqa: ANN001
        super().__init__(config=config, llm=llm, prompt_dir=channel.prompts_dir)
        self.channel = channel

    def setting(self, key: str, default: Any = None) -> Any:
        # 심사는 결정적이어야 하고, 문장 수만큼 판정이 필요하므로 출력 여유를 둡니다.
        if key == "temperature":
            return 0.0
        if key == "max_tokens":
            return 4000
        return super().setting(key, default)

    def run(self, topic: VerifiedTopic, carousel: Carousel) -> GroundingResult:
        claims = collect_claims(carousel)
        evidence = build_evidence(topic)
        if not evidence:
            return GroundingResult(
                status=STATUS_UNCHECKED, reason="근거로 쓸 검증된 재료가 없습니다"
            )
        if not claims:
            return GroundingResult(status=STATUS_CHECKED, evidence=evidence)

        payload = {
            "evidence": evidence,
            "claims": [
                {"id": c.id, "slide": c.slide, "field": c.field, "text": c.text}
                for c in claims
            ],
        }
        try:
            raw = self.ask_json(payload, instruction=INSTRUCTION)
        except AgentError as exc:
            logger.warning("[grounding] 심사 실패: %s", exc)
            return GroundingResult(
                status=STATUS_UNCHECKED, reason=f"심사 호출 실패: {exc}", evidence=evidence
            )

        verdicts = parse_verdicts(raw, claims, {item["id"] for item in evidence})
        result = GroundingResult(
            status=STATUS_CHECKED, verdicts=verdicts, evidence=evidence
        )
        logger.info(
            "[grounding] %d개 주장: supported %d / partial %d / unsupported %d / unchecked %d",
            len(verdicts),
            result.count(SUPPORTED),
            result.count(PARTIAL),
            result.count(UNSUPPORTED),
            result.count(UNCHECKED),
        )
        return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m channel.src.grounding",
        description="기존 산출물의 문장별 근거를 다시 심사합니다 (파일 수정 없음, LLM 1회 호출).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("output_id")
    check.add_argument("--research", type=Path, default=None, help="research.json 경로")
    return parser


def _find_topic(topic_id: str, research: Path | None) -> VerifiedTopic:
    from channel.src.inputs import load_topics, select_topic

    if research is not None:
        topics, _ = load_topics(research)
        return select_topic(topics, topic_id=topic_id)
    drafts = Path(__file__).resolve().parents[2] / "outputs" / "drafts"
    for path in sorted(drafts.glob("*/research.json"), reverse=True):
        try:
            topics, _ = load_topics(path)
            return select_topic(topics, topic_id=topic_id)
        except ContentOpsError:
            continue
    raise ContentOpsError(f"topic_id `{topic_id}` 를 outputs/drafts 에서 찾지 못했습니다")


def main(argv: list[str] | None = None) -> int:
    from src.agents.llm import build_llm
    from src.core.config import load_config, load_dotenv_file

    load_dotenv_file()
    args = build_parser().parse_args(argv)
    try:
        directory = (
            latest_output() if args.output_id == "latest" else resolve_output(args.output_id)
        )
        carousel = Carousel.from_dict(
            json.loads((directory / "carousel.json").read_text(encoding="utf-8"))
        )
        topic = _find_topic(str(carousel.meta.get("topic_id") or ""), args.research)
        result = InstagramGroundingChecker(
            config=load_config(), llm=build_llm(), channel=load_channel_config()
        ).run(topic, carousel)
    except (ApprovalError, ContentOpsError, OSError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2

    print(f"{directory.name}: status={result.status} {result.to_dict()['summary']}")
    if result.reason:
        print(f"  reason: {result.reason}")
    for item in result.verdicts:
        mark = {"supported": " ok ", "partial": "part", "unsupported": "FAIL"}.get(
            item.verdict, "????"
        )
        print(f"  [{mark}] slide {item.slide}: {item.text}")
        if item.verdict != SUPPORTED:
            print(f"         -> {item.note} (근거 {', '.join(item.evidence) or '없음'})")
    return 0 if result.status == STATUS_CHECKED and not result.count(UNSUPPORTED) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
