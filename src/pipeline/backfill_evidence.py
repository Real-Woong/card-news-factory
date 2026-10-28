"""Explicit, restartable structured-evidence backfill for stored articles.

This maintenance command performs no ranking, LLM, channel generation, or
approval work. It re-fetches only eligible article URLs and updates the same
SQLite row in place.

    python -m src.pipeline.backfill_evidence --dry-run
    python -m src.pipeline.backfill_evidence --limit 10
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.collectors.extract import html_to_text
from src.collectors.registry import build_http_client
from src.core.config import load_config
from src.core.errors import ContentOpsError
from src.core.evidence import build_source_metadata, evidence_backfill_reasons
from src.core.models import Article
from src.core.paths import PATHS
from src.core.text import canonicalize_url, to_iso, utcnow
from src.storage.db import connect, connect_readonly, init_schema
from src.storage.repository import ArticleRepository

REPORT_DIR = PATHS.outputs / "maintenance"


@dataclass
class BackfillCandidate:
    article_id: int
    url: str
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "article_id": self.article_id,
            "url": self.url,
            "reasons": self.reasons,
        }


@dataclass
class BackfillFailure:
    article_id: int
    url: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "article_id": self.article_id,
            "url": self.url,
            "reason": self.reason,
        }


@dataclass
class EvidenceBackfillReport:
    started_at: str = field(default_factory=lambda: to_iso(utcnow()))
    completed_at: str | None = None
    dry_run: bool = False
    limit: int | None = None
    eligible: int = 0
    selected: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    eligible_articles: list[BackfillCandidate] = field(default_factory=list)
    failures: list[BackfillFailure] = field(default_factory=list)

    def complete(self) -> None:
        self.completed_at = to_iso(utcnow())

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "dry_run": self.dry_run,
            "limit": self.limit,
            "eligible": self.eligible,
            "selected": self.selected,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "failed": self.failed,
            "eligible_articles": [item.to_dict() for item in self.eligible_articles],
            "failures": [failure.to_dict() for failure in self.failures],
        }


def eligible_articles(repo: ArticleRepository) -> list[tuple[Article, list[str]]]:
    """Return only records whose evidence contract is absent or outdated."""
    eligible: list[tuple[Article, list[str]]] = []
    for article in repo.articles_for_evidence_backfill():
        reasons = evidence_backfill_reasons(article.source_metadata)
        if reasons:
            eligible.append((article, reasons))
    return eligible


def run_backfill(
    repo: ArticleRepository,
    *,
    fetch_text: Callable[[str], str] | None,
    max_content_chars: int,
    dry_run: bool = False,
    limit: int | None = None,
) -> EvidenceBackfillReport:
    """Backfill eligible rows. Each failure leaves that row untouched."""
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    candidates = eligible_articles(repo)
    selected = candidates[:limit] if limit is not None else candidates
    report = EvidenceBackfillReport(
        dry_run=dry_run,
        limit=limit,
        eligible=len(candidates),
        selected=len(selected),
        eligible_articles=[
            BackfillCandidate(article.id or 0, article.url, reasons)
            for article, reasons in candidates
        ],
    )
    if dry_run:
        report.complete()
        return report
    if fetch_text is None:
        raise ValueError("fetch_text is required unless dry_run=True")

    for article, _reasons in selected:
        article_id = article.id or 0
        try:
            # Another maintenance process may have updated the row after scanning.
            current = repo.get_article_by_id(article_id)
            if current is None or not evidence_backfill_reasons(current.source_metadata):
                report.unchanged += 1
                continue
            raw_html = fetch_text(current.url)
            plain_text = html_to_text(raw_html)
            if not plain_text.strip():
                raise ValueError("fetched source contained no extractable text")
            _body, metadata = build_source_metadata(
                raw_content=raw_html,
                plain_text=plain_text,
                canonical_url=current.canonical_url or current.url,
                title=current.title,
                max_chars=max_content_chars,
            )
            canonical_url = canonicalize_url(
                str(metadata.get("canonical_url") or current.canonical_url or current.url)
            )
            if repo.update_article_evidence(
                article_id,
                source_metadata=metadata,
                canonical_url=canonical_url,
            ):
                report.updated += 1
            else:
                report.unchanged += 1
        except Exception as exc:  # noqa: BLE001 - one URL must not stop maintenance
            report.failures.append(
                BackfillFailure(
                    article_id=article_id,
                    url=article.url,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
    report.failed = len(report.failures)
    report.complete()
    return report


def write_report(
    report: EvidenceBackfillReport, output_dir: Path = REPORT_DIR
) -> Path:
    """Persist an atomic maintenance report."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = "-dry-run" if report.dry_run else ""
    path = output_dir / f"evidence-backfill-{stamp}{suffix}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.pipeline.backfill_evidence",
        description="Backfill versioned structured evidence for legacy articles.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list eligible rows without network requests or DB mutation",
    )
    parser.add_argument("--limit", type=int, default=None, help="process at most N rows")
    parser.add_argument("--db", default=None, help="SQLite path (default: configured DB)")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="maintenance report directory (default: outputs/maintenance)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        build_parser().error("--limit must be at least 1")
    config = load_config()
    db_path = Path(args.db).expanduser() if args.db else config.db_path()
    output_dir = (
        Path(args.output_dir).expanduser() if args.output_dir else REPORT_DIR
    )
    conn = None
    http = None
    try:
        if args.dry_run:
            conn = connect_readonly(db_path)
        else:
            conn = connect(db_path)
            init_schema(conn)
        repo = ArticleRepository(conn)
        fetch_text = None
        if not args.dry_run:
            http = build_http_client(config)
            fetch_text = lambda url: http.get_text(  # noqa: E731
                url,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
        report = run_backfill(
            repo,
            fetch_text=fetch_text,
            max_content_chars=int(config.get("collect.max_content_chars", 20000)),
            dry_run=bool(args.dry_run),
            limit=args.limit,
        )
        report_path = write_report(report, output_dir)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        print(f"report: {report_path}")
        return 1 if report.failed else 0
    except (ContentOpsError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"evidence backfill failed: {exc}")
        return 2
    finally:
        if http is not None:
            http.close()
        if conn is not None:
            conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
