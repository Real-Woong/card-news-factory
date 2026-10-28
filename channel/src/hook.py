"""표지 제목(훅) 후보 3개를 만들고, 사람이 검수 화면에서 고르게 합니다.

목표는 피드에서 멈추게 하는 **강한 표지 제목**이지만, 이 채널의 가치는 정확성이라 어그로는
**근거 있는 사실의 범위 안에서만** 허용합니다.

* 후보는 질문형(question)·숫자형(number)·반전형(contrast) 세 가지입니다(LLM 1회, ``ig_hook``).
* 후보마다 코드가 검사합니다 — 글자 수, 과장 표현 금지어, 내부 표현, **숫자가 근거에 있는지**.
  검사를 통과하지 못한 후보는 버립니다(고치지 않음).
* 후보 생성이 실패해도 파이프라인은 멈추지 않습니다(후보가 없을 뿐, 기존 제목은 그대로).
* 고른 제목은 일반 문구와 똑같이 근거 심사(``grounding``)·validator 를 다시 통과해야 합니다.
  선택은 ``approval choose-hook`` 이 ``carousel.json`` 을 바꾸고, 재렌더·근거 재심사는 사람이 합니다.
"""

from __future__ import annotations

import html as html_escape
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.agents.base import Agent
from src.core.errors import AgentError, ContentOpsError
from src.core.logging_setup import get_logger

from channel.src.config import ChannelConfig
from channel.src.schema import Carousel, SlideType, VerifiedTopic

logger = get_logger(__name__)

STYLES = ("question", "number", "contrast")

DEFAULT_BANNED = [
    "충격",
    "경악",
    "대박",
    "난리",
    "미쳤",
    "역대급",
    "폭등",
    "폭락",
    "무조건",
    "100%",
    "반드시 오른다",
    "지금 사야",
    "놓치면",
]

INSTRUCTION = (
    "Write exactly three alternative cover headlines in Korean for this verified carousel: "
    "one per style — question, number, contrast. Each must stop a scrolling reader who is NOT "
    "a blockchain/AI expert. Use only facts present in the input; never add a claim, number, "
    "date, or actor that is not in `facts`. No hype words, no emoji, no investment advice. "
    'Return JSON: {"options": [{"style": "question", "headline": "..."}, '
    '{"style": "number", "headline": "..."}, {"style": "contrast", "headline": "..."}]}'
)

# 숫자 + 바로 뒤 단위(한글 한 글자 · % · 영문). "5년짜리" 와 근거의 "5년 order" 를 같은 (5, 년) 으로 본다.
_NUMBER_UNIT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s?(%|[가-힣]|[A-Za-z]+)?")


def _number_pairs(text: str) -> set[tuple[str, str]]:
    """텍스트의 (숫자, 단위) 쌍. 단위가 없으면 빈 문자열."""
    pairs: set[tuple[str, str]] = set()
    for number, unit in _NUMBER_UNIT.findall(text):
        pairs.add((number.replace(",", ""), unit.lower() if unit.isascii() else unit))
    return pairs


@dataclass
class HookOption:
    """표지 제목 후보 하나."""

    style: str
    headline: str
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"style": self.style, "headline": self.headline, "issues": list(self.issues)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HookOption:
        return cls(
            style=str(data.get("style") or "").strip(),
            headline=str(data.get("headline") or "").strip(),
            issues=[str(item) for item in (data.get("issues") or [])],
        )


def _evidence_text(topic: VerifiedTopic | None, carousel: Carousel) -> str:
    """후보의 숫자가 있어야 하는 근거: 검증된 key_facts 와 확정된 슬라이드 문구."""
    parts: list[str] = [slide_text for slide in carousel.slides for slide_text in slide.text_parts]
    summary = dict(carousel.meta.get("research_summary") or {})
    for source in (summary, {"key_facts": getattr(topic, "key_facts", [])} if topic else {}):
        for fact in source.get("key_facts") or []:
            parts.append(str(fact.get("text") if isinstance(fact, dict) else fact))
        parts.extend(str(source.get(key) or "") for key in ("what_changed", "why_it_matters"))
    return "\n".join(parts)


