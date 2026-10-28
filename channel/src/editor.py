"""Instagram editor — 압축, 과장 제거, reading flow 정리.

LLM 편집 후 **코드로 강제**하는 부분이 있습니다.

* 금지 표현(banned_phrases) 제거는 코드가 마지막에 다시 확인합니다.
* 내부 표현(확인 필요, source truncated …)은 코드가 제거합니다.
  공개 출력에 새어 나가면 안 되는 문구이므로 모델 준수에 의존하지 않습니다.
* 길이 예산 초과는 editor 에게 되돌려 한 번 더 줄이게 하고, 그래도 넘치면
  validator 가 경고/차단합니다(자동 truncate 금지).
"""

from __future__ import annotations

import re
from typing import Any

from src.agents.base import Agent
from src.core.errors import AgentError
from src.core.logging_setup import get_logger

from channel.src.config import ChannelConfig
from channel.src.schema import Carousel, Slide, SlideType, VerifiedTopic

logger = get_logger(__name__)

INSTRUCTION = (
    "Edit this Instagram carousel. Your job is subtraction: compress sentences, "
    "remove repetition between slides, remove hype, and make the reading flow "
    "cover → detail → takeaway obvious. "
    "When cover and body overlap, preserve the body evidence and rewrite the cover "
    "as framing without exact numbers or versions. "
    "Keep every verified number, version and date exactly as written. "
    "Respect the character budgets per slide; if a slide is over budget, cut the "
    "least informative sentence instead of shortening a fact. "
    "Never add a note about verification, truncation or missing data. "
    "TONE: unify every bullet to 개조식/명사형 endings across all slides regardless "
    "of topic category. Rewrite ~합니다/~했습니다/~입니다/~할 수 있습니다 into noun "
    "phrases or terse declaratives. "
    'Return JSON: {"slides": [...same shape as input...], '
    '"changes": ["short Korean note per edit"]}'
)


def _strip_internal_language(text: str, markers: list[str]) -> str:
    """내부 표현이 포함된 문장/괄호를 제거합니다."""
    if not text:
        return text
    cleaned = text
    for marker in markers:
        if marker.lower() not in cleaned.lower():
            continue
        # 1) 괄호 안에 들어간 경우 통째로 제거: "(구체 URL은 소스에서 잘려 확인 필요)"
        cleaned = re.sub(
            rf"[（(][^)）]*{re.escape(marker)}[^)）]*[)）]", "", cleaned, flags=re.IGNORECASE
        )
        # 2) 문장 단위로 제거
        sentences = re.split(r"(?<=[.!?。])\s+|\n", cleaned)
        sentences = [s for s in sentences if marker.lower() not in s.lower()]
        cleaned = " ".join(s for s in sentences if s.strip())
    return re.sub(r"\s{2,}", " ", cleaned).strip(" ,·-")


