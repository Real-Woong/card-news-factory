"""Structured source evidence and claim-specific chunk retrieval.

The public article body remains bounded by ``collect.max_content_chars``.  This
module preserves small, structured facts separately so links and high-value
sections are not lost merely because they occur near the end of a long page.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

from src.core.text import clean_text, to_iso, utcnow

EVIDENCE_VERSION = 1
EXTRACTION_VERSION = 1
REQUIRED_EVIDENCE_FIELDS = frozenset(
    {
        "canonical_url",
        "outbound_links",
        "repository_links",
        "release_links",
        "headings",
        "chunks",
        "evidence_updated_at",
    }
)

_URL = re.compile(r"https?://[^\s<>\]\[)\"']+")
_VERSION = re.compile(r"\b(?:v?\d+\.\d+(?:\.\d+)?(?:[-+][\w.-]+)?)\b", re.I)
_DATE = re.compile(
    r"\b(?:20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2},?\s+20\d{2})\b",
    re.I,
)
_IDENTIFIER = re.compile(
    r"\b(?:CVE-\d{4}-\d+|EIP-\d+|ERC-\d+|PR\s*#?\d+|"
    r"commit\s+[0-9a-f]{7,40}|[A-Z][A-Z0-9_-]{2,}-\d+)\b",
    re.I,
)
_TOKEN = re.compile(r"[A-Za-z0-9가-힣_.%/-]+")
_PRIORITY = re.compile(
    r"release|changelog|availability|rollout|available|benchmark|evaluation|"
    r"performance|reference|repository|github|install|migration|breaking|"
    r"출시|배포|가용|벤치마크|평가|성능|저장소|변경",
    re.I,
)
_NOISE_URL = re.compile(
    r"(?:facebook|twitter|x\.com/intent|linkedin|reddit|mailto:|"
    r"privacy|cookie|terms|share)",
    re.I,
)
_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for",
    "with", "is", "are", "was", "were", "this", "that", "from", "as",
    "it", "be", "by", "at", "now", "new", "를", "을", "이", "가", "은",
    "는", "에", "의", "및", "로", "으로", "변경", "지원",
}


def _unique(values: list[str], *, limit: int = 100) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = value.strip().rstrip(".,;:")
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def source_relation(url: str) -> str | None:
    """Classify only relationships that can provide meaningful corroboration."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.lower()
    if host == "github.com" and "/releases" in path:
        return "implementation_artifact"
    if host == "github.com":
        return "repository"
    if host.startswith("docs.") or "/docs/" in path or path.startswith("/docs"):
        return "documentation"
    if "changelog" in path or "release-notes" in path:
        return "documentation"
    return None


_GENERIC_PATH_WORDS = frozenset(
    {"docs", "doc", "news", "blog", "references", "reference", "core", "tools", "guide", "guides",
     "latest", "stable", "index", "html", "pages", "page", "learn", "intro", "overview"}
)


def _path_words(url: str) -> set[str]:
    """URL 경로의 의미 있는 낱말(4자 이상, 일반 낱말 제외)."""
    words = re.split(r"[^a-z0-9]+", urlparse(url).path.lower())
    return {w for w in words if len(w) >= 4 and w not in _GENERIC_PATH_WORDS}


def is_related_doc(url: str, canonical_url: str) -> bool:
    """문서 링크가 이 기사와 주제가 겹치는가: 경로 낱말이 하나라도 같아야 한다.

    기사 페이지의 메뉴·푸터 링크(예: /docs/tools/actions)가 사건의 근거로 올라오는 것을 막는다.
    """
    return bool(_path_words(url) & _path_words(canonical_url))


def _supporting_sources(links: list[str], canonical_url: str) -> list[dict[str, Any]]:
    canonical_host = urlparse(canonical_url).netloc.lower().removeprefix("www.")
    sources: list[dict[str, Any]] = []
    for raw_url in links:
        url = raw_url.strip().rstrip("\\")  # 마크다운 이스케이프 잔여물(끝의 \) 제거
        relation = source_relation(url)
        if relation not in {"implementation_artifact", "documentation"}:
            continue
        host = urlparse(url).netloc.lower().removeprefix("www.")
        sources.append(
            {
                "name": "GitHub" if host == "github.com" else host,
                "url": url,
                "source_relation": relation,
                "authority": "primary",
                # A release/docs link selected by the announcement is useful event
                # evidence. A bare repository URL is retained but not counted.
                "supports_event": relation == "implementation_artifact"
                or (relation == "documentation" and is_related_doc(url, canonical_url)),
                "discovered_from": canonical_url,
                "same_domain": bool(host and host == canonical_host),
            }
        )
    return sources


