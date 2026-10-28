"""Configuration loading.

Responsibilities:

* load ``.env`` (if present) before reading YAML, so ``${VAR}`` works
* read the five YAML files in ``config/``
* expand ``${VAR}`` / ``${VAR:-default}`` placeholders and coerce scalars
* expose typed views for the parts that are read in many places
  (sources, channels, ranking) and dotted-key access for the rest

Nothing else in the codebase reads YAML or ``os.environ`` directly (except the
LLM/embedding providers, which read their API keys lazily).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.core.errors import ConfigError
from src.core.paths import PATHS, Paths

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

CONFIG_FILES = {
    "pipeline": "pipeline.yaml",
    "sources": "sources.yaml",
    "topics": "topics.yaml",
    "ranking": "ranking.yaml",
    "channels": "channels.yaml",
}


# --------------------------------------------------------------------------- #
# env handling
# --------------------------------------------------------------------------- #
def load_dotenv_file(path: Path | None = None) -> None:
    """Load ``.env`` into ``os.environ`` without overriding existing values.

    Uses python-dotenv when available and falls back to a minimal parser so the
    project still runs on a bare interpreter.
    """
    env_path = path or (PATHS.root / ".env")
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]

        load_dotenv(env_path, override=False)
        return
    except ImportError:
        pass

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def _coerce(value: str) -> Any:
    """Turn an expanded string into bool/int/float/None when it clearly is one."""
    lowered = value.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none", ""}:
        return None if lowered != "" else ""
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _expand(node: Any) -> Any:
    """Recursively expand ``${VAR}`` / ``${VAR:-default}`` placeholders."""
    if isinstance(node, dict):
        return {key: _expand(val) for key, val in node.items()}
    if isinstance(node, list):
        return [_expand(item) for item in node]
    if not isinstance(node, str):
        return node

    match = _ENV_PATTERN.fullmatch(node.strip())
    if match:
        name, default = match.group(1), match.group(2)
        raw = os.environ.get(name)
        if raw is None or raw == "":
            if default is None:
                raise ConfigError(
                    f"Environment variable ${{{name}}} is required by the config "
                    "and is not set (add it to .env or provide a ${VAR:-default})"
                )
            raw = default
        return _coerce(raw)

    def _sub(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        return os.environ.get(name) or (default if default is not None else "")

    return _ENV_PATTERN.sub(_sub, node)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - depends on user edits
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Expected a mapping at the top level of {path}")
    return _expand(data)


# --------------------------------------------------------------------------- #
# typed views
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SourceConfig:
    """One entry from ``config/sources.yaml``."""

    id: str
    name: str
    type: str
    category: str
    trust: str
    enabled: bool = True
    url: str | None = None
    repo: str | None = None
    query: str | None = None
    max_items: int = 15
    timeout_seconds: int = 20
    lookback_days: int = 10
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        """Human-readable target, used in logs and metadata."""
        return self.url or self.repo or self.query or self.id


@dataclass(frozen=True)
class ChannelConfig:
    """One entry from ``config/channels.yaml``."""

    id: str
    name: str
    format: str  # longform | shortform
    enabled: bool = True
    publish: bool = False
    max_chars: int = 1000
    hashtag_limit: int = 5
    thread: bool = False
    max_thread_items: int = 5
    style: str = ""
    language: str = "ko"
    secondary_language: str | None = None
    include_sources: bool = True


@dataclass(frozen=True)
class RankingConfig:
    """Typed view over ``config/ranking.yaml``."""

    trust_scores: dict[str, int]
    trust_max: int
    weights: dict[str, float]
    recency_half_life_days: float
    max_age_days: int
    type_boosts: dict[str, float]
    max_items: int
    max_per_source: int
    category_quota: dict[str, int]
    min_score: float
    intra_run_series_overlap: float = 0.60

    def trust_value(self, tier: str) -> int:
        return int(self.trust_scores.get(tier, 1))


def _dig(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


@dataclass
class AppConfig:
    """Aggregate configuration handed to every component."""

    pipeline: dict[str, Any]
    topics: dict[str, Any]
    channels_raw: dict[str, Any]
    ranking_raw: dict[str, Any]
    sources: list[SourceConfig]
    channels: list[ChannelConfig]
    ranking: RankingConfig
    paths: Paths = PATHS

    # -- generic access -----------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        """Dotted lookup into ``pipeline.yaml`` (e.g. ``"dedup.title_threshold"``)."""
        return _dig(self.pipeline, dotted, default)

    def topic_section(self, category: str) -> dict[str, Any]:
        return _dig(self.topics, f"categories.{category}", {}) or {}

    # -- convenience --------------------------------------------------------
    @property
    def stages(self) -> list[str]:
        stages = self.get("run.stages") or []
        if not stages:
            raise ConfigError("pipeline.yaml -> run.stages is empty")
        return list(stages)

    @property
    def categories(self) -> list[str]:
        return list((self.topics.get("categories") or {}).keys())

    def enabled_sources(self, *, categories: list[str] | None = None) -> list[SourceConfig]:
        result = [s for s in self.sources if s.enabled]
        if categories:
            wanted = {c.lower() for c in categories}
            result = [s for s in result if s.category.lower() in wanted]
        return result

    def enabled_channels(self, fmt: str | None = None) -> list[ChannelConfig]:
        result = [c for c in self.channels if c.enabled]
        if fmt:
            result = [c for c in result if c.format == fmt]
        return result

    def db_path(self) -> Path:
        return self.paths.resolve(self.get("storage.db_path", "data/content.db"))

    def vectorstore_path(self) -> Path:
        return self.paths.resolve(self.get("storage.vectorstore_dir", "data/vectorstore"))

    def drafts_path(self) -> Path:
        return self.paths.resolve(self.get("output.drafts_dir", "outputs/drafts"))

    def archive_path(self) -> Path:
        return self.paths.resolve(self.get("output.archive_dir", "outputs/archive"))


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
_KNOWN_SOURCE_KEYS = {
    "id",
    "name",
    "type",
    "category",
    "trust",
    "enabled",
    "url",
    "repo",
    "query",
    "max_items",
    "timeout_seconds",
    "lookback_days",
}


def _build_sources(raw: dict[str, Any]) -> list[SourceConfig]:
    defaults = raw.get("defaults") or {}
    entries = raw.get("sources") or []
    if not isinstance(entries, list):
        raise ConfigError("sources.yaml -> `sources` must be a list")

    sources: list[SourceConfig] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"sources.yaml -> sources[{index}] must be a mapping")
        for required in ("id", "type", "category", "trust"):
            if not entry.get(required):
                raise ConfigError(f"sources.yaml -> sources[{index}] is missing `{required}`")
        source_id = str(entry["id"])
        if source_id in seen:
            raise ConfigError(f"sources.yaml -> duplicate source id `{source_id}`")
        seen.add(source_id)
        sources.append(
            SourceConfig(
                id=source_id,
                name=str(entry.get("name") or source_id),
                type=str(entry["type"]),
                category=str(entry["category"]).lower(),
                trust=str(entry["trust"]),
                enabled=bool(entry.get("enabled", True)),
                url=entry.get("url"),
                repo=entry.get("repo"),
                query=entry.get("query"),
                max_items=int(entry.get("max_items", defaults.get("max_items", 15))),
                timeout_seconds=int(
                    entry.get("timeout_seconds", defaults.get("timeout_seconds", 20))
                ),
                lookback_days=int(entry.get("lookback_days", defaults.get("lookback_days", 10))),
                extra={k: v for k, v in entry.items() if k not in _KNOWN_SOURCE_KEYS},
            )
        )
    return sources


def _build_channels(raw: dict[str, Any]) -> list[ChannelConfig]:
    defaults = raw.get("defaults") or {}
    entries = raw.get("channels") or []
    channels: list[ChannelConfig] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"channels.yaml -> channels[{index}] must be a mapping")
        for required in ("id", "format"):
            if not entry.get(required):
                raise ConfigError(f"channels.yaml -> channels[{index}] is missing `{required}`")
        channels.append(
            ChannelConfig(
                id=str(entry["id"]),
                name=str(entry.get("name") or entry["id"]),
                format=str(entry["format"]),
                enabled=bool(entry.get("enabled", True)),
                publish=bool(entry.get("publish", False)),
                max_chars=int(entry.get("max_chars", 1000)),
                hashtag_limit=int(entry.get("hashtag_limit", defaults.get("hashtag_limit", 5))),
                thread=bool(entry.get("thread", False)),
                max_thread_items=int(entry.get("max_thread_items", 5)),
                style=str(entry.get("style", "")),
                language=str(entry.get("language", defaults.get("language", "ko"))),
                secondary_language=entry.get(
                    "secondary_language", defaults.get("secondary_language")
                ),
                include_sources=bool(
                    entry.get("include_sources", defaults.get("include_sources", True))
                ),
            )
        )
    return channels


def _build_ranking(raw: dict[str, Any]) -> RankingConfig:
    weights = raw.get("weights") or {}
    if not weights:
        raise ConfigError("ranking.yaml -> weights is required")
    selection = raw.get("selection") or {}
    return RankingConfig(
        trust_scores={str(k): int(v) for k, v in (raw.get("trust_scores") or {}).items()},
        trust_max=int(raw.get("trust_max", 5)),
        weights={str(k): float(v) for k, v in weights.items()},
        recency_half_life_days=float(_dig(raw, "recency.half_life_days", 3)),
        max_age_days=int(_dig(raw, "recency.max_age_days", 14)),
        type_boosts={str(k): float(v) for k, v in (raw.get("type_boosts") or {}).items()},
        max_items=int(selection.get("max_items", 5)),
        max_per_source=int(selection.get("max_per_source", 2)),
        category_quota={
            str(k): int(v) for k, v in (selection.get("category_quota") or {}).items()
        },
        min_score=float(selection.get("min_score", 0.0)),
        intra_run_series_overlap=float(selection.get("intra_run_series_overlap", 0.60)),
    )


def load_config(config_dir: Path | None = None, *, load_env: bool = True) -> AppConfig:
    """Load and validate every config file.

    Args:
        config_dir: Override the ``config/`` directory (used by tests).
        load_env: Load ``.env`` before expanding placeholders.

    Raises:
        ConfigError: A file is missing, malformed, or a required env var is unset.
    """
    if load_env:
        load_dotenv_file()

    directory = config_dir or PATHS.config
    raw = {key: _load_yaml(directory / filename) for key, filename in CONFIG_FILES.items()}

    sources = _build_sources(raw["sources"])
    ranking = _build_ranking(raw["ranking"])
    channels = _build_channels(raw["channels"])

    known_tiers = set(ranking.trust_scores)
    for source in sources:
        if source.trust not in known_tiers:
            raise ConfigError(
                f"sources.yaml -> source `{source.id}` uses unknown trust tier "
                f"`{source.trust}` (known: {sorted(known_tiers)})"
            )

    return AppConfig(
        pipeline=raw["pipeline"],
        topics=raw["topics"],
        channels_raw=raw["channels"],
        ranking_raw=raw["ranking"],
        sources=sources,
        channels=channels,
        ranking=ranking,
    )
