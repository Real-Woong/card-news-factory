"""arXiv collector (Atom API).

API docs: https://info.arxiv.org/help/api/user-manual.html
Sorted by submission date so we always see the newest preprints first.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from src.collectors.base import Collector
from src.collectors.feeds import parse_feed
from src.core.errors import CollectorError
from src.core.models import RawItem

ENDPOINT = (
    "http://export.arxiv.org/api/query"
    "?search_query={query}&start=0&max_results={max_results}"
    "&sortBy=submittedDate&sortOrder=descending"
)


class ArxivCollector(Collector):
    """Fetch recent preprints for a search query."""

    type_name = "arxiv"

    def fetch(self) -> list[RawItem]:
        query = self.source.query
        if not query:
            raise CollectorError(
                f"source `{self.source.id}` has no query", source_id=self.source.id
            )

        url = ENDPOINT.format(
            query=quote_plus(query), max_results=min(self.source.max_items, 50)
        )
        body = self.http.get_text(url, timeout=self.source.timeout_seconds)
        entries = parse_feed(body)

        items: list[RawItem] = []
        for entry in entries:
            if not entry.url or not entry.title:
                continue
            abstract = entry.summary or entry.content
            items.append(
                self.make_item(
                    url=entry.url,
                    title=entry.title,
                    summary=abstract[:600],
                    content=f"Abstract:\n{abstract}",
                    published_at=entry.published_at,
                    author=entry.author,
                    tags=["paper", *entry.tags[:5]],
                    raw_payload={"arxiv_query": query},
                )
            )
        return items
