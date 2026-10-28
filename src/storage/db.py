"""SQLite connection management and schema bootstrap."""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from src.core.errors import StorageError
from src.core.logging_setup import get_logger
from src.core.paths import PATHS

logger = get_logger(__name__)

SCHEMA_VERSION = 2
SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def connect(db_path: Path | str, *, create_parents: bool = True) -> sqlite3.Connection:
    """Open a configured SQLite connection.

    ``:memory:`` is supported for tests.
    """
    if str(db_path) != ":memory:" and create_parents:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    except sqlite3.Error as exc:
        raise StorageError(f"Cannot open SQLite database at {db_path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(db_path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def connect_readonly(db_path: Path | str) -> sqlite3.Connection:
    """Open an existing SQLite database without writes or WAL changes."""
    path = Path(db_path).expanduser().resolve()
    try:
        conn = sqlite3.connect(
            f"file:{quote(str(path))}?mode=ro",
            uri=True,
            timeout=30.0,
            isolation_level=None,
        )
    except sqlite3.Error as exc:
        raise StorageError(f"Cannot open SQLite database read-only at {path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Apply ``schema.sql`` (idempotent) and record the schema version."""
    if not SCHEMA_FILE.exists():  # pragma: no cover - packaging error
        raise StorageError(f"Schema file missing: {SCHEMA_FILE}")
    try:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(articles)")
        }
        if "source_metadata" not in columns:
            conn.execute("ALTER TABLE articles ADD COLUMN source_metadata TEXT")
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, datetime.now(UTC).isoformat()),
        )
    except sqlite3.Error as exc:
        raise StorageError(f"Failed to initialise schema: {exc}") from exc
    logger.debug("Schema ready (version %s)", SCHEMA_VERSION)


@contextmanager
def open_db(db_path: Path | str, *, init: bool = True) -> Iterator[sqlite3.Connection]:
    """Context manager yielding a ready-to-use connection."""
    conn = connect(db_path)
    try:
        if init:
            init_schema(conn)
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction (connections run in autocommit mode by default)."""
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def main(argv: list[str] | None = None) -> int:
    """``python -m src.storage.db --init`` creates the database."""
    parser = argparse.ArgumentParser(description="Project_ContentOps database utility")
    parser.add_argument("--init", action="store_true", help="create/upgrade the schema")
    parser.add_argument("--db", default=None, help="database path (default: data/content.db)")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else PATHS.db
    if not args.init:
        parser.print_help()
        return 0

    with open_db(db_path) as conn:
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
    print(f"Initialised {db_path}")
    print("Tables: " + ", ".join(tables))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
