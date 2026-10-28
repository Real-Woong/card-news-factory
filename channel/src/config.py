"""채널 설정 로더.

``channel/config/*.yaml`` 세 개를 읽어 하나의 :class:`ChannelConfig` 로 제공합니다.
공통 엔진의 ``AppConfig`` 와는 별개입니다(엔진은 브랜드 설정을 알지 못함).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.core.errors import ConfigError

CHANNEL_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILES = {
    "brand": "brand.yaml",
    "instagram": "instagram.yaml",
    "content": "content.yaml",
}


def _dig(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


@dataclass(frozen=True)
class TemplateSpec:
    """carousel 타입 하나의 구조 정의 (content.yaml -> templates.*)."""

    name: str
    slides: int
    when: str
    sections: list[dict[str, Any]]
    default: bool = False

    def section(self, index: int) -> dict[str, Any]:
        for entry in self.sections:
            if int(entry.get("index", -1)) == index:
                return entry
        return {}

    def slide_type(self, index: int) -> str:
        return str(self.section(index).get("type") or "content")

    def role(self, index: int) -> str:
        return str(self.section(index).get("role") or "")


@dataclass
class ChannelConfig:
    """R_A_Glance 채널 설정 전체."""

    brand: dict[str, Any]
    instagram: dict[str, Any]
    content: dict[str, Any]
    root: Path = CHANNEL_ROOT

    # -- 경로 ---------------------------------------------------------------
    @property
    def prompts_dir(self) -> Path:
        return self.root / "prompts"

    @property
    def templates_dir(self) -> Path:
        return self.root / "templates" / "instagram"

    @property
    def outputs_dir(self) -> Path:
        return (self.root / str(_dig(self.instagram, "output.dir", "../For_Review"))).resolve()

    # -- 일반 접근 ----------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        """``brand.colors.background`` 처럼 파일명을 앞에 붙여 조회합니다."""
        head, _, rest = dotted.partition(".")
        source = {"brand": self.brand, "instagram": self.instagram, "content": self.content}.get(
            head
        )
        if source is None:
            return default
        return _dig(source, rest, default) if rest else source

    # -- 자주 쓰는 값 -------------------------------------------------------
    @property
    def brand_name(self) -> str:
        return str(_dig(self.brand, "brand.name", "R_A_Glance"))

    @property
    def wordmark(self) -> str:
        return str(_dig(self.brand, "brand.wordmark", self.brand_name))

    @property
    def canvas(self) -> tuple[int, int]:
        return (
            int(_dig(self.brand, "canvas.width", 1080)),
            int(_dig(self.brand, "canvas.height", 1350)),
        )

    @property
    def allowed_slide_counts(self) -> list[int]:
        counts = _dig(self.instagram, "carousel.allowed_slide_counts", [3, 5, 7])
        return [int(c) for c in counts]

    @property
    def templates(self) -> dict[str, TemplateSpec]:
        specs: dict[str, TemplateSpec] = {}
        for name, raw in (self.content.get("templates") or {}).items():
            specs[str(name)] = TemplateSpec(
                name=str(name),
                slides=int(raw.get("slides", 5)),
                when=str(raw.get("when", "")),
                sections=list(raw.get("sections") or []),
                default=bool(raw.get("default", False)),
            )
        return specs

    def template(self, name: str) -> TemplateSpec:
        specs = self.templates
        if name not in specs:
            raise ConfigError(
                f"알 수 없는 carousel 타입 `{name}` (사용 가능: {sorted(specs)})"
            )
        return specs[name]

    def template_for_slides(self, count: int) -> TemplateSpec | None:
        for spec in self.templates.values():
            if spec.slides == count:
                return spec
        return None

    @property
    def default_template(self) -> str:
        configured = _dig(self.instagram, "carousel.default_template")
        if configured:
            return str(configured)
        for name, spec in self.templates.items():
            if spec.default:
                return name
        return "news_standard"

    @property
    def banned_phrases(self) -> list[str]:
        return [str(p) for p in (_dig(self.content, "tone.banned_phrases") or [])]

    @property
    def internal_markers(self) -> list[str]:
        return [str(m) for m in (self.instagram.get("internal_language_markers") or [])]

    @property
    def allowed_verdicts(self) -> set[str]:
        return {
            str(v).lower() for v in (_dig(self.content, "fact_check.allowed_verdicts") or [])
        } or {"verified", "partially_verified"}

    def text_budget(self, slide_type: str) -> dict[str, Any]:
        return dict(_dig(self.content, f"text_budget.{slide_type}", {}) or {})

    # -- closing slide 구성 (A/B 실험) --------------------------------------
    @property
    def closing_variant(self) -> str:
        return str(_dig(self.content, "closing.variant", "B")).strip().upper()

    def closing_spec(self, variant: str | None = None) -> dict[str, Any]:
        """현재(또는 지정한) closing variant 의 라벨·항목 수 규칙."""
        name = (variant or self.closing_variant).upper()
        variants = _dig(self.content, "closing.variants", {}) or {}
        spec = dict(variants.get(name) or {})
        spec.setdefault("primary_label", "Takeaway")
        spec.setdefault("secondary_label", "")
        spec.setdefault("primary_items", "1~2")
        spec.setdefault("secondary_items", "0")
        spec["variant"] = name
        return spec

    @property
    def closing_uses_secondary(self) -> bool:
        spec = self.closing_spec()
        return bool(spec.get("secondary_label")) and spec.get("secondary_items") not in {
            "0",
            0,
            "",
        }

    def output_filename(self, key: str, default: str = "") -> str:
        return str(_dig(self.instagram, f"output.files.{key}", default))

    def as_metadata(self) -> dict[str, Any]:
        """review/metadata 기록용 요약."""
        width, height = self.canvas
        return {
            "brand": self.brand_name,
            "canvas": f"{width}x{height}",
            "colors": {
                "background": _dig(self.brand, "colors.background"),
                "foreground": _dig(self.brand, "colors.foreground"),
            },
            "allowed_slide_counts": self.allowed_slide_counts,
            "default_template": self.default_template,
            "phase": _dig(self.instagram, "channel.phase", 1),
            "publish": bool(_dig(self.instagram, "channel.publish", False)),
        }


def load_channel_config(config_dir: Path | None = None) -> ChannelConfig:
    """세 YAML 파일을 읽어 :class:`ChannelConfig` 를 만듭니다.

    Raises:
        ConfigError: 파일이 없거나 YAML 이 잘못된 경우.
    """
    directory = config_dir or (CHANNEL_ROOT / "config")
    loaded: dict[str, dict[str, Any]] = {}
    for key, filename in CONFIG_FILES.items():
        path = directory / filename
        if not path.exists():
            raise ConfigError(f"채널 설정 파일이 없습니다: {path}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"YAML 오류 {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{path} 최상위는 매핑이어야 합니다")
        loaded[key] = data

    config = ChannelConfig(
        brand=loaded["brand"], instagram=loaded["instagram"], content=loaded["content"]
    )

    # 설정 간 일관성 검사: 템플릿 slide 수가 허용 목록에 있어야 합니다.
    allowed = set(config.allowed_slide_counts)
    for name, spec in config.templates.items():
        if spec.slides not in allowed:
            raise ConfigError(
                f"템플릿 `{name}` 의 slide 수 {spec.slides} 가 "
                f"instagram.yaml 의 허용 목록 {sorted(allowed)} 에 없습니다"
            )
        if len(spec.sections) != spec.slides:
            raise ConfigError(
                f"템플릿 `{name}`: slides={spec.slides} 인데 sections={len(spec.sections)} 개"
            )
    if config.default_template not in config.templates:
        raise ConfigError(
            f"default_template `{config.default_template}` 정의가 content.yaml 에 없습니다"
        )
    return config
