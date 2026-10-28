"""R_A_Glance 채널 파이프라인.

공통 엔진이 검증까지 끝낸 토픽을 받아 Instagram carousel 산출물을 만듭니다::

    verified topic (engine research.json)
      → planner   : 3 / 5 / 7 선택 + 슬라이드 서사
      → writer    : 슬라이드 문구
      → editor    : 압축 · 과장/내부 표현 제거
      → caption   : 캡션 + 해시태그
      → renderer  : HTML/CSS → PNG + preview.html
      → validator : 품질 게이트 → publishable

사용법::

    python -m channel.src.pipeline                      # 최신 엔진 출력의 1순위 토픽
    python -m channel.src.pipeline --index 2            # n번째 토픽
    python -m channel.src.pipeline --topic <topic_id>
    python -m channel.src.pipeline --no-render          # 렌더 없이 JSON/캡션만
    python -m channel.src.pipeline --template deep_dive # planner 결정 덮어쓰기

종료 코드: 0 publishable / 1 차단 항목 있음 / 2 설정·입력 오류 / 130 중단.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from src.agents.llm import build_llm
from src.core.config import load_config
from src.core.errors import AgentError, ConfigError, ContentOpsError, LLMError
from src.core.logging_setup import get_logger, setup_logging
from src.core.text import to_iso, utcnow

from channel.src.approval import init_record
from channel.src.autopublish import access_ok, finish_run
from channel.src.llm_guard import llm_ready
from channel.src.token_store import maintain
from channel.src.weekly import order_topics, research_age_days
from channel.src.auto_gate import InstagramReviewer, run_gate
from channel.src.config import ChannelConfig, load_channel_config
from channel.src.editor import InstagramEditor, sanitize_caption
from channel.src.grounding import InstagramGroundingChecker
from channel.src.hook import InstagramHookWriter
from channel.src.hook import review_lines as hook_review_lines
from channel.src.inputs import TopicInputError, load_topics, select_topic
from channel.src.material import MaterialAssessment, assess_material, enforce_template
from channel.src.planner import CarouselPlan, InstagramPlanner, build_template_plan
from channel.src.recovery import (
    REASON_DENSITY_RECOVERED,
    REASON_INSUFFICIENT_UNIQUE_MATERIAL,
    REASON_TEMPLATE_DOWNGRADED,
    REASON_THIN_AFTER_REPAIR,
    DensityRecoveryState,
    cover_body_duplicates,
    safe_downgrade_target,
    thin_content_slides,
    unused_verified_material,
)
from channel.src.renderer import CarouselRenderer, RenderError, RenderResult
from channel.src.repair import (
    InstagramIssueRepairer,
    InstagramRepairer,
    repair_loop,
    review_repair,
)
from channel.src.run_report import (
    REASON_DUPLICATE_TOPIC,
    REASON_GENERATION_FAILED,
    REASON_MATERIAL_TOO_THIN,
    REASON_QUALITY_GATE_FAILED,
    RESULT_FAILED,
    RESULT_GENERATED,
    RESULT_SKIPPED,
    ChannelRunReport,
    write_run_report,
)
from channel.src.schema import Carousel, Source, VerifiedTopic
from channel.src.similarity import find_duplicates
from channel.src.validator import CarouselValidator, ValidationReport
from channel.src.writer import InstagramCaptionWriter, InstagramWriter

logger = get_logger("R_A_Glance.pipeline")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ChannelRunResult:
    """한 번의 채널 실행 결과."""

    output_dir: Path
    topic: VerifiedTopic
    plan: CarouselPlan | None = None
    carousel: Carousel | None = None
    render: RenderResult | None = None
    report: ValidationReport | None = None
    files: list[Path] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    llm_usage: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    material: MaterialAssessment | None = None
    repaired_pairs: int = 0

    @property
    def quality_score(self) -> float:
        return self.report.quality.score if self.report and self.report.quality else 0.0

    @property
    def publishable(self) -> bool:
        return bool(self.report and self.report.publishable and not self.errors)


_DATED = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?$")


def existing_topic_ids(channel: ChannelConfig, days: int) -> set[str]:
    """최근 ``days`` 일 안에 이미 후보가 만들어진 topic_id(승인·보류·거절 모두)."""
    found: set[str] = set()
    root = channel.outputs_dir
    if not root.is_dir() or days <= 0:
        return found
    today = date.today()
    for folder in root.iterdir():
        if not (folder.is_dir() and _DATED.match(folder.name)):
            continue
        try:
            age = (today - date.fromisoformat(folder.name[:10])).days
            meta = json.loads((folder / "carousel.json").read_text(encoding="utf-8")).get("meta")
        except (ValueError, OSError):
            continue
        topic_id = (meta or {}).get("topic_id")
        if topic_id and age <= days:
            found.add(str(topic_id))
    return found


def _gate_info(result: Any) -> dict[str, Any]:
    """생성 결과에 기록된 자동 게이트 요약(없으면 빈 dict)."""
    carousel = getattr(result, "carousel", None)
    return (getattr(carousel, "meta", None) or {}).get("auto_gate") or {}


def _approved(result: ChannelRunResult, channel: ChannelConfig) -> bool:
    """후보가 사람 없이 게시 후보로 남을 수 있는가: 게시 가능 + (자동 게이트가 켜져 있다면) 게이트 승인."""
    if not result.publishable:
        return False
    if not bool(channel.get("content.auto_gate.enabled", True)):
        return True
    return _gate_info(result).get("outcome") == "approved"


def _unique_dir(base: Path) -> Path:
    if not base.exists():
        return base
    for suffix in range(2, 100):
        candidate = base.parent / f"{base.name}-{suffix}"
        if not candidate.exists():
            return candidate
    return base


# --------------------------------------------------------------------------- #
# 산출물 쓰기
# --------------------------------------------------------------------------- #
def write_carousel_json(channel: ChannelConfig, carousel: Carousel, output_dir: Path) -> Path:
    path = output_dir / channel.output_filename("carousel_json", "carousel.json")
    path.write_text(
        json.dumps(carousel.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def write_caption_md(channel: ChannelConfig, carousel: Carousel, output_dir: Path) -> Path:
    path = output_dir / channel.output_filename("caption_md", "caption.md")
    sources = [s for s in carousel.sources if s.name]
    lines = [
        carousel.caption.strip(),
        "",
        " ".join(carousel.hashtags),
        "",
        "<!-- metadata: 게시 시 본문에 포함하지 않습니다 -->",
        "<!--",
        f"template: {carousel.template}",
        f"topic_id: {carousel.meta.get('topic_id', '')}",
        f"verdict: {carousel.meta.get('fact_check_verdict', '')}",
        "sources:",
    ]
    lines += [f"  - {s.name}: {s.url}" for s in sources]
    lines += ["-->", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_review_md(
    channel: ChannelConfig,
    result: ChannelRunResult,
    output_dir: Path,
) -> Path:
    """사람이 검수할 내용 + 내부 표현(미검증 항목 등)을 모두 이 파일에만 씁니다."""
    from channel.src.validator import SEVERITY_LABEL

    carousel = result.carousel
    report = result.report
    path = output_dir / channel.output_filename("review_md", "review.md")

    lines = [
        f"# R_A_Glance 검수 — {output_dir.name}",
        "",
        f"- 판정: **{'게시 가능' if result.publishable else '게시 보류'}** "
        f"(publishable = {str(result.publishable).lower()}, "
        f"score = {result.quality_score:.2f})",
        f"- 토픽: {result.topic.headline}",
        f"- topic_id: `{result.topic.topic_id}`",
        f"- fact check: **{result.topic.verdict}**",
        f"- 템플릿: `{carousel.template if carousel else '-'}` "
        f"({carousel.slide_count if carousel else 0} slides)",
        f"- 렌더 백엔드: `{result.render.backend if result.render else '-'}`",
        f"- LLM: {result.llm_usage or '기록 없음'}",
        "",
    ]

    if carousel is not None:
        lines += hook_review_lines(carousel, output_dir.name)

    if report and report.quality:
        q = report.quality
        lines += [
            "## content_quality_score",
            "",
            f"- **{q.score:.2f}** / 기준 {q.threshold:.2f} → {'통과' if q.passes else '미달'}",
            "",
            "| 성분 | 점수 | 가중치 |",
            "|---|---|---|",
        ]
        for name, value in q.components.items():
            lines.append(f"| {name} | {value:.2f} | {q.weights.get(name, 0):.2f} |")
        if q.notes:
            lines += ["", *[f"- {note}" for note in q.notes]]
        lines.append("")

    if result.material:
        m = result.material
        lines += [
            "## 재료 게이트",
            "",
            f"- 사용 가능 fact {m.usable_facts}개 / {m.material_chars}자 → "
            f"최대 템플릿 `{m.max_template}`",
            f"- 판정: {m.reason}",
            "",
        ]

    if report and report.duplicates and report.duplicates.pairs:
        lines += ["## 슬라이드 간 중복", ""]
        for pair in report.duplicates.pairs:
            lines.append(
                f"- [{pair.severity}] {pair.score:.2f} slide {pair.slide_a} ↔ "
                f"{pair.slide_b}: \"{pair.text_a[:44]}\" / \"{pair.text_b[:44]}\""
            )
        if result.repaired_pairs:
            lines.append(f"- 재작성 시도: {result.repaired_pairs}쌍")
        lines.append("")

    if report and report.tone:
        t = report.tone
        lines += [
            "## 문체",
            "",
            f"- 명사형 {t.nominal} / 평서 {t.plain} / 경어체 {t.polite} "
            f"(명사형 비율 {t.nominal_ratio:.2f})",
            "",
        ]

    if result.plan:
        lines += [
            "## 템플릿 선택 근거",
            "",
            f"- planner: {result.plan.reason or '(설명 없음)'}",
            f"- 독자: {result.plan.audience or '-'}",
            "",
        ]

    if report:
        lines += ["## 품질 게이트", ""]
        for severity in ("block", "warn", "info"):
            items = [f for f in report.findings if f.severity == severity]
            if not items:
                continue
            label = SEVERITY_LABEL.get(severity, severity)
            for finding in items:
                where = f" — `{finding.where}`" if finding.where else ""
                lines.append(f"- {label} ({finding.code}) {finding.message}{where}")
        if not report.findings:
            lines.append("- 지적 사항 없음")
        lines += [
            "",
            f"검사 항목: {', '.join(dict.fromkeys(report.checked))}",
            "",
        ]

    if result.errors:
        lines += ["## 실행 오류", ""]
        lines += [f"- {error}" for error in result.errors]
        lines.append("")

    # 내부 정보(공개 출력에 넣지 않는 것들)
    lines += ["## 원문 대조용 정보 (공개 출력에 포함하지 않음)", ""]
    if result.topic.unverified_claims:
        lines += ["미검증·부분 검증 항목:", ""]
        lines += [f"- {claim}" for claim in result.topic.unverified_claims]
        lines.append("")
    lines += ["출처:", ""]
    lines += [f"- {s.name}: {s.url}" for s in result.topic.sources if s.name or s.url]
    lines += ["", "엔진 research 요약:", "", f"- what_changed: {result.topic.what_changed}"]
    lines += [f"- why_it_matters: {result.topic.why_it_matters}", ""]
    if result.topic.key_facts:
        lines += ["- key facts:"]
        lines += [f"  - {fact}" for fact in result.topic.key_facts]
        lines.append("")

    if carousel and carousel.meta.get("editor_changes"):
        lines += ["## 에디터 변경 내역", ""]
        lines += [f"- {change}" for change in carousel.meta["editor_changes"]]
        lines.append("")

    if result.render:
        lines += ["## 렌더 결과", ""]
        for slide_render in result.render.slides:
            lines.append(
                f"- slide {slide_render.index:02d}: {slide_render.png_path.name} "
                f"({slide_render.width}x{slide_render.height}, fit={slide_render.fit}, "
                f"scale={slide_render.fit_scale:g}, {slide_render.metrics})"
            )
        lines.append("")

    lines += [
        "## 사람이 할 일",
        "",
        "1. `[차단]` 항목 해결 (없으면 게시 가능)",
        "2. `preview.html` 로 5장 흐름·줄바꿈 확인",
        "3. 숫자·버전·날짜를 위 출처 원문과 대조",
        "4. `caption.md` 본문만 복사해 게시 (metadata 주석 제외)",
        "",
        f"생성 {to_iso(utcnow())}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _repair_semantic_duplicates(
    carousel: Carousel,
    *,
    topic: VerifiedTopic,
    assessment: MaterialAssessment,
    editor: InstagramEditor,
    channel: ChannelConfig,
) -> tuple[Carousel, int]:
    """cover는 먼저, 본문은 뒤쪽 문장을 우선해 중복을 수리."""
    repaired_pairs = 0
    cover_pairs = cover_body_duplicates(carousel, channel)
    if cover_pairs:
        carousel = editor.repair_cover_duplicates(carousel, cover_pairs, topic=topic)
        repaired_pairs += len(cover_pairs)

    cfg = channel.get("content.duplicate", {}) or {}
    attempts = int(cfg.get("repair_attempts", 1))
    block_at = float(cfg.get("block_threshold", 0.75))
    warn_at = float(cfg.get("warn_threshold", 0.62))
    cover_index = carousel.cover.index if carousel.cover else 1
    for attempt in range(attempts):
        found = find_duplicates(carousel, warn=warn_at, block=block_at)
        blocking = [
            pair
            for pair in found.blocks
            if cover_index not in (pair.slide_a, pair.slide_b)
        ]
        if not blocking:
            break
        unused = unused_verified_material(
            assessment.facts,
            carousel,
            duplicate_threshold=block_at,
        )
        logger.info(
            "[pipeline] 중복 %d쌍(block) 재작성 시도 %d/%d",
            len(blocking),
            attempt + 1,
            attempts,
        )
        carousel = editor.repair_duplicates(
            carousel,
            blocking,
            unused_material=unused,
        )
        repaired_pairs += len(blocking)
    return carousel, repaired_pairs


def _recheck_duplicates_after_refill(
    carousel: Carousel,
    recovery: DensityRecoveryState,
    *,
    topic: VerifiedTopic,
    assessment: MaterialAssessment,
    editor: InstagramEditor,
    channel: ChannelConfig,
) -> tuple[Carousel, int]:
    """밀도 회복이 슬라이드를 다시 채웠다면 중복 수리를 한 번 더 수행.

    회복 재료는 "현재 슬라이드와 유사도 < block 임계값"이면 새 재료로 취급되므로,
    앞 슬라이드를 말만 바꾼 문장이 다시 들어와 BLOCK 이 생길 수 있습니다. 회복이
    실제로 일어난 경우에만 재검사하며, 그 뒤에도 남는 중복이나 얇아진 슬라이드는
    숨기지 않고 validator 가 차단하도록 둡니다.
    """
    if not recovery.recovered:
        return carousel, 0
    return _repair_semantic_duplicates(
        carousel,
        topic=topic,
        assessment=assessment,
        editor=editor,
        channel=channel,
    )


def _recover_density(
    carousel: Carousel,
    *,
    assessment: MaterialAssessment,
    editor: InstagramEditor,
    channel: ChannelConfig,
) -> tuple[Carousel, DensityRecoveryState, str | None]:
    """미사용 검증 재료로 회복한 뒤, 필요할 때만 축소 대상을 반환."""
    thin_before = thin_content_slides(carousel, channel)
    state = DensityRecoveryState(thin_before=thin_before)
    if not thin_before:
        return carousel, state, None

    state.reasons.append(REASON_THIN_AFTER_REPAIR)
    block_at = float(channel.get("content.duplicate.block_threshold", 0.75))
    unused = unused_verified_material(
        assessment.facts,
        carousel,
        duplicate_threshold=block_at,
    )
    state.unused_material_count = len(unused)
    carousel = editor.recover_thin_slides(
        carousel,
        thin_before,
        unused_material=unused,
    )
    state.thin_after = thin_content_slides(carousel, channel)
    if not state.thin_after:
        state.reasons.append(REASON_DENSITY_RECOVERED)
        return carousel, state, None

    target, unique = safe_downgrade_target(carousel.template, assessment.facts, channel)
    state.unique_material_count = len(unique)
    state.unique_material_chars = sum(len(fact) for fact in unique)
    if target:
        state.downgraded_from = carousel.template
        state.downgraded_to = target
        state.reasons.append(REASON_TEMPLATE_DOWNGRADED)
        return carousel, state, target

    state.reasons.append(REASON_INSUFFICIENT_UNIQUE_MATERIAL)
    return carousel, state, None


# --------------------------------------------------------------------------- #
# 실행
# --------------------------------------------------------------------------- #
def run_channel(
    *,
    topic: VerifiedTopic,
    channel: ChannelConfig,
    app_config,  # noqa: ANN001 - 엔진 AppConfig
    llm,  # noqa: ANN001 - 엔진 LLMClient
    output_dir: Path,
    render: bool = True,
    template_override: str | None = None,
) -> ChannelRunResult:
    """토픽 하나 → 캐러셀 산출물."""
    result = ChannelRunResult(output_dir=output_dir, topic=topic)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 0) 재료 게이트 — LLM 호출 전에 토픽이 캐러셀을 채울 수 있는지 확인
    assessment = assess_material(topic, channel)
    result.material = assessment
    if not assessment.sufficient:
        result.errors.append(f"재료 부족으로 생산 중단: {assessment.reason}")
        logger.warning("[material] %s — %s", topic.headline[:50], assessment.reason)
        return result

    # 1) planner
    started = time.monotonic()
    planner = InstagramPlanner(config=app_config, llm=llm, channel=channel)
    if template_override:
        plan = build_template_plan(
            channel,
            template_override,
            reason=f"CLI --template {template_override} 로 지정",
        )
    else:
        plan = planner.run(topic)

    # planner 선택을 재료로 검증합니다(재료보다 큰 템플릿은 축소).
    adjusted, reason = enforce_template(plan.template, assessment, channel)
    if adjusted != plan.template:
        plan = build_template_plan(
            channel,
            adjusted,
            reason=f"{plan.reason} / {reason}",
            audience=plan.audience,
        )
    result.plan = plan
    result.timings["planner"] = time.monotonic() - started

    # 2) writer
    started = time.monotonic()
    writer = InstagramWriter(config=app_config, llm=llm, channel=channel)
    carousel = writer.run(topic, plan)
    result.timings["writer"] = time.monotonic() - started

    # 3) editor
    started = time.monotonic()
    editor = InstagramEditor(config=app_config, llm=llm, channel=channel)
    carousel = editor.run(carousel)
    result.timings["editor"] = time.monotonic() - started
    result.carousel = carousel

    # 3b) cover 우선 중복 수리 → 본문 중복 수리 → 밀도 회복/템플릿 축소.
    # 축소 시에는 기존 슬라이드를 잘라내지 않고 새 plan으로 전체를 재생성합니다.
    recovery_history: list[dict[str, Any]] = []
    recovery_failures: list[dict[str, Any]] = []
    for _recovery_pass in range(3):
        carousel, repaired = _repair_semantic_duplicates(
            carousel,
            topic=topic,
            assessment=assessment,
            editor=editor,
            channel=channel,
        )
        result.repaired_pairs += repaired
        recovery_failures.extend(carousel.meta.get("recovery_failures") or [])
        carousel, recovery, downgrade_to = _recover_density(
            carousel,
            assessment=assessment,
            editor=editor,
            channel=channel,
        )
        recovery_history.append(recovery.to_dict())
        if not downgrade_to:
            carousel, repaired = _recheck_duplicates_after_refill(
                carousel,
                recovery,
                topic=topic,
                assessment=assessment,
                editor=editor,
                channel=channel,
            )
            result.repaired_pairs += repaired
            recovery_failures.extend(carousel.meta.get("recovery_failures") or [])
            break
        previous = carousel.template
        plan = build_template_plan(
            channel,
            downgrade_to,
            reason=(
                f"{REASON_TEMPLATE_DOWNGRADED}: {previous} → {downgrade_to}; "
                "중복 제거 후 유일 검증 재료 수준에 맞춰 전체 서사 재구성"
            ),
            audience=plan.audience,
        )
        carousel = writer.run(topic, plan)
        carousel = editor.run(carousel)

    carousel.meta["density_recovery"] = {
        "attempted": any(item["thin_before"] for item in recovery_history),
        "passes": recovery_history,
        "thin_after": thin_content_slides(carousel, channel),
        "recovered": not thin_content_slides(carousel, channel),
        "template_downgraded": next(
            (
                {
                    "from": item["downgraded_from"],
                    "to": item["downgraded_to"],
                }
                for item in recovery_history
                if item["downgraded_to"]
            ),
            None,
        ),
        "reasons": list(
            dict.fromkeys(
                reason
                for item in recovery_history
                for reason in item["reasons"]
            )
        ),
    }
    if recovery_failures:
        carousel.meta["recovery_failures"] = list(
            {
                (failure.get("code"), failure.get("reason")): failure
                for failure in recovery_failures
                if isinstance(failure, dict)
            }.values()
        )
    carousel.meta["quality_metrics"] = {
        "recovery_failed": bool(recovery_failures)
        or bool(thin_content_slides(carousel, channel)),
    }
    carousel.meta["plan"] = plan.to_dict()
    result.plan = plan
    result.carousel = carousel

    # 3b') 표지 제목 후보 3개(질문·숫자·반전). 코드 검사를 통과한 것만 남기고, 실패해도 계속한다.
    # 고른 제목은 아래 근거 추적·validator 를 일반 문구와 똑같이 다시 통과해야 한다.
    if bool(channel.get("content.hook.enabled", True)):
        started = time.monotonic()
        hook_options = InstagramHookWriter(config=app_config, llm=llm, channel=channel).run(
            topic, carousel
        )
        carousel.meta["hook_options"] = [option.to_dict() for option in hook_options]
        result.timings["hook"] = time.monotonic() - started

    # 3c) 근거 추적 — 확정된 슬라이드의 문장이 research 근거에 있는지 심사합니다.
    # 문구를 고치지 않고 판정만 기록하며, 결과는 validator 가 게이트에 반영합니다.
    if bool(channel.get("content.grounding.enabled", True)):
        started = time.monotonic()
        checker = InstagramGroundingChecker(config=app_config, llm=llm, channel=channel)
        carousel.meta["grounding"] = checker.run(topic, carousel).to_dict()
        result.timings["grounding"] = time.monotonic() - started

        # 3c') 자동 수정: 근거가 일부만 있는 문장·어려운 제목을 근거 범위로 좁히고 다시 심사한다.
        # 캡션은 이 뒤에 확정된 슬라이드로 쓰므로 고친 문장이 그대로 반영된다.
        if bool(channel.get("content.repair.enabled", True)):
            started = time.monotonic()
            repair_loop(
                topic,
                carousel,
                channel,
                repairer=InstagramRepairer(config=app_config, llm=llm, channel=channel),
                checker=checker,
            )
            result.timings["repair"] = time.monotonic() - started
            if bool(channel.get("content.repair.review_enabled", True)):
                review_repair(
                    topic,
                    carousel,
                    channel,
                    repairer=InstagramIssueRepairer(config=app_config, llm=llm, channel=channel),
                    reviewer=InstagramReviewer(config=app_config, llm=llm, channel=channel),
                    checker=checker,
                )

    # 4) caption
    started = time.monotonic()
    caption, hashtags = InstagramCaptionWriter(
        config=app_config, llm=llm, channel=channel
    ).run(topic, carousel)
    caption, caption_changes = sanitize_caption(caption, channel)
    carousel.caption = caption
    carousel.hashtags = hashtags
    if caption_changes:
        carousel.meta.setdefault("editor_changes", []).extend(caption_changes)
    result.timings["caption"] = time.monotonic() - started
    result.carousel = carousel

    # 5) renderer
    if render:
        started = time.monotonic()
        renderer = CarouselRenderer(channel)
        try:
            result.render = renderer.render(carousel, output_dir)
        except RenderError as exc:
            result.errors.append(str(exc))
            logger.error("[render] 실패: %s", exc)
        result.timings["renderer"] = time.monotonic() - started

    # 6) 산출물 + 검증
    result.files.append(write_carousel_json(channel, carousel, output_dir))
    result.files.append(write_caption_md(channel, carousel, output_dir))
    if result.render and result.render.preview_path:
        result.files.append(result.render.preview_path)
        result.files.extend(s.png_path for s in result.render.slides)

    validator = CarouselValidator(channel)
    result.report = validator.validate(carousel, topic=topic, render=result.render)
    if result.errors:
        for error in result.errors:
            result.report.add("renderer_failure", "block", error)

    # Phase 2: 승인 대기 레코드 생성 (자동 게시는 하지 않음)
    if result.report is not None:
        init_record(
            output_dir,
            publishable=result.report.publishable,
            score=result.report.quality.score if result.report.quality else 0.0,
            warnings=[
                f"[BLOCK] {f.message}" if f.severity == "block" else f.message
                for f in result.report.findings
                if f.severity in {"block", "warn"}
            ],
        )

    # 자동 승인 게이트: 사람 검수를 대신한다. 실패·오류는 승인이 아니라 pending 으로 남긴다.
    if result.report is not None and bool(channel.get("content.auto_gate.enabled", True)):
        try:
            outcome, gate = run_gate(
                output_dir,
                channel,
                reviewer=InstagramReviewer(config=app_config, llm=llm, channel=channel),
                topic=topic,
            )
            carousel.meta["auto_gate"] = {"outcome": outcome, "passed": gate.passed}
        except Exception as exc:  # noqa: BLE001 — 게이트 오류가 생성 결과를 지우면 안 된다
            logger.error("[auto_gate] 실행 실패(pending 유지): %s", exc)

    result.llm_usage = llm.usage_summary() if hasattr(llm, "usage_summary") else {}
    # 재렌더 시에도 사용량이 보이도록 산출물에 기록합니다.
    carousel.meta["llm_usage"] = result.llm_usage
    result.files.append(write_carousel_json(channel, carousel, output_dir))
    result.files.append(write_review_md(channel, result, output_dir))
    return result


def run_candidates(
    *,
    topics: list[VerifiedTopic],
    channel: ChannelConfig,
    app_config,  # noqa: ANN001 - 엔진 AppConfig
    llm,  # noqa: ANN001 - 엔진 LLMClient
    output_dir: Path,
    render: bool = True,
    template_override: str | None = None,
    runner=run_channel,  # noqa: ANN001 - 테스트에서 fixture runner 주입
    skip_existing: bool = False,
) -> tuple[ChannelRunResult | None, ChannelRunReport, Path]:
    """후보를 순서대로 시도하고 모든 결과를 ``run_report.json`` 에 남깁니다."""
    report = ChannelRunReport(run_id=output_dir.name)
    result: ChannelRunResult | None = None
    output_dir.mkdir(parents=True, exist_ok=True)

    # 무인 운영: 후보가 승인되지 못하면(BLOCK·자동 게이트 보류) 같은 주 안에서 다음 토픽을 시도한다.
    # 폴더 이름이 날짜 형식일 때만(직접 지정한 --output-dir 은 그대로 둔다). 비용은 max_attempts 로 제한.
    max_attempts = max(1, int(channel.get("instagram.posting.max_attempts", 3)))
    date_base = output_dir.name[:10] if _DATED.match(output_dir.name) else None
    attempts = 0
    # 같은 소식을 두 번 올리지 않는다: 최근 후보가 이미 있는 토픽은 건너뛴다(번호 지정 실행은 제외).
    already = (
        existing_topic_ids(channel, int(channel.get("instagram.posting.dedupe_days", 14)))
        if skip_existing
        else set()
    )

    try:
        for position, candidate in enumerate(topics):
            if candidate.topic_id in already:
                report.add(
                    topic_id=candidate.topic_id,
                    title=candidate.headline,
                    result=RESULT_SKIPPED,
                    reason=REASON_DUPLICATE_TOPIC,
                    detail="최근 후보가 이미 있음",
                )
                continue
            try:
                result = runner(
                    topic=candidate,
                    channel=channel,
                    app_config=app_config,
                    llm=llm,
                    output_dir=output_dir,
                    render=render,
                    template_override=template_override,
                )
            except (AgentError, LLMError, ContentOpsError) as exc:
                report.add(
                    topic_id=candidate.topic_id,
                    title=candidate.headline,
                    result=RESULT_FAILED,
                    reason=REASON_GENERATION_FAILED,
                    detail=str(exc),
                )
                raise

            if result.material is not None and not result.material.sufficient:
                report.add(
                    topic_id=candidate.topic_id,
                    title=candidate.headline,
                    result=RESULT_SKIPPED,
                    reason=REASON_MATERIAL_TOO_THIN,
                    detail=result.material.reason,
                )
                logger.warning(
                    "[pipeline] 재료 부족으로 다음 토픽 시도: %s",
                    candidate.headline[:50],
                )
                continue

            attempts += 1
            approved = _approved(result, channel)
            if approved:
                report.add(
                    topic_id=candidate.topic_id,
                    title=candidate.headline,
                    result=RESULT_GENERATED,
                    output_id=output_dir.name,
                )
                break
            blocks = result.report.blocks if result.report else []
            gate = _gate_info(result)
            report.add(
                topic_id=candidate.topic_id,
                title=candidate.headline,
                result=RESULT_FAILED,
                reason=REASON_QUALITY_GATE_FAILED,
                output_id=output_dir.name,
                detail="; ".join(f.code for f in blocks) or f"auto_gate {gate.get('outcome', '')}",
            )
            more = position + 1 < len(topics)
            if date_base and more and attempts < max_attempts:
                logger.warning(
                    "[pipeline] %s 승인되지 못함(%s) — 다음 토픽 시도 (%d/%d)",
                    output_dir.name, gate.get("outcome") or "BLOCK", attempts, max_attempts,
                )
                output_dir = _unique_dir(output_dir.parent / date_base)
                output_dir.mkdir(parents=True, exist_ok=True)
                report.run_id = output_dir.name
                continue
            break
    finally:
        report.complete()
        report_path = write_run_report(report, output_dir)

    if result is not None:
        result.files.append(report_path)
    return result, report, report_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m channel.src.pipeline",
        description="검증된 토픽을 R_A_Glance Instagram carousel 로 변환합니다.",
    )
    parser.add_argument("--research", default=None, help="엔진 research.json 경로")
    parser.add_argument("--topic", default=None, help="topic_id (접두사 허용)")
    parser.add_argument("--index", type=int, default=None, help="토픽 순번 (0부터)")
    parser.add_argument("--template", default=None, help="planner 결정 대신 템플릿 강제")
    parser.add_argument("--output-dir", default=None, help="출력 디렉터리 직접 지정")
    parser.add_argument("--run-date", default=None, help="출력 폴더 날짜 (YYYY-MM-DD)")
    parser.add_argument("--no-render", action="store_true", help="PNG 렌더 생략")
    parser.add_argument(
        "--render-only",
        default=None,
        help="기존 carousel.json 을 다시 렌더/검증만 합니다 (LLM 호출 없음)",
    )
    parser.add_argument(
        "--closing",
        default=None,
        choices=["A", "B"],
        help="closing slide 구성 변형 (A: Takeaway + What to watch next, B: Takeaway 2줄)",
    )
    parser.add_argument("--list-topics", action="store_true", help="사용 가능한 토픽만 출력")
    parser.add_argument("--log-level", default=None, help="DEBUG|INFO|WARNING|ERROR")
    return parser


def rerender(
    carousel_path: Path, *, channel: ChannelConfig, output_dir: Path | None = None
) -> ChannelRunResult:
    """기존 ``carousel.json`` 을 다시 렌더링하고 검증합니다.

    문구를 손으로 고친 뒤 확인하거나, 템플릿/CSS 를 수정했을 때 LLM credits 없이
    결과만 다시 만들기 위한 경로입니다.
    """
    payload = json.loads(carousel_path.read_text(encoding="utf-8"))
    carousel = Carousel.from_dict(payload)
    target_dir = output_dir or carousel_path.parent

    meta = carousel.meta or {}
    research = dict(meta.get("research_summary") or {})
    topic = VerifiedTopic(
        topic_id=str(meta.get("topic_id") or ""),
        category=carousel.topic,
        headline=str(meta.get("engine_headline") or carousel.headline),
        research=research,
        fact_check={
            "verdict": meta.get("fact_check_verdict", ""),
            "unverified_claims": meta.get("unverified_claims", []),
            "partial_reasons": meta.get("partial_reasons", []),
            "quality_metrics": meta.get("engine_quality_metrics", {}),
        },
        sources=[Source.from_dict(s) for s in (meta.get("sources") or [])],
    )

    result = ChannelRunResult(output_dir=target_dir, topic=topic, carousel=carousel)
    result.llm_usage = dict(meta.get("llm_usage") or {})
    if plan_data := meta.get("plan"):
        result.plan = CarouselPlan(
            template=str(plan_data.get("template") or carousel.template),
            slides=int(plan_data.get("slides") or carousel.slide_count),
            reason=str(plan_data.get("reason") or ""),
            audience=str(plan_data.get("audience") or ""),
            slide_plan=list(plan_data.get("slide_plan") or []),
        )
    renderer = CarouselRenderer(channel)
    started = time.monotonic()
    try:
        result.render = renderer.render(carousel, target_dir)
    except RenderError as exc:
        result.errors.append(str(exc))
        logger.error("[render] 실패: %s", exc)
    result.timings["renderer"] = time.monotonic() - started

    result.files.append(write_carousel_json(channel, carousel, target_dir))
    result.files.append(write_caption_md(channel, carousel, target_dir))
    if result.render and result.render.preview_path:
        result.files.append(result.render.preview_path)
        result.files.extend(s.png_path for s in result.render.slides)

    result.report = CarouselValidator(channel).validate(
        carousel, topic=topic, render=result.render
    )
    for error in result.errors:
        result.report.add("renderer_failure", "block", error)
    if result.report is not None:
        init_record(
            target_dir,
            publishable=result.report.publishable,
            score=result.report.quality.score if result.report.quality else 0.0,
            warnings=[
                f"[BLOCK] {f.message}" if f.severity == "block" else f.message
                for f in result.report.findings
                if f.severity in {"block", "warn"}
            ],
        )
    result.files.append(write_review_md(channel, result, target_dir))
    return result


def _generation_failed(channel: ChannelConfig, args: argparse.Namespace, exc: Exception) -> int:
    """생성 실패는 "승인된 후보 없음"(종료 코드 1)과 다른 **실행 실패**다 — 알리고 종료 코드 2 로 끝낸다.

    (run_channel.sh 는 종료 코드 1 을 이미 알린 것으로 보고 조용히 넘기므로, 여기서 알리지 않으면
    Kiro 업그레이드 등으로 생성이 깨져도 아무도 모른다.)
    """
    if args.index is None and args.topic is None:  # 무인 실행만 알린다(수동 실행은 터미널에서 본다)
        finish_run(channel, approved_dir=None, title="", held=[f"생성 실패: {str(exc)[:160]}"])
    return 2


def _print_summary(result: ChannelRunResult) -> None:
    report = result.report
    print("\n" + "=" * 68)
    print(f"R_A_Glance — {result.topic.headline[:60]}")
    print(
        f"template={result.carousel.template if result.carousel else '-'} "
        f"slides={result.carousel.slide_count if result.carousel else 0} "
        f"verdict={result.topic.verdict}"
    )
    if report:
        print(
            f"publishable={str(report.publishable).lower()} "
            f"score={report.quality.score if report.quality else 0:.2f} "
            f"(block {len(report.blocks)} / warn {len(report.warnings)} / "
            f"info {len(report.infos)})"
        )
        for finding in report.blocks:
            print(f"  [차단] {finding.message}")
    print(f"LLM: {result.llm_usage or '호출 없음'}")
    print(f"output: {result.output_dir}")
    for path in result.files:
        try:
            print(f"  - {path.relative_to(result.output_dir)}")
        except ValueError:
            print(f"  - {path}")
    print("=" * 68)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        app_config = load_config()
        channel = load_channel_config()
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2

    setup_logging(app_config, level=args.log_level)

    if args.closing:
        channel.content.setdefault("closing", {})["variant"] = args.closing
        logger.info("[channel] closing variant=%s (CLI 지정)", args.closing)

    # 재렌더 경로: LLM 을 호출하지 않습니다.
    if args.render_only:
        carousel_path = Path(args.render_only).expanduser()
        if not carousel_path.exists():
            logger.error("carousel.json 을 찾을 수 없습니다: %s", carousel_path)
            return 2
        try:
            result = rerender(
                carousel_path,
                channel=channel,
                output_dir=Path(args.output_dir).expanduser() if args.output_dir else None,
            )
        except ContentOpsError as exc:
            logger.error("재렌더 실패: %s", exc)
            return 1
        _print_summary(result)
        return 0 if result.publishable else 1

    try:
        topics, research_path = load_topics(
            Path(args.research) if args.research else None,
            root=PROJECT_ROOT,
            allowed_verdicts=channel.allowed_verdicts,
        )
    except TopicInputError as exc:
        logger.error("입력 오류: %s", exc)
        return 2

    if args.list_topics:
        print(f"\n{research_path}")
        for index, topic in enumerate(topics):
            print(f"  [{index}] {topic.verdict:20} {topic.category:11} {topic.headline[:64]}")
        return 0

    try:
        topic = select_topic(topics, topic_id=args.topic, index=args.index)
    except TopicInputError as exc:
        logger.error("%s", exc)
        return 2

    try:
        llm = build_llm()
    except LLMError as exc:
        logger.error("LLM provider 사용 불가: %s", exc)
        return 2

    run_date = args.run_date or date.today().isoformat()
    output_dir = (
        Path(args.output_dir).expanduser()
        if args.output_dir
        else _unique_dir(channel.outputs_dir / run_date)
    )

    # 재료 부족 토픽은 건너뛰고 다음 토픽을 시도합니다(문장을 만들어 채우지 않음).
    gate = channel.get("content.material_gate", {}) or {}
    skip_insufficient = str(gate.get("on_insufficient", "skip")) == "skip"
    ordered_topics = [topic]
    unattended = args.topic is None and args.index is None
    if skip_insufficient and unattended:
        ordered_topics += [t for t in topics if t.topic_id != topic.topic_id]
    if unattended:
        # 무인 실행: 월요일에 정한 주간 순서(verified 만, 카테고리 다양성)를 그대로 따른다.
        ordered_topics = order_topics(topics)
        max_age = int(channel.get("instagram.posting.max_research_age_days", 8))
        age = research_age_days(research_path)
        if age > max_age:
            msg = f"RAG 결과가 {age}일 전 것입니다(상한 {max_age}일) — 월요일 엔진 실행을 확인하세요"
            logger.error("[pipeline] %s", msg)
            finish_run(channel, approved_dir=None, title="", held=[msg])
            return 1
        # Kiro CLI 버전이 바뀌었으면(자동 업그레이드) 카드를 만들기 전에 호출·파싱을 한 번 시험한다.
        ready, why = llm_ready(llm)
        if not ready:
            logger.error("[pipeline] %s", why)
            finish_run(channel, approved_dir=None, title="", held=[why])
            return 2
        # 게시할 수 없는 상태(토큰 만료·앱 차단 등)면 카드를 만들지 않는다: LLM 비용과 쌓이는 후보를 막는다.
        maintain(channel)  # 토큰 자동 갱신(저장본은 ~/.r_a_glance, .env 는 건드리지 않는다)
        reachable, why = access_ok(channel)
        if not reachable:
            msg = f"Instagram API 접근 불가라 카드를 만들지 않았습니다 — {why}"
            logger.error("[pipeline] %s", msg)
            finish_run(channel, approved_dir=None, title="", held=[msg])
            return 1

    try:
        result, _run_report, _run_report_path = run_candidates(
            topics=ordered_topics,
            channel=channel,
            app_config=app_config,
            llm=llm,
            output_dir=output_dir,
            render=not args.no_render,
            template_override=args.template,
            skip_existing=args.index is None and args.topic is None,
        )
    except KeyboardInterrupt:
        logger.warning("중단됨")
        return 130
    except (AgentError, LLMError) as exc:
        logger.error("콘텐츠 생성 실패: %s", exc)
        return _generation_failed(channel, args, exc)
    except ContentOpsError as exc:
        logger.error("실패: %s", exc)
        return _generation_failed(channel, args, exc)

    if result is None:
        logger.error("시도할 후보가 없습니다.")
        return 1
    _print_summary(result)

    # 무인 실행의 마지막 단계: 승인된 후보는 (설정이 켜져 있으면) 게시하고, 모든 결과를 알린다.
    approved = _approved(result, channel)
    if args.index is None and args.topic is None:  # 번호를 직접 지정한 수동 실행은 알림·게시를 하지 않는다
        held = [
            f"{t.title[:40]}: {t.reason or t.result}" for t in _run_report.topics if t.result != "generated"
        ]
        outcome = finish_run(
            channel,
            approved_dir=_run_report_path.parent if approved else None,
            title=result.carousel.headline if approved and result.carousel else "",
            held=held,
        )
        logger.info("[pipeline] 마무리: %s", outcome)

    return 0 if approved else 1


if __name__ == "__main__":
    raise SystemExit(main())
