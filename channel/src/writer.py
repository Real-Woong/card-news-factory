"""Instagram writer — 각 슬라이드의 실제 문구와 캡션을 만듭니다."""

from __future__ import annotations

from typing import Any

from src.agents.base import Agent
from src.core.errors import AgentError
from src.core.logging_setup import get_logger
from src.core.text import truncate

from channel.src.caption_style import caption_problems, known_names
from channel.src.config import ChannelConfig
from channel.src.planner import CarouselPlan
from channel.src.schema import Carousel, Slide, SlideType, VerifiedTopic

logger = get_logger(__name__)

SLIDE_INSTRUCTION = (
    "Write the actual Korean copy for every slide of this Instagram carousel. "
    "Use only the verified material given. Keep technical nouns in English "
    "(Agent, RAG, MCP, LLM, SDK, Mainnet, GA ...). "
    "TONE: every slide bullet must end in a noun phrase or a terse declarative "
    "(개조식/명사형). Do not use ~합니다/~했습니다/~입니다/~할 수 있습니다. "
    "This applies to AI and Blockchain topics identically. "
    "NO REPETITION: each fact appears on exactly one slide. Do not restate a fact "
    "from another slide with different wording. The cover must frame the outcome or "
    "reader impact and should avoid exact metrics, versions, and feature lists. Keep "
    "numbers, versions, and technical evidence in body slides. "
    "Respect the per-slide character budgets exactly — they are hard layout limits, "
    "not suggestions. Numbers, versions and dates must be copied verbatim from the "
    "facts; never invent or round them. "
    "Do not mention that anything is unverified, truncated, or missing: such notes "
    "belong to the internal review file, not to public slides. "
    'Return JSON: {"headline": "...", "slides": [{"index": 1, "type": "cover", '
    '"eyebrow": "...", "headline": "...", "subtitle": "..."}, {"index": 2, '
    '"type": "content", "title": "...", "body": ["...", "..."]}, ...]}'
)

CAPTION_FIX_INSTRUCTION = (
    " The previous caption broke the plain-language rule (see fix.problems). Rewrite it in "
    "plain Korean for a general reader: explain code names and jargon in everyday words, keep "
    "product names, and keep every fact and number unchanged. Do not add facts."
)

CAPTION_INSTRUCTION = (
    "Write the Instagram caption for this carousel in Korean. Structure: one-line "
    "hook, then 3-5 sentences of substance, then a short takeaway line, then a "
    "'Sources:' line listing publisher names only (no URLs). "
    "No clickbait, no emoji walls, no '확인 필요' style internal notes. "
    'Return JSON: {"hook": "...", "body": ["...", "..."], "takeaway": "...", '
    '"hashtags": ["#AI", "#..."]}'
)


