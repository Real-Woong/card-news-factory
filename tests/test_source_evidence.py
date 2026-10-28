"""Structured evidence, partial diagnostics, and source diversity regressions."""

from __future__ import annotations

import sqlite3

import pytest

from src.agents.fact_check import FactCheckAgent
from src.agents.llm import MockLLM
from src.core.evidence import build_source_metadata, retrieve_claim_evidence
from src.core.errors import AgentError
from src.core.models import Claim, ResearchNote, Verdict
from tests.conftest import make_article
from src.storage.db import SCHEMA_FILE, init_schema

from channel.src.config import load_channel_config
from channel.src.editor import InstagramEditor
from channel.src.quality import _source_diversity
from channel.src.renderer import RenderResult, SlideRender
from channel.src.schema import Source
from channel.src.similarity import DuplicatePair
from channel.src.validator import CarouselValidator
from tests.test_ra_glance import make_carousel, make_topic


@pytest.fixture(scope="module")
def channel():
    return load_channel_config()


def _document(html: str, *, limit: int = 600):
    from src.collectors.extract import html_to_text

    return build_source_metadata(
        raw_content=html,
        plain_text=html_to_text(html),
        canonical_url="https://vendor.example/blog/release",
        title="Vendor release",
        max_chars=limit,
    )


def test_long_article_preserves_repository_link_near_end():
    html = (
        '<html><head><link rel="canonical" href="https://vendor.example/releases/v2"/></head>'
        "<body><article><h1>Release</h1>"
        + "".join(f"<p>Background paragraph {index} " + "x" * 120 + "</p>" for index in range(30))
        + '<h2>Resources</h2><p><a href="https://github.com/acme/runtime">Repository</a></p>'
        + "</article></body></html>"
    )

    body, metadata = _document(html, limit=500)

    assert len(body) <= 500
    assert "https://github.com/acme/runtime" in metadata["repository_links"]
    assert metadata["canonical_url"] == "https://vendor.example/releases/v2"
    assert metadata["content_truncated"] is True


def test_benchmark_section_survives_long_body_budget():
    html = (
        "<article><h1>Overview</h1>"
        + "".join(f"<p>Historical context {index} " + "x" * 150 + "</p>" for index in range(20))
        + "<h2>Benchmark evaluation</h2>"
        + "<p>MLPerf inference latency improved from 340 ms to 120 ms.</p>"
        + "</article>"
    )

    body, metadata = _document(html, limit=650)

    assert "MLPerf" in body
    assert "Benchmark evaluation" in metadata["headings"]


def test_claim_specific_chunk_retrieval_prefers_matching_section():
    metadata = {
        "chunks": [
            {
                "id": "chunk-001",
                "section": "Availability",
                "text": "The preview is available only in the US region.",
            },
            {
                "id": "chunk-002",
                "section": "Benchmarks",
                "text": "MLPerf latency improved from 340 ms to 120 ms in v2.1.0.",
            },
        ]
    }

    matches = retrieve_claim_evidence(
        "MLPerf latency is 120 ms in v2.1.0", metadata, top_k=1
    )

    assert matches[0]["chunk_id"] == "chunk-002"
    assert matches[0]["section"] == "Benchmarks"


def test_structured_evidence_survives_sqlite_round_trip(repo):
    article = make_article()
    article.source_metadata = {
        "repository_links": ["https://github.com/acme/runtime"],
        "chunks": [
            {
                "id": "chunk-004",
                "section": "Release notes",
                "text": "Runtime v2.1.0 is generally available.",
            }
        ],
    }

    repo.insert_article(article)
    restored = repo.get_by_url_hash(article.key)

    assert restored is not None
    assert restored.source_metadata == article.source_metadata


def test_existing_database_is_migrated_for_source_metadata():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    legacy_schema = SCHEMA_FILE.read_text(encoding="utf-8").replace(
        "    source_metadata TEXT,          -- JSON object: internal structured evidence\n",
        "",
    )
    connection.executescript(legacy_schema)

    init_schema(connection)

    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(articles)")
    }
    assert "source_metadata" in columns
    connection.close()


