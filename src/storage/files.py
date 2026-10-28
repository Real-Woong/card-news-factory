"""File store for raw payloads and normalised articles.

Layout::

    data/raw/YYYY-MM-DD/<source_id>/<slug>-<hash8>.json   # exactly what we fetched
    data/processed/YYYY-MM-DD/<hash8>-<slug>.json         # normalised Article

Keeping the raw payload makes it possible to re-normalise historical data
without re-crawling.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from src.core.errors import StorageError
from src.core.logging_setup import get_logger
from src.core.models import Article, RawItem
from src.core.text import slugify

logger = get_logger(__name__)


class FileStore:
    """Writes JSON artefacts to ``data/raw`` and ``data/processed``."""

    def __init__(self, raw_dir: Path, processed_dir: Path, *, enabled: bool = True) -> None:
        self.raw_dir = raw_dir
        self.processed_dir = processed_dir
        self.enabled = enabled

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError as exc:
            raise StorageError(f"Cannot write {path}: {exc}") from exc
        return path

    @staticmethod
    def _filename(title: str, key: str) -> str:
        return f"{slugify(title, max_length=48)}-{key[:8]}.json"

    # -------------------------------------------------------------------- raw
    def save_raw(self, item: RawItem, *, run_date: date, key: str) -> str | None:
        """Persist a collector payload. Returns the project-relative path."""
        if not self.enabled:
            return None
        path = (
            self.raw_dir
            / run_date.isoformat()
            / slugify(item.source_id, max_length=40)
            / self._filename(item.title, key)
        )
        self._write_json(path, item.to_dict())
        return str(path)

    def save_raw_batch(
        self, items: list[RawItem], *, run_date: date, keys: list[str]
    ) -> list[str | None]:
        return [
            self.save_raw(item, run_date=run_date, key=key)
            for item, key in zip(items, keys, strict=True)
        ]

    # -------------------------------------------------------------- processed
    def save_processed(self, article: Article, *, run_date: date) -> str | None:
        if not self.enabled:
            return None
        path = (
            self.processed_dir
            / run_date.isoformat()
            / f"{article.key[:8]}-{slugify(article.title, max_length=48)}.json"
        )
        self._write_json(path, article.to_dict())
        return str(path)

    # ------------------------------------------------------------------ reads
    def load_raw_dir(self, directory: Path) -> list[dict[str, Any]]:
        """Load every ``*.json`` in a directory (used by the sample collector)."""
        if not directory.exists():
            return []
        payloads: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning("Skipping unreadable sample file %s: %s", path, exc)
                continue
            payloads.extend(data if isinstance(data, list) else [data])
        return payloads
