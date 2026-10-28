"""Publisher interface (phase 2).

Nothing here posts to a network yet - by design. Phase 1 stops at files under
``outputs/drafts/`` plus a ``posts`` row with ``status='draft'``.

The approval flow this is built for::

    1. pipeline writes drafts            -> posts.status = 'draft'
    2. human edits and approves          -> posts.status = 'approved'
    3. publisher picks up approved posts -> posts.status = 'published' (+ external_id)

:class:`FilePublisher` implements step 3 against the filesystem so the flow can
be exercised end to end before any real SNS credentials exist.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.config import ChannelConfig
from src.core.errors import ContentOpsError
from src.core.logging_setup import get_logger
from src.core.models import Draft
from src.core.text import to_iso, utcnow

logger = get_logger(__name__)


class PublishError(ContentOpsError):
    """Publishing failed."""


@dataclass
class PublishResult:
    channel_id: str
    ok: bool
    external_id: str | None = None
    external_url: str | None = None
    error: str | None = None
    detail: dict[str, Any] | None = None


class Publisher(ABC):
    """Sends an approved draft to a channel."""

    name: str = "publisher"

    @abstractmethod
    def publish(self, draft: Draft, channel: ChannelConfig) -> PublishResult:
        """Publish one draft.

        Implementations must be idempotent per ``(channel, draft)`` where the
        platform allows it, and must never publish when ``channel.publish`` is
        false.
        """

    def guard(self, channel: ChannelConfig) -> None:
        """Refuse to publish unless the channel explicitly opted in."""
        if not channel.publish:
            raise PublishError(
                f"channel `{channel.id}` has publish=false; approve it in channels.yaml first"
            )


class FilePublisher(Publisher):
    """Writes the approved post to ``outputs/archive/published/`` instead of an API."""

    name = "file"

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def publish(self, draft: Draft, channel: ChannelConfig) -> PublishResult:
        self.guard(channel)
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        path = self.directory / f"{stamp}-{channel.id}.md"
        try:
            path.write_text(
                f"# {draft.title}\n\n{draft.body}\n\n{' '.join(draft.hashtags)}\n",
                encoding="utf-8",
            )
        except OSError as exc:
            return PublishResult(channel_id=channel.id, ok=False, error=str(exc))
        logger.info("[publish] wrote %s", path)
        return PublishResult(
            channel_id=channel.id,
            ok=True,
            external_id=path.name,
            external_url=path.as_uri(),
            detail={"published_at": to_iso(utcnow())},
        )


# TODO(phase-2): XPublisher, LinkedInPublisher, ThreadsPublisher.
# Each should read credentials from environment variables only (see .env.example),
# retry on 429/5xx with backoff, and write the returned post id back to
# posts.external_id so dedup can compare against what was actually published.
PUBLISHERS: dict[str, type[Publisher]] = {FilePublisher.name: FilePublisher}
