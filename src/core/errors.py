"""Exception hierarchy.

Every failure raised by this project inherits from :class:`ContentOpsError`, so
the pipeline can distinguish "our" errors from genuine bugs.
"""

from __future__ import annotations


class ContentOpsError(Exception):
    """Base class for all project errors."""


class ConfigError(ContentOpsError):
    """Missing or malformed configuration."""


class CollectorError(ContentOpsError):
    """A collector failed to fetch or parse a source.

    Non-fatal by default: the pipeline logs and continues with other sources.
    """

    def __init__(self, message: str, *, source_id: str | None = None) -> None:
        super().__init__(message)
        self.source_id = source_id


class StorageError(ContentOpsError):
    """SQLite or file-store failure."""


class VectorStoreError(ContentOpsError):
    """Vector store / embedding failure."""


class LLMError(ContentOpsError):
    """LLM provider failure (network, auth, rate limit, bad response)."""

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


class AgentError(ContentOpsError):
    """An agent could not produce a usable result."""

    def __init__(self, message: str, *, agent: str | None = None) -> None:
        super().__init__(message)
        self.agent = agent


class StageError(ContentOpsError):
    """A pipeline stage failed."""

    def __init__(self, message: str, *, stage: str | None = None) -> None:
        super().__init__(message)
        self.stage = stage
