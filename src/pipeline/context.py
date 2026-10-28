"""Pipeline context: everything a stage needs, plus the state it produces.

Stages are plain functions ``(PipelineContext) -> None`` that read and mutate
this object. That keeps the flow inspectable (no hidden globals) while avoiding
a heavyweight orchestration framework on a single Mac mini.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.agents.editor import EditorAgent
from src.agents.fact_check import FactCheckAgent
from src.agents.llm import LLMClient
from src.agents.research import ResearchAgent
from src.agents.writer import WriterAgent
from src.collectors.base import CollectorResult
from src.core.config import AppConfig
from src.core.models import (
    Article,
    Draft,
    FactCheckReport,
    RawItem,
    ResearchNote,
    ReviewReport,
    RunRecord,
    ScoredArticle,
)
from src.dedup.service import DedupSummary
from src.rag.indexer import RagIndexer
from src.rag.retriever import RagRetriever
from src.storage.files import FileStore
from src.storage.repository import ArticleRepository


@dataclass
class PipelineContext:
    """Shared state for one run."""

    config: AppConfig
    run: RunRecord
    output_dir: Path
    run_date: date

    # infrastructure (None when a mode disables it)
    conn: sqlite3.Connection | None = None
    repo: ArticleRepository | None = None
    file_store: FileStore | None = None
    indexer: RagIndexer | None = None
    retriever: RagRetriever | None = None
    llm: LLMClient | None = None

    # agents
    research_agent: ResearchAgent | None = None
    fact_check_agent: FactCheckAgent | None = None
    writer_agent: WriterAgent | None = None
    editor_agent: EditorAgent | None = None

    # flags
    dry_run: bool = False
    offline: bool = False
    categories: list[str] | None = None

    # stage state
    collector_results: list[CollectorResult] = field(default_factory=list)
    raw_items: list[RawItem] = field(default_factory=list)
    articles: list[Article] = field(default_factory=list)
    stored_articles: list[Article] = field(default_factory=list)
    # Article keys that existed before this run (filled by the store stage).
    known_keys: set[str] = field(default_factory=set)
    dedup: DedupSummary | None = None
    scored: list[ScoredArticle] = field(default_factory=list)
    selected: list[ScoredArticle] = field(default_factory=list)
    overlap_skipped: list = field(default_factory=list)
    # article key -> TopicCluster (같은 사건을 다룬 기사 묶음)
    clusters: dict[str, Any] = field(default_factory=dict)
    notes: dict[str, ResearchNote] = field(default_factory=dict)
    reports: dict[str, FactCheckReport] = field(default_factory=dict)
    # article key(또는 channel:<id>) -> 실패 이유. LLM schema 불일치/CLI 실패로
    # 제외된 항목을 review.md 에 그대로 노출합니다.
    failed_items: dict[str, str] = field(default_factory=dict)
    drafts: list[Draft] = field(default_factory=list)
    review: ReviewReport | None = None

    # bookkeeping
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    stage_timings: dict[str, float] = field(default_factory=dict)
    written_files: list[Path] = field(default_factory=list)
    started_at: datetime | None = None

    # ------------------------------------------------------------------ helpers
    @property
    def mode(self) -> str:
        if self.dry_run:
            return "dry-run"
        return "offline" if self.offline else "live"

    @property
    def persist(self) -> bool:
        """Whether this run may write to the database / vector store."""
        return not self.dry_run and self.repo is not None

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def bump(self, key: str, amount: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + amount

    def selected_items(self) -> list[tuple[Article, ResearchNote, FactCheckReport]]:
        """Selected articles that have both a research note and a fact check."""
        bundle: list[tuple[Article, ResearchNote, FactCheckReport]] = []
        for item in self.selected:
            key = item.article.key
            note = self.notes.get(key)
            report = self.reports.get(key)
            if note is not None and report is not None:
                bundle.append((item.article, note, report))
        return bundle

    def publishable_items(self) -> list[tuple[Article, ResearchNote, FactCheckReport]]:
        """Items whose fact-check verdict is allowed by ``fact_check.allowed_verdicts``."""
        allowed = {
            str(v).lower()
            for v in (self.config.get("fact_check.allowed_verdicts") or [])
        } or {"verified", "partially_verified"}
        return [
            (article, note, report)
            for article, note, report in self.selected_items()
            if str(report.verdict).lower() in allowed
        ]

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run.run_id,
            "mode": self.mode,
            "output_dir": str(self.output_dir),
            "counts": self.counts,
            "warnings": self.warnings,
            "stage_timings": {k: round(v, 3) for k, v in self.stage_timings.items()},
            "files": [str(path) for path in self.written_files],
        }
