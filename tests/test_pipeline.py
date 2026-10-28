"""End-to-end pipeline tests (offline, mock LLM, isolated temp directories)."""

from __future__ import annotations

import json

import pytest

from src.core.config import load_config
from src.pipeline.run import build_parser, build_context, finalize, run_pipeline

EXPECTED_FILES = {
    "research.json",
    "selected_topics.json",
    "sources.md",
    "post_longform.md",
    "post_shortform.md",
    "review.md",
    "run_metadata.json",
}


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Real config, but every write redirected into tmp_path."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hashing")
    monkeypatch.setenv("VECTOR_STORE", "local")

    config = load_config(load_env=False)
    config.pipeline["storage"] = {
        "db_path": str(tmp_path / "content.db"),
        "raw_dir": str(tmp_path / "raw"),
        "processed_dir": str(tmp_path / "processed"),
        "vectorstore_dir": str(tmp_path / "vectorstore"),
    }
    config.pipeline["output"] = {
        "drafts_dir": str(tmp_path / "drafts"),
        "archive_dir": str(tmp_path / "archive"),
        "keep_recent_drafts": 0,
    }
    return config


def _run(config, argv: list[str]):
    args = build_parser().parse_args(argv)
    ctx = build_context(config, args)
    run_pipeline(ctx)
    finalize(ctx)
    return ctx


class TestOfflineRun:
    def test_full_pipeline_produces_all_files(self, isolated_config):
        ctx = _run(isolated_config, ["--offline"])

        assert ctx.run.status == "success"
        assert ctx.run.stages_completed == isolated_config.stages
        produced = {path.name for path in ctx.written_files}
        assert EXPECTED_FILES <= produced

    def test_counts_are_sane(self, isolated_config):
        ctx = _run(isolated_config, ["--offline"])
        assert ctx.counts["collected"] >= 10
        assert ctx.counts["normalized"] >= 10
        assert ctx.counts["stored"] >= 10
        assert 0 < ctx.counts["selected"] <= isolated_config.ranking.max_items
        assert ctx.counts["research_notes"] == ctx.counts["selected"]
        assert ctx.counts["fact_checked"] == ctx.counts["selected"]
        assert ctx.counts["drafts"] >= 1
        assert ctx.counts["rag_chunks"] > 0

    def test_both_categories_are_selected(self, isolated_config):
        ctx = _run(isolated_config, ["--offline"])
        categories = {item.article.category for item in ctx.selected}
        assert categories == {"ai", "blockchain"}

    def test_research_json_structure(self, isolated_config):
        ctx = _run(isolated_config, ["--offline"])
        payload = json.loads((ctx.output_dir / "research.json").read_text(encoding="utf-8"))
        assert payload["run_id"] == ctx.run.run_id
        assert payload["items"]
        item = payload["items"][0]
        assert {"article", "research", "fact_check"} <= set(item)
        assert item["research"]["what_changed"]
        assert item["fact_check"]["verdict"]

    def test_selected_topics_json_exposes_score_components(self, isolated_config):
        ctx = _run(isolated_config, ["--offline"])
        payload = json.loads(
            (ctx.output_dir / "selected_topics.json").read_text(encoding="utf-8")
        )
        assert payload["selected"]
        components = payload["selected"][0]["components"]
        assert {"trust", "recency", "topic", "signal", "novelty"} == set(components)

    def test_review_md_contains_checklist_and_verdict(self, isolated_config):
        ctx = _run(isolated_config, ["--offline"])
        text = (ctx.output_dir / "review.md").read_text(encoding="utf-8")
        assert "## 체크리스트" in text
        assert ("게시 추천" in text) or ("게시 보류" in text)
        assert ctx.review is not None

    def test_posts_reference_only_selected_urls(self, isolated_config):
        ctx = _run(isolated_config, ["--offline"])
        assert ctx.review is not None
        invented = [i for i in ctx.review.items if i.kind == "links_resolve"]
        assert invented == [], f"draft contains links outside the selected set: {invented}"

    def test_drafts_stay_within_channel_limits(self, isolated_config):
        ctx = _run(isolated_config, ["--offline"])
        limits = {c.id: c.max_chars for c in isolated_config.channels}
        for draft in ctx.drafts:
            assert draft.char_count <= limits[draft.channel_id]

    def test_database_rows_written(self, isolated_config):
        ctx = _run(isolated_config, ["--offline"])
        from src.storage.db import open_db
        from src.storage.repository import ArticleRepository

        with open_db(isolated_config.db_path()) as conn:
            repo = ArticleRepository(conn)
            counts = repo.counts()
            assert counts["articles"] >= 10
            assert counts["posts"] >= 1
            assert repo.last_successful_run() is not None

    def test_second_run_deduplicates_everything(self, isolated_config):
        first = _run(isolated_config, ["--offline"])
        second = _run(isolated_config, ["--offline"])
        assert first.counts["duplicates"] == 0
        assert second.counts["duplicates"] == second.counts["normalized"]
        assert second.counts.get("kept", 0) == 0


