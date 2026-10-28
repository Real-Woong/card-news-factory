"""Instagram planner — carousel 타입과 슬라이드 서사를 결정합니다.

공통 엔진의 :class:`src.agents.base.Agent` 를 재사용하되 프롬프트는 채널 디렉터리
(``channel/prompts/instagram_planner.md``) 에서 읽습니다. 엔진은 이 모듈을
알지 못합니다(의존 방향 단방향).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.agents.base import Agent
from src.core.errors import AgentError
from src.core.logging_setup import get_logger
from src.core.text import truncate

from channel.src.config import ChannelConfig
from channel.src.schema import VerifiedTopic

logger = get_logger(__name__)

INSTRUCTION = (
    "Decide the carousel structure for this verified topic. "
    "Choose exactly one template from the provided options based on how much "
    "verifiable substance exists — never pad slides to reach a longer template, "
    "never compress a protocol/architecture change into 3 slides. "
    "Then write a one-line plan for each slide describing what it must convey "
    "(not the final copy). "
    'Return JSON: {"template": "...", "reason": "...", "audience": "...", '
    '"slide_plan": [{"index": 1, "type": "cover", "goal": "..."}, ...]}'
)


@dataclass
class CarouselPlan:
    """planner 결과."""

    template: str
    slides: int
    reason: str = ""
    audience: str = ""
    slide_plan: list[dict[str, Any]] = field(default_factory=list)

    def goal_for(self, index: int) -> str:
        for entry in self.slide_plan:
            if int(entry.get("index", -1)) == index:
                return str(entry.get("goal") or "")
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "slides": self.slides,
            "reason": self.reason,
            "audience": self.audience,
            "slide_plan": self.slide_plan,
        }


def build_template_plan(
    channel: ChannelConfig,
    template: str,
    *,
    reason: str,
    audience: str = "",
) -> CarouselPlan:
    """고정 템플릿의 전체 서사를 설정에서 새로 구성합니다."""
    spec = channel.template(template)
    return CarouselPlan(
        template=spec.name,
        slides=spec.slides,
        reason=reason,
        audience=audience,
        slide_plan=[
            {
                "index": section["index"],
                "type": spec.slide_type(section["index"]),
                "role": spec.role(section["index"]),
                "goal": spec.role(section["index"]),
            }
            for section in spec.sections
        ],
    )


class InstagramPlanner(Agent):
    """토픽의 정보량을 보고 3 / 5 / 7 중 하나를 고릅니다."""

    name = "instagram_planner"
    task = "ig_plan"
    prompt_file = "instagram_planner.md"

    def __init__(self, *, config, llm, channel: ChannelConfig) -> None:  # noqa: ANN001
        super().__init__(config=config, llm=llm, prompt_dir=channel.prompts_dir)
        self.channel = channel

    def build_payload(self, topic: VerifiedTopic) -> dict[str, Any]:
        options = [
            {
                "template": name,
                "slides": spec.slides,
                "when": spec.when,
                "sections": spec.sections,
            }
            for name, spec in self.channel.templates.items()
        ]
        return {
            "topic": {
                "topic_id": topic.topic_id,
                "category": topic.category,
                "headline": topic.headline,
                "what_changed": topic.what_changed,
                "why_it_matters": topic.why_it_matters,
                "key_facts": topic.key_facts,
                "technical_details": topic.technical_details,
                "fact_check_verdict": topic.verdict,
                "unverified_claims": topic.unverified_claims,
                "sources": [s.to_dict() for s in topic.sources],
            },
            "template_options": options,
            "default_template": self.channel.default_template,
            "rules": {
                "allowed_slide_counts": self.channel.allowed_slide_counts,
                "audience": "개발자 / CS 전공 학생 / AI·Blockchain builder",
                "questions_to_answer": self.channel.get("content.questions", []),
                "do_not_pad": "정보가 부족하면 더 짧은 템플릿을 고를 것",
                "do_not_compress": "핵심 fact 가 5개 이상이면 deep_dive 고려",
            },
        }

    def run(self, topic: VerifiedTopic) -> CarouselPlan:
        data = self.ask_json(self.build_payload(topic), instruction=INSTRUCTION)

        template = str(data.get("template") or "").strip()
        templates = self.channel.templates
        if template not in templates:
            # 모델이 잘못된 이름을 주면 slide 수로 역매핑을 시도합니다.
            raw_count = data.get("slides") or data.get("slide_count")
            spec = None
            if raw_count is not None:
                try:
                    spec = self.channel.template_for_slides(int(raw_count))
                except (TypeError, ValueError):
                    spec = None
            if spec is None:
                raise AgentError(
                    f"planner 가 허용되지 않은 템플릿을 반환했습니다: {template!r} "
                    f"(허용: {sorted(templates)})",
                    agent=self.name,
                )
            template = spec.name

        spec = templates[template]
        plan_entries: list[dict[str, Any]] = []
        for entry in data.get("slide_plan") or []:
            if not isinstance(entry, dict):
                continue
            try:
                index = int(entry.get("index"))
            except (TypeError, ValueError):
                continue
            if not 1 <= index <= spec.slides:
                continue
            plan_entries.append(
                {
                    "index": index,
                    "type": spec.slide_type(index),
                    "role": spec.role(index),
                    "goal": truncate(str(entry.get("goal") or "").strip(), 200),
                }
            )
        plan_entries.sort(key=lambda entry: entry["index"])

        # 누락된 슬라이드는 템플릿 정의의 role 로 채웁니다(구조는 항상 완전해야 함).
        present = {entry["index"] for entry in plan_entries}
        for index in range(1, spec.slides + 1):
            if index not in present:
                plan_entries.append(
                    {
                        "index": index,
                        "type": spec.slide_type(index),
                        "role": spec.role(index),
                        "goal": spec.role(index),
                    }
                )
        plan_entries.sort(key=lambda entry: entry["index"])

        plan = CarouselPlan(
            template=template,
            slides=spec.slides,
            reason=truncate(str(data.get("reason") or "").strip(), 300),
            audience=truncate(str(data.get("audience") or "").strip(), 200),
            slide_plan=plan_entries,
        )
        logger.info(
            "[planner] %s → %s (%d slides): %s",
            truncate(topic.headline, 50),
            plan.template,
            plan.slides,
            truncate(plan.reason, 80),
        )
        return plan