class InstagramEditor(Agent):
    """편집 후 금지/내부 표현을 코드로 최종 정리합니다."""

    name = "instagram_editor"
    task = "ig_edit"
    prompt_file = "instagram_editor.md"

    def __init__(self, *, config, llm, channel: ChannelConfig) -> None:  # noqa: ANN001
        super().__init__(config=config, llm=llm, prompt_dir=channel.prompts_dir)
        self.channel = channel

    def build_payload(self, carousel: Carousel) -> dict[str, Any]:
        budgets = {
            slide_type: self.channel.text_budget(slide_type)
            for slide_type in ("cover", "content", "closing")
        }
        return {
            "carousel": {
                "template": carousel.template,
                "headline": carousel.headline,
                "slides": [slide.to_dict() for slide in carousel.slides],
            },
            "budgets": budgets,
            "rules": {
                "banned_phrases": self.channel.banned_phrases,
                "never_write": self.channel.internal_markers,
                "keep_verbatim": "숫자, 버전, 날짜, 제품명",
                "tone": self.channel.get("content.tone.principles", []),
            },
        }

    def run(self, carousel: Carousel) -> Carousel:
        try:
            data = self.ask_json(self.build_payload(carousel), instruction=INSTRUCTION)
        except AgentError as exc:
            # 편집 실패는 치명적이지 않습니다: 원본을 코드 정리만 해서 진행합니다.
            logger.warning("[editor] LLM 편집 실패, 코드 정리만 적용: %s", exc)
            data = {"slides": [slide.to_dict() for slide in carousel.slides], "changes": []}

        edited_by_index: dict[int, dict[str, Any]] = {}
        for raw in data.get("slides") or []:
            if not isinstance(raw, dict):
                continue
            try:
                edited_by_index[int(raw.get("index"))] = raw
            except (TypeError, ValueError):
                continue

        markers = self.channel.internal_markers
        banned = self.channel.banned_phrases
        code_changes: list[str] = []
        new_slides: list[Slide] = []

        for original in carousel.slides:
            raw = edited_by_index.get(original.index)
            slide = Slide.from_dict({**original.to_dict(), **(raw or {})})
            slide.index = original.index
            slide.type = original.type
            # 출처는 편집 대상이 아닙니다(검증된 데이터 유지).
            slide.sources = original.sources

            for attribute in ("eyebrow", "headline", "subtitle", "title"):
                value = getattr(slide, attribute)
                cleaned = _strip_internal_language(value, markers)
                for phrase in banned:
                    if phrase and phrase in cleaned:
                        cleaned = cleaned.replace(phrase, "").strip()
                        code_changes.append(f"slide {slide.index}: 금지 표현 제거 '{phrase}'")
                if cleaned != value:
                    if value and not cleaned:
                        # 전부 지워지면 원본 유지 대신 실패로 처리해야 안전합니다.
                        raise AgentError(
                            f"slide {slide.index} 의 {attribute} 가 정리 후 비었습니다 "
                            "(내부 표현만으로 구성된 문장)",
                            agent=self.name,
                        )
                    code_changes.append(f"slide {slide.index}: {attribute} 내부 표현 정리")
                    setattr(slide, attribute, cleaned)

            cleaned_body: list[str] = []
            for line in slide.body:
                cleaned = _strip_internal_language(line, markers)
                for phrase in banned:
                    if phrase and phrase in cleaned:
                        cleaned = cleaned.replace(phrase, "").strip()
                        code_changes.append(f"slide {slide.index}: 금지 표현 제거 '{phrase}'")
                if not cleaned:
                    code_changes.append(f"slide {slide.index}: 내부 표현 문장 삭제")
                    continue
                if cleaned != line:
                    code_changes.append(f"slide {slide.index}: 본문 내부 표현 정리")
                cleaned_body.append(cleaned)

            # 중복 문장 제거(같은 캐러셀 안에서 같은 문장 반복 금지)
            deduped: list[str] = []
            for line in cleaned_body:
                if line not in deduped:
                    deduped.append(line)
                else:
                    code_changes.append(f"slide {slide.index}: 중복 문장 제거")
            slide.body = deduped
            new_slides.append(slide)

        llm_changes = [str(c) for c in (data.get("changes") or []) if str(c).strip()]
        edited = Carousel(
            template=carousel.template,
            topic=carousel.topic,
            headline=_strip_internal_language(carousel.headline, markers) or carousel.headline,
            slides=new_slides,
            caption=carousel.caption,
            hashtags=carousel.hashtags,
            brand=carousel.brand,
            meta={**carousel.meta, "editor_changes": llm_changes + code_changes},
        )
        logger.info(
            "[editor] 변경 %d건 (LLM %d / 코드 %d)",
            len(llm_changes) + len(code_changes),
            len(llm_changes),
            len(code_changes),
        )
        return edited

    def repair_duplicates(
        self,
        carousel: Carousel,
        pairs: list[Any],
        *,
        unused_material: list[str] | None = None,
    ) -> Carousel:
        """중복으로 판정된 bullet 을 다시 쓰게 합니다.

        빈 공간을 채우지 않습니다: 대체 재료가 없으면 문장을 삭제하는 쪽을 택하도록
        지시하고, 최소 항목 수는 validator 가 다시 검사합니다.

        Args:
            carousel: 수정할 캐러셀.
            pairs: :class:`channel.src.similarity.DuplicatePair` 목록.
            unused_material: 아직 쓰지 않은 검증된 문장(대체 재료).
        """
        if not pairs:
            return carousel

        payload = {
            "carousel": {
                "template": carousel.template,
                "slides": [slide.to_dict() for slide in carousel.slides],
            },
            "duplicate_pairs": [
                {
                    "similarity": getattr(pair, "score", 0.0),
                    "slide_a": getattr(pair, "slide_a", 0),
                    "slide_b": getattr(pair, "slide_b", 0),
                    "text_a": getattr(pair, "text_a", ""),
                    "text_b": getattr(pair, "text_b", ""),
                    "rewrite_slide": getattr(pair, "slide_b", 0),
                }
                for pair in pairs
            ],
            "unused_material": unused_material or [],
            "budgets": {
                slide_type: self.channel.text_budget(slide_type)
                for slide_type in ("cover", "content", "closing")
            },
            "rules": {
                "never_write": self.channel.internal_markers,
                "banned_phrases": self.channel.banned_phrases,
                "tone": self.channel.get("content.tone.style", "개조식 / 명사형"),
                "no_padding": "대체 재료가 없으면 문장을 삭제한다",
            },
        }
        try:
            data = self.ask_json(payload, instruction=REPAIR_INSTRUCTION)
        except AgentError as exc:
            logger.warning("[editor] 중복 재작성 실패, 원본 유지: %s", exc)
            carousel.meta.setdefault("recovery_failures", []).append(
                {
                    "code": "llm_recovery_failed",
                    "reason": "duplicate_unresolved",
                    "detail": str(exc),
                }
            )
            return carousel

        updated = {}
        for raw in data.get("slides") or []:
            if not isinstance(raw, dict):
                continue
            try:
                updated[int(raw.get("index"))] = raw
            except (TypeError, ValueError):
                continue

        new_slides: list[Slide] = []
        for original in carousel.slides:
            raw = updated.get(original.index)
            if raw is None:
                new_slides.append(original)
                continue
            slide = Slide.from_dict({**original.to_dict(), **raw})
            slide.index = original.index
            slide.type = original.type
            slide.sources = original.sources
            new_slides.append(slide)

        changes = [str(c) for c in (data.get("changes") or []) if str(c).strip()]
        logger.info("[editor] 중복 재작성: %d 슬라이드 수정, 변경 %d건", len(updated), len(changes))
        return Carousel(
            template=carousel.template,
            topic=carousel.topic,
            headline=carousel.headline,
            slides=new_slides,
            caption=carousel.caption,
            hashtags=carousel.hashtags,
            brand=carousel.brand,
            meta={
                **carousel.meta,
                "editor_changes": [*(carousel.meta.get("editor_changes") or []), *changes],
                "duplicate_repair": {
                    "pairs": len(pairs),
                    "slides_rewritten": sorted(updated),
                },
            },
        )

    def recover_thin_slides(
        self,
        carousel: Carousel,
        thin_indexes: list[int],
        *,
        unused_material: list[str],
    ) -> Carousel:
        """얇아진 content slide를 추적 가능한 미사용 검증 재료로만 회복."""
        if not thin_indexes or not unused_material:
            return carousel
        payload = {
            "thin_slides": [
                slide.to_dict()
                for slide in carousel.slides
                if slide.index in thin_indexes
            ],
            "unused_verified_material": unused_material,
            "budget": self.channel.text_budget("content"),
            "rules": {
                "tone": self.channel.get("content.tone.style", "개조식 / 명사형"),
                "no_padding": True,
                "source_fact_required": True,
            },
        }
        try:
            data = self.ask_json(payload, instruction=DENSITY_RECOVERY_INSTRUCTION)
        except AgentError as exc:
            logger.warning("[editor] 밀도 회복 실패, 원본 유지: %s", exc)
            carousel.meta.setdefault("recovery_failures", []).append(
                {
                    "code": "llm_recovery_failed",
                    "reason": "thin_slide_unresolved",
                    "detail": str(exc),
                }
            )
            return carousel

        allowed = set(unused_material)
        claimed_sources: set[str] = set()
        updated: dict[int, dict[str, Any]] = {}
        for raw in data.get("slides") or []:
            if not isinstance(raw, dict):
                continue
            try:
                index = int(raw.get("index"))
            except (TypeError, ValueError):
                continue
            if index not in thin_indexes:
                continue
            grounded_body: list[str] = []
            for item in raw.get("body") or []:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "").strip()
                source_fact = str(item.get("source_fact") or "").strip()
                if (
                    text
                    and source_fact in allowed
                    and source_fact not in claimed_sources
                ):
                    grounded_body.append(text)
                    claimed_sources.add(source_fact)
            if grounded_body:
                updated[index] = {
                    "title": str(raw.get("title") or "").strip(),
                    "body": grounded_body,
                }

        slides: list[Slide] = []
        for original in carousel.slides:
            raw = updated.get(original.index)
            if raw is None:
                slides.append(original)
                continue
            # 수리 후 남아 있던 고유 사실은 버리지 않고, 추적 가능한 새 재료만 덧붙입니다.
            slide = Slide.from_dict(
                {
                    **original.to_dict(),
                    **raw,
                    "body": [*original.body, *raw["body"]],
                }
            )
            slide.index = original.index
            slide.type = original.type
            slide.sources = original.sources
            slides.append(slide)

        changes = [
            f"slide {index}: 미사용 검증 재료로 밀도 회복"
            for index in sorted(updated)
        ]
        return Carousel(
            template=carousel.template,
            topic=carousel.topic,
            headline=carousel.headline,
            slides=slides,
            caption=carousel.caption,
            hashtags=carousel.hashtags,
            brand=carousel.brand,
            meta={
                **carousel.meta,
                "editor_changes": [
                    *(carousel.meta.get("editor_changes") or []),
                    *changes,
                ],
            },
        )

    def repair_cover_duplicates(
        self,
        carousel: Carousel,
        pairs: list[Any],
        *,
        topic: VerifiedTopic,
    ) -> Carousel:
        """body 사실을 고정한 채 cover의 framing만 재작성합니다."""
        cover = carousel.cover
        if cover is None or not pairs:
            return carousel
        payload = {
            "cover": cover.to_dict(),
            "duplicate_pairs": [
                {
                    "score": getattr(pair, "score", 0.0),
                    "cover_text": getattr(pair, "text_a", ""),
                    "body_text": getattr(pair, "text_b", ""),
                }
                for pair in pairs
            ],
            "framing_material": {
                "headline": topic.headline,
                "what_changed": topic.what_changed,
                "why_it_matters": topic.why_it_matters,
                "category": topic.category,
            },
            "budget": self.channel.text_budget("cover"),
        }
        try:
            data = self.ask_json(payload, instruction=COVER_REPAIR_INSTRUCTION)
        except AgentError as exc:
            logger.warning("[editor] cover 중복 수리 실패, 원본 유지: %s", exc)
            carousel.meta.setdefault("recovery_failures", []).append(
                {
                    "code": "llm_recovery_failed",
                    "reason": "cover_rewrite_failed",
                    "detail": str(exc),
                }
            )
            return carousel

        raw = data.get("cover") or {}
        if not isinstance(raw, dict):
            return carousel
        rewritten = Slide.from_dict({**cover.to_dict(), **raw})
        rewritten.index = cover.index
        rewritten.type = SlideType.COVER
        # LLM이 본문 필드를 반환해도 cover 외 내용은 변경하지 않습니다.
        rewritten.body = list(cover.body)
        rewritten.extra_title = cover.extra_title
        rewritten.extra_body = list(cover.extra_body)
        rewritten.sources = list(cover.sources)
        slides = [rewritten if slide.index == cover.index else slide for slide in carousel.slides]
        return Carousel(
            template=carousel.template,
            topic=carousel.topic,
            headline=rewritten.headline or carousel.headline,
            slides=slides,
            caption=carousel.caption,
            hashtags=carousel.hashtags,
            brand=carousel.brand,
            meta={
                **carousel.meta,
                "editor_changes": [
                    *(carousel.meta.get("editor_changes") or []),
                    "slide 1: body 사실을 유지하고 cover를 framing 중심으로 재작성",
                ],
                "cover_duplicate_repair": {
                    "attempted": True,
                    "pairs": len(pairs),
                    "body_preserved": True,
                },
            },
        )


