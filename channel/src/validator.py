"""품질 게이트.

BLOCK 이 하나라도 있으면 ``publishable = false`` 입니다. 검사는 전부 기계적이며,
판정 근거를 ``review.md`` 에 남깁니다.

BLOCK
    slide 수가 3/5/7 아님, headline 없음, 출처 없음, caption 없음,
    슬라이드 이미지 없음, 렌더 실패, overflow, 미해결 핵심 사실,
    내부 표현 유출

WARNING
    headline 과다 길이, 슬라이드 과밀, partially_verified, 단일 출처,
    caption 길이 초과
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from src.core.logging_setup import get_logger
from src.core.text import canonicalize_url, truncate

from channel.src.config import ChannelConfig
from channel.src.freshness import find_stale_dates
from channel.src.grounding import (
    PARTIAL,
    STATUS_CHECKED,
    SUPPORTED,
    UNSUPPORTED,
    GroundingResult,
    collect_claims,
)
from channel.src.quality import QualityScore, ToneReport, analyze_tone, compute_quality_score
from channel.src.caption_style import caption_problems, known_names
from channel.src.title_style import JARGON, title_problems
from channel.src.recovery import cover_body_duplicates
from channel.src.renderer import RenderResult
from channel.src.schema import Carousel, SlideType, VerifiedTopic
from channel.src.similarity import DuplicateReport, find_duplicates
from channel.src.visual import ungrounded_values

logger = get_logger(__name__)

SEVERITY_LABEL = {"block": "[차단]", "warn": "[확인]", "info": "[참고]"}
_URL_IN_TEXT = re.compile(r"https?://\S+")


@dataclass
class Finding:
    """검사 결과 한 줄."""

    code: str
    severity: str  # block | warn | info
    message: str
    where: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "where": self.where,
        }


@dataclass
class ValidationReport:
    """캐러셀 하나의 품질 게이트 결과."""

    findings: list[Finding] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    quality: QualityScore | None = None
    duplicates: DuplicateReport | None = None
    tone: ToneReport | None = None

    def add(self, code: str, severity: str, message: str, where: str = "") -> None:
        self.findings.append(
            Finding(code=code, severity=severity, message=message, where=where)
        )

    @property
    def blocks(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "block"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warn"]

    @property
    def infos(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "info"]

    @property
    def publishable(self) -> bool:
        """BLOCK 0건 **그리고** content_quality_score 기준 통과.

        "기술적으로 생성 가능"이 아니라 "사람이 그대로 게시해도 되는 수준"을 뜻합니다.
        """
        if self.blocks:
            return False
        if self.quality is not None and not self.quality.passes:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "publishable": self.publishable,
            "blocks": len(self.blocks),
            "warnings": len(self.warnings),
            "content_quality_score": self.quality.to_dict() if self.quality else None,
            "duplicates": self.duplicates.to_dict() if self.duplicates else None,
            "tone": self.tone.to_dict() if self.tone else None,
            "findings": [f.to_dict() for f in self.findings],
            "checked": self.checked,
        }


class CarouselValidator:
    """스키마 · 텍스트 예산 · 렌더 결과 · 내부 표현 유출을 검사합니다."""

    def __init__(self, channel: ChannelConfig) -> None:
        self.channel = channel

    # ------------------------------------------------------------------ 구조
    def _check_structure(self, carousel: Carousel, report: ValidationReport) -> None:
        report.checked.append("slide_count")
        allowed = self.channel.allowed_slide_counts
        if carousel.slide_count not in allowed:
            report.add(
                "slide_count_not_allowed",
                "block",
                f"slide 수 {carousel.slide_count} 는 허용값 {allowed} 이 아닙니다",
            )

        spec = self.channel.templates.get(carousel.template)
        if spec is None:
            report.add(
                "unknown_template", "block", f"알 수 없는 템플릿 `{carousel.template}`"
            )
        elif spec.slides != carousel.slide_count:
            report.add(
                "slide_count_mismatch",
                "block",
                f"템플릿 `{carousel.template}` 은 {spec.slides}장인데 "
                f"{carousel.slide_count}장이 생성되었습니다",
            )

        report.checked.append("slide_indexes")
        indexes = [slide.index for slide in carousel.slides]
        if indexes != list(range(1, carousel.slide_count + 1)):
            report.add(
                "slide_index_invalid", "block", f"slide index 가 연속적이지 않습니다: {indexes}"
            )

        report.checked.append("slide_types")
        if carousel.cover is None:
            report.add("missing_cover", "block", "cover 슬라이드가 없습니다")
        if carousel.closing is None:
            report.add("missing_closing", "block", "closing 슬라이드가 없습니다")
        if spec is not None:
            for slide in carousel.slides:
                expected = spec.slide_type(slide.index)
                if expected and str(slide.type) != expected:
                    report.add(
                        "slide_type_mismatch",
                        "warn",
                        f"slide {slide.index} 타입이 {slide.type} (템플릿 기대값 {expected})",
                        where=f"slide {slide.index}",
                    )

    # -------------------------------------------------------------- 필수 내용
    def _check_content(self, carousel: Carousel, report: ValidationReport) -> None:
        report.checked.append("headline")
        cover = carousel.cover
        headline = (cover.headline if cover else "") or carousel.headline
        if not headline.strip():
            report.add("missing_headline", "block", "headline 이 없습니다")
        else:
            budget = self.channel.text_budget("cover")
            hard = int(budget.get("headline_max_chars", 78))
            soft = int(budget.get("headline_warn_chars", 62))
            if len(headline) > hard:
                report.add(
                    "headline_too_long",
                    "warn",
                    f"headline {len(headline)}자 (권장 상한 {hard}자) — 레이아웃 확인 필요",
                    where="slide 1",
                )
            elif len(headline) > soft:
                report.add(
                    "headline_too_long",
                    "info",
                    f"headline {len(headline)}자 (여유 기준 {soft}자)",
                    where="slide 1",
                )

        report.checked.append("sources")
        sources = [
            source
            for source in carousel.sources
            if source.name
            and source.source_relation != "mirror"
            and source.supports_event
        ]
        sources = list(
            {
                canonicalize_url(source.url) or source.name.lower(): source
                for source in sources
            }.values()
        )
        if not sources:
            report.add("missing_source", "block", "closing 슬라이드에 출처가 없습니다")
        elif len(sources) < int(
            self.channel.get("content.fact_check.warn_if_sources_below", 2)
        ):
            report.add(
                "single_source",
                "warn",
                f"출처가 {len(sources)}개입니다 — 교차 확인 권장",
                where="slide %d" % (carousel.closing.index if carousel.closing else 0),
            )

        report.checked.append("caption")
        if not carousel.caption.strip():
            report.add("missing_caption", "block", "caption 이 없습니다")
        else:
            caption_cfg = self.channel.get("instagram.caption", {}) or {}
            max_chars = int(caption_cfg.get("max_chars", 2200))
            if len(carousel.caption) > max_chars:
                report.add(
                    "caption_too_long",
                    "block",
                    f"caption {len(carousel.caption)}자 > Instagram 제한 {max_chars}자",
                )
            elif len(carousel.caption) > int(caption_cfg.get("target_chars", 900)) * 1.5:
                report.add(
                    "caption_too_long",
                    "warn",
                    f"caption {len(carousel.caption)}자 — 목표보다 김",
                )
            if not self.channel.get("instagram.caption.include_urls_in_body", False):
                if _URL_IN_TEXT.search(carousel.caption):
                    report.add(
                        "url_in_caption_body",
                        "warn",
                        "caption 본문에 URL 이 있습니다 (URL 은 metadata 로만 유지)",
                    )

        report.checked.append("hashtags")
        hashtag_cfg = self.channel.get("instagram.caption.hashtags", {}) or {}
        low, high = int(hashtag_cfg.get("min", 3)), int(hashtag_cfg.get("max", 6))
        if not low <= len(carousel.hashtags) <= high:
            report.add(
                "hashtag_count",
                "warn",
                f"해시태그 {len(carousel.hashtags)}개 (권장 {low}~{high}개)",
            )

    # ------------------------------------------------------------ 텍스트 밀도
    def _check_density(self, carousel: Carousel, report: ValidationReport) -> None:
        report.checked.append("text_density")
        for slide in carousel.slides:
            budget = self.channel.text_budget(str(slide.type))
            if not budget:
                continue
            where = f"slide {slide.index}"

            if slide.type is SlideType.COVER:
                if len(slide.eyebrow) > int(budget.get("eyebrow_max_chars", 34)):
                    report.add(
                        "slide_too_dense", "warn", f"eyebrow {len(slide.eyebrow)}자", where
                    )
                if len(slide.subtitle) > int(budget.get("subtitle_max_chars", 110)):
                    report.add(
                        "slide_too_dense",
                        "warn",
                        f"subtitle {len(slide.subtitle)}자 (상한 "
                        f"{budget.get('subtitle_max_chars', 110)}자)",
                        where,
                    )
                continue

            if len(slide.title) > int(budget.get("title_max_chars", 46)):
                report.add(
                    "slide_too_dense",
                    "warn",
                    f"title {len(slide.title)}자 (상한 {budget.get('title_max_chars', 46)}자)",
                    where,
                )
            items = len(slide.body)
            low = int(budget.get("body_min_items", 1))
            high = int(budget.get("body_max_items", 5))
            if items < low:
                report.add(
                    "slide_too_sparse", "warn", f"본문 {items}개 (최소 {low}개)", where
                )
            elif items > high:
                report.add(
                    "slide_too_dense", "warn", f"본문 {items}개 (최대 {high}개)", where
                )
            total = sum(len(line) for line in slide.body)
            if total > int(budget.get("body_total_max_chars", 380)):
                report.add(
                    "slide_too_dense",
                    "warn",
                    f"본문 합계 {total}자 (상한 "
                    f"{budget.get('body_total_max_chars', 380)}자)",
                    where,
                )
            floor = int(budget.get("body_total_min_chars", 0))
            if floor and total < floor and slide.type is SlideType.CONTENT:
                # news_short 는 재료 부족이 그대로 빈 화면이 되므로 BLOCK 입니다.
                # (실측: 3장 캐러셀의 content slide 가 48자였음)
                short_templates = {
                    name
                    for name, spec in self.channel.templates.items()
                    if spec.slides <= 3
                }
                severity = "block" if carousel.template in short_templates else "warn"
                report.add(
                    "news_short_density_below_minimum"
                    if severity == "block"
                    else "slide_too_sparse",
                    severity,
                    f"본문 합계 {total}자 (하한 {floor}자) — 화면이 비어 보입니다. "
                    "문장을 늘리지 말고 재료가 더 있는 토픽을 쓰거나 템플릿을 바꾸세요",
                    where,
                )
            for line in slide.body:
                if len(line) > int(budget.get("body_item_max_chars", 92)):
                    report.add(
                        "slide_too_dense",
                        "warn",
                        f"문장 {len(line)}자: {truncate(line, 40)}",
                        where,
                    )

            # closing variant A 의 두 번째 블록
            if slide.extra_body:
                if len(slide.extra_title) > int(
                    budget.get("secondary_title_max_chars", 34)
                ):
                    report.add(
                        "slide_too_dense",
                        "warn",
                        f"두 번째 라벨 {len(slide.extra_title)}자",
                        where,
                    )
                if len(slide.extra_body) > int(budget.get("secondary_body_max_items", 2)):
                    report.add(
                        "slide_too_dense",
                        "warn",
                        f"두 번째 블록 {len(slide.extra_body)}개 항목 (최대 "
                        f"{budget.get('secondary_body_max_items', 2)}개)",
                        where,
                    )
                for line in slide.extra_body:
                    if len(line) > int(budget.get("secondary_body_item_max_chars", 92)):
                        report.add(
                            "slide_too_dense",
                            "warn",
                            f"두 번째 블록 문장 {len(line)}자: {truncate(line, 40)}",
                            where,
                        )

    def _check_closing_fill(self, carousel: Carousel, report: ValidationReport) -> None:
        """closing slide 가 시각적으로 비어 보이는지 점검.

        문장을 억지로 늘리는 것은 금지이므로, 부족하면 경고만 남깁니다.
        """
        report.checked.append("closing_fill")
        closing = carousel.closing
        if closing is None:
            return
        spec = self.channel.closing_spec()
        lines = len(closing.body) + len(closing.extra_body)
        if lines <= 1:
            report.add(
                "closing_sparse",
                "warn",
                f"closing 본문이 {lines}줄입니다 (variant {spec['variant']}) — "
                "하단 여백이 커집니다. 재료가 있으면 한 줄 추가를 검토하세요",
                where=f"slide {closing.index}",
            )
        if self.channel.closing_uses_secondary and not closing.extra_body:
            report.add(
                "closing_secondary_empty",
                "info",
                f"'{spec['secondary_label']}' 블록이 비어 있습니다 "
                "(재료가 없으면 정상 — 억지로 채우지 않음)",
                where=f"slide {closing.index}",
            )

    def _check_density_recovery(
        self, carousel: Carousel, report: ValidationReport
    ) -> None:
        """중복 수리 후 밀도 회복 결과를 게이트에 반영합니다."""
        report.checked.append("density_recovery")
        recovery = carousel.meta.get("density_recovery") or {}
        if not recovery or not recovery.get("attempted"):
            return

        downgrade = recovery.get("template_downgraded")
        if downgrade:
            report.add(
                "template_downgraded",
                "info",
                f"유일 검증 재료 수준에 맞춰 {downgrade.get('from')} → "
                f"{downgrade.get('to')} 전체 재생성",
            )

        unresolved = [int(index) for index in (recovery.get("thin_after") or [])]
        if unresolved:
            where = ", ".join(f"slide {index}" for index in unresolved)
            report.add(
                "thin_slide_after_repair",
                "block",
                "중복 수리 후 본문 밀도 하한을 회복하지 못했습니다",
                where=where,
            )
            report.add(
                "insufficient_unique_material",
                "block",
                "더 짧은 템플릿을 안전하게 채울 유일 검증 재료도 부족합니다",
                where=where,
            )
        else:
            report.add(
                "density_recovered",
                "info",
                "미사용 검증 재료 또는 템플릿 재생성으로 밀도 회복",
            )

    def _check_recovery_failures(
        self, carousel: Carousel, report: ValidationReport
    ) -> None:
        """LLM 복구 실패는 더 약한 콘텐츠로 자동 통과시키지 않습니다."""
        report.checked.append("recovery_failures")
        failures = carousel.meta.get("recovery_failures") or []
        for failure in failures:
            if not isinstance(failure, dict):
                continue
            reason = str(failure.get("reason") or "llm_recovery_failed")
            report.add(
                "llm_recovery_failed",
                "block",
                "LLM 콘텐츠 복구가 완료되지 않아 사람 검토가 필요합니다",
            )
            report.add(
                reason,
                "block",
                f"콘텐츠 복구 실패: {reason} — 사람 검토 필요",
            )
    def _check_grounding(self, carousel: Carousel, report: ValidationReport) -> None:
        """문장별 근거 판정을 게이트에 반영합니다.

        검증하지 못한 콘텐츠는 통과가 아니라 BLOCK 입니다. 근거 검사 기록이 아예 없는
        과거 산출물(기능 도입 전)만 정보로 남깁니다.
        """
        report.checked.append("grounding")
        raw = carousel.meta.get("grounding")
        if not isinstance(raw, dict):
            if bool(self.channel.get("content.grounding.enabled", True)):
                report.add(
                    "grounding_not_recorded",
                    "info",
                    "근거 검사 기록이 없습니다 (근거 추적 도입 전 산출물이거나 비활성)",
                )
            return

        result = GroundingResult.from_dict(raw)
        if result.status != STATUS_CHECKED:
            report.add(
                "grounding_unchecked",
                "block",
                f"문장별 근거를 검증하지 못했습니다: {result.reason or '원인 불명'}",
            )
            return

        for item in result.verdicts:
            where = f"slide {item.slide}"
            quoted = truncate(item.text, 80)
            detail = f" — {item.note}" if item.note else ""
            if item.verdict == UNSUPPORTED:
                report.add(
                    "unsupported_claim",
                    "block",
                    f"근거 없는 주장: \"{quoted}\"{detail}",
                    where=where,
                )
            elif item.verdict == PARTIAL:
                report.add(
                    "partial_claim_support",
                    "warn",
                    f"근거가 일부만 있는 주장: \"{quoted}\"{detail}",
                    where=where,
                )
            elif item.verdict != SUPPORTED:
                report.add(
                    "grounding_unchecked",
                    "block",
                    f"문장 근거를 판정하지 못했습니다: \"{quoted}\"{detail}",
                    where=where,
                )

        # 심사 뒤에 문구가 바뀌었다면(사람 수정·재렌더) 판정은 낡은 것입니다.
        checked_texts = {item.text for item in result.verdicts}
        edited = [c for c in collect_claims(carousel) if c.text not in checked_texts]
        for claim in edited:
            report.add(
                "grounding_stale",
                "warn",
                f"근거 심사 이후 바뀐 문장: \"{truncate(claim.text, 80)}\" — 원문과 직접 대조하세요",
                where=f"slide {claim.slide}",
            )

        if result.verdicts and all(item.verdict == SUPPORTED for item in result.verdicts):
            report.add(
                "grounding_verified",
                "info",
                f"{len(result.verdicts)}개 문장 모두 근거 확인",
            )

    # ------------------------------------------------------- 내부 표현 / 톤
    def _check_title_plain(self, carousel: Carousel, report: ValidationReport) -> None:
        """제목은 직관적이어야 한다(내용은 전문적이어도 된다): 영어 전문용어·긴 제목을 경고한다.

        차단하지 않고 경고만 한다(사람이 review.md 에서 보고 고친다). 기준은 content.yaml → title_style.
        """
        for slide in carousel.slides:
            if slide.type is SlideType.COVER or not slide.title:
                continue
            for code, message in title_problems(slide.title, self.channel):
                note = (
                    " — 제목은 쉬운 한국어로, 용어는 본문에 두세요" if code == JARGON else ""
                )
                report.add(
                    code,
                    "warn",
                    f"slide {slide.index} 제목에 {message}{note}"
                    if code == JARGON
                    else f"slide {slide.index} {message}",
                    where=f"slide {slide.index}",
                )

    def _check_caption_plain(self, carousel: Carousel, report: ValidationReport) -> None:
        """캡션도 쉬운 말로: 코드 식별자·영어 전문용어·영어 과다를 경고한다(차단 아님)."""
        names = known_names(
            carousel.headline,
            carousel.cover.headline if carousel.cover else "",
            carousel.cover.subtitle if carousel.cover else "",
            " ".join(source.name for source in carousel.sources),
        )
        for problem in caption_problems(carousel.caption, self.channel, names):
            report.add("caption_jargon", "warn", f"캡션: {problem}", where="caption")

    def _check_visuals(self, carousel: Carousel, report: ValidationReport) -> None:
        """차트 값은 반드시 슬라이드 문구에 있어야 한다(없으면 근거 없는 숫자이므로 BLOCK)."""
        for slide in carousel.slides:
            for value in ungrounded_values(slide):
                report.add(
                    "visual_ungrounded",
                    "block",
                    f"slide {slide.index} 차트 값 {value!r} 이(가) 슬라이드 문구에 없습니다",
                    where=f"slide {slide.index}",
                )

    def _check_freshness(
        self, carousel: Carousel, report: ValidationReport, today: date | None
    ) -> None:
        if not self.channel.get("content.freshness.enabled", True):
            return
        report.checked.append("freshness")
        for item in find_stale_dates(carousel, today=today):
            report.add(
                "stale_deadline",
                "block",
                f"이미 {item.days_ago}일 지난 날짜({item.day.isoformat()})를 앞으로 올 일처럼 "
                f"서술합니다(표현: `{item.cue}`): {truncate(item.text, 90)}",
                where=f"slide {item.slide}",
            )

    def _check_language(self, carousel: Carousel, report: ValidationReport) -> None:
        report.checked.append("internal_language")
        public_text = carousel.all_text()
        lowered = public_text.lower()
        for marker in self.channel.internal_markers:
            if marker.lower() in lowered:
                report.add(
                    "internal_language_leak",
                    "block",
                    f"공개 텍스트에 내부 표현이 있습니다: '{marker}' "
                    "(review.md 에만 기록되어야 함)",
                )

        report.checked.append("banned_phrases")
        for phrase in self.channel.banned_phrases:
            if phrase and phrase in public_text:
                report.add("banned_phrase", "warn", f"과장 표현: '{phrase}'")

        report.checked.append("slide_urls")
        if not self.channel.get("instagram.sources.show_urls_on_slide", False):
            for slide in carousel.slides:
                for part in slide.text_parts:
                    if _URL_IN_TEXT.search(part):
                        report.add(
                            "url_on_slide",
                            "warn",
                            f"슬라이드 텍스트에 URL 이 있습니다: {truncate(part, 50)}",
                            where=f"slide {slide.index}",
                        )

    # ------------------------------------------------------------ 사실 / 렌더
    def _check_facts(
        self, carousel: Carousel, topic: VerifiedTopic | None, report: ValidationReport
    ) -> None:
        report.checked.append("fact_check")
        verdict = (
            topic.verdict if topic else str(carousel.meta.get("fact_check_verdict") or "")
        ).lower()
        allowed = self.channel.allowed_verdicts
        if verdict and verdict not in allowed:
            report.add(
                "unresolved_critical_fact",
                "block",
                f"fact check verdict `{verdict}` 는 게시 대상이 아닙니다 (허용 {sorted(allowed)})",
            )
        warn_verdicts = {
            str(v).lower() for v in (self.channel.get("content.fact_check.warn_verdicts") or [])
        }
        if verdict in warn_verdicts:
            report.add(
                "partially_verified",
                "warn",
                f"verdict={verdict} — 게시 전 원문과 수치/범위를 대조하세요",
            )
            partial_reasons = (
                topic.fact_check.get("partial_reasons")
                if topic is not None
                else carousel.meta.get("partial_reasons")
            ) or []
            for reason in partial_reasons:
                report.add(
                    f"partial_{reason}",
                    "info",
                    f"partially_verified 원인: {reason}",
                )

        unverified = list(carousel.meta.get("unverified_claims") or [])
        if topic is not None:
            unverified = topic.unverified_claims or unverified
        for claim in unverified[:5]:
            report.add(
                "unverified_claim_reference",
                "info",
                f"미검증 항목(슬라이드에 사용 금지): {truncate(claim, 110)}",
            )

    def _check_render(
        self, carousel: Carousel, render: RenderResult | None, report: ValidationReport
    ) -> None:
        report.checked.append("render")
        if render is None:
            report.add("missing_slide_image", "block", "렌더링이 실행되지 않았습니다")
            return
        if render.errors:
            for error in render.errors:
                report.add("renderer_failure", "block", f"렌더 실패: {error}")
        if len(render.slides) != carousel.slide_count:
            report.add(
                "missing_slide_image",
                "block",
                f"PNG {len(render.slides)}장 / 슬라이드 {carousel.slide_count}장",
            )
        for slide_render in render.slides:
            if not Path(slide_render.png_path).exists():
                report.add(
                    "missing_slide_image",
                    "block",
                    f"PNG 파일이 없습니다: {slide_render.png_path.name}",
                    where=f"slide {slide_render.index}",
                )
            if slide_render.overflow:
                report.add(
                    "text_overflow",
                    "block",
                    f"텍스트가 영역을 넘칩니다 (fit={slide_render.fit}, "
                    f"{slide_render.metrics})",
                    where=f"slide {slide_render.index}",
                )
            elif slide_render.fit_scale < 1.0:
                report.add(
                    "autofit_applied",
                    "info",
                    f"폰트 {slide_render.fit_scale:g}배로 축소되어 맞춰졌습니다 "
                    "— 문구를 줄이면 더 읽기 쉬워집니다",
                    where=f"slide {slide_render.index}",
                )
        self._check_uniform(carousel, render, report)

    def _check_uniform(
        self, carousel: Carousel, render: RenderResult, report: ValidationReport
    ) -> None:
        """카드뉴스는 균일해야 한다: content·closing 슬라이드는 제목·푸터 y 와 글자 배율이 같다.

        렌더가 실측한 값으로 비교하므로 CSS 가 바뀌어 틀이 흔들리면 여기서 잡힌다.
        """
        tolerance = int(self.channel.get("brand.layout.uniform_tolerance_px", 2))
        kinds = {slide.index: slide.type for slide in carousel.slides}
        body = [
            slide_render
            for slide_render in render.slides
            if kinds.get(slide_render.index) is not SlideType.COVER and not slide_render.overflow
        ]
        for label, position in (("제목", 0), ("푸터", 1)):
            values = [r.geom[position] for r in body if r.geom is not None]
            if len(values) >= 2 and max(values) - min(values) > tolerance:
                report.add(
                    "layout_nonuniform",
                    "warn",
                    f"슬라이드마다 {label} 위치(y)가 다릅니다: {sorted(set(values))} "
                    f"(허용 ±{tolerance}px) — 틀이 흔들려 보입니다",
                )
        if bool(self.channel.get("brand.autofit.uniform", True)):
            scales = {round(r.fit_scale, 2) for r in body}
            if len(scales) > 1:
                report.add(
                    "font_scale_mixed",
                    "warn",
                    f"슬라이드마다 글자 배율이 다릅니다: {sorted(scales)} — 크기가 들쑥날쑥해 보입니다",
                )

    # ------------------------------------------------ semantic duplicate / tone
    def _check_duplicates(self, carousel: Carousel, report: ValidationReport) -> None:
        """슬라이드 간 같은 사실 반복 검사 (bullet 단위)."""
        report.checked.append("semantic_duplicate")
        cfg = self.channel.get("content.duplicate", {}) or {}
        warn = float(cfg.get("warn_threshold", 0.62))
        block = float(cfg.get("block_threshold", 0.75))
        cover_warn = float(cfg.get("cover_warn_threshold", 0.72))
        cover_block = float(cfg.get("cover_block_threshold", 0.85))

        duplicates = find_duplicates(carousel, warn=min(warn, cover_warn), block=block)
        existing = {
            (pair.slide_a, pair.slide_b, pair.text_a, pair.text_b)
            for pair in duplicates.pairs
        }
        for pair in cover_body_duplicates(carousel, self.channel):
            key = (pair.slide_a, pair.slide_b, pair.text_a, pair.text_b)
            if key not in existing:
                duplicates.pairs.append(pair)
                existing.add(key)
        duplicates.pairs.sort(key=lambda pair: pair.score, reverse=True)
        cover_index = carousel.cover.index if carousel.cover else 1

        kept = []
        for pair in duplicates.pairs:
            involves_cover = cover_index in (pair.slide_a, pair.slide_b)
            # cover 는 예고 역할이라 기준을 높입니다.
            warn_at = cover_warn if involves_cover else warn
            block_at = cover_block if involves_cover else block
            if pair.score < warn_at:
                continue
            pair.severity = "block" if pair.score >= block_at else "warn"
            kept.append(pair)
            report.add(
                "cover_body_semantic_duplicate"
                if involves_cover
                else "semantic_duplicate",
                pair.severity,
                f"같은 사실 반복 (유사도 {pair.score:.2f}): "
                f"slide {pair.slide_a} \"{truncate(pair.text_a, 40)}\" ↔ "
                f"slide {pair.slide_b} \"{truncate(pair.text_b, 40)}\"",
                where=f"slide {pair.slide_a}/{pair.slide_b}",
            )
        duplicates.pairs = kept
        report.duplicates = duplicates

    def _check_tone(self, carousel: Carousel, report: ValidationReport) -> None:
        """문체 통일 검사 — 카테고리와 무관하게 개조식/명사형."""
        report.checked.append("tone")
        tone = analyze_tone(carousel, self.channel)
        report.tone = tone
        min_ratio = float(self.channel.get("content.tone.min_nominal_ratio", 0.7))

        if tone.polite:
            examples = "; ".join(truncate(line, 34) for line in tone.polite_lines[:2])
            report.add(
                "tone_inconsistency",
                "warn",
                f"경어체 종결 {tone.polite}줄 — 개조식/명사형으로 통일 필요: {examples}",
            )
        if tone.total and tone.nominal_ratio < min_ratio:
            report.add(
                "tone_inconsistency",
                "warn",
                f"명사형 비율 {tone.nominal_ratio:.2f} < 기준 {min_ratio:.2f} "
                f"(명사형 {tone.nominal} / 평서 {tone.plain} / 경어 {tone.polite})",
            )

    def _check_density_variance(self, carousel: Carousel, report: ValidationReport) -> None:
        report.checked.append("density_variance")
        totals = [
            sum(len(line) for line in [*slide.body, *slide.extra_body])
            for slide in carousel.slides
            if slide.type is not SlideType.COVER
        ]
        if len(totals) < 2 or min(totals) == 0:
            return
        limit = float(self.channel.get("content.density_variance_warn", 3.0))
        spread = max(totals) / max(min(totals), 1)
        if spread > limit:
            report.add(
                "slide_density_variance",
                "warn",
                f"슬라이드 밀도 편차 {spread:.1f}배 (최소 {min(totals)}자 / 최대 {max(totals)}자) "
                f"— 읽는 리듬이 끊깁니다",
            )

    # --------------------------------------------------------------------- API
    def validate(
        self,
        carousel: Carousel,
        *,
        topic: VerifiedTopic | None = None,
        render: RenderResult | None = None,
        today: date | None = None,
    ) -> ValidationReport:
        report = ValidationReport()
        self._check_structure(carousel, report)
        self._check_content(carousel, report)
        self._check_density(carousel, report)
        self._check_density_recovery(carousel, report)
        self._check_recovery_failures(carousel, report)
        self._check_closing_fill(carousel, report)
        self._check_duplicates(carousel, report)
        self._check_tone(carousel, report)
        self._check_density_variance(carousel, report)
        self._check_language(carousel, report)
        self._check_facts(carousel, topic, report)
        self._check_grounding(carousel, report)
        self._check_visuals(carousel, report)
        self._check_title_plain(carousel, report)
        self._check_caption_plain(carousel, report)
        self._check_freshness(carousel, report, today)
        self._check_render(carousel, render, report)

        report.quality = compute_quality_score(
            carousel,
            channel=self.channel,
            topic=topic,
            duplicates=report.duplicates,
            tone=report.tone,
            render=render,
        )
        if not report.quality.passes:
            report.add(
                "quality_below_threshold",
                "warn",
                f"content_quality_score {report.quality.score:.2f} < 기준 "
                f"{report.quality.threshold:.2f} → 게시 보류 "
                f"({'; '.join(report.quality.notes) or '성분 확인'})",
            )

        logger.info(
            "[validate] publishable=%s score=%.2f (block %d / warn %d / info %d)",
            report.publishable,
            report.quality.score,
            len(report.blocks),
            len(report.warnings),
            len(report.infos),
        )
        return report
