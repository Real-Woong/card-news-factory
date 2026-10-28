"""Versioned structured-evidence maintenance workflow regressions."""

from __future__ import annotations

import json

from src.collectors.base import CollectSettings
from src.collectors.sample import SampleCollector
from src.core.evidence import (
    EVIDENCE_VERSION,
    EXTRACTION_VERSION,
    build_source_metadata,
)
from src.pipeline.backfill_evidence import (
    eligible_articles,
    main,
    run_backfill,
    write_report,
)
from src.storage.db import connect, init_schema
from src.storage.repository import ArticleRepository
from tests.conftest import make_article

ARTICLE_HTML = """
<html><head><link rel="canonical" href="https://openai.com/index/example/"/></head>
<body><article>
  <h1>Structured outputs v2.1.0</h1>
  <p>Structured outputs are generally available.</p>
  <h2>Benchmark evaluation</h2>
  <p>Median latency improved from 340 ms to 120 ms.</p>
  <p><a href="https://github.com/openai/example/releases/tag/v2.1.0">Release</a></p>
</article></body></html>
"""


def _current_metadata() -> dict:
    _body, metadata = build_source_metadata(
        raw_content=ARTICLE_HTML,
        plain_text=(
            "Structured outputs v2.1.0\nStructured outputs are generally available.\n"
            "Benchmark evaluation\nMedian latency improved from 340 ms to 120 ms."
        ),
        canonical_url="https://openai.com/index/example/",
        title="Structured outputs",
        max_chars=20_000,
    )
    return metadata


def _insert_legacy(repo):
    article = make_article()
    article.source_metadata = {}
    repo.insert_article(article)
    return article


def test_legacy_article_is_detected_as_needing_backfill(repo):
    article = _insert_legacy(repo)

    candidates = eligible_articles(repo)

    assert [candidate.key for candidate, _reasons in candidates] == [article.key]
    assert "evidence_version_outdated" in candidates[0][1]
    assert "missing_repository_links" in candidates[0][1]


def test_current_article_is_skipped(repo):
    article = make_article()
    article.source_metadata = _current_metadata()
    repo.insert_article(article)

    assert eligible_articles(repo) == []


def test_dry_run_performs_no_fetch_or_database_mutation(repo):
    article = _insert_legacy(repo)
    calls: list[str] = []

    report = run_backfill(
        repo,
        fetch_text=lambda url: calls.append(url) or ARTICLE_HTML,
        max_content_chars=20_000,
        dry_run=True,
    )

    restored = repo.get_by_url_hash(article.key)
    assert report.eligible == 1
    assert report.updated == 0
    assert calls == []
    assert restored is not None and restored.source_metadata == {}


def test_successful_backfill_updates_structured_evidence(repo):
    article = _insert_legacy(repo)

    report = run_backfill(
        repo,
        fetch_text=lambda _url: ARTICLE_HTML,
        max_content_chars=20_000,
    )

    restored = repo.get_by_url_hash(article.key)
    assert report.updated == 1
    assert report.failed == 0
    assert restored is not None
    assert restored.source_metadata["evidence_version"] == EVIDENCE_VERSION
    assert restored.source_metadata["extraction_version"] == EXTRACTION_VERSION
    assert restored.source_metadata["repository_links"]
    assert restored.source_metadata["evidence_updated_at"]


def test_backfill_preserves_article_identity_and_history(repo):
    article = _insert_legacy(repo)
    original_id = article.id
    original_collected_at = repo.conn.execute(
        "SELECT collected_at FROM articles WHERE id = ?", (original_id,)
    ).fetchone()[0]
    repo.conn.execute(
        "UPDATE articles SET status = 'used', selected_run_id = 'historic-run' WHERE id = ?",
        (original_id,),
    )

    run_backfill(
        repo,
        fetch_text=lambda _url: ARTICLE_HTML,
        max_content_chars=20_000,
    )

    row = repo.conn.execute(
        "SELECT id, collected_at, status, selected_run_id FROM articles WHERE id = ?",
        (original_id,),
    ).fetchone()
    assert row["id"] == original_id
    assert row["collected_at"] == original_collected_at
    assert row["status"] == "used"
    assert row["selected_run_id"] == "historic-run"


def test_failed_fetch_preserves_old_data_and_records_failure(repo):
    article = _insert_legacy(repo)
    before = repo.get_by_url_hash(article.key).to_dict()

    def fail(_url: str) -> str:
        raise RuntimeError("temporary upstream failure")

    report = run_backfill(
        repo,
        fetch_text=fail,
        max_content_chars=20_000,
    )

    after = repo.get_by_url_hash(article.key).to_dict()
    assert report.failed == 1
    assert "temporary upstream failure" in report.failures[0].reason
    assert after == before


def test_failed_backfill_is_eligible_for_safe_retry(repo):
    article = _insert_legacy(repo)
    failed = run_backfill(
        repo,
        fetch_text=lambda _url: (_ for _ in ()).throw(RuntimeError("retry me")),
        max_content_chars=20_000,
    )
    assert failed.failed == 1
    assert [item.key for item, _reasons in eligible_articles(repo)] == [article.key]

    retried = run_backfill(
        repo,
        fetch_text=lambda _url: ARTICLE_HTML,
        max_content_chars=20_000,
    )

    assert retried.updated == 1
    assert retried.failed == 0
    assert eligible_articles(repo) == []


def test_new_collection_uses_current_evidence_version(tmp_path):
    fixture = tmp_path / "article.json"
    fixture.write_text(
        json.dumps(
            {
                "source_id": "fixture",
                "source_name": "Fixture",
                "source_type": "rss",
                "trust": "official",
                "category": "ai",
                "url": "https://vendor.example/release",
                "title": "Runtime v2.1.0",
                "content": ARTICLE_HTML,
                "published_days_ago": 1,
            }
        ),
        encoding="utf-8",
    )

    result = SampleCollector(
        tmp_path, settings=CollectSettings(max_content_chars=500)
    ).run()
    metadata = result.items[0].raw_payload["source_metadata"]

    assert metadata["evidence_version"] == EVIDENCE_VERSION
    assert metadata["extraction_version"] == EXTRACTION_VERSION
    assert metadata["evidence_updated_at"]


def test_backfill_report_is_persisted(tmp_path, repo):
    _insert_legacy(repo)
    report = run_backfill(
        repo,
        fetch_text=None,
        max_content_chars=20_000,
        dry_run=True,
        limit=1,
    )

    path = write_report(report, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["eligible"] == 1
    assert payload["selected"] == 1
    assert payload["eligible_articles"][0]["reasons"]


def test_cli_dry_run_opens_database_without_mutation(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = connect(db_path)
    init_schema(connection)
    repository = ArticleRepository(connection)
    article = make_article()
    repository.ensure_sources_from_articles([article])
    article.source_metadata = {}
    repository.insert_article(article)
    connection.close()
    before = db_path.read_bytes()

    exit_code = main(
        [
            "--dry-run",
            "--db",
            str(db_path),
            "--output-dir",
            str(tmp_path / "reports"),
        ]
    )

    assert exit_code == 0
    assert db_path.read_bytes() == before
