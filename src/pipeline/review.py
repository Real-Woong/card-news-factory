"""Human-review checklist generation.

The pipeline never publishes. Its final product is ``review.md``: a short list of
things a human must look at, with an explicit recommendation. Checks are
deliberately mechanical - they catch the failure modes an LLM writer has
(unsupported numbers, invented links, hype, repeated topics) rather than judging
editorial quality.
"""

from __future__ import annotations

import re

from src.core.logging_setup import get_logger
from src.core.models import ReviewReport, Verdict
from src.core.text import domain_of, truncate

logger = get_logger(__name__)

HYPE_TERMS = [
    "혁명적",
    "게임체인저",
    "역사상",
    "충격",
    "미쳤",
    "대박",
    "필수",
    "revolutionary",
    "game-changing",
    "game changer",
    "insane",
    "must-have",
    "unprecedented",
]

_URL = re.compile(r"https?://[^\s)\]}>\"']+")
_NUMBER = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:%|x|ms|s|GB|MB|TB|tok/s|배|만|억)?\b")


def build_review(ctx) -> ReviewReport:  # noqa: ANN001 - avoids a circular import
    """Assemble the review report for one run."""
    report = ReviewReport(run_id=ctx.run.run_id)
    items = ctx.selected_items()
    publishable = ctx.publishable_items()

    # ---------------------------------------------------------------- verdicts
    for article, note, check in items:
        reference = article.url
        if check.verdict == Verdict.CONTRADICTED:
            report.add(
                "unverified_claims",
                f"출처와 모순되는 내용: {truncate(article.title, 70)}",
                severity="block",
                reference=reference,
            )
        elif check.verdict == Verdict.UNVERIFIED:
            report.add(
                "unverified_claims",
                f"검증되지 않음 (초안에서 제외됨): {truncate(article.title, 70)}",
                severity="warn",
                reference=reference,
            )
        for claim in check.unverified_claims[:5]:
            report.add(
                "unverified_claims",
                f"사실 확인 필요: {truncate(claim, 140)}",
                severity="warn",
                reference=reference,
            )
        for correction in check.corrections[:5]:
            report.add(
                "numbers_and_dates",
                f"수정 제안: {truncate(correction, 140)}",
                severity="warn",
                reference=reference,
            )
        for reason in check.partial_reasons:
            report.add(
                "unverified_claims",
                f"부분 검증 원인: {reason}",
                severity="info",
                reference=reference,
            )
        if not check.primary_source_found:
            report.add(
                "missing_sources",
                f"1차 출처 미확인: {truncate(article.title, 70)}",
                severity="block",
                reference=reference,
            )
        for question in note.open_questions[:3]:
            report.add(
                "unverified_claims",
                f"원문이 답하지 않은 항목: {truncate(question, 140)}",
                severity="info",
                reference=reference,
            )

    # ----------------------------------------------------------- duplicate risk
    for skip in getattr(ctx, "overlap_skipped", []):
        detail = skip.to_dict()
        report.add(
            "duplicate_risk",
            f"같은 사안으로 판단해 제외: {truncate(str(detail['skipped_title']), 60)} "
            f"(선택됨: {truncate(str(detail['kept_title']), 50)}, "
            f"{detail['kind']} {detail['similarity']})",
            severity="warn",
            reference=str(detail["skipped_url"]),
        )

    if ctx.dedup is not None:
        for warning in ctx.dedup.warnings:
            report.add(
                "duplicate_risk",
                f"과거 콘텐츠와 유사: {truncate(warning.article.title, 60)} "
                f"({'; '.join(warning.notes)})",
                severity="warn",
                reference=warning.article.url,
            )
        for duplicate in ctx.dedup.duplicates[:10]:
            report.add(
                "duplicate_risk",
                f"중복으로 제외됨: {truncate(duplicate.article.title, 60)} "
                f"[{duplicate.reason} {duplicate.score:.2f}]",
                severity="info",
                reference=duplicate.article.url,
            )

    # ------------------------------------------------------------------ drafts
    allowed_urls = {article.url for article, _n, _r in publishable} | {
        str(source.get("url") or "")
        for _a, note, _r in publishable
        for source in note.sources
    }
    # A link to a domain we cited is "extra detail quoted from the source";
    # anything else is a reference the writer produced on its own.
    allowed_domains = {domain_of(url) for url in allowed_urls if url}
    allowed_domains.discard("")

    for draft in ctx.drafts:
        if draft.over_limit:
            report.add(
                "tone_and_hype",
                f"{draft.channel_id}: 글자 수 초과 ({draft.char_count}자)",
                severity="block",
                reference=draft.channel_id,
            )
        lowered = draft.body.lower()
        for term in HYPE_TERMS:
            if term.lower() in lowered:
                report.add(
                    "tone_and_hype",
                    f"{draft.channel_id}: 과장 표현 확인 필요 - '{term}'",
                    severity="warn",
                    reference=draft.channel_id,
                )
        for url in sorted({m.group(0).rstrip(".,)…") for m in _URL.finditer(draft.body)}):
            if url in allowed_urls:
                continue
            domain = domain_of(url)
            if domain and domain in allowed_domains:
                # Quoted from one of our sources (release notes often link out):
                # worth a look, but not an invented reference.
                report.add(
                    "links_resolve",
                    f"{draft.channel_id}: 원문에서 가져온 부가 링크 확인 - {url}",
                    severity="warn",
                    reference=url,
                )
            else:
                report.add(
                    "links_resolve",
                    f"{draft.channel_id}: 선별 항목·출처 도메인에 없는 링크 - {url}",
                    severity="block",
                    reference=url,
                )
        if not draft.sources:
            report.add(
                "missing_sources",
                f"{draft.channel_id}: 초안에 출처가 없음",
                severity="block",
                reference=draft.channel_id,
            )
        numbers = {m.group(0).strip() for m in _NUMBER.finditer(draft.body)}
        if numbers:
            report.add(
                "numbers_and_dates",
                f"{draft.channel_id}: 숫자 재확인 - {', '.join(sorted(numbers)[:8])}",
                severity="info",
                reference=draft.channel_id,
            )

    if not ctx.drafts:
        report.add(
            "missing_sources", "생성된 초안이 없음", severity="block", reference=ctx.run.run_id
        )

    # LLM 실패로 제외된 항목은 숨기지 않고 노출합니다.
    for key, reason in getattr(ctx, "failed_items", {}).items():
        title = next(
            (i.article.title for i in ctx.selected if i.article.key == key),
            key,
        )
        report.add(
            "pipeline",
            f"LLM 응답 실패로 제외됨: {truncate(str(title), 60)} — {truncate(reason, 160)}",
            severity="warn",
            reference=key,
        )

    for warning in ctx.warnings[:20]:
        report.add("pipeline", warning, severity="info")

    # -------------------------------------------------------------- confidence
    if items:
        verified = sum(1 for _a, _n, r in items if r.verdict == Verdict.VERIFIED)
        partially = sum(1 for _a, _n, r in items if r.verdict == Verdict.PARTIALLY_VERIFIED)
        base = (verified + 0.6 * partially) / len(items)
    else:
        base = 0.0
    penalty = 0.15 * len([i for i in report.items if i.severity == "warn"])
    report.confidence = max(0.0, min(1.0, base - penalty))

    threshold = float(ctx.config.get("review.auto_recommend_threshold", 0.8))
    report.recommend_publish = (
        bool(ctx.drafts) and not report.blockers and report.confidence >= threshold
    )
    report.summary = (
        f"{len(publishable)}/{len(items)} 항목이 초안에 사용됨, "
        f"차단 {len(report.blockers)}건, 확인 필요 "
        f"{len([i for i in report.items if i.severity == 'warn'])}건, "
        f"신뢰도 {report.confidence:.2f} (기준 {threshold:.2f})"
    )

    logger.info(
        "[review] %d item(s), %d blocker(s), recommend_publish=%s",
        len(report.items),
        len(report.blockers),
        report.recommend_publish,
    )
    return report
