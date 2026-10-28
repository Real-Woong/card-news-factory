"""Filesystem layout.

All paths derive from the project root so the pipeline behaves the same whether
it is started by ``make run``, ``python -m src.pipeline.run`` or launchd (which
uses ``/`` as the working directory).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

# src/core/paths.py -> src/core -> src -> project root
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Paths:
    """Resolved project directories."""

    root: Path = PROJECT_ROOT

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def raw(self) -> Path:
        return self.data / "raw"

    @property
    def processed(self) -> Path:
        return self.data / "processed"

    @property
    def vectorstore(self) -> Path:
        return self.data / "vectorstore"

    @property
    def samples(self) -> Path:
        return self.data / "samples"

    @property
    def db(self) -> Path:
        return self.data / "content.db"

    @property
    def knowledge(self) -> Path:
        return self.root / "knowledge"

    @property
    def prompts(self) -> Path:
        return self.root / "prompts"

    @property
    def outputs(self) -> Path:
        return self.root / "outputs"

    @property
    def drafts(self) -> Path:
        return self.outputs / "drafts"

    @property
    def archive(self) -> Path:
        return self.outputs / "archive"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def resolve(self, value: str | Path) -> Path:
        """Resolve a possibly-relative config path against the project root."""
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.root / path

    def draft_dir(self, run_date: date, *, suffix: str | None = None) -> Path:
        """Output directory for one run: ``outputs/drafts/YYYY-MM-DD``."""
        name = run_date.isoformat()
        if suffix:
            name = f"{name}{suffix}"
        return self.drafts / name

    def raw_dir(self, run_date: date) -> Path:
        return self.raw / run_date.isoformat()

    def processed_dir(self, run_date: date) -> Path:
        return self.processed / run_date.isoformat()

    def ensure_runtime_dirs(self) -> None:
        """Create the directories the pipeline writes to."""
        for path in (
            self.data,
            self.raw,
            self.processed,
            self.vectorstore,
            self.drafts,
            self.archive,
            self.logs,
        ):
            path.mkdir(parents=True, exist_ok=True)


PATHS = Paths()
