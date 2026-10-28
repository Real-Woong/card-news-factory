"""Collectors: pull items from external sources into :class:`RawItem` objects."""

from src.collectors.arxiv import ArxivCollector
from src.collectors.base import CollectSettings, Collector, CollectorResult
from src.collectors.github import GithubReleasesCollector
from src.collectors.http import HttpClient, HttpSettings
from src.collectors.registry import COLLECTOR_TYPES, build_collector, build_collectors
from src.collectors.rss import RssCollector
from src.collectors.sample import SampleCollector

__all__ = [
    "COLLECTOR_TYPES",
    "ArxivCollector",
    "CollectSettings",
    "Collector",
    "CollectorResult",
    "GithubReleasesCollector",
    "HttpClient",
    "HttpSettings",
    "RssCollector",
    "SampleCollector",
    "build_collector",
    "build_collectors",
]
