"""Record a live post in the ``posts`` history so the engine does not repeat it.

The input is a generic output directory holding ``carousel.json`` and
``publishing.json``; nothing here knows which channel layer produced it.
Idempotent per ``(channel, external_id)``.

    python -m src.pipeline.record_publication <output_dir>

Prints the post id on success; prints nothing when there is nothing to record.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.core.config import load_config
from src.core.errors import ContentOpsError
from src.core.models import PublishedPost
from src.core.text import parse_datetime, utcnow
from src.storage.db import open_db
from src.storage.repository import ArticleRepository


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContentOpsError(f"{path} 를 읽을 수 없습니다: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContentOpsError(f"{path} 형식이 올바르지 않습니다")
    return payload


def _caption(carousel: dict) -> str:
    caption = str(carousel.get("caption") or "")
    tags = " ".join(t for t in (carousel.get("hashtags") or []) if t not in caption)
    return f"{caption}\n\n{tags}".strip()


def record_publication(directory: Path, *, db_path: Path | str | None = None) -> int | None:
    """Insert (or find) the published post. None when not published or no run to attach to."""
    result = _read_json(directory / "publishing.json")
    external_id = result.get("external_id")
    if not (result.get("ok") and external_id):
        return None
    carousel = _read_json(directory / "carousel.json")
    channel = str(carousel.get("channel") or "instagram")
    topic_id = str((carousel.get("meta") or {}).get("topic_id") or "").strip()
    path = db_path if db_path is not None else load_config().db_path()
    with open_db(path) as conn:
        repo = ArticleRepository(conn)
        existing = repo.find_post_id_by_external_id(channel, str(external_id))
        if existing is not None:
            return existing
        run = repo.last_successful_run()
        if run is None:
            return None
        post_id = repo.save_post(
            PublishedPost(
                run_id=run["run_id"],
                channel_id=channel,
                format="carousel",
                title=str(carousel.get("headline") or ""),
                body=_caption(carousel),
                status="published",
                article_keys=[topic_id] if topic_id else [],
                published_at=parse_datetime(result.get("completed_at")) or utcnow(),
                external_id=str(external_id),
            )
        )
        if result.get("external_url"):
            repo.set_post_external_url(post_id, str(result["external_url"]))
        return post_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="게시된 산출물을 posts 이력에 기록")
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        post_id = record_publication(args.output_dir)
    except ContentOpsError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    if post_id is not None:
        print(post_id)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