def _html_structure(
    raw_html: str, canonical_url: str
) -> tuple[list[str], list[str], list[str], list[str], str]:
    headings: list[str] = []
    list_items: list[str] = []
    tables: list[str] = []
    links: list[str] = []
    page_canonical = ""
    if "<" not in raw_html or ">" not in raw_html:
        return headings, list_items, tables, links, page_canonical
    try:
        from bs4 import BeautifulSoup  # type: ignore[import-not-found]

        soup = BeautifulSoup(raw_html, "html.parser")
        canonical_tag = soup.find("link", rel=lambda value: value and "canonical" in value)
        if canonical_tag is not None:
            page_canonical = urljoin(
                canonical_url, str(canonical_tag.get("href") or "").strip()
            )
        root = soup.find("article") or soup.find("main") or soup.body or soup
        headings = [
            clean_text(tag.get_text(" "), max_chars=300)
            for tag in root.find_all(re.compile(r"^h[1-6]$"))
        ]
        list_items = [clean_text(tag.get_text(" "), max_chars=700) for tag in root.find_all("li")]
        tables = [clean_text(tag.get_text(" | "), max_chars=1800) for tag in root.find_all("table")]
        for tag in root.find_all("a", href=True):
            url = urljoin(canonical_url, str(tag.get("href") or "").strip())
            if url.startswith(("http://", "https://")) and not _NOISE_URL.search(url):
                links.append(url)
    except Exception:  # noqa: BLE001 - evidence enrichment must not break collection
        links.extend(
            urljoin(canonical_url, href)
            for href in re.findall(r"href=[\"']([^\"']+)", raw_html, re.I)
        )
    return (
        _unique(headings),
        _unique(list_items),
        _unique(tables, limit=20),
        _unique(links),
        page_canonical,
    )


def logical_chunks(text: str, *, headings: list[str] | None = None) -> list[dict[str, str]]:
    """Split clean text at paragraph boundaries while retaining section labels."""
    known_headings = {value.lower(): value for value in (headings or [])}
    raw_parts = [clean_text(part, max_chars=1800) for part in re.split(r"\n\s*\n|\n", text)]
    chunks: list[dict[str, str]] = []
    section = "intro"
    pending = ""
    for raw in raw_parts:
        if not raw:
            continue
        if raw.lower() in known_headings:
            if pending:
                chunks.append({"section": section, "kind": "paragraph", "text": pending})
                pending = ""
            section = known_headings[raw.lower()]
            continue
        if len(pending) + len(raw) + 1 <= 1400:
            pending = f"{pending}\n{raw}".strip()
        else:
            if pending:
                chunks.append({"section": section, "kind": "paragraph", "text": pending})
            pending = raw
    if pending:
        chunks.append({"section": section, "kind": "paragraph", "text": pending})
    for index, chunk in enumerate(chunks):
        chunk["id"] = f"chunk-{index:03d}"
    return chunks[:120]


def section_aware_excerpt(
    chunks: list[dict[str, str]], *, title: str, max_chars: int
) -> str:
    """Keep intro plus high-value sections without increasing the body limit."""
    if not chunks:
        return clean_text(title, max_chars=max_chars)
    full = "\n\n".join(chunk["text"] for chunk in chunks)
    if len(full) <= max_chars:
        return full
    ranked = sorted(
        enumerate(chunks),
        key=lambda item: (
            0 if item[0] < 2 else 1,
            0 if _PRIORITY.search(f"{item[1].get('section', '')} {item[1]['text']}") else 1,
            item[0],
        ),
    )
    selected: list[tuple[int, str]] = []
    used = len(title) + 2
    for index, chunk in ranked:
        section = chunk.get("section") or ""
        text = chunk["text"]
        rendered = f"{section}\n{text}" if section not in {"", "intro"} else text
        if used + len(rendered) + 2 > max_chars:
            continue
        selected.append((index, rendered))
        used += len(rendered) + 2
    selected.sort()
    return "\n\n".join(text for _, text in selected)


