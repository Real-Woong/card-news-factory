"""Config loading, text utilities and domain models."""

from __future__ import annotations

import os

import pytest

from src.core.config import load_config
from src.core.errors import ConfigError
from src.core.text import (
    canonicalize_url,
    chunk_text,
    clean_text,
    entity_overlap,
    normalize_title,
    parse_datetime,
    title_similarity,
    url_hash,
)


class TestConfig:
    def test_loads_all_sections(self, config):
        assert config.sources, "sources.yaml produced no entries"
        assert config.channels, "channels.yaml produced no entries"
        assert config.ranking.weights
        assert set(config.categories) == {"ai", "blockchain"}

    def test_stage_list_matches_requirement(self, config):
        assert config.stages == [
            "collect",
            "normalize",
            "store",
            "deduplicate",
            "update_rag",
            "rank",
            "research",
            "fact_check",
            "generate_drafts",
            "generate_review",
        ]

    def test_dotted_get(self, config):
        assert isinstance(config.get("dedup.title_threshold"), float)
        assert config.get("nope.missing", "fallback") == "fallback"

    def test_every_source_has_a_known_trust_tier(self, config):
        tiers = set(config.ranking.trust_scores)
        assert {s.trust for s in config.sources} <= tiers

    def test_trust_scores_match_spec(self, config):
        assert config.ranking.trust_scores == {
            "official": 5,
            "github": 5,
            "paper": 4,
            "major_news": 3,
            "community": 1,
            "social": 1,
        }

    def test_no_channel_publishes_by_default(self, config):
        assert all(channel.publish is False for channel in config.channels)

    def test_env_placeholder_uses_default(self, monkeypatch):
        monkeypatch.delenv("MAX_SELECTED", raising=False)
        cfg = load_config(load_env=False)
        assert cfg.get("rank.max_selected") == 5

    def test_env_placeholder_override(self, monkeypatch):
        monkeypatch.setenv("MAX_SELECTED", "3")
        cfg = load_config(load_env=False)
        assert cfg.get("rank.max_selected") == 3
        os.environ.pop("MAX_SELECTED", None)

    def test_missing_config_dir_raises(self, tmp_path):
        with pytest.raises(ConfigError):
            load_config(tmp_path, load_env=False)


class TestText:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://WWW.Example.com/post/?utm_source=x", "https://example.com/post"),
            ("https://example.com/post/#section", "https://example.com/post"),
            ("https://example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2"),
        ],
    )
    def test_canonicalize_url(self, raw, expected):
        assert canonicalize_url(raw) == expected

    def test_url_hash_ignores_tracking_params(self):
        assert url_hash("https://example.com/x?utm_campaign=a") == url_hash(
            "https://example.com/x/"
        )

    def test_normalize_title_drops_stopwords(self):
        assert normalize_title("The New Release of the API!") == "release api"

    def test_title_similarity(self):
        assert title_similarity("vLLM v0.9.0 released", "vLLM v0.9.0 released") == 1.0
        assert title_similarity("vLLM v0.9.0 released", "Ethereum devnet-9 live") < 0.4

    def test_title_similarity_is_symmetric(self):
        a = "Structured outputs are now generally available in the Responses API"
        b = "Structured outputs reach GA: what it means for open model serving"
        assert title_similarity(a, b) == title_similarity(b, a)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            # Real titles that previously collided: different editions of one series.
            ("ollama/ollama v0.34.3-rc1", "ollama/ollama: v0.34.1"),
            ("Solana Changelog: September 18, 2026", "Solana Changelog: September 3, 2026"),
            ("MystenLabs/sui: testnet-v1.80.0", "MystenLabs/sui: devnet-v1.80.0"),
            ("anza-xyz/agave: Release v4.3.0", "anza-xyz/agave: Release v4.3.0-rc.1"),
        ],
    )
    def test_different_editions_are_not_duplicates(self, left, right):
        # Must stay below the configured dedup threshold (0.82) so every release
        # is collected, while still scoring high enough to be reported.
        assert title_similarity(left, right) <= 0.6

    def test_identical_headline_is_a_duplicate(self):
        assert (
            title_similarity(
                "Structured outputs are now generally available",
                "Structured Outputs Are Now Generally Available!",
            )
            >= 0.82
        )

    @pytest.mark.parametrize(
        ("left", "right", "expected"),
        [
            ("ollama/ollama v0.34.3-rc1", "ollama/ollama: v0.34.1", 1.0),
            ("MystenLabs/sui: testnet-v1.80.0", "MystenLabs/sui: devnet-v1.80.0", 0.6),
            ("vllm-project/vllm v0.99.0", "ethereum/go-ethereum v1.99.0", 0.0),
            ("Our framework for reporting misalignment", "Helping older adults use AI", 0.0),
        ],
    )
    def test_entity_overlap(self, left, right, expected):
        assert entity_overlap(left, right) == pytest.approx(expected, abs=0.1)

    def test_clean_text_truncates(self):
        assert clean_text("a" * 100, max_chars=10).endswith("…")

    def test_chunk_text_overlaps(self):
        chunks = chunk_text("x" * 3000, chunk_size=1000, overlap=100)
        assert len(chunks) >= 3
        assert all(len(chunk) <= 1000 for chunk in chunks)

    @pytest.mark.parametrize(
        "raw",
        [
            "2026-09-18T10:00:00Z",
            "2026-09-18T10:00:00+00:00",
            "Fri, 18 Sep 2026 10:00:00 GMT",
        ],
    )
    def test_parse_datetime_formats(self, raw):
        parsed = parse_datetime(raw)
        assert parsed is not None and parsed.year == 2026

    def test_parse_datetime_garbage(self):
        assert parse_datetime("not a date") is None


class TestModels:
    def test_article_derives_hashes(self, article):
        assert article.key == article.url_hash_value
        assert article.canonical_url == "https://openai.com/index/example"
        assert article.content_hash_value
        assert article.domain == "openai.com"

    def test_reference_is_json_safe(self, article):
        import json

        json.dumps(article.reference())