class InstagramWriter(Agent):
    """planner 의 구조에 맞춰 슬라이드 문구를 생성합니다."""

    name = "instagram_writer"
    task = "ig_write"
    prompt_file = "instagram_writer.md"

    def __init__(self, *, config, llm, channel: ChannelConfig) -> None:  # noqa: ANN001
        super().__init__(config=config, llm=llm, prompt_dir=channel.prompts_dir)
        self.channel = channel

    # ------------------------------------------------------------------ payload
    def _slide_requirements(self, plan: CarouselPlan) -> list[dict[str, Any]]:
        requirements: list[dict[str, Any]] = []
        for entry in plan.slide_plan:
            slide_type = str(entry["type"])
            budget = self.channel.text_budget(slide_type)
            requirement: dict[str, Any] = {
                "index": entry["index"],
                "type": slide_type,
                "role": entry.get("role", ""),
                "goal": entry.get("goal", ""),
            }
            if slide_type == "cover":
                requirement["fields"] = {
                    "eyebrow": f"최대 {budget.get('eyebrow_max_chars', 34)}자, "
                    "예: 'AI / GOOGLE DEEPMIND'",
                    "headline": f"최대 {budget.get('headline_max_chars', 78)}자 (2~3줄)",
                    "subtitle": f"최대 {budget.get('subtitle_max_chars', 110)}자 (1~2줄)",
                }
            else:
                requirement["fields"] = {
                    "title": f"최대 {budget.get('title_max_chars', 46)}자",
                    "body": (
                        f"{budget.get('body_min_items', 2)}~{budget.get('body_max_items', 5)}개 "
                        f"항목, 각 최대 {budget.get('body_item_max_chars', 92)}자, "
                        f"합계 {budget.get('body_total_max_chars', 380)}자 이내"
                    ),
                }
                if slide_type == "closing":
                    spec = self.channel.closing_spec()
                    requirement["fields"]["title"] = f"'{spec['primary_label']}' 고정"
                    requirement["fields"]["body"] = (
                        f"{spec['primary_items']}개 항목, 각 최대 "
                        f"{budget.get('body_item_max_chars', 92)}자"
                    )
                    if self.channel.closing_uses_secondary:
                        requirement["fields"]["extra_title"] = (
                            f"'{spec['secondary_label']}' 고정"
                        )
                        requirement["fields"]["extra_body"] = (
                            f"{spec['secondary_items']}개 항목, 각 최대 "
                            f"{budget.get('secondary_body_item_max_chars', 92)}자 — "
                            "다음에 확인할 지점. 재료가 없으면 비워 둘 것(억지로 늘리지 않음)"
                        )
                    requirement["guidance"] = str(spec.get("guidance") or "").strip()
            requirements.append(requirement)
        return requirements

    def build_payload(
        self,
        topic: VerifiedTopic,
        plan: CarouselPlan,
        *,
        slice_indexes: list[int] | None = None,
        used_facts: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "topic": {
                "category": topic.category,
                "headline": topic.headline,
                "what_changed": topic.what_changed,
                "why_it_matters": topic.why_it_matters,
                "key_facts": topic.key_facts,
                "technical_details": topic.technical_details,
                "verdict": topic.verdict,
                # 아래 항목은 "쓰지 말 것" 목록입니다.
                "claims_to_avoid": topic.unverified_claims,
                # closing 의 "what to watch next" 재료. 검증 상태를 언급하지 말고
                # 앞으로 확인할 지점으로 바꿔 쓸 것.
                "watch_candidates": topic.open_questions,
                "source_names": [s.name for s in topic.sources if s.name],
            },
            "plan": plan.to_dict(),
            "slide_requirements": [
                req
                for req in self._slide_requirements(plan)
                if slice_indexes is None or req["index"] in slice_indexes
            ],
            # 이미 앞 슬라이드에서 사용한 문장. 같은 사실을 다시 쓰지 않습니다.
            "used_facts": used_facts or [],
            "closing_format": self.channel.closing_spec(),
            "style": {
                "language": self.channel.get("content.language.primary", "ko"),
                "keep_english": self.channel.get("content.language.keep_english", []),
                "tone": self.channel.get("content.tone.principles", []),
                "banned_phrases": self.channel.banned_phrases,
                "brand": self.channel.brand_name,
            },
        }

    # ---------------------------------------------------------------------- run
    def _parse_slides(
        self, data: dict[str, Any], plan: CarouselPlan, wanted: list[int]
    ) -> dict[int, Slide]:
        """응답에서 요청한 index 의 슬라이드만 파싱합니다."""
        raw_slides = data.get("slides")
        if not isinstance(raw_slides, list) or not raw_slides:
            raise AgentError("writer 응답에 slides 가 없습니다", agent=self.name)

        spec = self.channel.template(plan.template)
        parsed: dict[int, Slide] = {}
        for raw in raw_slides:
            if not isinstance(raw, dict):
                continue
            try:
                index = int(raw.get("index"))
            except (TypeError, ValueError):
                continue
            if index not in wanted:
                continue
            body = raw.get("body") or []
            if isinstance(body, str):
                body = [body]
            extra_body = raw.get("extra_body") or []
            if isinstance(extra_body, str):
                extra_body = [extra_body]
            parsed[index] = Slide(
                index=index,
                type=SlideType(spec.slide_type(index)),
                eyebrow=str(raw.get("eyebrow") or "").strip(),
                headline=str(raw.get("headline") or "").strip(),
                subtitle=str(raw.get("subtitle") or "").strip(),
                title=str(raw.get("title") or "").strip(),
                body=[str(item).strip() for item in body if str(item).strip()],
                extra_title=str(raw.get("extra_title") or "").strip(),
                extra_body=[str(item).strip() for item in extra_body if str(item).strip()],
            )
        return parsed

    def run(self, topic: VerifiedTopic, plan: CarouselPlan) -> Carousel:
        spec = self.channel.template(plan.template)
        chunk_from = int(self.channel.get("content.duplicate.chunk_from_slides", 7))

        slides: dict[int, Slide] = {}
        headline = ""

        if spec.slides >= chunk_from:
            # 장수가 많으면 두 번에 나눠 생성하고, 두 번째 호출에 used_facts 를
            # 넘겨 앞에서 쓴 사실의 재사용을 막습니다.
            split = (spec.slides + 1) // 2
            first = list(range(1, split + 1))
            second = list(range(split + 1, spec.slides + 1))

            data = self.ask_json(
                self.build_payload(topic, plan, slice_indexes=first),
                instruction=SLIDE_INSTRUCTION,
            )
            headline = str(data.get("headline") or "").strip()
            slides.update(self._parse_slides(data, plan, first))

            used = [
                line
                for index in sorted(slides)
                for line in [*slides[index].body, *slides[index].extra_body]
            ]
            if slides.get(1) and slides[1].subtitle:
                used.append(slides[1].subtitle)

            data2 = self.ask_json(
                self.build_payload(topic, plan, slice_indexes=second, used_facts=used),
                instruction=SLIDE_INSTRUCTION
                + " These slides must introduce facts NOT present in used_facts.",
            )
            slides.update(self._parse_slides(data2, plan, second))
            logger.info(
                "[writer] %s: 2회 분할 생성 (used_facts %d줄 전달)", plan.template, len(used)
            )
        else:
            wanted = list(range(1, spec.slides + 1))
            data = self.ask_json(
                self.build_payload(topic, plan), instruction=SLIDE_INSTRUCTION
            )
            headline = str(data.get("headline") or "").strip()
            slides.update(self._parse_slides(data, plan, wanted))

        missing = [i for i in range(1, spec.slides + 1) if i not in slides]
        if missing:
            raise AgentError(
                f"writer 가 slide {missing} 를 생성하지 않았습니다 "
                f"(template={plan.template}, 필요 {spec.slides}장)",
                agent=self.name,
            )
        ordered = [slides[i] for i in range(1, spec.slides + 1)]

        # 출처는 LLM 이 만들지 않습니다. 검증된 토픽 데이터에서 그대로 옮깁니다.
        closing = next((s for s in ordered if s.type is SlideType.CLOSING), None)
        if closing is not None:
            max_shown = int(self.channel.get("instagram.sources.max_shown", 3))
            closing.sources = topic.sources[:max_shown]

        carousel = Carousel(
            template=plan.template,
            topic=topic.category.upper() if topic.category else "",
            headline=headline or topic.headline,
            slides=ordered,
            brand=self.channel.brand_name,
            meta={
                "topic_id": topic.topic_id,
                "engine_headline": topic.headline,
                "fact_check_verdict": topic.verdict,
                "plan": plan.to_dict(),
                "sources": [s.to_dict() for s in topic.sources],
                "unverified_claims": topic.unverified_claims,
                "partial_reasons": list(
                    topic.fact_check.get("partial_reasons") or []
                ),
                "engine_quality_metrics": dict(
                    topic.fact_check.get("quality_metrics") or {}
                ),
                # 재렌더(--render-only) 시에도 검수 정보를 유지하기 위해 엔진 요약을
                # 함께 보관합니다. 공개 출력에는 쓰이지 않습니다.
                "research_summary": {
                    "what_changed": topic.what_changed,
                    "why_it_matters": topic.why_it_matters,
                    "key_facts": topic.key_facts,
                },
            },
        )
        logger.info(
            "[writer] %s: %d slides, headline %r",
            plan.template,
            carousel.slide_count,
            truncate(carousel.headline, 60),
        )
        return carousel