def check_option(
    option: HookOption, *, channel: ChannelConfig, evidence: str
) -> list[str]:
    """후보의 문제 목록(비어 있으면 통과). 숫자는 근거에 없으면 탈락."""
    text = option.headline
    issues: list[str] = []
    budget = channel.get("content.text_budget.cover", {}) or {}
    limit = int(budget.get("headline_max_chars", 78))
    if not text:
        return ["제목이 비어 있습니다"]
    if len(text.replace("\n", "")) > limit:
        issues.append(f"제목이 {limit}자를 넘습니다")
    if text.count("\n") > 2:
        issues.append("제목이 3줄을 넘습니다")
    banned = list(channel.get("content.hook.banned", DEFAULT_BANNED) or DEFAULT_BANNED)
    for word in banned:
        if word in text:
            issues.append(f"과장 표현 금지어: {word!r}")
    for marker in channel.internal_markers:
        if marker.lower() in text.lower():
            issues.append(f"내부 표현: {marker!r}")
    if re.search(r"https?://|www\.", text):
        issues.append("URL 포함")
    # 숫자는 **단위까지** 근거에 있어야 한다("9년" 은 근거의 "9월" 로 통과하지 못한다).
    known = _number_pairs(evidence)
    known_numbers = {number for number, _ in known}
    for number, unit in sorted(_number_pairs(text)):
        found = (number, unit) in known if unit else number in known_numbers
        if not found:
            issues.append(f"근거에 없는 숫자: {number}{unit}")
    return issues


def clean_options(
    raw: Any, *, channel: ChannelConfig, carousel: Carousel, topic: VerifiedTopic | None
) -> list[HookOption]:
    """LLM 응답에서 검사를 통과한 후보만 남긴다(스타일당 1개, 현재 제목과 같은 것은 제외)."""
    evidence = _evidence_text(topic, carousel)
    current = re.sub(r"\s+", "", carousel.cover.headline if carousel.cover else carousel.headline)
    seen: set[str] = set()
    kept: list[HookOption] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        option = HookOption(
            style=str(item.get("style") or "").strip().lower(),
            headline=str(item.get("headline") or "").strip(),
        )
        if option.style not in STYLES or option.style in seen:
            continue
        if re.sub(r"\s+", "", option.headline) == current:
            continue
        option.issues = check_option(option, channel=channel, evidence=evidence)
        if option.issues:
            logger.info("[hook] %s 후보 탈락: %s", option.style, "; ".join(option.issues))
            continue
        seen.add(option.style)
        kept.append(option)
    return kept


class InstagramHookWriter(Agent):
    """표지 제목 후보 3개(질문·숫자·반전)를 만든다. 실패해도 파이프라인은 계속된다."""

    name = "instagram_hook"
    task = "ig_hook"
    prompt_file = "instagram_hook.md"

    def __init__(self, *, config, llm, channel: ChannelConfig) -> None:  # noqa: ANN001
        super().__init__(config=config, llm=llm, prompt_dir=channel.prompts_dir)
        self.channel = channel

    def build_payload(self, topic: VerifiedTopic, carousel: Carousel) -> dict[str, Any]:
        body = [
            line
            for slide in carousel.slides
            if slide.type is SlideType.CONTENT
            for line in slide.body
        ]
        return {
            "category": topic.category,
            "current_headline": carousel.cover.headline if carousel.cover else carousel.headline,
            "current_subtitle": carousel.cover.subtitle if carousel.cover else "",
            "facts": {
                "what_changed": topic.what_changed,
                "why_it_matters": topic.why_it_matters,
                "key_facts": [
                    fact.get("text") if isinstance(fact, dict) else fact
                    for fact in topic.key_facts
                ],
                "slide_body": body,
            },
            "rules": {
                "audience": "투자·기술에 관심은 있지만 블록체인·AI 전문가는 아닌 일반 독자",
                "max_chars": int(
                    self.channel.get("content.text_budget.cover.headline_max_chars", 78)
                ),
                "max_lines": 2,
                "banned_words": list(
                    self.channel.get("content.hook.banned", DEFAULT_BANNED) or DEFAULT_BANNED
                ),
            },
        }

    def run(self, topic: VerifiedTopic, carousel: Carousel) -> list[HookOption]:
        try:
            data = self.ask_json(self.build_payload(topic, carousel), instruction=INSTRUCTION)
        except AgentError as exc:
            logger.warning("[hook] 제목 후보 생성 실패(무시하고 계속): %s", exc)
            return []
        options = clean_options(
            data.get("options"), channel=self.channel, carousel=carousel, topic=topic
        )
        logger.info("[hook] 제목 후보 %d개 통과", len(options))
        return options