REPAIR_INSTRUCTION = (
    "These bullet pairs repeat the same fact across different slides of one "
    "carousel. Rewrite ONLY the bullets on the later slide of each pair so that "
    "each fact appears exactly once. "
    "Options, in order of preference: (1) replace with a different verified fact "
    "from unused_material, (2) shift the angle so it adds new information "
    "(e.g. operational consequence instead of the change itself), (3) delete the "
    "bullet if the slide still meets its minimum item count. "
    "Changing the wording while keeping the same subject and object is NOT a fix and "
    "will be flagged again: for a body slide such as 'Why it matters', replace a "
    "restated change with a consequence that the input slides or unused_material "
    "explicitly state (who is affected, scope, required action) - never infer behaviour "
    "the material does not state, such as fallbacks or compatibility - "
    "or delete it, down to the minimum item count; also drop items beyond the maximum "
    "item count, starting with the ones that overlap an earlier slide. "
    "If the later slide is the closing (Takeaway) slide, do not paraphrase the earlier "
    "slide's bullet: replace it with a judgment the body does not state (priority, "
    "deadline, who it affects) or with an unused fact, or delete the line. One "
    "closing line is enough. "
    "Do NOT invent facts, do not pad, and keep every number/version verbatim. "
    "Keep 개조식/명사형 endings (no ~합니다/~입니다). "
    'Return JSON: {"slides": [...only the slides you changed...], "changes": [...]}'
)