class InstagramCaptionWriter(Agent):
    """캡션은 캐러셀과 별도로 생성합니다(형식과 길이 규칙이 다름)."""

    name = "instagram_writer"
    task = "ig_caption"
    prompt_file = "instagram_writer.md"

    def __init__(self, *, config, llm, channel: ChannelConfig) -> None:  # noqa: ANN001
        super().__init__(config=config, llm=llm, prompt_dir=channel.prompts_dir)
        self.channel = channel

    def build_payload(self, topic: VerifiedTopic, carousel: Carousel) -> dict[str, Any]:
        caption_cfg = self.channel.get("instagram.caption", {}) or {}
        hashtags = caption_cfg.get("hashtags", {}) or {}
        base_tag = (hashtags.get("base") or {}).get(topic.category.lower(), "")
        return {
            "carousel": {
                "headline": carousel.headline,
                "slides": [slide.to_dict() for slide in carousel.slides],
            },
            "topic": {
                "category": topic.category,
                "what_changed": topic.what_changed,
                "why_it_matters": topic.why_it_matters,
                "key_facts": topic.key_facts,
                "claims_to_avoid": topic.unverified_claims,
            },
            "source_names": [s.name for s in carousel.sources if s.name],
            "rules": {
                "target_chars": caption_cfg.get("target_chars", 900),
                "max_chars": caption_cfg.get("max_chars", 2200),
                "sentences": f"{caption_cfg.get('min_sentences', 3)}~"
                f"{caption_cfg.get('max_sentences', 5)}문장",
                "hashtags": f"{hashtags.get('min', 3)}~{hashtags.get('max', 6)}개",
                "required_hashtag": base_tag,
                "no_urls_in_body": not caption_cfg.get("include_urls_in_body", False),
                "banned_phrases": self.channel.banned_phrases,
            },
        }

    @staticmethod
    def _parts(data: dict[str, Any]) -> tuple[str, list[str], str]:
        hook = str(data.get("hook") or "").strip()
        body = data.get("body") or []
        if isinstance(body, str):
            body = [body]
        body_lines = [str(line).strip() for line in body if str(line).strip()]
        return hook, body_lines, str(data.get("takeaway") or "").strip()

    def _plain_problems(
        self, topic: VerifiedTopic, carousel: Carousel, hook: str, body: list[str], take: str
    ) -> list[str]:
        names = known_names(
            carousel.headline,
            carousel.cover.headline if carousel.cover else "",
            carousel.cover.subtitle if carousel.cover else "",
            topic.headline,
            " ".join(s.name for s in carousel.sources),
        )
        return caption_problems("\n".join([hook, *body, take]), self.channel, names)

    def run(self, topic: VerifiedTopic, carousel: Carousel) -> tuple[str, list[str]]:
        """``(caption_text, hashtags)`` 반환."""
        payload = self.build_payload(topic, carousel)
        data = self.ask_json(payload, instruction=CAPTION_INSTRUCTION)
        hook, body_lines, takeaway = self._parts(data)

        if not hook and not body_lines:
            raise AgentError("caption 응답이 비어 있습니다", agent=self.name)

        # 쉬운 말 규칙: 어기면 구체적인 문제를 알려 주고 다시 쓰게 한다(개선될 때만 채택).
        retries = int((self.channel.get("content.caption_style", {}) or {}).get("retry", 1))
        problems = self._plain_problems(topic, carousel, hook, body_lines, takeaway)
        for _ in range(max(0, retries) if problems else 0):
            try:
                again = self.ask_json(
                    {**payload, "fix": {
                        "problems": problems,
                        "previous": {"hook": hook, "body": body_lines, "takeaway": takeaway},
                    }},
                    instruction=CAPTION_INSTRUCTION + CAPTION_FIX_INSTRUCTION,
                )
            except AgentError as exc:
                logger.warning("[caption] 쉬운 말 재작성 실패(처음 캡션 유지): %s", exc)
                break
            new_hook, new_body, new_take = self._parts(again)
            new_problems = self._plain_problems(topic, carousel, new_hook, new_body, new_take)
            if (new_hook or new_body) and len(new_problems) < len(problems):
                hook, body_lines, takeaway, data, problems = (
                    new_hook, new_body, new_take, again, new_problems)
                logger.info("[caption] 쉬운 말로 다시 씀: 문제 %d건 남음", len(problems))
            else:
                break

        caption_cfg = self.channel.get("instagram.caption", {}) or {}
        hashtag_cfg = caption_cfg.get("hashtags", {}) or {}
        max_tags = int(hashtag_cfg.get("max", 6))
        base_tag = (hashtag_cfg.get("base") or {}).get(topic.category.lower(), "")

        tags: list[str] = []
        if base_tag:
            tags.append(base_tag)
        for raw in data.get("hashtags") or []:
            tag = str(raw).strip()
            if not tag:
                continue
            if not tag.startswith("#"):
                tag = "#" + tag.lstrip("#")
            if tag not in tags:
                tags.append(tag)
        tags = tags[:max_tags]

        source_names = [s.name for s in carousel.sources if s.name]
        blocks = [hook, "", "\n".join(body_lines)]
        if takeaway:
            blocks += ["", takeaway]
        if source_names:
            blocks += ["", "Sources: " + " · ".join(dict.fromkeys(source_names))]
        caption = "\n".join(block for block in blocks if block is not None).strip()

        logger.info("[caption] %d자, 해시태그 %d개", len(caption), len(tags))
        return caption, tags
