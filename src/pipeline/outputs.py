"""Output writers.

One run produces::

    outputs/drafts/YYYY-MM-DD/
    ├── research.json          # what the agents found, with RAG context
    ├── selected_topics.json   # ranking decisions (scores + components)
    ├── sources.md             # every source used, grouped by trust tier
    ├── post_longform.md       # long-form channel drafts
    ├── post_shortform.md      # short-form channel drafts (+ thread parts)
    ├── review.md              # the human checklist
    └── run_metadata.json      # mode, providers, counts, timings

Which files are written is controlled by ``channels.yaml -> outputs``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.core.errors import StorageError
from src.core.logging_setup import get_logger
from src.core.models import Draft, ReviewReport
from src.core.text import to_iso, truncate, utcnow

logger = get_logger(__name__)

SEVERITY_ICON = {"block": "[차단]", "warn": "[확인]", "info": "[참고]"}
CHECKLIST_TITLES = {
    "unverified_claims": "사실 확인이 필요한 문장",
    "missing_sources": "출처",
    "duplicate_risk": "중복 가능성",
    "tone_and_hype": "표현/톤",
    "numbers_and_dates": "숫자와 날짜",
    "links_resolve": "링크",
    "pipeline": "파이프라인 경고",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _display_path(ctx, path: Path) -> str:  # noqa: ANN001
    """Project-relative path for logs; absolute when the target is elsewhere
    (``--output-dir /tmp/...`` or a test temp directory)."""
    try:
        return str(path.relative_to(ctx.config.paths.root))
    except ValueError:
        return str(path)


def _write(ctx, name: str, content: str) -> Path:  # noqa: ANN001 - circular import
    path = ctx.output_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise StorageError(f"Cannot write {path}: {exc}") from exc
    ctx.written_files.append(path)
    logger.info("[output] %s (%d bytes)", _display_path(ctx, path), len(content))
    return path


def _write_json(ctx, name: str, payload: dict[str, Any]) -> Path:  # noqa: ANN001
    return _write(ctx, name, json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _enabled(ctx, key: str) -> bool:  # noqa: ANN001
    outputs = (ctx.config.channels_raw.get("outputs") or {})
    return bool(outputs.get(key, True))


def _draft_section(draft: Draft) -> str:
    lines = [f"## {draft.channel_id}", ""]
    if draft.title:
        lines += [f"**{draft.title}**", ""]
    lines += [draft.body.strip(), ""]
    if draft.thread_parts:
        lines += ["### Thread", ""]
        for index, part in enumerate(draft.thread_parts, 1):
            lines += [f"{index}. {part}", ""]
    if draft.hashtags:
        lines += [" ".join(draft.hashtags), ""]
    lines += [
        f"- 글자 수: {draft.char_count}" + ("  **(제한 초과)**" if draft.over_limit else ""),
    ]
    if draft.editor_changes:
        lines.append("- 에디터 수정 내역: " + "; ".join(draft.editor_changes))
    if draft.sources:
        lines.append("- 출처:")
        lines += [
            f"  - [{s.get('source')}] {s.get('title')} — {s.get('url')} "
            f"(fact check: {s.get('verdict')})"
            for s in draft.sources
        ]
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# individual files
# --------------------------------------------------------------------------- #
def write_research_json(ctx) -> None:  # noqa: ANN001
    payload = {
        "run_id": ctx.run.run_id,
        "generated_at": to_iso(utcnow()),
        "mode": ctx.mode,
        "items": [
            {
                "article": article.reference(),
                "score": next(
                    (
                        item.to_dict()
                        for item in ctx.selected
                        if item.article.key == article.key
                    ),
                    None,
                ),
                "research": note.to_dict(),
                "fact_check": report.to_dict(),
            }
            for article, note, report in ctx.selected_items()
        ],
    }
    _write_json(ctx, "research.json", payload)


def write_selected_topics_json(ctx) -> None:  # noqa: ANN001
    payload = {
        "run_id": ctx.run.run_id,
        "generated_at": to_iso(utcnow()),
        "selection_rules": {
            "max_items": ctx.config.ranking.max_items,
            "max_per_source": ctx.config.ranking.max_per_source,
            "category_quota": ctx.config.ranking.category_quota,
            "min_score": ctx.config.ranking.min_score,
            "weights": ctx.config.ranking.weights,
        },
        "clusters": {
            key: cluster.to_dict()
            for key, cluster in (getattr(ctx, "clusters", {}) or {}).items()
            if getattr(cluster, "size", 1) > 1
        },
        "selected": [item.to_dict() for item in ctx.selected],
        "considered": [item.to_dict() for item in ctx.scored[:30]],
        "skipped_same_story": [skip.to_dict() for skip in getattr(ctx, "overlap_skipped", [])],
        "dropped_duplicates": [
            result.to_dict() for result in (ctx.dedup.duplicates if ctx.dedup else [])
        ],
    }
    _write_json(ctx, "selected_topics.json", payload)


def write_sources_md(ctx) -> None:  # noqa: ANN001
    items = ctx.selected_items()
    by_tier: dict[str, list[str]] = {}
    for article, _note, report in items:
        line = (
            f"- [{article.source_name}] [{article.title}]({article.url})  \n"
            f"  발행: {to_iso(article.published_at) or 'unknown'} · "
            f"카테고리: {article.category} · fact check: {report.verdict} "
            f"({report.confidence:.2f})"
        )
        by_tier.setdefault(article.trust_tier, []).append(line)

    lines = [
        f"# Sources — {ctx.run_date.isoformat()}",
        "",
        f"run_id: `{ctx.run.run_id}` · mode: `{ctx.mode}`",
        "",
        "1차 출처(official/github/paper)를 우선하며, 2차 보도만을 근거로 한 항목은 "
        "초안에 사용하지 않습니다.",
        "",
    ]
    for tier in ("official", "github", "paper", "major_news", "community", "social"):
        if tier not in by_tier:
            continue
        lines += [f"## {tier} (trust {ctx.config.ranking.trust_value(tier)})", ""]
        lines += by_tier[tier]
        lines.append("")

    if ctx.collector_results:
        lines += ["## 수집 상태", ""]
        for result in ctx.collector_results:
            status = "ok" if result.ok else f"failed: {result.error}"
            lines.append(
                f"- `{result.source_id}`: {result.count} item(s), "
                f"{result.duration_seconds:.1f}s — {status}"
            )
        lines.append("")

    _write(ctx, "sources.md", "\n".join(lines))


def _write_posts(ctx, fmt: str, filename: str) -> None:  # noqa: ANN001
    drafts = [draft for draft in ctx.drafts if draft.format == fmt]
    header = [
        f"# {'Long-form' if fmt == 'longform' else 'Short-form'} drafts — "
        f"{ctx.run_date.isoformat()}",
        "",
        "> 자동 생성 초안입니다. 사람이 검수하기 전에는 게시하지 마세요. "
        "`review.md`를 먼저 확인하세요.",
        "",
    ]
    if not drafts:
        header += ["_이 실행에서는 해당 포맷의 초안이 생성되지 않았습니다._", ""]
    body = header + [_draft_section(draft) for draft in drafts]
    _write(ctx, filename, "\n".join(body))


def write_review(ctx) -> None:  # noqa: ANN001
    report: ReviewReport | None = ctx.review
    if report is None or not _enabled(ctx, "review_md"):
        return

    grouped: dict[str, list] = {}
    for item in report.items:
        grouped.setdefault(item.kind, []).append(item)

    recommendation = "게시 추천" if report.recommend_publish else "게시 보류 (수정 필요)"
    lines = [
        f"# Review — {ctx.run_date.isoformat()}",
        "",
        f"- run_id: `{report.run_id}`",
        f"- 모드: `{ctx.mode}`",
        f"- 판정: **{recommendation}**",
        f"- 신뢰도: {report.confidence:.2f}",
        f"- 요약: {report.summary}",
        "",
        "## 체크리스트",
        "",
    ]

    order = list(CHECKLIST_TITLES)
    for kind in order + [k for k in grouped if k not in CHECKLIST_TITLES]:
        entries = grouped.get(kind)
        if not entries:
            continue
        lines += [f"### {CHECKLIST_TITLES.get(kind, kind)}", ""]
        for entry in entries:
            icon = SEVERITY_ICON.get(entry.severity, "[참고]")
            reference = f" — `{entry.reference}`" if entry.reference else ""
            lines.append(f"- {icon} {entry.message}{reference}")
        lines.append("")

    if not report.items:
        lines += ["- 확인이 필요한 항목이 없습니다.", ""]

    lines += [
        "## 사람이 해야 할 일",
        "",
        "1. `[차단]` 항목을 모두 해결하거나 해당 내용을 초안에서 제거",
        "2. `[확인]` 항목의 수치·날짜·버전을 원문과 대조",
        "3. `post_longform.md` / `post_shortform.md` 문장을 직접 다듬기",
        "4. 게시 후 `data/content.db` 의 `posts.status` 를 `published` 로 업데이트",
        "",
        "## 선별된 항목",
        "",
    ]
    for article, note, check in ctx.selected_items():
        lines += [
            f"### {article.title}",
            "",
            f"- 출처: [{article.source_name}]({article.url}) · {article.trust_tier}",
            f"- fact check: **{check.verdict}** ({check.confidence:.2f})",
            f"- partial reasons: {', '.join(check.partial_reasons) or '-'}",
            f"- 바뀐 점: {note.what_changed}",
            "",
        ]
        traces = [
            claim
            for claim in check.checked_claims
            if claim.matched_chunk and claim.evidence_excerpt
        ]
        if traces:
            lines += ["#### 내부 evidence trace", ""]
            for claim in traces[:8]:
                lines.append(
                    f"- `{claim.source_id}` / `{claim.matched_section or claim.matched_chunk}`: "
                    f"{truncate(claim.evidence_excerpt, 240)}"
                )
            lines.append("")

    _write(ctx, "review.md", "\n".join(lines))


def write_run_metadata(ctx) -> None:  # noqa: ANN001
    if not _enabled(ctx, "run_metadata_json"):
        return
    payload = {
        **ctx.summary(),
        "generated_at": to_iso(utcnow()),
        "llm_provider": getattr(ctx.llm, "provider", "unknown"),
        "llm_model": getattr(ctx.llm, "model", ""),
        "llm_usage": ctx.llm.usage_summary() if ctx.llm is not None else {},
        "embedder": getattr(getattr(ctx.indexer, "embedder", None), "name", "none"),
        "vector_store": getattr(getattr(ctx.indexer, "store", None), "name", "none"),
        "stages": ctx.run.stages_completed,
        "status": ctx.run.status,
        "collectors": [
            {
                "source_id": result.source_id,
                "ok": result.ok,
                "items": result.count,
                "error": result.error,
                "seconds": round(result.duration_seconds, 2),
            }
            for result in ctx.collector_results
        ],
    }
    _write_json(ctx, "run_metadata.json", payload)


# --------------------------------------------------------------------------- #
# bundle
# --------------------------------------------------------------------------- #
def write_draft_bundle(ctx) -> None:  # noqa: ANN001
    """Write every artefact except ``review.md`` / ``run_metadata.json``."""
    if _enabled(ctx, "research_json"):
        write_research_json(ctx)
    if _enabled(ctx, "selected_topics_json"):
        write_selected_topics_json(ctx)
    if _enabled(ctx, "sources_md"):
        write_sources_md(ctx)
    if _enabled(ctx, "post_longform_md"):
        _write_posts(ctx, "longform", "post_longform.md")
    if _enabled(ctx, "post_shortform_md"):
        _write_posts(ctx, "shortform", "post_shortform.md")
