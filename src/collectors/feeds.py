"""Feed parsing.

``feedparser`` is used when available; otherwise a small ElementTree-based
parser handles RSS 2.0 and Atom, which is all our sources emit.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.collectors.extract import html_to_text
from src.core.errors import CollectorError
from src.core.logging_setup import get_logger
from src.core.text import clean_text, parse_datetime

logger = get_logger(__name__)

_ATOM = "{http://www.w3.org/2005/Atom}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_CONTENT = "{http://purl.org/rss/1.0/modules/content/}"


@dataclass
class FeedEntry:
    """Normalised feed entry, independent of the parsing backend."""

    title: str
    url: str
    summary: str = ""
    content: str = ""
    published_at: datetime | None = None
    author: str | None = None
    tags: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _first(parent: ET.Element, *paths: str) -> ET.Element | None:
    for path in paths:
        found = parent.find(path)
        if found is not None:
            return found
    return None


def _atom_link(entry: ET.Element) -> str:
    fallback = ""
    for link in entry.findall(f"{_ATOM}link"):
        rel = link.get("rel", "alternate")
        href = link.get("href", "")
        if not href:
            continue
        if rel == "alternate":
            return href
        fallback = fallback or href
    return fallback


def _parse_with_elementtree(xml_text: str) -> list[FeedEntry]:
    try:
        root = ET.fromstring(xml_text)  # noqa: S314 - trusted-ish feeds, no entity expansion
    except ET.ParseError as exc:
        raise CollectorError(f"Malformed feed XML: {exc}") from exc

    entries: list[FeedEntry] = []

    # RSS 2.0
    for item in root.iter("item"):
        title = _text(_first(item, "title", f"{_DC}title"))
        link = _text(item.find("link")) or _text(item.find("guid"))
        summary = _text(item.find("description"))
        content = _text(_first(item, f"{_CONTENT}encoded")) or summary
        published = _text(_first(item, "pubDate", f"{_DC}date"))
        author = _text(_first(item, "author", f"{_DC}creator")) or None
        tags = [_text(c) for c in item.findall("category") if _text(c)]
        entries.append(
            FeedEntry(
                title=clean_text(title),
                url=str(link).strip(),
                summary=clean_text(html_to_text(summary)),
                content=clean_text(html_to_text(content)),
                published_at=parse_datetime(published),
                author=author,
                tags=tags,
            )
        )

    # Atom
    for item in root.iter(f"{_ATOM}entry"):
        title = _text(item.find(f"{_ATOM}title"))
        summary = _text(item.find(f"{_ATOM}summary"))
        content = _text(item.find(f"{_ATOM}content")) or summary
        published = _text(_first(item, f"{_ATOM}published", f"{_ATOM}updated"))
        author_el = item.find(f"{_ATOM}author")
        author = _text(author_el.find(f"{_ATOM}name")) if author_el is not None else None
        tags = [c.get("term", "") for c in item.findall(f"{_ATOM}category") if c.get("term")]
        entries.append(
            FeedEntry(
                title=clean_text(title),
                url=_atom_link(item),
                summary=clean_text(html_to_text(summary)),
                content=clean_text(html_to_text(content)),
                published_at=parse_datetime(published),
                author=author or None,
                tags=tags,
            )
        )

    return entries


def _parse_with_feedparser(xml_text: str) -> list[FeedEntry]:
    import feedparser  # type: ignore[import-not-found]

    parsed = feedparser.parse(xml_text)
    if getattr(parsed, "bozo", 0) and not parsed.entries:
        raise CollectorError(f"Feed could not be parsed: {getattr(parsed, 'bozo_exception', '')}")

    entries: list[FeedEntry] = []
    for entry in parsed.entries:
        content = ""
        if getattr(entry, "content", None):
            content = " ".join(part.get("value", "") for part in entry.content)
        summary = entry.get("summary", "") or entry.get("description", "")
        published = (
            entry.get("published")
            or entry.get("updated")
            or entry.get("created")
            or entry.get("pubDate")
        )
        tags = [t.get("term", "") for t in entry.get("tags", []) if t.get("term")]
        entries.append(
            FeedEntry(
                title=clean_text(entry.get("title", "")),
                url=(entry.get("link") or entry.get("id") or "").strip(),
                summary=clean_text(html_to_text(summary)),
                content=clean_text(html_to_text(content or summary)),
                published_at=parse_datetime(published),
                author=entry.get("author") or None,
                tags=tags,
            )
        )
    return entries


def parse_feed(xml_text: str) -> list[FeedEntry]:
    """Parse an RSS/Atom document into :class:`FeedEntry` objects."""
    if not xml_text or not xml_text.strip():
        return []
    try:
        return _parse_with_feedparser(xml_text)
    except ImportError:
        logger.debug("feedparser not installed; using the ElementTree fallback")
    except CollectorError:
        raise
    except Exception as exc:  # noqa: BLE001 - fall back rather than fail the source
        logger.debug("feedparser failed (%s); using the ElementTree fallback", exc)
    return _parse_with_elementtree(xml_text)
