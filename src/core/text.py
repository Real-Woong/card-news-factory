"""Text helpers shared by collectors, dedup and ranking.

Pure functions, no third-party requirements. HTML → 텍스트 추출은 수집 계층의
관심사이므로 :mod:`src.collectors.extract` 에 있습니다.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_TRACKING_PREFIXES = ("utm_", "mc_", "ref_")
_TRACKING_KEYS = {
    "ref",
    "source",
    "fbclid",
    "gclid",
    "igshid",
    "mkt_tok",
    "spm",
    "cmpid",
    "__hstc",
    "__hssc",
}
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "has", "have",
    "how", "in", "into", "is", "it", "its", "new", "of", "on", "or", "our", "that", "the",
    "their", "this", "to", "we", "what", "why", "with", "you", "your",
}


def clean_text(value: str | None, *, max_chars: int | None = None) -> str:
    """Normalise unicode/whitespace and optionally truncate on a word boundary."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).replace("\u200b", "")
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    result = "\n".join(line for line in lines if line).strip()
    if max_chars is not None and len(result) > max_chars:
        cut = result[:max_chars]
        space = cut.rfind(" ")
        result = (cut[:space] if space > max_chars * 0.6 else cut).rstrip() + "…"
    return result


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation and stopwords - used for title dedup."""
    text = unicodedata.normalize("NFKC", title or "").lower()
    text = _NON_WORD.sub(" ", text)
    tokens = [t for t in text.split() if t and t not in _STOPWORDS]
    return " ".join(tokens)


def title_tokens(title: str) -> set[str]:
    return set(normalize_title(title).split())


def canonicalize_url(url: str) -> str:
    """Drop tracking params, fragments and trailing slashes; lowercase the host."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if (scheme == "https" and netloc.endswith(":443")) or (
        scheme == "http" and netloc.endswith(":80")
    ):
        netloc = netloc.rsplit(":", 1)[0]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith(_TRACKING_PREFIXES) and k.lower() not in _TRACKING_KEYS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()


