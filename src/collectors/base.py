"""Collector interface.

Adding a source type means:

1. subclass :class:`Collector`, implement ``fetch()``
2. register it in ``src/collectors/registry.py``
3. reference the new ``type:`` in ``config/sources.yaml``

``Collector.run()`` wraps ``fetch()`` so a broken source degrades into a logged
:class:`CollectorResult` with ``ok=False`` instead of killing the run.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from src.core.config import SourceConfig
from src.core.errors import CollectorError
from src.core.logging_setup import get_logger
from src.core.models import RawItem
from src.core.evidence import build_source_metadata, evidence_backfill_reasons
from src.core.text import age_days, clean_text, utcnow
from src.collectors.http import HttpClient

logger = get_logger(__name__)


@dataclass
class CollectSettings:
    """Runtime knobs shared by all collectors (from ``pipeline.yaml -> collect``)."""

    max_content_chars: int = 20000
    fetch_full_content: bool = True
    max_total_items: int = 200


@dataclass
class CollectorResult:
    """Outcome of one source."""

    source_id: str
    items: list[RawItem] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    duration_seconds: float = 0.0
    skipped: int = 0

    @property
    def count(self) -> int:
        return len(self.items)


class Collector(ABC):
    """Base class for all collectors."""

    type_name: ClassVar[str] = "base"

    def __init__(
        self,
        source: SourceConfig,
        *,
        http: HttpClient,
        settings: CollectSettings | None = None,
    ) -> None:
        self.source = source
        self.http = http
        self.settings = settings or CollectSettings()
        self.log = get_logger(f"{__name__}.{self.type_name}")

    # ------------------------------------------------------------------ public
    def run(self) -> CollectorResult:
        """Fetch items, never raising."""
        started = time.monotonic()
        try:
            items = self.fetch()
        except CollectorError as exc:
            self.log.warning("[%s] %s", self.source.id, exc)
            return CollectorResult(
                source_id=self.source.id,
                ok=False,
                error=str(exc),
                duration_seconds=time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 - a bad source must not abort the run
            self.log.exception("[%s] unexpected collector failure", self.source.id)
            return CollectorResult(
                source_id=self.source.id,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_seconds=time.monotonic() - started,
            )

        fresh, skipped = self._filter_recent(items)
        capped = fresh[: self.source.max_items]
        self.log.info(
            "[%s] %d item(s) (%d too old, %d over cap)",
            self.source.id,
            len(capped),
            skipped,
            max(len(fresh) - len(capped), 0),
        )
        return CollectorResult(
            source_id=self.source.id,
            items=capped,
            duration_seconds=time.monotonic() - started,
            skipped=skipped,
        )

    @abstractmethod
    def fetch(self) -> list[RawItem]:
        """Retrieve items from the source.

        Raises:
            CollectorError: on any network/parse problem.
        """

    # ----------------------------------------------------------------- helpers
    def _filter_recent(self, items: list[RawItem]) -> tuple[list[RawItem], int]:
        """Drop items older than ``lookback_days`` (undated items are kept)."""
        now = utcnow()
        lookback = self.source.lookback_days
        kept: list[RawItem] = []
        skipped = 0
        for item in items:
            if item.published_at is None:
                kept.append(item)
                continue
            if age_days(item.published_at, now=now) <= lookback:
                kept.append(item)
            else:
                skipped += 1
        return kept, skipped

    def make_item(
        self,
        *,
        url: str,
        title: str,
        summary: str = "",
        content: str = "",
        published_at: datetime | None = None,
        author: str | None = None,
        tags: list[str] | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> RawItem:
        """Build a :class:`RawItem` carrying this source's metadata."""
        if not url or not title:
            raise CollectorError(
                f"Item from `{self.source.id}` is missing a url or title",
                source_id=self.source.id,
            )
        payload = dict(raw_payload or {})
        metadata = dict(payload.get("source_metadata") or {})
        if metadata and not evidence_backfill_reasons(metadata):
            bounded_content = content
        else:
            bounded_content, metadata = build_source_metadata(
                raw_content=content,
                plain_text=clean_text(content),
                canonical_url=url.strip(),
                title=title,
                max_chars=self.settings.max_content_chars,
            )
            payload["source_metadata"] = metadata
        return RawItem(
            source_id=self.source.id,
            source_name=self.source.name,
            source_type=self.source.type,
            trust_tier=self.source.trust,
            category=self.source.category,
            url=url.strip(),
            title=clean_text(title, max_chars=300),
            summary=clean_text(summary, max_chars=1200),
            content=clean_text(
                bounded_content, max_chars=self.settings.max_content_chars
            ),
            published_at=published_at,
            author=author,
            tags=tags or [],
            raw_payload=payload,
        )
