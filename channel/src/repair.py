"""근거 부족 문장·어려운 제목의 자동 수정(1~2회) — 사람이 고치는 일을 대신한다.

근거 추적이 ``partial``/``unsupported`` 로 판정한 문장과 영어 전문용어 제목을 LLM(``ig_repair``)이
근거가 지지하는 범위로 좁혀 쓰거나(본문 줄은 삭제 가능) 쉬운 말로 바꿉니다. 모델의 출력은 코드가
다시 검증하며, 어긴 수정은 **적용하지 않습니다**(그 문장은 그대로 남아 게이트가 보류합니다):

* 고칠 대상(id)이 아닌 곳은 건드리지 않는다. 원문과 정확히 일치하는 문장만 바꾼다.
* 새 문장의 숫자는 원문 또는 근거에 있는 것만 허용(새 수치 금지). URL·내부 표현 금지.
* 글자 수 상한, 삭제 후 최소 줄 수, 제목 규칙(전문용어)을 지킨다.

수정 후에는 근거 추적을 다시 돌려 결과를 반영합니다. 무한 반복하지 않습니다(``repair.max_rounds``).

CLI (기존 산출물 수정; 이후 ``--render-only`` 와 ``auto_gate`` 를 다시 실행)::

    python -m channel.src.repair <output-id|latest> [--research path]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.agents.base import Agent
from src.core.errors import AgentError, ContentOpsError
from src.core.logging_setup import get_logger

from channel.src.approval import ApprovalError, latest_output, resolve_output
from channel.src.config import ChannelConfig, load_channel_config
from channel.src.grounding import (
    PARTIAL,
    STATUS_CHECKED,
    UNSUPPORTED,
    GroundingResult,
    InstagramGroundingChecker,
)
from channel.src.schema import Carousel, Slide, SlideType, VerifiedTopic
from channel.src.title_style import JARGON, title_problems

logger = get_logger(__name__)

INSTRUCTION = (
    "Fix ONLY the listed targets so that they stay inside what the evidence supports. "
    "Never add facts, numbers, dates or names that are not in the evidence or the original. "
    'Return JSON: {"fixes": [{"id": "t1", "action": "rewrite|drop", "text": "..."}]}'
)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_URL = re.compile(r"https?://|www\.", re.I)
# 근거가 영어 숫자 낱말("two")로 말한 것을 한국어 숫자("2종")로 옮기는 것은 새 숫자가 아니다.
_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}


def _allowed_numbers(*texts: str) -> set[str]:
    """원문·근거에 나온 숫자(숫자 낱말 포함)."""
    allowed: set[str] = set()
    for text in texts:
        allowed |= set(_NUMBER.findall(text))
        lowered = text.lower()
        allowed |= {
            digit for word, digit in _NUMBER_WORDS.items()
            if re.search(rf"\b{word}\b", lowered)
        }
    return allowed


@dataclass
class Target:
    id: str
    slide: int
    field: str  # title | headline | subtitle | body | extra_body
    text: str
    reason: str
    evidence: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slide": self.slide,
            "field": self.field,
            "text": self.text,
            "reason": self.reason,
            "evidence": self.evidence,
        }


@dataclass
class RepairReport:
    applied: list[dict[str, str]] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"applied": self.applied, "rejected": self.rejected}


# --------------------------------------------------------------------------- #
# 대상 수집
# --------------------------------------------------------------------------- #
def collect_targets(carousel: Carousel, channel: ChannelConfig) -> list[Target]:
    """근거가 부족한 문장 + 규칙을 어긴 제목."""
    grounding = carousel.meta.get("grounding") or {}
    evidence_text = {str(e.get("id")): str(e.get("text")) for e in grounding.get("evidence") or []}
    targets: list[Target] = []

    for verdict in grounding.get("verdicts") or []:
        kind = verdict.get("verdict")
        if kind not in {PARTIAL, UNSUPPORTED}:
            continue
        slide = _slide(carousel, int(verdict.get("slide") or 0))
        field_name = _locate(slide, str(verdict.get("text") or "")) if slide else None
        if slide is None or field_name is None:
            continue
        reason = ("일부만 근거가 있음" if kind == PARTIAL else "근거 없음") + (
            f": {verdict.get('note')}" if verdict.get("note") else ""
        )
        targets.append(
            Target(
                id=f"t{len(targets) + 1}",
                slide=slide.index,
                field=field_name,
                text=str(verdict["text"]),
                reason=reason,
                evidence=[evidence_text[e] for e in verdict.get("evidence") or [] if e in evidence_text],
            )
        )

    for slide in carousel.slides:
        if slide.type is SlideType.COVER or not slide.title:
            continue
        if any(code == JARGON for code, _ in title_problems(slide.title, channel)):
            targets.append(
                Target(
                    id=f"t{len(targets) + 1}",
                    slide=slide.index,
                    field="title",
                    text=slide.title,
                    reason="제목 전문용어: 쉬운 한국어로(용어는 본문에 있음)",
                    evidence=[line for line in slide.body[:3]],
                )
            )
    return targets


def _slide(carousel: Carousel, index: int) -> Slide | None:
    return next((s for s in carousel.slides if s.index == index), None)


def _locate(slide: Slide, text: str) -> str | None:
    """문장이 슬라이드의 어느 필드에 있는지."""
    if slide.headline and slide.headline.strip() == text:
        return "headline"
    if slide.subtitle and slide.subtitle.strip() == text:
        return "subtitle"
    if text in [line.strip() for line in slide.body]:
        return "body"
    if text in [line.strip() for line in slide.extra_body]:
        return "extra_body"
    return None


# --------------------------------------------------------------------------- #
# 수정 적용(코드가 검증)
# --------------------------------------------------------------------------- #
def _limit(channel: ChannelConfig, slide: Slide, field_name: str) -> int:
    if field_name == "headline":
        return int(channel.get("content.text_budget.cover.headline_max_chars", 78))
    if field_name == "subtitle":
        return int(channel.get("content.text_budget.cover.subtitle_max_chars", 40))
    if field_name == "title":
        return int(channel.get("content.title_style.max_chars", 30)) + 10
    section = "closing" if slide.type is SlideType.CLOSING else "content"
    key = "secondary_body_item_max_chars" if field_name == "extra_body" else "body_item_max_chars"
    return int(channel.get(f"content.text_budget.{section}.{key}", 92))


def _min_items(channel: ChannelConfig, slide: Slide, field_name: str) -> int:
    section = "closing" if slide.type is SlideType.CLOSING else "content"
    key = "secondary_body_min_items" if field_name == "extra_body" else "body_min_items"
    return int(channel.get(f"content.text_budget.{section}.{key}", 1))


def _rejection(
    new: str, target: Target, slide: Slide, channel: ChannelConfig, evidence_blob: str
) -> str | None:
    """새 문장이 규칙을 어기면 그 이유, 아니면 None."""
    if not new:
        return "빈 문장"
    if new == target.text:
        return "바뀐 것이 없음"
    if _URL.search(new):
        return "URL 포함"
    if any(marker in new for marker in channel.internal_markers):
        return "내부 표현 포함"
    if len(new) > _limit(channel, slide, target.field):
        return "글자 수 초과"
    allowed = _allowed_numbers(target.text, evidence_blob)
    extra = [n for n in _NUMBER.findall(new) if n not in allowed]
    if extra:
        return f"원문·근거에 없는 숫자 {extra}"
    if target.field == "title":
        # 정확성을 위해 고치는 제목은 근거에 나오는 이름(제품명 등)을 써도 된다. 전문용어를 풀어 쓰라는
        # 수정(reason 이 "제목 전문용어")에서는 영어 이름도 허용하지 않는다.
        names = (
            frozenset()
            if target.reason.startswith("제목 전문용어")
            else frozenset(re.findall(r"[A-Za-z][A-Za-z\-]*", evidence_blob))
        )
        if any(c == JARGON for c, _ in title_problems(new, channel, names)):
            return "제목에 여전히 전문용어가 많음"
    return None


def apply_fixes(
    carousel: Carousel,
    targets: list[Target],
    raw: Any,
    channel: ChannelConfig,
    *,
    evidence_blob: str,
) -> RepairReport:
    """LLM 출력(raw)을 검증해 ``carousel`` 에 적용한다. 검증에 실패한 수정은 적용하지 않는다."""
    report = RepairReport()
    fixes = {
        str(item.get("id")): item
        for item in (raw.get("fixes") if isinstance(raw, dict) else None) or []
        if isinstance(item, dict)
    }
    for target in targets:
        item = fixes.get(target.id)
        slide = _slide(carousel, target.slide)
        record = {"slide": str(target.slide), "field": target.field, "old": target.text}
        if item is None or slide is None:
            report.rejected.append({**record, "why": "수정 결과 없음"})
            continue
        action = str(item.get("action") or "").lower()
        if action == "drop":
            problem = _apply_drop(slide, target, channel)
            if problem:
                report.rejected.append({**record, "why": problem})
            else:
                report.applied.append({**record, "action": "drop"})
            continue
        if action != "rewrite":
            report.rejected.append({**record, "why": f"알 수 없는 action `{action}`"})
            continue
        new = str(item.get("text") or "").strip()
        problem = _rejection(new, target, slide, channel, evidence_blob)
        if problem is None:
            problem = _apply_rewrite(slide, target, new)
        if problem:
            report.rejected.append({**record, "why": problem, "new": new})
        else:
            report.applied.append({**record, "action": "rewrite", "new": new})
    return report


def _apply_rewrite(slide: Slide, target: Target, new: str) -> str | None:
    if target.field in {"headline", "subtitle", "title"}:
        if (getattr(slide, target.field) or "").strip() != target.text:
            return "원문과 일치하지 않음"
        setattr(slide, target.field, new)
        return None
    items = getattr(slide, target.field)
    for position, line in enumerate(items):
        if line.strip() == target.text:
            items[position] = new
            return None
    return "원문과 일치하지 않음"


def _apply_drop(slide: Slide, target: Target, channel: ChannelConfig) -> str | None:
    if target.field not in {"body", "extra_body"}:
        return "제목·표지 문구는 삭제할 수 없음"
    items = getattr(slide, target.field)
    if len(items) - 1 < _min_items(channel, slide, target.field):
        return "최소 줄 수 미달"
    for position, line in enumerate(items):
        if line.strip() == target.text:
            del items[position]
            return None
    return "원문과 일치하지 않음"


# --------------------------------------------------------------------------- #
# 에이전트와 반복
# --------------------------------------------------------------------------- #
class InstagramRepairer(Agent):
    """근거 부족 문장·어려운 제목을 고치는 LLM 에이전트(``ig_repair``)."""

    name = "instagram_repair"
    task = "ig_repair"
    prompt_file = "instagram_repair.md"

    def __init__(self, *, config, llm, channel: ChannelConfig) -> None:  # noqa: ANN001
        super().__init__(config=config, llm=llm, prompt_dir=channel.prompts_dir)
        self.channel = channel

    def setting(self, key: str, default: Any = None) -> Any:
        if key == "temperature":
            return 0.0
        return super().setting(key, default)

    def run(
        self,
        topic: VerifiedTopic,
        carousel: Carousel,
        targets: list[Target],
        previous_rejections: list[dict[str, str]] | None = None,
    ) -> Any:
        evidence = (carousel.meta.get("grounding") or {}).get("evidence") or []
        payload: dict[str, Any] = {
            "targets": [t.to_payload() for t in targets],
            "evidence": evidence,
            "rules": {"style": "개조식/명사형", "title_max_chars": 30},
        }
        if previous_rejections:
            # 재시도: 지난번 수정안이 왜 거절됐는지 알려 같은 실수를 반복하지 않게 한다.
            payload["previous_rejections"] = previous_rejections
        try:
            return self.ask_json(payload, instruction=INSTRUCTION)
        except AgentError as exc:
            logger.warning("[repair] 수정 호출 실패(그대로 둠): %s", exc)
            return {}


_SLIDE_WHERE = re.compile(r"slide\s*(\d+)", re.I)
ISSUE_INSTRUCTION = (
    "An independent fact-checker found this problem in the slides. Fix it by narrowing or "
    "rewriting the target text so it stays inside what the evidence supports (or drop a body "
    "line). Use ONLY the evidence; never add new facts or numbers. "
    'Return JSON: {"fixes": [{"id": "<issue id>", "action": "rewrite|drop", "text": "new text"}]}'
)


def _issue_slide(issue: dict[str, str]) -> int:
    where = str(issue.get("where") or "")
    match = _SLIDE_WHERE.search(where)
    if match:
        return int(match.group(1))
    return 1 if where.strip().lower() == "cover" else 0


def locate_quote(slide: Slide, quote: str) -> tuple[str, str] | None:
    """심사관이 인용한 문구가 슬라이드의 어느 필드(와 그 전체 문구)인지. 모호하면 None."""
    quote = quote.strip()
    if not quote:
        return None
    fields: list[tuple[str, str]] = []
    if slide.title:
        fields.append(("title", slide.title))
    if slide.headline:
        fields.append(("headline", slide.headline))
    if slide.subtitle:
        fields.append(("subtitle", slide.subtitle))
    fields += [("body", line) for line in slide.body]
    fields += [("extra_body", line) for line in slide.extra_body]
    exact = [(f, t) for f, t in fields if t.strip() == quote]
    hits = exact or [(f, t) for f, t in fields if quote in t]
    return (hits[0][0], hits[0][1].strip()) if len(hits) == 1 else None


class InstagramIssueRepairer(InstagramRepairer):
    """독립 심사관의 이슈 하나를 고친다(이슈마다 한 번 호출 — 한 번에 여러 개를 맡기면 일부만 답한다)."""

    name = "instagram_repair_issue"
    task = "ig_repair_issue"
    prompt_file = "instagram_repair_issue.md"

    def run_issue(
        self,
        carousel: Carousel,
        issue: dict[str, str],
        previous_rejections: list[dict[str, str]] | None = None,
    ) -> Any:
        slide = _slide(carousel, _issue_slide(issue))
        target = locate_quote(slide, str(issue.get("quote") or "")) if slide else None
        payload: dict[str, Any] = {
            "issue": {"id": issue["id"], "note": issue.get("note"), "kind": issue.get("kind")},
            "target": {"field": target[0], "text": target[1]} if target else None,
            "slide": (
                {
                    "title": slide.title,
                    "headline": slide.headline,
                    "subtitle": slide.subtitle,
                    "body": slide.body,
                    "extra_body": slide.extra_body,
                }
                if slide
                else None
            ),
            "evidence": (carousel.meta.get("grounding") or {}).get("evidence") or [],
            "rules": {"style": "개조식/명사형", "title_max_chars": 30},
        }
        if previous_rejections:
            payload["previous_rejections"] = previous_rejections
        try:
            return self.ask_json(payload, instruction=ISSUE_INSTRUCTION)
        except AgentError as exc:
            logger.warning("[repair] 이슈 수정 호출 실패(그대로 둠): %s", exc)
            return {}


def issue_target(carousel: Carousel, issue: dict[str, str], raw: Any) -> tuple[Target | None, Any]:
    """이슈 하나의 수정 대상과 모델의 수정안. 고칠 문구는 심사관이 인용한 것에서 코드가 찾는다."""
    slide = _slide(carousel, _issue_slide(issue))
    located = locate_quote(slide, str(issue.get("quote") or "")) if slide else None
    fixes = raw.get("fixes") if isinstance(raw, dict) else None
    item = next(
        (f for f in fixes or [] if isinstance(f, dict) and str(f.get("id")) == str(issue["id"])),
        None,
    )
    if slide is None or located is None or item is None:
        return None, item
    target = Target(
        id=str(issue["id"]), slide=slide.index, field=located[0], text=located[1],
        reason=str(issue.get("note") or ""),
    )
    return target, item


def review_repair(
    topic: VerifiedTopic,
    carousel: Carousel,
    channel: ChannelConfig,
    *,
    repairer: InstagramIssueRepairer,
    reviewer: Any,
    checker: InstagramGroundingChecker,
) -> list[dict[str, Any]]:
    """독립 심사관이 high 로 지적한 슬라이드 문제를 이슈마다 고치고 근거를 다시 심사한다.

    심사관의 최종 재심사는 게이트가 한다(여기서는 고치기만). ``repair.review_rounds`` 번까지.
    """
    rounds = int(channel.get("content.repair.review_rounds", 1))
    limit = int(channel.get("content.repair.review_max_issues", 5))
    history: list[dict[str, Any]] = []
    rejections: dict[str, list[dict[str, str]]] = {}
    for number in range(1, rounds + 1):
        ok, found = reviewer.run(topic, carousel)
        issues: list[dict[str, str]] = []
        seen: set[tuple[int, str]] = set()
        for issue in found:
            key = (_issue_slide(issue), str(issue.get("quote") or ""))
            if issue.get("severity") == "high" and key[0] and key not in seen:
                seen.add(key)
                issues.append({**issue, "id": f"i{len(issues) + 1}"})
        if ok or not issues:
            break
        evidence_blob = " ".join(
            str(e.get("text") or "")
            for e in (carousel.meta.get("grounding") or {}).get("evidence") or []
        )
        report = RepairReport()
        for issue in issues[:limit]:
            raw = repairer.run_issue(carousel, issue, rejections.get(issue["quote"]))
            target, item = issue_target(carousel, issue, raw)
            record = {"slide": str(_issue_slide(issue)), "field": "?",
                      "old": str(issue.get("quote") or issue.get("note") or "")[:80]}
            if target is None:
                why = ("수정 응답 없음" if item is None else "인용한 문구를 슬라이드에서 못 찾음")
                report.rejected.append({**record, "why": why})
                continue
            one = apply_fixes(carousel, [target], {"fixes": [item]}, channel,
                              evidence_blob=evidence_blob)
            report.applied += one.applied
            report.rejected += one.rejected
            if one.rejected:
                rejections[issue["quote"]] = one.rejected
        history.append(
            {"round": number, "kind": "review", "targets": len(issues), **report.to_dict()}
        )
        logger.info(
            "[repair] 심사 이슈 %d회차: 이슈 %d / 적용 %d / 거절 %d",
            number, len(issues), len(report.applied), len(report.rejected),
        )
        if not report.applied:
            break
        regrounded = checker.run(topic, carousel)
        carousel.meta["grounding"] = regrounded.to_dict()
        if regrounded.status != STATUS_CHECKED:
            break
    if history:
        carousel.meta["repair"] = [*carousel.meta.get("repair", []), *history]
    return history


def repair_loop(
    topic: VerifiedTopic,
    carousel: Carousel,
    channel: ChannelConfig,
    *,
    repairer: InstagramRepairer,
    checker: InstagramGroundingChecker,
) -> list[dict[str, Any]]:
    """수정 → 근거 재심사를 ``repair.max_rounds`` 번까지. 결과는 ``carousel.meta`` 에 반영한다."""
    rounds = int(channel.get("content.repair.max_rounds", 1))
    history: list[dict[str, Any]] = []
    rejections: list[dict[str, str]] = []
    for number in range(1, rounds + 1):
        targets = collect_targets(carousel, channel)
        if not targets:
            break
        evidence_blob = " ".join(
            str(e.get("text") or "") for e in (carousel.meta.get("grounding") or {}).get("evidence") or []
        )
        raw = repairer.run(topic, carousel, targets, rejections)
        report = apply_fixes(carousel, targets, raw, channel, evidence_blob=evidence_blob)
        rejections = report.rejected
        history.append({"round": number, "targets": len(targets), **report.to_dict()})
        logger.info(
            "[repair] %d회차: 대상 %d / 적용 %d / 거절 %d",
            number, len(targets), len(report.applied), len(report.rejected),
        )
        if not report.applied and not report.rejected:
            break
        if not report.applied:
            continue  # 거절만 있었다 — 이유를 알려 주고 다시 시도(재심사할 변경은 없음)
        regrounded: GroundingResult = checker.run(topic, carousel)
        carousel.meta["grounding"] = regrounded.to_dict()
        if regrounded.status != STATUS_CHECKED:
            break
    if history:
        carousel.meta["repair"] = history
    return history


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _refresh_caption(channel, config, llm, topic, carousel, directory) -> None:  # noqa: ANN001
    """슬라이드가 바뀌었으면 캡션도 고친 슬라이드로 다시 쓴다(과장이 캡션에 남지 않게)."""
    from channel.src.editor import sanitize_caption
    from channel.src.pipeline import write_caption_md
    from channel.src.writer import InstagramCaptionWriter

    caption, hashtags = InstagramCaptionWriter(config=config, llm=llm, channel=channel).run(
        topic, carousel
    )
    caption, _ = sanitize_caption(caption, channel)
    carousel.caption, carousel.hashtags = caption, hashtags
    write_caption_md(channel, carousel, directory)


def main(argv: list[str] | None = None) -> int:
    from src.agents.llm import build_llm
    from src.core.config import load_config, load_dotenv_file

    from channel.src.grounding import _find_topic
    from channel.src.pipeline import write_carousel_json

    parser = argparse.ArgumentParser(prog="channel.src.repair", description=__doc__)
    parser.add_argument("output_id")
    parser.add_argument("--research", type=Path, default=None)
    args = parser.parse_args(argv)

    load_dotenv_file()
    channel = load_channel_config()
    try:
        directory = latest_output() if args.output_id == "latest" else resolve_output(args.output_id)
        carousel = Carousel.from_dict(
            json.loads((directory / "carousel.json").read_text(encoding="utf-8"))
        )
        topic = _find_topic(str(carousel.meta.get("topic_id") or ""), args.research)
        config, llm = load_config(), build_llm()
        repairer = InstagramRepairer(config=config, llm=llm, channel=channel)
        checker = InstagramGroundingChecker(config=config, llm=llm, channel=channel)
        history = repair_loop(topic, carousel, channel, repairer=repairer, checker=checker)
        if bool(channel.get("content.repair.review_enabled", True)):
            from channel.src.auto_gate import InstagramReviewer

            history += review_repair(
                topic, carousel, channel,
                repairer=InstagramIssueRepairer(config=config, llm=llm, channel=channel),
                reviewer=InstagramReviewer(config=config, llm=llm, channel=channel),
                checker=checker,
            )
        if any(entry["applied"] for entry in history):
            _refresh_caption(channel, config, llm, topic, carousel, directory)
        if history:
            write_carousel_json(channel, carousel, directory)
    except (ApprovalError, ContentOpsError, OSError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2

    if not history:
        print(f"{directory.name}: 고칠 문장이 없습니다")
        return 0
    for entry in history:
        print(f"{directory.name} {entry['round']}회차: 적용 {len(entry['applied'])} / 거절 {len(entry['rejected'])}")
        for item in entry["applied"]:
            print(f"  [적용] slide {item['slide']} {item['field']}: {item['old']} -> {item.get('new', '(삭제)')}")
        for item in entry["rejected"]:
            print(f"  [거절] slide {item['slide']} {item['field']}: {item['why']}")
            if item.get("new"):
                print(f"         제안: {item['new']}")
    print("다음: --render-only 로 다시 렌더링한 뒤 python -m channel.src.auto_gate 를 실행하세요")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
