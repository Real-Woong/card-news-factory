"""Offline sample collector.

Reads JSON fixtures from ``data/samples/*.json`` so the whole pipeline can run
with no network access (``--dry-run`` / ``--offline``). Each fixture is either a
single object or a list of objects::

    {
      "source_id": "openai_news",
      "source_name": "OpenAI News",
      "source_type": "rss",
      "trust": "official",
      "category": "ai",
      "url": "https://openai.com/index/example/",
      "title": "...",
      "summary": "...",
      "content": "...",
      "published_days_ago": 1,      # or "published_at": "2026-09-18T10:00:00Z"
      "tags": ["api"]
    }

``published_days_ago`` keeps fixtures permanently "fresh", so recency scoring
and the lookback filter behave the same no matter when the sample runs.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from src.collectors.base import CollectSettings, Collector, CollectorResult
from src.core.config import SourceConfig
from src.core.errors import CollectorError
from src.core.evidence import build_source_metadata
from src.core.logging_setup import get_logger
from src.core.models import RawItem
from src.core.text import clean_text, parse_datetime, utcnow

logger = get_logger(__name__)

SAMPLE_SOURCE = SourceConfig(
    id="sample",
    name="Bundled sample data",
    type="sample",
    category="ai",
    trust="official",
    max_items=1000,
    lookback_days=3650,
)


class SampleCollector(Collector):
    """Load fixtures from disk; each item keeps its own source metadata."""

    type_name = "sample"

    def __init__(
        self,
        sample_dir: Path,
        *,
        settings: CollectSettings | None = None,
        source: SourceConfig | None = None,
    ) -> None:
        super().__init__(source or SAMPLE_SOURCE, http=None, settings=settings)  # type: ignore[arg-type]
        self.sample_dir = sample_dir

    def fetch(self) -> list[RawItem]:
        if not self.sample_dir.exists():
            raise CollectorError(
                f"Sample directory not found: {self.sample_dir}", source_id=self.source.id
            )

        payloads: list[dict[str, Any]] = []
        for path in sorted(self.sample_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning("Skipping unreadable sample %s: %s", path, exc)
                continue
            if isinstance(data, list):
                payloads.extend(d for d in data if isinstance(d, dict))
            elif isinstance(data, dict):
                payloads.append(data)

        if not payloads:
            raise CollectorError(
                f"No usable JSON fixtures in {self.sample_dir}", source_id=self.source.id
            )

        items = [self._to_item(p) for p in payloads]
        return [item for item in items if item is not None]

    def _to_item(self, payload: dict[str, Any]) -> RawItem | None:
        url = str(payload.get("url") or "").strip()
        title = str(payload.get("title") or "").strip()
        if not url or not title:
            logger.warning("Sample item without url/title skipped: %r", payload.get("title"))
            return None

        published = parse_datetime(payload.get("published_at"))
        if published is None and payload.get("published_days_ago") is not None:
            try:
                days = float(payload["published_days_ago"])
            except (TypeError, ValueError):
                days = 1.0
            published = utcnow() - timedelta(days=days)

        raw_content = str(payload.get("content") or "")
        content, source_metadata = build_source_metadata(
            raw_content=raw_content,
            plain_text=clean_text(raw_content),
            canonical_url=url,
            title=title,
            max_chars=self.settings.max_content_chars,
        )
        return RawItem(
            source_id=str(payload.get("source_id") or "sample"),
            source_name=str(payload.get("source_name") or payload.get("source_id") or "Sample"),
            source_type=str(payload.get("source_type") or "rss"),
            trust_tier=str(payload.get("trust") or payload.get("trust_tier") or "official"),
            category=str(payload.get("category") or "ai").lower(),
            url=url,
            title=clean_text(title, max_chars=300),
            summary=clean_text(str(payload.get("summary") or ""), max_chars=1200),
            content=content,
            published_at=published,
            author=payload.get("author"),
            tags=[str(t) for t in (payload.get("tags") or [])],
            raw_payload={"fixture": True, "source_metadata": source_metadata},
        )

    def run(self) -> CollectorResult:  # noqa: D102 - inherit docs
        result = super().run()
        if result.ok:
            logger.info("Loaded %d sample item(s) from %s", result.count, self.sample_dir)
        return result
