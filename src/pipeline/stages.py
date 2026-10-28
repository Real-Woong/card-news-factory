"""Pipeline stages.

Order (from ``pipeline.yaml -> run.stages``)::

    collect -> normalize -> store -> deduplicate -> update_rag -> rank
            -> research -> fact_check -> generate_drafts -> generate_review

Each stage is a function taking the :class:`PipelineContext`. Registered in
:data:`STAGES`, so adding a stage means adding a function and a config entry.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.collectors.registry import build_collectors
from src.core.errors import AgentError, StageError, StorageError
from src.core.logging_setup import get_logger
from src.core.models import Article, Draft, RawItem
from src.dedup.service import DedupService, resolve_embedding_profile
from src.pipeline import outputs, review
from src.pipeline.context import PipelineContext
from src.ranking.scorer import ArticleScorer
from src.ranking.cluster import ClusterService
from src.ranking.selector import TopicSelector
from src.ranking.topics import TopicTagger

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# 1. collect
# --------------------------------------------------------------------------- #
def stage_collect(ctx: PipelineContext) -> None:
    """Pull items from every enabled source (or the sample fixtures)."""
    collectors = build_collectors(
        ctx.config, categories=ctx.categories, offline=ctx.offline or ctx.dry_run
    )
    if not collectors:
        raise StageError("No collectors are enabled", stage="collect")

    max_total = int(ctx.config.get("collect.max_total_items", 200))
    continue_on_error = bool(ctx.config.get("run.continue_on_collector_error", True))
    items: list[RawItem] = []

    for collector in collectors:
        result = collector.run()
        ctx.collector_results.append(result)

        if ctx.persist and ctx.repo is not None:
            ctx.repo.record_source_result(
                collector.source.id,
                status="ok" if result.ok else "error",
                error=result.error,
            )
        if not result.ok:
            ctx.warn(f"collector {result.source_id} failed: {result.error}")
            if not continue_on_error:
                raise StageError(
                    f"collector {result.source_id} failed: {result.error}", stage="collect"
                )
            continue

        items.extend(result.items)
        if len(items) >= max_total:
            logger.warning("[collect] reached max_total_items=%d; stopping early", max_total)
            break

    ctx.raw_items = items[:max_total]
    ctx.counts["collected"] = len(ctx.raw_items)
    ctx.counts["sources_ok"] = sum(1 for r in ctx.collector_results if r.ok)
    ctx.counts["sources_failed"] = sum(1 for r in ctx.collector_results if not r.ok)
    logger.info(
        "[collect] %d item(s) from %d/%d source(s)",
        len(ctx.raw_items),
        ctx.counts["sources_ok"],
        len(ctx.collector_results),
    )


# --------------------------------------------------------------------------- #
# 2. normalize
# --------------------------------------------------------------------------- #
def stage_normalize(ctx: PipelineContext) -> None:
    """RawItem -> Article: canonical URLs, hashes, topic/signal tags."""
    tagger = TopicTagger(ctx.config)
    trust_scores = ctx.config.ranking.trust_scores
    articles: list[Article] = []
    seen: set[str] = set()

    for item in ctx.raw_items:
        try:
            article = Article(
                url=item.url,
                title=item.title,
                source_id=item.source_id,
                source_name=item.source_name,
                source_type=item.source_type,
                trust_tier=item.trust_tier,
                trust_score=int(trust_scores.get(item.trust_tier, 1)),
                category=item.category,
                summary=item.summary,
                content=item.content,
                published_at=item.published_at,
                collected_at=item.collected_at,
                author=item.author,
                tags=item.tags,
                canonical_url=str(
                    (item.raw_payload.get("source_metadata") or {}).get(
                        "canonical_url"
                    )
                    or item.url
                ),
                source_metadata=dict(item.raw_payload.get("source_metadata") or {}),
            )
        except Exception as exc:  # noqa: BLE001 - one bad item must not stop the run
            ctx.warn(f"normalize failed for {item.url}: {exc}")
            continue

        if article.key in seen:  # identical URL twice in one batch
            continue
        seen.add(article.key)
        tagger.tag(article)
        articles.append(article)

    ctx.articles = articles
    ctx.counts["normalized"] = len(articles)
    logger.info("[normalize] %d article(s)", len(articles))


# --------------------------------------------------------------------------- #
# 3. store
# --------------------------------------------------------------------------- #
def stage_store(ctx: PipelineContext) -> None:
    """Write raw payloads + normalised articles to disk and SQLite."""
    if ctx.file_store is not None:
        for item, article in zip(ctx.raw_items, ctx.articles, strict=False):
            article.raw_path = ctx.file_store.save_raw(
                item, run_date=ctx.run_date, key=article.key
            )
            article.processed_path = ctx.file_store.save_processed(
                article, run_date=ctx.run_date
            )

    if not ctx.persist or ctx.repo is None:
        ctx.stored_articles = ctx.articles
        ctx.counts["stored"] = 0
        logger.info("[store] skipped DB write (mode=%s)", ctx.mode)
        return

    try:
        ctx.repo.ensure_sources_from_articles(ctx.articles)
        # Snapshot what we already knew *before* inserting, so the dedup stage can
        # distinguish "seen in an earlier run" from "inserted by this run".
        ctx.known_keys = ctx.repo.existing_keys([article.key for article in ctx.articles])
        inserted = ctx.repo.insert_articles(ctx.articles)
    except StorageError as exc:
        raise StageError(str(exc), stage="store") from exc

    ctx.stored_articles = ctx.articles
    ctx.counts["stored"] = inserted
    ctx.counts["already_known"] = len(ctx.articles) - inserted
    logger.info("[store] %d new, %d already known", inserted, ctx.counts["already_known"])


# --------------------------------------------------------------------------- #
# 4. deduplicate
# --------------------------------------------------------------------------- #
def stage_deduplicate(ctx: PipelineContext) -> None:
    """Drop repeats by URL, title similarity and embedding similarity."""
    service = DedupService(
        config=ctx.config,
        repo=ctx.repo,
        retriever=ctx.retriever,
        # A snapshot exists only when the store stage actually wrote to the DB.
        known_keys=ctx.known_keys if ctx.persist else None,
    )
    summary = service.run(ctx.articles, run_id=ctx.run.run_id)
    if ctx.persist:
        service.persist(summary, run_id=ctx.run.run_id)

    ctx.dedup = summary
    ctx.articles = summary.kept
    ctx.counts.update(summary.counts)
    for warning in summary.warnings:
        ctx.warn(
            f"near-duplicate: {warning.article.title[:60]} ({'; '.join(warning.notes)})"
        )


# --------------------------------------------------------------------------- #
# 5. update RAG
# --------------------------------------------------------------------------- #
def stage_update_rag(ctx: PipelineContext) -> None:
    """Embed new articles, past posts and knowledge notes."""
    if ctx.indexer is None:
        logger.info("[rag] indexer disabled; skipping")
        return

    chunks = ctx.indexer.index_articles(ctx.articles)
    if ctx.config.get("rag.index_knowledge", True):
        chunks += ctx.indexer.index_knowledge()
    if ctx.repo is not None:
        chunks += ctx.indexer.index_posts(ctx.repo.posts_pending_index(limit=100))

    ctx.counts["rag_chunks"] = chunks
    if ctx.retriever is not None:
        for name, count in ctx.retriever.counts().items():
            ctx.counts[f"vectors_{name}"] = count


# --------------------------------------------------------------------------- #
# 6. rank
# --------------------------------------------------------------------------- #
def stage_rank(ctx: PipelineContext) -> None:
    """Score candidates and select the shortlist for this run."""
    candidates = list(ctx.articles)
    if ctx.repo is not None and not ctx.dry_run:
        # Include recent unused items so a quiet day still produces a post.
        known = {a.key for a in candidates}
        for article in ctx.repo.candidates_for_ranking(
            max_age_days=ctx.config.ranking.max_age_days
        ):
            if article.key not in known:
                candidates.append(article)
                known.add(article.key)

    if not candidates:
        logger.warning("[rank] nothing to rank")
        ctx.counts["selected"] = 0
        return

    novelty = _novelty_scores(ctx, candidates)
    scorer = ArticleScorer(ctx.config)
    ctx.scored = scorer.score_all(candidates, novelty_by_key=novelty)

    # 클러스터를 선별 **전에** 만들어, 같은 사건의 두 기사가 각각 선택되는 것을 막고
    # 선택된 토픽에는 복수 출처를 붙입니다.
    cluster_service = ClusterService(ctx.config, retriever=ctx.retriever)
    clusters = cluster_service.build(candidates) if cluster_service.enabled else []
    cluster_by_key: dict[str, Any] = {}
    for cluster in clusters:
        for article in cluster.articles:
            cluster_by_key[article.key] = cluster

    def cluster_key_fn(article: Article) -> str | None:
        cluster = cluster_by_key.get(article.key)
        return cluster.primary.key if cluster is not None and cluster.size > 1 else None

    profile = resolve_embedding_profile(ctx.config, ctx.retriever)
    selector = TopicSelector(
        ctx.config, embedding_overlap_threshold=profile.get("intra_run_threshold")
    )
    similarity_fn = None
    if ctx.retriever is not None:
        def similarity_fn(left: Article, right: Article) -> float:  # noqa: ANN202
            return ctx.retriever.similarity(
                f"{left.title}\n{left.summary}", f"{right.title}\n{right.summary}"
            )

    ctx.selected = selector.select(
        ctx.scored,
        similarity_fn=similarity_fn,
        cluster_key_fn=cluster_key_fn if clusters else None,
    )
    ctx.overlap_skipped = selector.skipped_overlap

    for item in ctx.selected:
        cluster = cluster_by_key.get(item.article.key)
        if cluster is not None:
            ctx.clusters[item.article.key] = cluster
    multi = sum(1 for c in ctx.clusters.values() if c.size > 1)
    ctx.counts["clustered_topics"] = multi
    ctx.counts["cluster_sources_total"] = sum(c.size for c in ctx.clusters.values())
    if multi:
        logger.info("[rank] 선별 %d건 중 %d건이 복수 출처", len(ctx.selected), multi)

    if ctx.persist and ctx.repo is not None:
        ctx.repo.save_scores(ctx.scored)
        ctx.repo.mark_selected([item.article.key for item in ctx.selected], ctx.run.run_id)

    ctx.counts["ranked"] = len(ctx.scored)
    ctx.counts["selected"] = len(ctx.selected)


def _novelty_scores(ctx: PipelineContext, candidates: list[Article]) -> dict[str, float]:
    """``1 - max similarity`` against earlier coverage, per article."""
    if ctx.retriever is None:
        return {}
    novelty: dict[str, float] = {}
    for article in candidates:
        query = f"{article.title}\n{article.summary}"[:2000]
        best = 0.0
        for collection in (
            ctx.retriever.posts_collection,
            ctx.retriever.articles_collection,
        ):
            hit = ctx.retriever.best_match(
                query, collection=collection, exclude_doc_keys={article.key}
            )
            if hit is not None:
                best = max(best, hit.score)
        novelty[article.key] = max(0.0, 1.0 - best)
    return novelty


# --------------------------------------------------------------------------- #
# 7. research
# --------------------------------------------------------------------------- #
def stage_research(ctx: PipelineContext) -> None:
    """Extract "what changed" for each selected item, with RAG context."""
    if ctx.research_agent is None:
        raise StageError("research agent is not configured", stage="research")

    for item in ctx.selected:
        article = item.article
        context = (
            ctx.retriever.context_for_article(article) if ctx.retriever is not None else []
        )
        cluster = ctx.clusters.get(article.key)
        try:
            note = ctx.research_agent.run(article, context, cluster=cluster)
        except AgentError as exc:
            # schema 불일치/CLI 실패는 해당 item 만 버립니다(mock 폴백 없음).
            ctx.warn(f"research 실패로 제외: {article.title[:60]} — {exc}")
            ctx.failed_items[article.key] = f"research: {exc}"
            continue
        ctx.notes[article.key] = note
        if ctx.persist and ctx.repo is not None:
            ctx.repo.save_research_note(ctx.run.run_id, note)

    ctx.counts["research_notes"] = len(ctx.notes)
    ctx.counts["research_failed"] = len(ctx.selected) - len(ctx.notes)


# --------------------------------------------------------------------------- #
# 8. fact check
# --------------------------------------------------------------------------- #
def stage_fact_check(ctx: PipelineContext) -> None:
    """Verify each research note against its source text."""
    if ctx.fact_check_agent is None:
        raise StageError("fact check agent is not configured", stage="fact_check")

    for item in ctx.selected:
        article = item.article
        note = ctx.notes.get(article.key)
        if note is None:
            continue
        try:
            report = ctx.fact_check_agent.run(article, note)
        except AgentError as exc:
            ctx.warn(f"fact check 실패로 제외: {article.title[:60]} — {exc}")
            ctx.failed_items[article.key] = f"fact_check: {exc}"
            continue
        ctx.reports[article.key] = report
        if ctx.persist and ctx.repo is not None:
            ctx.repo.save_fact_check(ctx.run.run_id, report)

    verdicts: dict[str, int] = {}
    for report in ctx.reports.values():
        key = f"verdict_{report.verdict}"
        verdicts[key] = verdicts.get(key, 0) + 1
    ctx.counts.update(verdicts)
    for metric in (
        "partially_verified_truncation",
        "partially_verified_source_scarcity",
        "fully_verified_single_source",
        "fully_verified_multi_source",
    ):
        ctx.counts[metric] = sum(
            1 for report in ctx.reports.values() if report.quality_metrics.get(metric)
        )
    ctx.counts["fact_checked"] = len(ctx.reports)
    ctx.counts["fact_check_failed"] = len(ctx.notes) - len(ctx.reports)
    ctx.counts["publishable"] = len(ctx.publishable_items())


# --------------------------------------------------------------------------- #
# 9. generate drafts
# --------------------------------------------------------------------------- #
def stage_generate_drafts(ctx: PipelineContext) -> None:
    """Write one draft per enabled channel, then run the editor over it."""
    if ctx.writer_agent is None or ctx.editor_agent is None:
        raise StageError("writer/editor agents are not configured", stage="generate_drafts")

    items = ctx.publishable_items()
    if not items:
        ctx.warn("no publishable item survived fact checking; drafts will be empty")

    channels = ctx.config.enabled_channels()
    if not channels:
        raise StageError("no channel is enabled in channels.yaml", stage="generate_drafts")

    reports = [report for _a, _n, report in items]
    drafts: list[Draft] = []

    for channel in channels:
        if not items:
            continue
        try:
            draft = ctx.writer_agent.run(channel=channel, items=items)
            draft = ctx.editor_agent.run(draft=draft, channel=channel, reports=reports)
        except AgentError as exc:
            ctx.warn(f"draft 생성 실패 (channel={channel.id}): {exc}")
            ctx.failed_items[f"channel:{channel.id}"] = str(exc)
            continue
        drafts.append(draft)

        if ctx.persist and ctx.repo is not None:
            from src.core.models import PublishedPost

            ctx.repo.save_post(
                PublishedPost(
                    run_id=ctx.run.run_id,
                    channel_id=channel.id,
                    format=channel.format,
                    title=draft.title,
                    body=draft.body,
                    status="draft",
                    article_keys=[article.key for article, _n, _r in items],
                )
            )

    ctx.drafts = drafts
    ctx.counts["drafts"] = len(drafts)

    if ctx.persist and ctx.repo is not None and items:
        ctx.repo.mark_used([article.key for article, _n, _r in items])

    outputs.write_draft_bundle(ctx)


# --------------------------------------------------------------------------- #
# 10. generate review
# --------------------------------------------------------------------------- #
def stage_generate_review(ctx: PipelineContext) -> None:
    """Build the human-review checklist and write ``review.md``."""
    ctx.review = review.build_review(ctx)
    outputs.write_review(ctx)
    ctx.counts["review_items"] = len(ctx.review.items)
    ctx.counts["review_blockers"] = len(ctx.review.blockers)


STAGES: dict[str, Callable[[PipelineContext], None]] = {
    "collect": stage_collect,
    "normalize": stage_normalize,
    "store": stage_store,
    "deduplicate": stage_deduplicate,
    "update_rag": stage_update_rag,
    "rank": stage_rank,
    "research": stage_research,
    "fact_check": stage_fact_check,
    "generate_drafts": stage_generate_drafts,
    "generate_review": stage_generate_review,
}
