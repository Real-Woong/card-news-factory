"""Collector registry: maps ``sources.yaml -> type`` to an implementation."""

from __future__ import annotations

from src.collectors.arxiv import ArxivCollector
from src.collectors.base import CollectSettings, Collector
from src.collectors.github import GithubReleasesCollector
from src.collectors.http import HttpClient, HttpSettings
from src.collectors.rss import RssCollector
from src.collectors.sample import SampleCollector
from src.core.config import AppConfig, SourceConfig
from src.core.errors import ConfigError
from src.core.logging_setup import get_logger

logger = get_logger(__name__)

COLLECTOR_TYPES: dict[str, type[Collector]] = {
    RssCollector.type_name: RssCollector,
    GithubReleasesCollector.type_name: GithubReleasesCollector,
    ArxivCollector.type_name: ArxivCollector,
    SampleCollector.type_name: SampleCollector,
}


def build_http_client(config: AppConfig) -> HttpClient:
    return HttpClient(
        HttpSettings(
            timeout=float(config.get("collect.http_timeout_seconds", 20)),
            max_retries=int(config.get("collect.max_retries", 2)),
            delay_seconds=float(config.get("collect.request_delay_seconds", 1.0)),
        )
    )


def build_collect_settings(config: AppConfig) -> CollectSettings:
    return CollectSettings(
        max_content_chars=int(config.get("collect.max_content_chars", 20000)),
        fetch_full_content=bool(config.get("collect.fetch_full_content", True)),
        max_total_items=int(config.get("collect.max_total_items", 200)),
    )


def build_collector(
    source: SourceConfig, *, http: HttpClient, settings: CollectSettings
) -> Collector:
    """Instantiate the collector for one source.

    Raises:
        ConfigError: unknown ``type``.
    """
    collector_cls = COLLECTOR_TYPES.get(source.type)
    if collector_cls is None:
        raise ConfigError(
            f"source `{source.id}` uses unknown type `{source.type}` "
            f"(known: {sorted(COLLECTOR_TYPES)})"
        )
    return collector_cls(source, http=http, settings=settings)


def build_collectors(
    config: AppConfig,
    *,
    categories: list[str] | None = None,
    offline: bool = False,
) -> list[Collector]:
    """Build every enabled collector, or the single sample collector when offline."""
    settings = build_collect_settings(config)

    if offline:
        sample_dir = config.paths.resolve(config.get("collect.sample_dir", "data/samples"))
        return [SampleCollector(sample_dir, settings=settings)]

    http = build_http_client(config)
    collectors: list[Collector] = []
    for source in config.enabled_sources(categories=categories):
        try:
            collectors.append(build_collector(source, http=http, settings=settings))
        except ConfigError as exc:
            logger.warning("Skipping source: %s", exc)
    return collectors