def build_source_metadata(
    *, raw_content: str, plain_text: str, canonical_url: str, title: str, max_chars: int
) -> tuple[str, dict[str, Any]]:
    """Return a bounded body and separately preserved structured evidence."""
    headings, list_items, tables, hrefs, page_canonical = _html_structure(
        raw_content, canonical_url
    )
    links = _unique([*hrefs, *_URL.findall(raw_content), *_URL.findall(plain_text)])
    chunks = logical_chunks(plain_text, headings=headings)
    for label, values in (("list_item", list_items), ("table", tables)):
        for value in values:
            chunks.append(
                {
                    "id": f"chunk-{len(chunks):03d}",
                    "section": label,
                    "kind": label,
                    "text": value,
                }
            )
    body = section_aware_excerpt(chunks, title=title, max_chars=max_chars)
    metadata: dict[str, Any] = {
        "evidence_version": EVIDENCE_VERSION,
        "extraction_version": EXTRACTION_VERSION,
        "canonical_url": page_canonical or canonical_url,
        "original_content_chars": len(plain_text),
        "content_truncated": len(plain_text) > len(body),
        "outbound_links": links,
        "repository_links": [
            url
            for url in links
            if urlparse(url).netloc.lower().removeprefix("www.") == "github.com"
        ],
        "release_links": [
            url
            for url in links
            if source_relation(url) == "implementation_artifact"
        ],
        "version_strings": _unique(_VERSION.findall(plain_text)),
        "dates": _unique(_DATE.findall(plain_text)),
        "identifiers": _unique(_IDENTIFIER.findall(plain_text)),
        "headings": headings,
        "list_items": list_items[:40],
        "tables": tables[:10],
        "chunks": chunks,
    }
    metadata["evidence_updated_at"] = to_iso(utcnow())
    metadata["supporting_sources"] = _supporting_sources(links, canonical_url)
    return body, metadata


def evidence_backfill_reasons(metadata: dict[str, Any] | None) -> list[str]:
    """Explain why stored evidence needs the current extraction contract."""
    evidence = metadata or {}
    reasons: list[str] = []
    try:
        evidence_version = int(evidence.get("evidence_version") or 0)
    except (TypeError, ValueError):
        evidence_version = 0
    try:
        extraction_version = int(evidence.get("extraction_version") or 0)
    except (TypeError, ValueError):
        extraction_version = 0
    if evidence_version < EVIDENCE_VERSION:
        reasons.append("evidence_version_outdated")
    if extraction_version < EXTRACTION_VERSION:
        reasons.append("extraction_version_outdated")
    missing = sorted(REQUIRED_EVIDENCE_FIELDS - set(evidence))
    reasons.extend(f"missing_{field}" for field in missing)
    return reasons


def retrieve_claim_evidence(
    claim: str, source_metadata: dict[str, Any], *, top_k: int = 2
) -> list[dict[str, str]]:
    """Return the most relevant logical chunks for one claim."""
    claim_tokens = {
        token.lower()
        for token in _TOKEN.findall(claim)
        if len(token) > 1 and token.lower() not in _STOPWORDS
    }
    identifiers = (
        set(_VERSION.findall(claim))
        | set(_DATE.findall(claim))
        | set(_IDENTIFIER.findall(claim))
    )
    ranked: list[tuple[float, int, dict[str, str]]] = []
    for index, raw in enumerate(source_metadata.get("chunks") or []):
        if not isinstance(raw, dict) or not raw.get("text"):
            continue
        text = str(raw["text"])
        tokens = {token.lower() for token in _TOKEN.findall(text)}
        overlap = len(claim_tokens & tokens)
        identifier_hits = sum(1 for value in identifiers if value and value.lower() in text.lower())
        score = overlap + identifier_hits * 4
        if score <= 0:
            continue
        ranked.append((score, -index, raw))
    ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
    return [
        {
            "chunk_id": str(raw.get("id") or ""),
            "section": str(raw.get("section") or ""),
            "excerpt": clean_text(str(raw.get("text") or ""), max_chars=700),
        }
        for _, _, raw in ranked[:top_k]
    ]