def test_partial_verdict_records_source_truncated_reason(config, monkeypatch):
    article = make_article(content="Short retained intro")
    article.source_metadata = {
        "content_truncated": True,
        "chunks": [{"id": "chunk-000", "section": "intro", "text": "Short retained intro"}],
    }
    note = ResearchNote(
        article_key=article.key,
        title=article.title,
        url=article.url,
        category=article.category,
        key_facts=[Claim(text="Repository ships benchmark harness v2.1.0")],
        sources=[
            {
                "url": article.url,
                "source_relation": "primary_announcement",
                "supports_event": True,
            }
        ],
    )
    agent = FactCheckAgent(config=config, llm=MockLLM())
    monkeypatch.setattr(
        agent,
        "ask_json",
        lambda *_args, **_kwargs: {
            "verdict": "partially_verified",
            "confidence": 0.65,
            "checked_claims": [
                {
                    "claim": "Repository ships benchmark harness v2.1.0",
                    "verdict": "unverified",
                    "confidence": 0.3,
                }
            ],
            "unverified_claims": ["Repository ships benchmark harness v2.1.0"],
            "primary_source_found": True,
            "needs_human_review": True,
        },
    )

    report = agent.run(article, note)

    assert report.verdict == Verdict.PARTIALLY_VERIFIED
    assert "source_truncated" in report.partial_reasons
    assert report.quality_metrics["partially_verified_truncation"] is True


def test_official_plus_github_release_improves_source_diversity():
    carousel = make_carousel()
    carousel.closing.sources = [
        Source(
            name="Vendor Blog",
            url="https://vendor.example/release",
            authority="primary",
            source_relation="primary_announcement",
        ),
        Source(
            name="GitHub",
            url="https://github.com/acme/runtime/releases/tag/v2.1.0",
            authority="primary",
            source_relation="implementation_artifact",
        ),
    ]

    assert _source_diversity(carousel) == 0.82


def test_mirror_does_not_increase_source_diversity():
    carousel = make_carousel()
    carousel.closing.sources = [
        Source(
            name="Vendor Blog",
            url="https://vendor.example/release",
            authority="primary",
            source_relation="primary_announcement",
        ),
        Source(
            name="Syndicated copy",
            url="https://mirror.example/release",
            authority="secondary",
            source_relation="mirror",
        ),
        Source(
            name="Vendor Blog tracking URL",
            url="https://vendor.example/release?utm_source=syndication",
            authority="secondary",
            source_relation="independent_secondary",
        ),
    ]

    assert _source_diversity(carousel) == 0.55


def test_verified_authoritative_single_source_is_not_a_block(channel, tmp_path):
    carousel = make_carousel()
    render = RenderResult()
    for slide in carousel.slides:
        png = tmp_path / f"{slide.index}.png"
        html = tmp_path / f"{slide.index}.html"
        png.write_bytes(b"fixture")
        html.write_text("fixture", encoding="utf-8")
        render.slides.append(
            SlideRender(
                index=slide.index,
                png_path=png,
                html_path=html,
                width=1080,
                height=1350,
            )
        )

    report = CarouselValidator(channel).validate(
        carousel, topic=make_topic(), render=render
    )

    assert "single_source" in {finding.code for finding in report.warnings}
    assert not report.blocks
    assert report.publishable is True


def test_failed_llm_recovery_remains_blocked(channel):
    carousel = make_carousel()
    pair = DuplicatePair(
        score=0.91,
        slide_a=1,
        slide_b=2,
        text_a=carousel.cover.subtitle,
        text_b=carousel.slides[1].body[0],
        severity="block",
    )
    editor = object.__new__(InstagramEditor)
    editor.channel = channel
    editor.ask_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AgentError("fixture failure", agent="instagram_editor")
    )

    failed = editor.repair_cover_duplicates(carousel, [pair], topic=make_topic())
    report = CarouselValidator(channel).validate(failed, topic=make_topic())
    block_codes = {finding.code for finding in report.blocks}

    assert "llm_recovery_failed" in block_codes
    assert "cover_rewrite_failed" in block_codes
    assert report.publishable is False