DENSITY_RECOVERY_INSTRUCTION = (
    "Recover ONLY the listed thin slides using unused_verified_material. "
    "Do not create filler or new facts. You may reframe the slide title, but every "
    "body item must cite one exact input fact in source_fact. Use each source fact "
    "at most once and keep numbers/versions unchanged. Return JSON: "
    '{"slides": [{"index": 2, "title": "...", "body": '
    '[{"text": "Korean copy", "source_fact": "exact input fact"}]}]}'
)

COVER_REPAIR_INSTRUCTION = (
    "Rewrite ONLY the cover. The body is authoritative and immutable. Prefer framing, "
    "outcome, category-level change, and reader impact. Remove exact metrics, exact "
    "version numbers, and detailed feature lists that occur in duplicate_pairs. Do not "
    "invent facts or reuse exact body wording. Return JSON: "
    '{"cover": {"index": 1, "type": "cover", "eyebrow": "...", '
    '"headline": "...", "subtitle": "..."}}'
)


def sanitize_caption(caption: str, channel: ChannelConfig) -> tuple[str, list[str]]:
    changes: list[str] = []
    cleaned = caption
    for marker in channel.internal_markers:
        if marker.lower() in cleaned.lower():
            cleaned = _strip_internal_language(cleaned, [marker])
            changes.append(f"caption: 내부 표현 제거 '{marker}'")
    for phrase in channel.banned_phrases:
        if phrase and phrase in cleaned:
            cleaned = cleaned.replace(phrase, "").strip()
            changes.append(f"caption: 금지 표현 제거 '{phrase}'")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, changes
