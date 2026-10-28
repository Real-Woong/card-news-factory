"""GitHub releases collector.

Uses the public REST API. ``GITHUB_TOKEN`` is optional but raises the rate limit
from 60 to 5000 requests/hour.
"""

from __future__ import annotations

import os
from typing import Any

from src.collectors.base import Collector
from src.core.errors import CollectorError
from src.core.models import RawItem
from src.core.text import clean_text, parse_datetime

API = "https://api.github.com/repos/{repo}/releases?per_page={per_page}"


class GithubReleasesCollector(Collector):
    """One item per published (non-draft) release."""

    type_name = "github_releases"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def fetch(self) -> list[RawItem]:
        repo = self.source.repo
        if not repo:
            raise CollectorError(
                f"source `{self.source.id}` has no repo (expected 'owner/name')",
                source_id=self.source.id,
            )

        url = API.format(repo=repo, per_page=min(self.source.max_items, 30))
        payload = self.http.get_json(
            url, headers=self._headers(), timeout=self.source.timeout_seconds
        )
        if not isinstance(payload, list):
            raise CollectorError(
                f"Unexpected GitHub response for {repo}: {type(payload).__name__}",
                source_id=self.source.id,
            )

        items: list[RawItem] = []
        for release in payload:
            if not isinstance(release, dict) or release.get("draft"):
                continue
            item = self._to_item(repo, release)
            if item is not None:
                items.append(item)
        return items

    def _to_item(self, repo: str, release: dict[str, Any]) -> RawItem | None:
        html_url = release.get("html_url") or ""
        tag = release.get("tag_name") or ""
        if not html_url:
            return None

        name = release.get("name") or tag or "release"
        title = f"{repo} {tag}".strip() if tag and tag not in str(name) else f"{repo}: {name}"
        # Base collector applies the bounded, section-aware body policy after
        # structured links and identifiers have been preserved.
        notes = clean_text(release.get("body") or "")
        prerelease = bool(release.get("prerelease"))
        tags = ["release", "prerelease" if prerelease else "stable", repo.split("/")[0]]

        return self.make_item(
            url=html_url,
            title=title,
            summary=notes[:400] or f"{repo} published {tag}",
            content=f"Repository: {repo}\nTag: {tag}\n\n{notes}",
            published_at=parse_datetime(release.get("published_at") or release.get("created_at")),
            author=(release.get("author") or {}).get("login"),
            tags=tags,
            raw_payload={
                "repo": repo,
                "tag_name": tag,
                "prerelease": prerelease,
                "release_id": release.get("id"),
            },
        )
