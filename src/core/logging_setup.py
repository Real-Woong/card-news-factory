"""Logging setup: rotating file handler + console handler.

Call :func:`setup_logging` once at process start, then use
``logging.getLogger(__name__)`` everywhere else.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

from src.core.paths import PATHS

_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s:%(lineno)d %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


class _RunIdFilter(logging.Filter):
    """Attach the current run id to every record (useful when tailing logs)."""

    def __init__(self, run_id: str | None) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id or "-"
        return True


def setup_logging(
    config: Any | None = None,
    *,
    level: str | None = None,
    run_id: str | None = None,
    log_dir: Path | None = None,
) -> logging.Logger:
    """Configure the root logger.

    Args:
        config: An ``AppConfig``; its ``logging.*`` section is used when present.
        level: Explicit level override (e.g. from ``--verbose``).
        run_id: Stamped onto every record.
        log_dir: Override the log directory.

    Returns:
        The project root logger (``contentops``).
    """
    global _configured

    cfg_level = "INFO"
    file_name = "pipeline.log"
    max_bytes = 5 * 1024 * 1024
    backup_count = 5
    directory = log_dir or PATHS.logs

    if config is not None:
        cfg_level = str(config.get("logging.level", cfg_level) or cfg_level)
        file_name = str(config.get("logging.file", file_name) or file_name)
        max_bytes = int(config.get("logging.max_bytes", max_bytes) or max_bytes)
        backup_count = int(config.get("logging.backup_count", backup_count) or backup_count)
        if log_dir is None:
            directory = config.paths.resolve(config.get("logging.dir", "logs"))

    resolved_level = (level or cfg_level).upper()
    numeric_level = getattr(logging, resolved_level, logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    if _configured:
        for handler in root.handlers:
            handler.setLevel(numeric_level)
        return logging.getLogger("contentops")

    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(numeric_level)
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(console)

    try:
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            directory / file_name,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG if numeric_level > logging.DEBUG else numeric_level)
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt=_DATE_FORMAT))
        root.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover - permissions / read-only disk
        root.warning("File logging disabled (%s)", exc)

    run_filter = _RunIdFilter(run_id)
    for handler in root.handlers:
        handler.addFilter(run_filter)

    # Third-party chatter.
    for noisy in ("urllib3", "httpx", "httpcore", "chromadb", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    return logging.getLogger("contentops")


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger."""
    return logging.getLogger(name)
