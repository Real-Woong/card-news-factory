"""Repository layer: the only module that writes SQL.

Stages depend on :class:`ArticleRepository` rather than sqlite3, which keeps the
door open for swapping in Postgres later.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from src.core.config import SourceConfig
from src.core.errors import StorageError
from src.core.logging_setup import get_logger
from src.core.models import (
    Article,
    DedupResult,
    FactCheckReport,
    PublishedPost,
    ResearchNote,
    RunRecord,
    ScoredArticle,
)
from src.core.text import parse_datetime, to_iso, utcnow

logger = get_logger(__name__)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def row_to_article(row: sqlite3.Row) -> Article:
    """Map an ``articles`` row back to the domain model."""
    return Article(
        id=row["id"],
        url=row["url"],
        title=row["title"],
        source_id=row["source_id"],
        source_name=row["source_name"] or row["source_id"],
        source_type=row["source_type"] or "rss",
        trust_tier=row["trust_tier"] or "community",
        trust_score=int(row["trust_score"] or 1),
        category=row["category"],
        summary=row["summary"] or "",
        content=row["content"] or "",
        published_at=parse_datetime(row["published_at"]),
        collected_at=parse_datetime(row["collected_at"]) or utcnow(),
        author=row["author"],
        tags=_loads(row["tags"], []),
        topics=_loads(row["topics"], []),
        signals=_loads(row["signals"], []),
        language=row["language"],
        canonical_url=row["canonical_url"],
        url_hash_value=row["url_hash"],
        content_hash_value=row["content_hash"],
        title_normalized=row["title_normalized"],
        domain=row["domain"] or "",
        raw_path=row["raw_path"],
        processed_path=row["processed_path"],
        source_metadata=_loads(row["source_metadata"], {}),
    )


class ArticleRepository:
    """CRUD + bookkeeping over the SQLite database."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ sources
    def upsert_sources(self, sources: list[SourceConfig], trust_scores: dict[str, int]) -> None:
        now = utcnow().isoformat()
        rows = [
            (
                s.id,
                s.name,
                s.type,
                s.category,
                s.trust,
                int(trust_scores.get(s.trust, 1)),
                s.endpoint,
                int(s.enabled),
                now,
            )
            for s in sources
        ]
        try:
            self.conn.executemany(
                """
                INSERT INTO sources (id, name, type, category, trust_tier, trust_score,
                                     endpoint, enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, type=excluded.type, category=excluded.category,
                    trust_tier=excluded.trust_tier, trust_score=excluded.trust_score,
                    endpoint=excluded.endpoint, enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
        except sqlite3.Error as exc:
            raise StorageError(f"upsert_sources failed: {exc}") from exc

    def ensure_sources_from_articles(self, articles: list[Article]) -> None:
        """Create ``sources`` rows for source ids that are not in the config.

        Guards the ``articles.source_id`` foreign key when data arrives from
        fixtures or a source that was removed from ``sources.yaml``.
        """
        seen: dict[str, Article] = {}
        for article in articles:
            seen.setdefault(article.source_id, article)
        if not seen:
            return
        now = utcnow().isoformat()
        self.conn.executemany(
            """
            INSERT INTO sources (id, name, type, category, trust_tier, trust_score,
                                 endpoint, enabled, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL, 1, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            [
                (
                    a.source_id,
                    a.source_name,
                    a.source_type,
                    a.category,
                    a.trust_tier,
                    a.trust_score,
                    now,
                )
                for a in seen.values()
            ],
        )

    def record_source_result(
        self, source_id: str, *, status: str, error: str | None = None
    ) -> None:
        self.conn.execute(
            """
            UPDATE sources
               SET last_collected_at = ?, last_status = ?, last_error = ?
             WHERE id = ?
            """,
            (utcnow().isoformat(), status, error, source_id),
        )

    # ----------------------------------------------------------------- articles
    def get_by_url_hash(self, url_hash: str) -> Article | None:
        row = self.conn.execute(
            "SELECT * FROM articles WHERE url_hash = ?", (url_hash,)
        ).fetchone()
        return row_to_article(row) if row else None

    def get_article_by_id(self, article_id: int) -> Article | None:
        row = self.conn.execute(
            "SELECT * FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        return row_to_article(row) if row else None

    def articles_for_evidence_backfill(self) -> list[Article]:
        """Return stored articles without deciding eligibility in SQL/JSON."""
        columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(articles)")
        }
        if "source_metadata" in columns:
            sql = "SELECT * FROM articles ORDER BY id"
        else:
            # Lets --dry-run inspect a pre-migration DB without mutating its schema.
            sql = "SELECT *, NULL AS source_metadata FROM articles ORDER BY id"
        return [row_to_article(row) for row in self.conn.execute(sql)]

    def update_article_evidence(
        self,
        article_id: int,
        *,
        source_metadata: dict[str, Any],
        canonical_url: str,
    ) -> bool:
        """Update evidence in-place; identity and lifecycle columns are untouched."""
        cursor = self.conn.execute(
            """
            UPDATE articles
               SET source_metadata = ?, canonical_url = ?
             WHERE id = ?
            """,
            (_json(source_metadata), canonical_url, article_id),
        )
        return cursor.rowcount == 1

    def exists_url_hash(self, url_hash: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM articles WHERE url_hash = ? LIMIT 1", (url_hash,)
            ).fetchone()
            is not None
        )

    def existing_keys(self, keys: list[str]) -> set[str]:
        """Which of ``keys`` are already stored.

        Called by the store stage *before* inserting, so the dedup stage can tell
        "seen in an earlier run" from "inserted a moment ago by this run".
        """
        found: set[str] = set()
        for start in range(0, len(keys), 400):  # stay well under SQLITE_MAX_VARIABLE_NUMBER
            batch = keys[start : start + 400]
            placeholders = ",".join("?" * len(batch))
            rows = self.conn.execute(
                f"SELECT url_hash FROM articles WHERE url_hash IN ({placeholders})",  # noqa: S608
                batch,
            )
            found.update(row["url_hash"] for row in rows)
        return found

    def find_by_canonical_url(self, canonical_url: str) -> str | None:
        row = self.conn.execute(
            "SELECT url_hash FROM articles WHERE canonical_url = ? LIMIT 1", (canonical_url,)
        ).fetchone()
        return row["url_hash"] if row else None

    def find_by_content_hash(self, content_hash: str) -> str | None:
        row = self.conn.execute(
            "SELECT url_hash FROM articles WHERE content_hash = ? LIMIT 1", (content_hash,)
        ).fetchone()
        return row["url_hash"] if row else None

    def insert_article(self, article: Article) -> int:
        """Insert, or refresh content on conflict. Returns the row id."""
        params = (
            article.url_hash_value,
            article.url,
            article.canonical_url,
            article.domain,
            article.title,
            article.title_normalized,
            article.summary,
            article.content,
            article.content_hash_value,
            article.author,
            article.language,
            article.category,
            article.source_id,
            article.source_name,
            article.source_type,
            article.trust_tier,
            article.trust_score,
            _json(article.tags),
            _json(article.topics),
            _json(article.signals),
            to_iso(article.published_at),
            to_iso(article.collected_at),
            article.raw_path,
            article.processed_path,
            _json(article.source_metadata),
        )
        try:
            self.conn.execute(
                """
                INSERT INTO articles (
                    url_hash, url, canonical_url, domain, title, title_normalized,
                    summary, content, content_hash, author, language, category,
                    source_id, source_name, source_type, trust_tier, trust_score,
                    tags, topics, signals, published_at, collected_at,
                    raw_path, processed_path, source_metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url_hash) DO UPDATE SET
                    summary        = COALESCE(NULLIF(excluded.summary, ''), articles.summary),
                    content        = COALESCE(NULLIF(excluded.content, ''), articles.content),
                    topics         = excluded.topics,
                    signals        = excluded.signals,
                    processed_path = COALESCE(excluded.processed_path, articles.processed_path),
                    raw_path       = COALESCE(excluded.raw_path, articles.raw_path),
                    source_metadata = COALESCE(NULLIF(excluded.source_metadata, '{}'), articles.source_metadata)
                """,
                params,
            )
        except sqlite3.Error as exc:
            raise StorageError(f"insert_article failed for {article.url}: {exc}") from exc

        row = self.conn.execute(
            "SELECT id FROM articles WHERE url_hash = ?", (article.url_hash_value,)
        ).fetchone()
        article.id = int(row["id"])
        return article.id

    def insert_articles(self, articles: list[Article]) -> int:
        inserted = 0
        for article in articles:
            before = self.exists_url_hash(article.url_hash_value)
            self.insert_article(article)
            inserted += 0 if before else 1
        return inserted

    def recent_articles(
        self,
        *,
        days: int = 45,
        limit: int = 2000,
        exclude_key: str | None = None,
        include_duplicates: bool = False,
    ) -> list[Article]:
        since = (utcnow() - timedelta(days=days)).isoformat()
        clause = "" if include_duplicates else " AND is_duplicate = 0"
        sql = (
            "SELECT * FROM articles WHERE collected_at >= ?"
            + clause
            + (" AND url_hash != ?" if exclude_key else "")
            + " ORDER BY collected_at DESC LIMIT ?"
        )
        params: tuple[Any, ...] = (
            (since, exclude_key, limit) if exclude_key else (since, limit)
        )
        return [row_to_article(r) for r in self.conn.execute(sql, params)]

    def recent_title_index(self, *, days: int = 45, limit: int = 5000) -> list[dict[str, str]]:
        """Lightweight (url_hash, title, title_normalized) list for title dedup."""
        since = (utcnow() - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            """
            SELECT url_hash, title, title_normalized
              FROM articles
             WHERE collected_at >= ? AND is_duplicate = 0
             ORDER BY collected_at DESC
             LIMIT ?
            """,
            (since, limit),
        )
        return [dict(r) for r in rows]

    def candidates_for_ranking(self, *, max_age_days: int, limit: int = 500) -> list[Article]:
        """Fresh, non-duplicate, not-yet-used articles."""
        since = (utcnow() - timedelta(days=max_age_days)).isoformat()
        rows = self.conn.execute(
            """
            SELECT * FROM articles
             WHERE is_duplicate = 0
               AND status IN ('new', 'indexed')
               AND COALESCE(published_at, collected_at) >= ?
             ORDER BY COALESCE(published_at, collected_at) DESC
             LIMIT ?
            """,
            (since, limit),
        )
        return [row_to_article(r) for r in rows]

    def pending_index(self, *, limit: int = 500) -> list[Article]:
        rows = self.conn.execute(
            """
            SELECT * FROM articles
             WHERE is_duplicate = 0 AND indexed_at IS NULL
             ORDER BY collected_at DESC LIMIT ?
            """,
            (limit,),
        )
        return [row_to_article(r) for r in rows]

    def mark_indexed(self, keys: list[str]) -> None:
        if not keys:
            return
        now = utcnow().isoformat()
        self.conn.executemany(
            "UPDATE articles SET indexed_at = ?, status = CASE WHEN status = 'new' "
            "THEN 'indexed' ELSE status END WHERE url_hash = ?",
            [(now, key) for key in keys],
        )

    def mark_duplicate(self, result: DedupResult, *, run_id: str | None = None) -> None:
        """Record a dedup decision.

        A *self* match (``matched_key == url_hash``) means "we already collected
        this URL in an earlier run". That is not a duplicate article, so the
        stored row keeps its status and stays eligible for future ranking - only
        the audit row is written. Flagging it would permanently retire an item
        just because it reappeared in a feed.
        """
        self_match = result.matched_key == result.article.url_hash_value
        if not self_match:
            self.conn.execute(
                """
                UPDATE articles
                   SET is_duplicate = 1, dedup_reason = ?, dedup_score = ?,
                       duplicate_of = ?, status = 'duplicate'
                 WHERE url_hash = ?
                """,
                (
                    str(result.reason),
                    result.score,
                    result.matched_key,
                    result.article.url_hash_value,
                ),
            )
        self.conn.execute(
            """
            INSERT INTO dedup_log (run_id, url_hash, matched_key, reason, score, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                result.article.url_hash_value,
                result.matched_key,
                str(result.reason),
                result.score,
                utcnow().isoformat(),
            ),
        )

    def save_scores(self, scored: list[ScoredArticle]) -> None:
        self.conn.executemany(
            "UPDATE articles SET score = ?, score_breakdown = ? WHERE url_hash = ?",
            [
                (item.score, _json(item.to_dict()), item.article.url_hash_value)
                for item in scored
            ],
        )

    def mark_selected(self, keys: list[str], run_id: str) -> None:
        if not keys:
            return
        self.conn.executemany(
            "UPDATE articles SET status = 'selected', selected_run_id = ? WHERE url_hash = ?",
            [(run_id, key) for key in keys],
        )

    def mark_used(self, keys: list[str]) -> None:
        if not keys:
            return
        self.conn.executemany(
            "UPDATE articles SET status = 'used' WHERE url_hash = ?", [(k,) for k in keys]
        )

    # --------------------------------------------------------------------- runs
    def start_run(self, record: RunRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO runs (run_id, started_at, status, mode, stages_completed, counts)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET started_at = excluded.started_at,
                status = excluded.status, mode = excluded.mode
            """,
            (
                record.run_id,
                to_iso(record.started_at),
                record.status,
                record.mode,
                _json(record.stages_completed),
                _json(record.counts),
            ),
        )

    def finish_run(self, record: RunRecord) -> None:
        self.conn.execute(
            """
            UPDATE runs
               SET finished_at = ?, status = ?, stages_completed = ?, counts = ?,
                   output_dir = ?, error = ?
             WHERE run_id = ?
            """,
            (
                to_iso(record.finished_at or utcnow()),
                record.status,
                _json(record.stages_completed),
                _json(record.counts),
                record.output_dir,
                record.error,
                record.run_id,
            ),
        )

    def last_successful_run(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE status = 'success' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    # -------------------------------------------------------------------- posts
    def save_post(self, post: PublishedPost) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO posts (run_id, channel_id, format, title, body, status,
                               article_keys, char_count, created_at, published_at,
                               external_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post.run_id,
                post.channel_id,
                post.format,
                post.title,
                post.body,
                post.status,
                _json(post.article_keys),
                len(post.body),
                to_iso(post.created_at),
                to_iso(post.published_at),
                post.external_id,
            ),
        )
        post_id = int(cursor.lastrowid or 0)
        post.id = post_id
        if post.article_keys:
            self.conn.executemany(
                "INSERT OR IGNORE INTO post_articles (post_id, url_hash) VALUES (?, ?)",
                [(post_id, key) for key in post.article_keys],
            )
        return post_id

    def find_post_id_by_external_id(self, channel_id: str, external_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM posts WHERE channel_id = ? AND external_id = ? LIMIT 1",
            (channel_id, external_id),
        ).fetchone()
        return int(row["id"]) if row else None

    def set_post_external_url(self, post_id: int, external_url: str) -> None:
        self.conn.execute(
            "UPDATE posts SET external_url = ? WHERE id = ?", (external_url, post_id)
        )

    def recent_posts(self, *, days: int = 180, limit: int = 500) -> list[PublishedPost]:
        since = (utcnow() - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM posts WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (since, limit),
        )
        return [
            PublishedPost(
                id=r["id"],
                run_id=r["run_id"],
                channel_id=r["channel_id"],
                format=r["format"],
                title=r["title"] or "",
                body=r["body"],
                status=r["status"],
                article_keys=_loads(r["article_keys"], []),
                created_at=parse_datetime(r["created_at"]) or utcnow(),
                published_at=parse_datetime(r["published_at"]),
                external_id=r["external_id"],
            )
            for r in rows
        ]

    def posts_pending_index(self, *, limit: int = 500) -> list[PublishedPost]:
        """Posts not yet present in the vector store."""
        rows = self.conn.execute(
            """
            SELECT p.* FROM posts p
             WHERE NOT EXISTS (
                 SELECT 1 FROM vector_index v
                  WHERE v.doc_type = 'post' AND v.doc_key = CAST(p.id AS TEXT)
             )
             ORDER BY p.created_at DESC LIMIT ?
            """,
            (limit,),
        )
        return [
            PublishedPost(
                id=r["id"],
                run_id=r["run_id"],
                channel_id=r["channel_id"],
                format=r["format"],
                title=r["title"] or "",
                body=r["body"],
                status=r["status"],
                article_keys=_loads(r["article_keys"], []),
                created_at=parse_datetime(r["created_at"]) or utcnow(),
            )
            for r in rows
        ]

    # ------------------------------------------------------- agent artefacts
    def save_research_note(self, run_id: str, note: ResearchNote) -> None:
        self.conn.execute(
            "INSERT INTO research_notes (run_id, url_hash, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (run_id, note.article_key, _json(note.to_dict()), utcnow().isoformat()),
        )

    def save_fact_check(self, run_id: str, report: FactCheckReport) -> None:
        self.conn.execute(
            """
            INSERT INTO fact_checks (run_id, url_hash, verdict, confidence, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                report.article_key,
                str(report.verdict),
                report.confidence,
                _json(report.to_dict()),
                utcnow().isoformat(),
            ),
        )

    # -------------------------------------------------------- vector bookkeeping
    def record_vector_chunks(
        self,
        *,
        collection: str,
        doc_key: str,
        doc_type: str,
        chunk_ids: list[str],
        content_hash: str,
    ) -> None:
        now = utcnow().isoformat()
        self.conn.execute(
            "DELETE FROM vector_index WHERE doc_key = ? AND collection = ?",
            (doc_key, collection),
        )
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO vector_index
                (chunk_id, collection, doc_key, doc_type, content_hash, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [(cid, collection, doc_key, doc_type, content_hash, now) for cid in chunk_ids],
        )

    def indexed_content_hash(self, *, collection: str, doc_key: str) -> str | None:
        row = self.conn.execute(
            "SELECT content_hash FROM vector_index WHERE doc_key = ? AND collection = ? LIMIT 1",
            (doc_key, collection),
        ).fetchone()
        return row["content_hash"] if row else None

    def chunk_ids_for(self, *, collection: str, doc_key: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT chunk_id FROM vector_index WHERE doc_key = ? AND collection = ?",
            (doc_key, collection),
        )
        return [r["chunk_id"] for r in rows]

    # ------------------------------------------------------------------- stats
    def counts(self) -> dict[str, int]:
        def scalar(sql: str) -> int:
            row = self.conn.execute(sql).fetchone()
            return int(row[0]) if row else 0

        return {
            "articles": scalar("SELECT COUNT(*) FROM articles"),
            "articles_duplicate": scalar("SELECT COUNT(*) FROM articles WHERE is_duplicate = 1"),
            "articles_indexed": scalar(
                "SELECT COUNT(*) FROM articles WHERE indexed_at IS NOT NULL"
            ),
            "posts": scalar("SELECT COUNT(*) FROM posts"),
            "runs": scalar("SELECT COUNT(*) FROM runs"),
        }

    def stats_since(self, *, days: int = 7) -> dict[str, int]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM articles WHERE collected_at >= ?", (since,)
        ).fetchone()
        return {"collected_last_days": int(row["c"]) if row else 0}
