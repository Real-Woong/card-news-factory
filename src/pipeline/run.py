"""Pipeline entrypoint.

    python -m src.pipeline.run              # full run (network + configured LLM)
    python -m src.pipeline.run --dry-run    # sample data, mock LLM, no writes
    python -m src.pipeline.run --offline    # sample data, real DB/vector writes
    python -m src.pipeline.run --stages collect,normalize,store

Exit codes: 0 success, 1 pipeline failure, 2 configuration error, 130 interrupted.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from datetime import date, datetime
from pathlib import Path

from src.agents.editor import EditorAgent
from src.agents.fact_check import FactCheckAgent
from src.agents.llm import build_llm
from src.agents.research import ResearchAgent
from src.agents.writer import WriterAgent
from src.core.config import AppConfig, load_config
from src.core.errors import ConfigError, ContentOpsError, LLMError, StageError
from src.core.logging_setup import get_logger, setup_logging
from src.core.models import RunRecord
from src.core.text import utcnow
from src.pipeline import outputs
from src.pipeline.context import PipelineContext
from src.pipeline.stages import STAGES
from src.rag.factory import build_rag
from src.rag.indexer import RagIndexer
from src.rag.retriever import RagRetriever
from src.storage.db import connect, init_schema
from src.storage.files import FileStore
from src.storage.repository import ArticleRepository

logger = get_logger("contentops.run")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.pipeline.run",
        description="Collect AI/Blockchain news, build RAG context and generate "
        "SNS post drafts for human review.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="use bundled sample data and the mock LLM; write drafts but no DB/vector data",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="use bundled sample data but persist everything (DB, vector store, drafts)",
    )
    parser.add_argument(
        "--categories",
        default=None,
        help="restrict collection to these categories (comma separated, e.g. ai,blockchain)",
    )
    parser.add_argument(
        "--stages",
        default=None,
        help="run only these stages (comma separated, in the given order)",
    )
    parser.add_argument("--max-items", type=int, default=None, help="override rank.max_selected")
    parser.add_argument("--output-dir", default=None, help="override the draft output directory")
    parser.add_argument("--run-date", default=None, help="override the run date (YYYY-MM-DD)")
    parser.add_argument("--log-level", default=None, help="DEBUG|INFO|WARNING|ERROR")
    parser.add_argument("-v", "--verbose", action="store_true", help="shortcut for --log-level DEBUG")
    return parser


# --------------------------------------------------------------------------- #
# wiring
# --------------------------------------------------------------------------- #
def _resolve_run_date(raw: str | None) -> date:
    if not raw:
        return utcnow().astimezone().date()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ConfigError(f"--run-date must be YYYY-MM-DD, got {raw!r}") from exc


def _unique_output_dir(base: Path) -> Path:
    """Avoid clobbering an earlier run on the same day."""
    if not base.exists():
        return base
    for suffix in range(2, 100):
        candidate = base.parent / f"{base.name}-{suffix}"
        if not candidate.exists():
            return candidate
    return base


def _new_run_id(run_date: date) -> str:
    """``YYYY-MM-DD-HHMMSS-xxxx``.

    The random suffix keeps ids unique when two runs start in the same second
    (back-to-back manual runs, or a launchd retry), which would otherwise
    overwrite the earlier row in ``runs``.
    """
    stamp = utcnow().astimezone().strftime("%H%M%S")
    return f"{run_date.isoformat()}-{stamp}-{uuid.uuid4().hex[:4]}"


def build_context(config: AppConfig, args: argparse.Namespace) -> PipelineContext:
    """Wire up storage, RAG and agents according to the selected mode."""
    run_date = _resolve_run_date(args.run_date)
    run_id = _new_run_id(run_date)

    if args.output_dir:
        output_dir = Path(args.output_dir).expanduser()
    else:
        output_dir = _unique_output_dir(config.drafts_path() / run_date.isoformat())

    config.paths.ensure_runtime_dirs()
    output_dir.mkdir(parents=True, exist_ok=True)

    record = RunRecord(run_id=run_id, mode="dry-run" if args.dry_run else ("offline" if args.offline else "live"))
    ctx = PipelineContext(
        config=config,
        run=record,
        output_dir=output_dir,
        run_date=run_date,
        dry_run=bool(args.dry_run),
        offline=bool(args.offline),
        categories=[c.strip() for c in args.categories.split(",")] if args.categories else None,
        started_at=utcnow(),
    )

    # --- storage -----------------------------------------------------------
    ctx.conn = connect(config.db_path())
    init_schema(ctx.conn)
    ctx.repo = ArticleRepository(ctx.conn)
    ctx.repo.upsert_sources(config.sources, config.ranking.trust_scores)

    ctx.file_store = FileStore(
        config.paths.resolve(config.get("storage.raw_dir", "data/raw")),
        config.paths.resolve(config.get("storage.processed_dir", "data/processed")),
        enabled=not ctx.dry_run,
    )

    # --- RAG ---------------------------------------------------------------
    embedder, store = build_rag(config, ephemeral=ctx.dry_run)
    ctx.indexer = RagIndexer(
        config=config, embedder=embedder, store=store, repo=ctx.repo, dry_run=ctx.dry_run
    )
    ctx.retriever = RagRetriever(config=config, embedder=embedder, store=store)

    # --- agents ------------------------------------------------------------
    # dry-run 은 항상 mock. 그 외에는 설정값을 그대로 쓰고, 만들 수 없으면
    # 즉시 실패합니다(가짜 문장이 게시 후보가 되는 것을 막기 위해 폴백 없음).
    provider = "mock" if ctx.dry_run else str(config.get("agents.provider", "mock"))
    ctx.llm = build_llm(provider)
    ctx.research_agent = ResearchAgent(config=config, llm=ctx.llm)
    ctx.fact_check_agent = FactCheckAgent(config=config, llm=ctx.llm)
    ctx.writer_agent = WriterAgent(config=config, llm=ctx.llm)
    ctx.editor_agent = EditorAgent(config=config, llm=ctx.llm)

    return ctx


def archive_old_drafts(config: AppConfig) -> int:
    """Move all but the newest ``output.keep_recent_drafts`` directories to archive."""
    keep = int(config.get("output.keep_recent_drafts", 12) or 0)
    if keep <= 0:
        return 0
    drafts_dir = config.drafts_path()
    archive_dir = config.archive_path()
    archive_dir.mkdir(parents=True, exist_ok=True)

    directories = sorted(
        (path for path in drafts_dir.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )
    moved = 0
    for path in directories[keep:]:
        target = archive_dir / path.name
        if target.exists():
            continue
        try:
            path.rename(target)
            moved += 1
        except OSError as exc:  # pragma: no cover - permissions
            logger.warning("Could not archive %s: %s", path, exc)
    if moved:
        logger.info("[archive] moved %d old draft directory(ies) to %s", moved, archive_dir)
    return moved


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
def run_pipeline(ctx: PipelineContext, *, stage_names: list[str] | None = None) -> PipelineContext:
    """Execute the configured stages in order."""
    stages = stage_names or ctx.config.stages
    unknown = [name for name in stages if name not in STAGES]
    if unknown:
        raise ConfigError(f"Unknown stage(s): {unknown}. Known: {sorted(STAGES)}")

    continue_on_error = bool(ctx.config.get("run.continue_on_stage_error", False))
    if ctx.repo is not None and not ctx.dry_run:
        ctx.repo.start_run(ctx.run)

    logger.info(
        "[run] %s mode=%s stages=%s output=%s",
        ctx.run.run_id,
        ctx.mode,
        ",".join(stages),
        ctx.output_dir,
    )

    for name in stages:
        started = time.monotonic()
        logger.info("---- stage: %s ----", name)
        try:
            STAGES[name](ctx)
        except ContentOpsError as exc:
            elapsed = time.monotonic() - started
            ctx.stage_timings[name] = elapsed
            message = f"stage `{name}` failed: {exc}"
            logger.error(message)
            ctx.warn(message)
            ctx.run.status = "partial" if continue_on_error else "failed"
            ctx.run.error = message
            if not continue_on_error:
                raise StageError(message, stage=name) from exc
            continue
        ctx.stage_timings[name] = time.monotonic() - started
        ctx.run.stages_completed.append(name)

    if ctx.run.status == "running":
        ctx.run.status = "success"
    return ctx


def finalize(ctx: PipelineContext) -> None:
    """Persist the run record, write metadata and archive old drafts."""
    ctx.run.finished_at = utcnow()
    ctx.run.counts = ctx.counts
    ctx.run.output_dir = str(ctx.output_dir)
    try:
        outputs.write_run_metadata(ctx)
    except ContentOpsError as exc:
        logger.warning("Could not write run metadata: %s", exc)

    if ctx.repo is not None and not ctx.dry_run:
        try:
            ctx.repo.finish_run(ctx.run)
        except ContentOpsError as exc:
            logger.warning("Could not record run: %s", exc)
    if ctx.indexer is not None:
        ctx.indexer.store.persist()
    if not ctx.dry_run:
        archive_old_drafts(ctx.config)
    if ctx.conn is not None:
        ctx.conn.close()


def print_summary(ctx: PipelineContext) -> None:
    duration = (
        (ctx.run.finished_at - ctx.run.started_at).total_seconds()
        if ctx.run.finished_at and ctx.run.started_at
        else 0.0
    )
    review = ctx.review
    lines = [
        "",
        "=" * 64,
        f"run {ctx.run.run_id}  [{ctx.run.status}]  mode={ctx.mode}  {duration:.1f}s",
        f"collected={ctx.counts.get('collected', 0)} "
        f"kept={ctx.counts.get('kept', 0)} "
        f"duplicates={ctx.counts.get('duplicates', 0)} "
        f"selected={ctx.counts.get('selected', 0)} "
        f"drafts={ctx.counts.get('drafts', 0)}",
    ]
    if review is not None:
        lines.append(
            f"review: {'PUBLISH-READY' if review.recommend_publish else 'NEEDS WORK'} "
            f"(confidence {review.confidence:.2f}, {len(review.blockers)} blocker(s))"
        )
    lines.append(f"output: {ctx.output_dir}")
    for path in ctx.written_files:
        lines.append(f"  - {path.name}")
    if ctx.warnings:
        lines.append(f"warnings: {len(ctx.warnings)} (see logs)")
    lines.append("=" * 64)
    print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.max_items is not None:
        config.pipeline.setdefault("rank", {})["max_selected"] = args.max_items

    level = "DEBUG" if args.verbose else args.log_level
    setup_logging(config, level=level)

    ctx: PipelineContext | None = None
    try:
        ctx = build_context(config, args)
        stage_names = (
            [s.strip() for s in args.stages.split(",") if s.strip()] if args.stages else None
        )
        run_pipeline(ctx, stage_names=stage_names)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        if ctx is not None:
            ctx.run.status = "failed"
            ctx.run.error = "interrupted"
            finalize(ctx)
        return 130
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return 2
    except LLMError as exc:
        # provider 를 만들 수 없으면 mock 으로 폴백하지 않고 실패합니다.
        logger.error("LLM provider 사용 불가: %s", exc)
        logger.error(
            "LLM_PROVIDER=%s / KIRO_CLI_COMMAND=%s",
            os.environ.get("LLM_PROVIDER", "(미설정)"),
            os.environ.get("KIRO_CLI_COMMAND", "(미설정)"),
        )
        if ctx is not None:
            ctx.run.status = "failed"
            ctx.run.error = f"llm provider unavailable: {exc}"
            finalize(ctx)
        return 2
    except ContentOpsError as exc:
        logger.error("Pipeline failed: %s", exc)
        if ctx is not None:
            finalize(ctx)
            print_summary(ctx)
        return 1
    except Exception:  # noqa: BLE001 - last resort, keep the traceback in the log
        logger.exception("Unexpected failure")
        if ctx is not None:
            ctx.run.status = "failed"
            finalize(ctx)
        return 1

    finalize(ctx)
    print_summary(ctx)
    return 0 if ctx.run.status in {"success", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
