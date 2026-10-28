"""RSS / Atom collector (official blogs, newsrooms)."""

from __future__ import annotations

from src.collectors.base import Collector
from src.collectors.extract import html_to_text
from src.core.evidence import build_source_metadata
from src.collectors.feeds import parse_feed
from src.core.errors import CollectorError
from src.core.models import RawItem
from src.core.text import clean_text


class RssCollector(Collector):
    """Fetch a feed and optionally enrich entries with the full article body."""

    type_name = "rss"

    def fetch(self) -> list[RawItem]:
        if not self.source.url:
            raise CollectorError(f"source `{self.source.id}` has no url", source_id=self.source.id)

        body = self.http.get_text(
            self.source.url,
            headers={"Accept": "application/rss+xml, application/atom+xml, application/xml"},
            timeout=self.source.timeout_seconds,
        )
        entries = parse_feed(body)
        if not entries:
            self.log.debug("[%s] feed returned no entries", self.source.id)

        items: list[RawItem] = []
        for entry in entries[: self.source.max_items]:
            if not entry.url or not entry.title:
                continue
            content = entry.content or entry.summary
            source_metadata = None
            if self.settings.fetch_full_content:
                fetched = self._fetch_article_document(entry.url, title=entry.title)
                if fetched is not None:
                    content, source_metadata = fetched
            items.append(
                self.make_item(
                    url=entry.url,
                    title=entry.title,
                    summary=entry.summary or content[:400],
                    content=content,
                    published_at=entry.published_at,
                    author=entry.author,
                    tags=entry.tags,
                    raw_payload={
                        "feed_url": self.source.url,
                        **(
                            {"source_metadata": source_metadata}
                            if source_metadata
                            else {}
                        ),
                    },
                )
            )
        return items

    def _fetch_article_document(
        self, url: str, *, title: str
    ) -> tuple[str, dict] | None:
        """Best-effort body plus structured evidence fetch."""
        try:
            html = self.http.get_text(
                url, headers={"Accept": "text/html"}, timeout=self.source.timeout_seconds
            )
        except CollectorError as exc:
            self.log.debug("[%s] full-text fetch failed for %s: %s", self.source.id, url, exc)
            return None
        plain_text = clean_text(html_to_text(html))
        return build_source_metadata(
            raw_content=html,
            plain_text=plain_text,
            canonical_url=url,
            title=title,
            max_chars=self.settings.max_content_chars,
        )
