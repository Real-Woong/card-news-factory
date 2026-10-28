"""Storage layer: schema, article CRUD, run/post bookkeeping."""

from __future__ import annotations

from tests.conftest import make_article


class TestSchema:
    def test_tables_exist(self, conn):
        names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "sources",
            "articles",
            "runs",
            "posts",
            "dedup_log",
            "research_notes",
            "fact_checks",
            "vector_index",
        } <= names

    def test_init_is_idempotent(self, conn):
        from src.storage.db import init_schema

        init_schema(conn)
        init_schema(conn)


class TestArticleRepository:
    def test_insert_and_fetch(self, repo):
        article = make_article()
        article_id = repo.insert_article(article)
        assert article_id > 0

        loaded = repo.get_by_url_hash(article.key)
        assert loaded is not None
        assert loaded.title == article.title
        assert loaded.trust_score == 5

    def test_insert_is_idempotent_on_url(self, repo):
        article = make_article()
        repo.insert_article(article)
        repo.insert_article(make_article())
        assert repo.counts()["articles"] == 1

    def test_insert_articles_counts_only_new(self, repo):
        first = make_article()
        second = make_article(url="https://openai.com/index/other/", title="Another change")
        assert repo.insert_articles([first, second]) == 2
        assert repo.insert_articles([first, second]) == 0

    def test_find_by_canonical_and_content_hash(self, repo):
        article = make_article()
        repo.insert_article(article)
        assert repo.find_by_canonical_url(article.canonical_url) == article.key
        assert repo.find_by_content_hash(article.content_hash_value) == article.key

    def test_mark_duplicate_writes_audit_row(self, repo, conn):
        from src.core.models import DedupReason, DedupResult

        article = make_article()
        repo.insert_article(article)
        repo.mark_duplicate(
            DedupResult(
                article=article,
                is_duplicate=True,
                reason=DedupReason.TITLE,
                score=0.91,
                matched_key="abc",
            ),
            run_id="test-run",
        )
        row = conn.execute(
            "SELECT is_duplicate, dedup_reason, status FROM articles WHERE url_hash = ?",
            (article.key,),
        ).fetchone()
        assert row["is_duplicate"] == 1
        assert row["status"] == "duplicate"
        assert conn.execute("SELECT COUNT(*) FROM dedup_log").fetchone()[0] == 1

    def test_candidates_exclude_duplicates(self, repo):
        keep = make_article()
        drop = make_article(url="https://openai.com/index/dup/", title="Dup item")
        repo.insert_articles([keep, drop])

        from src.core.models import DedupReason, DedupResult

        repo.mark_duplicate(
            DedupResult(article=drop, is_duplicate=True, reason=DedupReason.URL, score=1.0)
        )
        keys = {a.key for a in repo.candidates_for_ranking(max_age_days=14)}
        assert keep.key in keys
        assert drop.key not in keys

    def test_recent_title_index(self, repo):
        repo.insert_article(make_article())
        rows = repo.recent_title_index(days=30)
        assert rows and "title_normalized" in rows[0]

    def test_ensure_sources_from_articles(self, repo, conn):
        unknown = make_article(source_id="brand_new_source")
        repo.ensure_sources_from_articles([unknown])
        repo.insert_article(unknown)
        assert conn.execute(
            "SELECT COUNT(*) FROM sources WHERE id = 'brand_new_source'"
        ).fetchone()[0] == 1

    def test_run_and_post_lifecycle(self, repo):
        from src.core.models import PublishedPost, RunRecord
        from src.core.text import utcnow

        record = RunRecord(run_id="run-1", mode="offline")
        repo.start_run(record)

        post_id = repo.save_post(
            PublishedPost(
                run_id="run-1",
                channel_id="x_short",
                format="shortform",
                title="t",
                body="b",
                article_keys=["k1"],
            )
        )
        assert post_id > 0

        record.finished_at = utcnow()
        record.status = "success"
        record.stages_completed = ["collect"]
        repo.finish_run(record)

        assert repo.last_successful_run()["run_id"] == "run-1"
        assert repo.recent_posts()[0].channel_id == "x_short"
        assert [p.id for p in repo.posts_pending_index()] == [post_id]

    def test_vector_index_bookkeeping(self, repo):
        repo.record_vector_chunks(
            collection="articles",
            doc_key="key1",
            doc_type="article",
            chunk_ids=["article:key1:0", "article:key1:1"],
            content_hash="hash1",
        )
        assert repo.indexed_content_hash(collection="articles", doc_key="key1") == "hash1"
        assert len(repo.chunk_ids_for(collection="articles", doc_key="key1")) == 2

        repo.record_vector_chunks(
            collection="articles",
            doc_key="key1",
            doc_type="article",
            chunk_ids=["article:key1:0"],
            content_hash="hash2",
        )
        assert len(repo.chunk_ids_for(collection="articles", doc_key="key1")) == 1


class TestFileStore:
    def test_writes_raw_and_processed(self, tmp_path):
        from datetime import date

        from src.core.models import RawItem
        from src.storage.files import FileStore

        store = FileStore(tmp_path / "raw", tmp_path / "processed")
        article = make_article()
        item = RawItem(
            source_id="openai_news",
            source_name="OpenAI News",
            source_type="rss",
            trust_tier="official",
            category="ai",
            url=article.url,
            title=article.title,
            content=article.content,
        )
        raw_path = store.save_raw(item, run_date=date(2026, 9, 20), key=article.key)
        processed_path = store.save_processed(article, run_date=date(2026, 9, 20))
        assert raw_path and processed_path
        assert (tmp_path / "raw" / "2026-09-20").exists()
        assert (tmp_path / "processed" / "2026-09-20").exists()

    def test_disabled_store_writes_nothing(self, tmp_path):
        from datetime import date

        from src.storage.files import FileStore

        store = FileStore(tmp_path / "raw", tmp_path / "processed", enabled=False)
        assert store.save_processed(make_article(), run_date=date(2026, 9, 20)) is None
        assert not (tmp_path / "processed").exists()