def apply_choice(carousel_path: Path, selector: str) -> str:
    """``carousel.json`` 의 표지 제목을 후보로 교체한다. 바뀐 제목을 돌려준다.

    ``selector`` 는 후보 스타일(question·number·contrast) 또는 1부터 시작하는 번호.
    재렌더와 근거 재심사는 하지 않는다(사람이 이어서 실행).
    """
    try:
        payload = json.loads(carousel_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContentOpsError(f"carousel.json 을 읽을 수 없습니다: {carousel_path} ({exc})") from exc

    options = [HookOption.from_dict(o) for o in (payload.get("meta") or {}).get("hook_options", [])]
    if not options:
        raise ContentOpsError("이 산출물에는 제목 후보가 없습니다(hook_options 비어 있음)")

    chosen: HookOption | None = None
    if selector.isdigit() and 1 <= int(selector) <= len(options):
        chosen = options[int(selector) - 1]
    else:
        chosen = next((o for o in options if o.style == selector.lower()), None)
    if chosen is None:
        names = ", ".join(f"{i}={o.style}" for i, o in enumerate(options, start=1))
        raise ContentOpsError(f"알 수 없는 후보 {selector!r} (사용 가능: {names})")

    slides = payload.get("slides") or []
    cover = next((s for s in slides if s.get("type") == "cover"), None)
    if cover is None:
        raise ContentOpsError("표지 슬라이드를 찾을 수 없습니다")
    previous = str(cover.get("headline") or "")
    cover["headline"] = chosen.headline
    payload["headline"] = chosen.headline.replace("\n", " ")
    meta = payload.setdefault("meta", {})
    meta["hook_choice"] = {"style": chosen.style, "previous": previous}
    carousel_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return chosen.headline


STYLE_LABEL = {"question": "질문형", "number": "숫자형", "contrast": "반전형"}


def _choose_command(output_id: str, selector: str) -> str:
    return f"python -m channel.src.approval choose-hook {output_id} {selector}"


def review_lines(carousel: Carousel, output_id: str) -> list[str]:
    """review.md 에 넣을 제목 후보 섹션(후보가 없으면 빈 목록)."""
    options = [HookOption.from_dict(o) for o in carousel.meta.get("hook_options", [])]
    if not options:
        return []
    current = (carousel.cover.headline if carousel.cover else carousel.headline).replace("\n", " ")
    lines = ["## 표지 제목 후보", "", f"- 현재 제목: **{current}**"]
    choice = carousel.meta.get("hook_choice")
    if choice:
        lines.append(f"- 선택됨: {STYLE_LABEL.get(choice.get('style'), choice.get('style'))}")
    lines.append("")
    for number, option in enumerate(options, start=1):
        label = STYLE_LABEL.get(option.style, option.style)
        lines.append(
            f"{number}. **{label}** — {option.headline.replace(chr(10), ' / ')}  "
            f"`{_choose_command(output_id, option.style)}`"
        )
    lines += [
        "",
        "고른 뒤에는 `--render-only` 로 다시 렌더하고, 문구가 바뀌었으므로 "
        "`python -m channel.src.grounding check <output-id>` 로 근거를 다시 심사합니다.",
        "",
    ]
    return lines


def preview_html(carousel: Carousel, output_id: str) -> str:
    """preview.html 에 넣을 제목 후보 블록(후보가 없으면 빈 문자열)."""
    options = [HookOption.from_dict(o) for o in carousel.meta.get("hook_options", [])]
    if not options:
        return ""
    esc = html_escape.escape
    current = (carousel.cover.headline if carousel.cover else carousel.headline).replace("\n", " ")
    chosen = (carousel.meta.get("hook_choice") or {}).get("style")
    items = "".join(
        "<li>"
        f"<b>{esc(STYLE_LABEL.get(option.style, option.style))}"
        f"{' (선택됨)' if option.style == chosen else ''}</b> "
        f"{esc(option.headline.replace(chr(10), ' / '))}<br />"
        f"<code>{esc(_choose_command(output_id, option.style))}</code>"
        "</li>"
        for option in options
    )
    return (
        '<section class="hooks"><h2>표지 제목 후보</h2>'
        f"<p>현재: <b>{esc(current)}</b></p><ol>{items}</ol></section>"
    )
