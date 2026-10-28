"""Collectors: feed parsing, sample fixtures, registry wiring."""

from __future__ import annotations

from src.collectors.base import CollectSettings
from src.collectors.feeds import parse_feed
from src.collectors.registry import COLLECTOR_TYPES, build_collectors
from src.collectors.sample import SampleCollector

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Example</title>
  <item>
    <title>vLLM v1.2.3 released</title>
    <link>https://example.com/vllm-123</link>
    <description>&lt;p&gt;Throughput up 30%.&lt;/p&gt;</description>
    <pubDate>Fri, 18 Sep 2026 10:00:00 GMT</pubDate>
    <category>release</category>
  </item>
</channel></rss>
"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom</title>
  <entry>
    <title>Devnet-9 is live</title>
    <link rel="alternate" href="https://example.org/devnet-9"/>
    <summary>Blob target moves from 6 to 12.</summary>
    <published>2026-09-18T10:00:00Z</published>
    <author><name>EF</name></author>
  </entry>
</feed>
"""


class TestFeedParsing:
    def test_rss(self):
        entries = parse_feed(RSS)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.title == "vLLM v1.2.3 released"
        assert entry.url == "https://example.com/vllm-123"
        assert "30%" in entry.summary
        assert "<p>" not in entry.summary
        assert entry.published_at is not None

    def test_atom(self):
        entries = parse_feed(ATOM)
        assert len(entries) == 1
        assert entries[0].url == "https://example.org/devnet-9"
        assert entries[0].published_at is not None

    def test_empty_input(self):
        assert parse_feed("") == []

    def test_malformed_input_does_not_crash(self):
        # feedparser tolerates junk; the fallback raises CollectorError. Both are fine
        # as long as nothing unexpected escapes.
        from src.core.errors import CollectorError

        try:
            parse_feed("<<<not xml")
        except CollectorError:
            pass


class TestSampleCollector:
    def test_loads_bundled_fixtures(self, config):
        collector = SampleCollector(
            config.paths.samples, settings=CollectSettings(max_content_chars=5000)
        )
        result = collector.run()
        assert result.ok
        assert result.count >= 10
        categories = {item.category for item in result.items}
        assert categories == {"ai", "blockchain"}
        assert all(item.url and item.title for item in result.items)
        assert all(item.published_at is not None for item in result.items)

    def test_fixtures_are_marked_as_samples(self, config):
        collector = SampleCollector(config.paths.samples)
        for item in collector.run().items:
            assert "SAMPLE FIXTURE" in item.content

    def test_missing_directory_is_reported_not_raised(self, tmp_path):
        result = SampleCollector(tmp_path / "nope").run()
        assert result.ok is False
        assert result.error and "not found" in result.error


class TestRegistry:
    def test_all_configured_types_are_registered(self, config):
        assert {s.type for s in config.sources} <= set(COLLECTOR_TYPES)

    def test_offline_mode_returns_only_sample_collector(self, config):
        collectors = build_collectors(config, offline=True)
        assert len(collectors) == 1
        assert isinstance(collectors[0], SampleCollector)

    def test_live_mode_builds_enabled_sources(self, config):
        collectors = build_collectors(config)
        assert len(collectors) == len(config.enabled_sources())

    def test_category_filter(self, config):
        collectors = build_collectors(config, categories=["blockchain"])
        assert {c.source.category for c in collectors} == {"blockchain"}