class TestDryRun:
    def test_dry_run_writes_no_database_rows(self, isolated_config):
        ctx = _run(isolated_config, ["--dry-run"])
        assert ctx.mode == "dry-run"
        assert ctx.run.status == "success"

        from src.storage.db import open_db
        from src.storage.repository import ArticleRepository

        with open_db(isolated_config.db_path()) as conn:
            counts = ArticleRepository(conn).counts()
        assert counts["articles"] == 0
        assert counts["posts"] == 0
        assert counts["runs"] == 0

    def test_dry_run_still_writes_drafts(self, isolated_config):
        ctx = _run(isolated_config, ["--dry-run"])
        assert EXPECTED_FILES <= {path.name for path in ctx.written_files}

    def test_dry_run_leaves_no_raw_files(self, isolated_config, tmp_path):
        _run(isolated_config, ["--dry-run"])
        assert not list((tmp_path / "raw").rglob("*.json"))

    def test_dry_run_leaves_vector_store_empty_on_disk(self, isolated_config, tmp_path):
        _run(isolated_config, ["--dry-run"])
        assert not list((tmp_path / "vectorstore").rglob("*.jsonl"))


class TestStageSelection:
    def test_subset_of_stages(self, isolated_config):
        args = build_parser().parse_args(["--offline", "--stages", "collect,normalize"])
        ctx = build_context(isolated_config, args)
        run_pipeline(ctx, stage_names=["collect", "normalize"])
        finalize(ctx)
        assert ctx.run.stages_completed == ["collect", "normalize"]
        assert ctx.articles and not ctx.drafts

    def test_unknown_stage_raises(self, isolated_config):
        from src.core.errors import ConfigError

        args = build_parser().parse_args(["--offline"])
        ctx = build_context(isolated_config, args)
        with pytest.raises(ConfigError):
            run_pipeline(ctx, stage_names=["nope"])
        finalize(ctx)

    def test_category_filter(self, isolated_config):
        args = build_parser().parse_args(["--offline", "--categories", "blockchain"])
        ctx = build_context(isolated_config, args)
        run_pipeline(ctx, stage_names=["collect", "normalize"])
        finalize(ctx)
        # The sample collector ignores category filters at fetch time, so assert the
        # CLI value is wired through rather than the fixture contents.
        assert ctx.categories == ["blockchain"]

    def test_max_items_override(self, isolated_config):
        isolated_config.pipeline.setdefault("rank", {})["max_selected"] = 2
        ctx = _run(isolated_config, ["--offline"])
        assert len(ctx.selected) <= 2


class TestPublisherGuard:
    def test_publishing_is_blocked_by_default(self, isolated_config, tmp_path):
        from src.core.models import Draft
        from src.publishers import FilePublisher, PublishError

        channel = isolated_config.channels[0]
        publisher = FilePublisher(tmp_path / "published")
        with pytest.raises(PublishError):
            publisher.publish(
                Draft(channel_id=channel.id, format=channel.format, title="t", body="b"),
                channel,
            )