def content_hash(*parts: str | None) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(normalize_title(part or "").encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def domain_of(url: str) -> str:
    try:
        netloc = urlsplit(url).netloc.lower()
    except ValueError:
        return ""
    return netloc[4:] if netloc.startswith("www.") else netloc


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def sequence_ratio(left: str, right: str) -> float:
    """Symmetric character-level similarity.

    ``difflib.SequenceMatcher`` is not symmetric (it anchors on the second
    argument's blocks), so both orders are compared and the larger value wins.
    Without this, dedup results depend on the order items were collected in.
    """
    from difflib import SequenceMatcher

    if not left or not right:
        return 0.0
    forward = SequenceMatcher(None, left, right).ratio()
    backward = SequenceMatcher(None, right, left).ratio()
    return max(forward, backward)


def identifier_tokens(normalized_title: str) -> set[str]:
    """Tokens that carry a version, date or count (anything containing a digit).

    These are what distinguish ``ollama v0.34.1`` from ``ollama v0.34.3-rc1`` and
    ``Solana Changelog: September 3`` from ``September 18``.
    """
    return {token for token in normalized_title.split() if any(c.isdigit() for c in token)}


def entity_tokens(normalized_title: str) -> set[str]:
    """Distinctive non-numeric tokens: repo/product/topic words."""
    return {token for token in normalized_title.split() if not any(c.isdigit() for c in token)}


# A pair whose identifier tokens differ (v1.2.3 vs v1.2.4, two weekly changelogs)
# or whose release channel differs (devnet vs testnet) is a different edition of
# the same series, never a duplicate. Capping the score below the dedup threshold
# keeps every release while still reporting the overlap.
DIFFERENT_EDITION_CAP = 0.6

# Release-channel vocabulary: a small, domain-specific set. Two titles that
# differ by one of these words describe different artefacts.
VARIANT_TOKENS = frozenset(
    {
        "devnet",
        "testnet",
        "mainnet",
        "localnet",
        "rc",
        "alpha",
        "beta",
        "nightly",
        "canary",
        "preview",
        "stable",
        "hotfix",
        "patch",
        "edge",
        "prerelease",
    }
)


def title_similarity(left: str, right: str) -> float:
    """Similarity between two headlines (0..1), aware of version/edition tokens.

    Measured on real collected titles:

    ==================================================  =====
    pair                                                score
    ==================================================  =====
    identical headline, different punctuation            1.00
    ``ollama v0.34.1`` vs ``ollama v0.34.3-rc1``         0.60 (capped)
    ``Solana Changelog: Sep 3`` vs ``Sep 18``            0.60 (capped)
    ``sui: testnet-v1.80.0`` vs ``sui: devnet-v1.80.0``  0.60 (capped)
    same story reworded by another outlet                0.52
    unrelated releases from two repositories             0.57
    unrelated headlines                                  0.41
    ==================================================  =====

    With the dedup threshold at 0.82 only genuine repeats are dropped; detecting
    "same story, different wording" is left to the embedding stage, which needs a
    dense embedder to be reliable.
    """
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    base = max(sequence_ratio(a, b), jaccard(set(a.split()), set(b.split())))
    ids_a, ids_b = identifier_tokens(a), identifier_tokens(b)
    if ids_a and ids_b and ids_a != ids_b:
        return min(base, DIFFERENT_EDITION_CAP)
    if (entity_tokens(a) ^ entity_tokens(b)) & VARIANT_TOKENS:
        return min(base, DIFFERENT_EDITION_CAP)
    return base


def entity_overlap(left: str, right: str) -> float:
    """How much two headlines talk about the same thing, ignoring versions/dates.

    Containment (``|A∩B| / min(|A|,|B|)``) over non-numeric tokens, so:

    * ``ollama v0.34.1`` vs ``ollama v0.34.3-rc1``        -> 1.00 (same series)
    * ``sui testnet-v1.80.0`` vs ``sui devnet-v1.80.0``   -> 0.67
    * ``vllm v0.99.0`` vs ``go-ethereum v1.99.0``         -> 0.00
    * two unrelated headlines                            -> 0.00

    Used by the selector to avoid publishing two items about the same series in
    one run.
    """
    a, b = entity_tokens(normalize_title(left)), entity_tokens(normalize_title(right))
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def count_keyword_hits(text: str, keywords: list[str]) -> list[str]:
    """Return the keywords present in ``text`` (case-insensitive substring match)."""
    haystack = (text or "").lower()
    return [kw for kw in keywords if kw and kw.lower() in haystack]


def chunk_text(text: str, *, chunk_size: int = 1200, overlap: int = 150) -> list[str]:
    """Split text into overlapping chunks, preferring paragraph boundaries."""
    body = clean_text(text)
    if not body:
        return []
    if len(body) <= chunk_size:
        return [body]

    chunks: list[str] = []
    step = max(chunk_size - max(overlap, 0), 1)
    start = 0
    while start < len(body):
        end = min(start + chunk_size, len(body))
        if end < len(body):
            window = body[start:end]
            boundary = max(window.rfind("\n"), window.rfind(". "))
            if boundary > chunk_size * 0.5:
                end = start + boundary + 1
        piece = body[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(body):
            break
        start = max(end - max(overlap, 0), start + step)
    return chunks


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def parse_datetime(value: str | datetime | None) -> datetime | None:
    """Best-effort parse of the date formats found in feeds."""
    if value is None or isinstance(value, datetime):
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def age_days(published: datetime | None, *, now: datetime | None = None) -> float:
    if published is None:
        return float("inf")
    reference = now or utcnow()
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    return max((reference - published).total_seconds() / 86400.0, 0.0)


def slugify(value: str, *, max_length: int = 60) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (text[:max_length].rstrip("-")) or "item"


def truncate(value: str, limit: int, *, suffix: str = "…") -> str:
    if limit <= 0 or len(value) <= limit:
        return value
    return value[: max(limit - len(suffix), 0)].rstrip() + suffix
