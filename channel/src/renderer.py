"""HTML/CSS → PNG 렌더러.

``JSON data + HTML + CSS = PNG slide`` 파이프라인입니다.

백엔드는 두 가지이며 ``instagram.yaml -> renderer.backend_priority`` 순서로 시도합니다.

* ``playwright`` — 설치되어 있으면 사용 (브라우저 자동화, 권장)
* ``chrome_cli`` — headless Chrome 의 ``--screenshot`` 사용 (이 환경에서 검증된 경로)

**자동 truncate 는 하지 않습니다.** 텍스트가 넘치면 brand.yaml 의 ``autofit.scales``
배율을 순서대로 적용하고, 마지막 배율에서도 넘치면 :class:`RenderError` 로 실패합니다
(품질 게이트에서 BLOCK).
"""

from __future__ import annotations

import base64
import html as html_escape
import json
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.errors import ContentOpsError
from src.core.logging_setup import get_logger
from src.core.text import to_iso, utcnow

from channel.src.config import ChannelConfig
from channel.src.colors import MIN_TEXT_CONTRAST, palette_contrasts, theme_palette
from channel.src.hook import preview_html as hook_preview_html
from channel.src.layout import SlideStyle, plan_styles
from channel.src.schema import Carousel, Slide, SlideType
from channel.src.visual import attach_visuals, number_spans

logger = get_logger(__name__)


class RenderError(ContentOpsError):
    """렌더링 실패 (백엔드 없음, overflow, 크기 불일치 등)."""


@dataclass
class SlideRender:
    """슬라이드 한 장의 렌더 결과."""

    index: int
    png_path: Path
    html_path: Path
    fit: str = "ok"
    fit_scale: float = 1.0
    metrics: str = ""
    width: int = 0
    height: int = 0

    @property
    def overflow(self) -> bool:
        return self.fit != "ok"

    @property
    def geom(self) -> tuple[int, int] | None:
        """(본문 첫 요소 top, 푸터 top) 실측 y. 측정값이 없으면 None."""
        match = re.search(r"geom (-?\d+),(-?\d+)", self.metrics)
        return (int(match.group(1)), int(match.group(2))) if match else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "png": self.png_path.name,
            "fit": self.fit,
            "fit_scale": self.fit_scale,
            "metrics": self.metrics,
            "size": f"{self.width}x{self.height}",
        }


@dataclass
class RenderResult:
    """캐러셀 전체 렌더 결과."""

    slides: list[SlideRender] = field(default_factory=list)
    preview_path: Path | None = None
    backend: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and all(not s.overflow for s in self.slides)

    @property
    def overflow_slides(self) -> list[int]:
        return [s.index for s in self.slides if s.overflow]


def uniform_scale(slides: list[tuple[bool, str, float]]) -> float | None:
    """content·closing 슬라이드가 함께 쓸 글자 배율(가장 많이 줄어든 슬라이드 기준).

    ``slides`` 는 (표지 여부, fit 결과, 배율). 표지와 넘친 슬라이드는 제외하며, 맞출 슬라이드가
    둘 미만이면 None(통일할 필요 없음).
    """
    scales = [scale for is_cover, fit, scale in slides if not is_cover and fit == "ok"]
    return min(scales) if len(scales) >= 2 else None


def _esc(value: str) -> str:
    return html_escape.escape(value or "", quote=True)


class CarouselRenderer:
    """템플릿 + 브랜드 설정으로 슬라이드 PNG 와 preview.html 을 만듭니다."""

    def __init__(self, channel: ChannelConfig) -> None:
        self.channel = channel
        self.templates_dir = channel.templates_dir
        self.shared_dir = self.templates_dir / "shared"

    # ------------------------------------------------------------- 템플릿 로드
    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RenderError(f"템플릿을 읽을 수 없습니다: {path} ({exc})") from exc

    def _accent(self, topic: str) -> str:
        """카테고리(``AI``/``BLOCKCHAIN``)별 포인트 컬러. 없으면 default."""
        accents = self.channel.get("brand.colors.accent", {}) or {}
        return str(accents.get((topic or "").lower()) or accents.get("default") or "#F5F5F5")

    def _css_variables(self) -> dict[str, str]:
        brand = self.channel.brand
        width, height = self.channel.canvas
        colors = brand.get("colors", {})
        layout = brand.get("layout", {})
        typo = brand.get("typography", {})

        def font_list(key: str, fallback: str) -> str:
            stack = typo.get(key) or []
            if not stack:
                return fallback
            return ", ".join(f'"{name}"' if " " in name else name for name in stack)

        values: dict[str, str] = {
            "CANVAS_W": str(width),
            "CANVAS_H": str(height),
            "BG": str(colors.get("background", "#0D0D0D")),
            "FG": str(colors.get("foreground", "#F5F5F5")),
            "MUTED": str(colors.get("muted", "rgba(245,245,245,0.62)")),
            "HAIRLINE": str(colors.get("hairline", "rgba(245,245,245,0.16)")),
            "MARGIN_X": str(layout.get("margin_x", 88)),
            "MARGIN_Y": str(layout.get("margin_y", 88)),
            "MAIN_TOP": str(layout.get("main_top", 64)),
            "COVER_BOTTOM": str(layout.get("cover_bottom", 40)),
            "GAP": str(layout.get("gap", 28)),
            "BODY_GAP": str(layout.get("body_gap", 20)),
            "TITLE_BODY_GAP": str(layout.get("title_body_gap", 40)),
            "FONT_STACK": font_list("font_stack", "sans-serif"),
            "MONO_STACK": font_list("mono_stack", "monospace"),
        }
        for role, prefix in (
            ("headline", "HEADLINE"),
            ("subtitle", "SUBTITLE"),
            ("title", "TITLE"),
            ("body", "BODY"),
            ("meta", "META"),
        ):
            spec = typo.get(role, {}) or {}
            values[f"{prefix}_SIZE"] = str(spec.get("size", 34))
            values[f"{prefix}_WEIGHT"] = str(spec.get("weight", 400))
            values[f"{prefix}_LH"] = str(spec.get("line_height", 1.4))
            values[f"{prefix}_LS"] = str(spec.get("letter_spacing", "0"))
        return values

    # --------------------------------------------------------------- HTML 조립
    def _main_html(self, slide: Slide, carousel: Carousel) -> str:
        if slide.type is SlideType.COVER:
            # 작성자가 넣은 줄바꿈은 의도된 것이므로 <br> 로 유지합니다.
            headline = "<br />".join(_esc(line) for line in slide.headline.split("\n") if line)
            parts = [self._cover_art(carousel.topic), f'<h1 class="t-headline">{headline}</h1>']
            if slide.subtitle:
                parts.append(f'<p class="t-subtitle">{_esc(slide.subtitle)}</p>')
            cue = str(self.channel.get("content.cover.cue", "") or "").strip()
            if cue:
                # 스와이프 신호: 캐러셀임을 알리고 끝까지 넘겨 보게 한다(장수는 코드가 센다).
                parts.append(
                    f'<p class="cover__cue">{_esc(cue)} · {carousel.slide_count}장 →</p>'
                )
            return "\n".join(parts)

        parts = []
        # 상단 eyebrow 와 같은 말이면(예: TAKEAWAY + SOURCES / TAKEAWAY) 제목을 한 번 더 쓰지 않는다.
        repeats_eyebrow = (
            slide.type is SlideType.CLOSING
            and slide.title.strip().lower()
            in self.channel.template(carousel.template).role(slide.index).lower()
        )
        if slide.title and not repeats_eyebrow:
            parts.append(f'<h2 class="t-title">{_esc(slide.title)}</h2>')
        # 번호 카드(01 02 03)는 이미 큰 숫자가 있어, 큰 수치 블록까지 얹으면 한 장이 넘쳐 덱 전체
        # 글자가 줄어든다(균일 축소) → 번호 카드에는 수치 블록을 그리지 않는다.
        if slide.visual and self._style_for(slide, carousel).layout != "numbered":
            parts.append(self._visual_html(slide.visual))
        if slide.body:
            parts.append(self._list_html(slide.body))

        # closing variant A: 두 번째 블록 (What to watch next)
        if slide.extra_body:
            block = ['<div class="closing-extra">']
            if slide.extra_title:
                block.append(
                    f'<div class="closing-extra__label t-meta">{_esc(slide.extra_title)}</div>'
                )
            block.append(self._list_html(slide.extra_body, css_class="body-list--checklist"))
            block.append("</div>")
            parts.append("\n".join(block))

        aids = self._aids_html(slide, carousel)
        if aids:
            parts.append(aids)
        return "\n".join(parts)

    def _aids_html(self, slide: Slide, carousel: Carousel) -> str:
        """읽는 사람을 돕는 보조 줄(본문 아래, 남는 공간): 용어 풀이 + 마지막 장 저장 안내.

        용어 풀이는 ``content.glossary.terms`` 에 사람이 적어 둔 일반 정의만 쓴다. 슬라이드 글에 그
        용어가 실제로 나온 경우에만 붙이고, 뉴스 사실은 말하지 않는다. carousel.json 에는 들어가지
        않는 렌더 전용 장식이라 승인 해시·사실 검증 대상이 아니다.
        """
        lines: list[str] = []
        glossary = self.channel.get("content.glossary", {}) or {}
        if glossary.get("enabled", False):
            text = " ".join([slide.title, *slide.body, *slide.extra_body])
            found: list[tuple[int, str, str]] = []
            for term, meaning in (glossary.get("terms", {}) or {}).items():
                hit = re.search(rf"(?<![A-Za-z]){re.escape(str(term))}(?![A-Za-z])", text, re.I)
                if hit:
                    found.append((hit.start(), str(term), str(meaning)))
            limit = int(glossary.get("max_per_slide", 2))
            if slide.type is SlideType.CLOSING:
                limit = min(limit, 1)  # 마지막 장은 출처·저장 안내가 있어 풀이는 한 줄만
            for _, term, meaning in sorted(found)[:limit]:
                lines.append(
                    f'<p class="aid aid--gloss"><b>{_esc(term)}</b> = {_esc(meaning)}</p>'
                )
        cta = str(self.channel.get("content.closing.cta", "") or "").strip()
        if cta and slide.type is SlideType.CLOSING:
            lines.append(f'<p class="aid aid--cta">{_esc(cta)}</p>')
        if not lines:
            return ""
        label = str(glossary.get("label", "용어 풀이")) if any("aid--gloss" in x for x in lines) else ""
        head = f'<div class="aids__label t-meta">{_esc(label)}</div>' if label else ""
        return f'<div class="aids">{head}{"".join(lines)}</div>'

    def _cover_art(self, topic: str = "") -> str:
        """표지의 큰 시각 초점: 카테고리 로고(눈 + G + 카테고리 아이콘).

        ``brand.cover_logos`` 에 카테고리별 PNG 가 있으면 그것을 테마 글자색으로 칠해(mask) 쓴다.
        파일이 없으면 아래 코드로 그린 일러스트로 대체한다.
        """
        logos = self.channel.get("brand.cover_logos", {}) or {}
        raw = str(logos.get((topic or "").lower()) or logos.get("default") or "").strip()
        file = self.channel.root / raw if raw else None
        if file and file.is_file() and file.suffix.lower() == ".png":
            uri = f"data:image/png;base64,{base64.b64encode(file.read_bytes()).decode('ascii')}"
            return (
                f'<div class="cover__art cover__art--logo" role="img" '
                f'aria-label="{_esc(topic)}">'
                f"<span style=\"-webkit-mask-image:url('{uri}');mask-image:url('{uri}')\"></span>"
                "</div>"
            )
        return self._cover_drawing()

    @staticmethod
    def _cover_drawing() -> str:
        """코드로 그린 대체 일러스트(로고 눈 + 이중선 + 선으로 된 G).

        로고와 같은 구조(눈 윤곽 + 이중선 + 선으로 된 G)라 브랜드와 이어지고, 순수 장식이라 사실을
        묘사하지 않는다. 선 색은 테마 글자색(currentColor)이라 표지 바탕이 무엇이든 대비가 유지된다.
        본문 흐름 안의 첫 요소(남는 높이를 차지)라서 제목이 길어도 글자와 겹치지 않는다.
        """
        eye = "M12 120 C 90 20, 350 20, 428 120 C 350 220, 90 220, 12 120 Z"
        inner = "M62 120 C 126 56, 314 56, 378 120 C 314 184, 126 184, 62 120 Z"
        # G: 중심 (220,120) 반지름 46 의 원호(열린 곳은 오른쪽 위)와 안쪽으로 뻗는 가로획
        glyph = "M256.2 91.7 A46 46 0 1 0 266 120 L226 120"
        return (
            '<div class="cover__art" aria-hidden="true">'
            '<svg viewBox="0 0 440 240" preserveAspectRatio="xMinYMid meet" fill="none" '
            'stroke="currentColor" stroke-linejoin="round">'
            f'<path d="{eye}" stroke-width="13"/>'
            f'<path d="{inner}" stroke-width="5" opacity="0.5"/>'
            f'<path d="{glyph}" stroke-width="16"/>'
            "</svg></div>"
        )

    @staticmethod
    def _highlight(line: str) -> str:
        """줄 안의 숫자(단위 포함)를 포인트 컬러로 강조한다. 문구는 그대로이고 태그만 감쌈."""
        out: list[str] = []
        cursor = 0
        for start, end in number_spans(line):
            out.append(_esc(line[cursor:start]))
            out.append(f'<mark class="hl">{_esc(line[start:end])}</mark>')
            cursor = end
        out.append(_esc(line[cursor:]))
        return "".join(out)

    def _list_html(self, lines: list[str], *, css_class: str = "") -> str:
        items = "\n".join(
            f'<li class="t-body"><span>{self._highlight(line)}</span></li>' for line in lines
        )
        classes = f"body-list {css_class}".strip()
        return f'<ul class="{classes}">{items}</ul>'

    @staticmethod
    def _visual_html(visual: dict[str, Any]) -> str:
        """stat / compare 차트 블록. 값은 모두 슬라이드 문구에서 온 것이다(visual.py)."""
        kind = visual.get("type")
        if kind == "stat":
            number = _esc(str(visual.get("number") or ""))
            unit = _esc(str(visual.get("unit") or ""))
            return (
                '<div class="visual visual--stat">'
                f'<span class="visual__number">{number}</span>'
                f'<span class="visual__unit">{unit}</span>'
                "</div>"
            )
        if kind == "compare":
            before = visual.get("before") or {}
            after = visual.get("after") or {}
            top = max(float(before.get("number") or 0), float(after.get("number") or 0)) or 1.0

            def row(role: str, label: str, side: dict[str, Any]) -> str:
                # 막대 길이는 값에 비례하되 너무 짧아 보이지 않게 최소 8% 를 둔다.
                width = max(float(side.get("number") or 0) / top * 100, 8.0)
                return (
                    f'<div class="vbar vbar--{role}">'
                    f'<span class="vbar__label t-meta">{label}</span>'
                    f'<div class="vbar__track"><div class="vbar__fill" '
                    f'style="width:{width:.1f}%"></div></div>'
                    f'<span class="vbar__value">{_esc(str(side.get("text") or ""))}</span>'
                    "</div>"
                )

            return (
                '<div class="visual visual--compare">'
                + row("before", "이전", before)
                + row("after", "이후", after)
                + "</div>"
            )
        return ""

    def _sources_html(self, slide: Slide) -> str:
        """closing slide 의 출처 블록. main 밖에 두어 하단에 고정합니다."""
        if slide.type is not SlideType.CLOSING or not slide.sources:
            return ""
        unique: list[str] = []
        for source in slide.sources:  # 같은 이름(예: huggingface.co 두 번)은 한 번만 보여 준다
            if source.name and source.name.strip().lower() not in {n.lower() for n in unique}:
                unique.append(source.name.strip())
        names = "".join(f"<span>{_esc(name)}</span>" for name in unique)
        if not names:
            return ""
        return (
            '<div class="slide__sources sources t-meta">'
            '<div class="sources__label">SOURCES</div>'
            f'<div class="sources__list">{names}</div>'
            "</div>"
        )

    @staticmethod
    def _image_dims(path: Path) -> tuple[float, float] | None:
        """PNG·SVG 의 가로·세로(비율 계산용). 알 수 없으면 None."""
        try:
            if path.suffix.lower() == ".png":
                width, height = CarouselRenderer._png_size(path)
                return (width, height) if width and height else None
            if path.suffix.lower() in {".jpg", ".jpeg"}:
                return CarouselRenderer._jpeg_size(path)
            if path.suffix.lower() == ".svg":
                text = path.read_text(encoding="utf-8")[:4000]
                box = re.search(r'viewBox="[-\d.]+[ ,]+[-\d.]+[ ,]+([\d.]+)[ ,]+([\d.]+)"', text)
                if box:
                    return float(box.group(1)), float(box.group(2))
                size = re.search(r'width="([\d.]+)(?:px)?"[^>]*height="([\d.]+)(?:px)?"', text)
                if size:
                    return float(size.group(1)), float(size.group(2))
        except (OSError, ValueError):
            return None
        return None

    @staticmethod
    def _jpeg_size(path: Path) -> tuple[float, float] | None:
        """JPEG 의 SOF 마커에서 가로·세로를 읽는다."""
        data = path.read_bytes()
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                height, width = struct.unpack(">HH", data[index + 5 : index + 9])
                return (width, height) if width and height else None
            index += 2 + struct.unpack(">H", data[index + 2 : index + 4])[0]
        return None

    def _logo_html(self) -> str:
        """푸터 로고. ``brand.logo.path`` 가 있으면 그 파일, 없으면 코드로 그린 기본 로고.

        mask 모드는 단색 로고를 테마 글자색으로 칠해 어떤 배경에서도 보이게 한다(색 대비 보장).
        """
        config = self.channel.get("brand.logo", {}) or {}
        name = _esc(self.channel.wordmark)
        height = int(config.get("height", 40))
        raw_path = str(config.get("path") or "").strip()
        if raw_path:
            file = self.channel.root / raw_path
            dims = self._image_dims(file) if file.is_file() else None
            mime = {
                ".png": "image/png",
                ".svg": "image/svg+xml",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
            }.get(file.suffix.lower())
            if dims and mime:
                width = round(height * dims[0] / dims[1])
                uri = f"data:{mime};base64,{base64.b64encode(file.read_bytes()).decode('ascii')}"
                size = f"height:{height}px;width:{width}px"
                if str(config.get("shape", "")) == "circle":
                    # 원형 배지: 정사각형 안에서 원으로 잘라 흰 모서리·가장자리를 없앤다.
                    # 이미지는 살짝 키워(scale) 원 가장자리의 여백이 남지 않게 한다.
                    return (
                        f'<span class="logo logo--circle" role="img" aria-label="{name}" '
                        f'style="height:{height}px;width:{height}px">'
                        f'<img alt="" src="{uri}" /></span>'
                    )
                # JPG 는 투명 배경이 없어 mask 로 칠할 수 없다 → 원본 색 그대로 쓴다.
                if str(config.get("mode", "mask")) == "image" or mime == "image/jpeg":
                    return (
                        f'<img class="logo logo--image" alt="{name}" src="{uri}" '
                        f'style="{size}" />'
                    )
                return (
                    f'<span class="logo logo--mask" role="img" aria-label="{name}" '
                    f"style=\"{size};-webkit-mask-image:url('{uri}');mask-image:url('{uri}')\">"
                    "</span>"
                )
            logger.warning("[render] 로고 파일을 쓸 수 없어 기본 로고로 대체(PNG·SVG 확인): %s", raw_path)

        head, sep, tail = self.channel.wordmark.rpartition("_")
        text = f"<b>{_esc(head + sep)}</b>{_esc(tail)}" if sep else f"<b>{name}</b>"
        return (
            f'<span class="logo logo--default" role="img" aria-label="{name}">'
            f'<span class="logo__mark"></span><span class="logo__text">{text}</span></span>'
        )

    def _style_for(self, slide: Slide, carousel: Carousel) -> SlideStyle:
        return plan_styles(carousel, self.channel).get(slide.index, SlideStyle())

    def _slide_html(
        self,
        slide: Slide,
        carousel: Carousel,
        style: SlideStyle | None = None,
        scales: list[float] | None = None,
    ) -> str:
        style = style or self._style_for(slide, carousel)
        template = self._read(self.shared_dir / "slide.html")
        layout_css = self._read(self.templates_dir / carousel.template / "layout.css")
        scales = scales or self.channel.get("brand.autofit.scales", [1.0]) or [1.0]

        cover = carousel.cover
        category = (cover.eyebrow if cover else "") or carousel.topic
        if slide.type is SlideType.COVER:
            top_left = slide.eyebrow or category
            top_right = ""
        else:
            spec = self.channel.template(carousel.template)
            top_left = spec.role(slide.index).upper()
            top_right = f"{slide.index:02d} / {carousel.slide_count:02d}"

        values = {
            **self._css_variables(),
            "STYLE_CLASSES": f"slide--layout-{style.layout} slide--theme-{style.theme}",
            "GHOST": (
                f'<div class="slide__ghost" aria-hidden="true">{slide.index:02d}</div>'
                if slide.type is SlideType.CONTENT
                else ""
            ),
            "PROGRESS": f"{slide.index / max(carousel.slide_count, 1) * 100:.2f}",
            "TITLE": f"{carousel.brand} {slide.index:02d}",
            "BASE_CSS": self._read(self.shared_dir / "base.css"),
            "TYPOGRAPHY_CSS": self._read(self.shared_dir / "typography.css"),
            "LAYOUT_CSS": layout_css,
            "SLIDE_TYPE": str(slide.type),
            "INDEX": str(slide.index),
            "TOP_LEFT": _esc(top_left),
            "TOP_RIGHT": _esc(top_right),
            "MAIN": self._main_html(slide, carousel),
            "SOURCES_BLOCK": self._sources_html(slide),
            "LOGO": self._logo_html(),
            # 균일성: 푸터는 모든 슬라이드가 같다(로고 + 태그라인).
            "BOTTOM_RIGHT": _esc(self.channel.get("brand.brand.tagline", "")),
            "AUTOFIT_SCALES": json.dumps(list(scales)),
        }
        values.update(self._theme_values(style.theme, carousel.topic))
        rendered = template
        for key, value in values.items():
            rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
        return rendered

    def _theme_values(self, theme: str, topic: str) -> dict[str, str]:
        """테마별 색(배경·글자·보조·강조)을 코드에서 확정해 CSS 변수 값으로 돌려준다.

        대비가 모자라면 색을 조정하므로(colors.py) 흰 바탕에 흰 글씨 같은 조합이 나올 수 없다.
        """
        palette = theme_palette(
            theme,
            base_bg=str(self.channel.get("brand.colors.background", "#0D0D0D")),
            base_fg=str(self.channel.get("brand.colors.foreground", "#F5F5F5")),
            accent=self._accent(topic),
        )
        for name, ratio in palette_contrasts(palette).items():
            if ratio < MIN_TEXT_CONTRAST:
                logger.warning(
                    "[render] %s 테마 %s 대비 %.2f:1 (< %.1f) — 브랜드 색 조합을 확인하세요",
                    theme,
                    name,
                    ratio,
                    MIN_TEXT_CONTRAST,
                )
        return {
            "BG": palette.bg,
            "FG": palette.fg,
            "MUTED": palette.muted,
            "HAIRLINE": palette.hairline,
            "ACCENT": palette.accent,
        }

    # ------------------------------------------------------------------ 백엔드
    def _chrome_binary(self) -> str | None:
        for candidate in self.channel.get("instagram.renderer.chrome_candidates", []) or []:
            if Path(candidate).exists():
                return str(candidate)
        for name in ("google-chrome", "chromium", "chrome"):
            found = shutil.which(name)
            if found:
                return found
        return None

    def _available_backend(self) -> str:
        priority = self.channel.get("instagram.renderer.backend_priority", ["chrome_cli"]) or []
        for backend in priority:
            if backend == "playwright":
                try:
                    import playwright.sync_api  # type: ignore[import-not-found]  # noqa: F401

                    return "playwright"
                except ImportError:
                    logger.debug("[render] playwright 미설치 → 다음 백엔드 시도")
            elif backend == "chrome_cli" and self._chrome_binary():
                return "chrome_cli"
        raise RenderError(
            "렌더 백엔드를 찾을 수 없습니다. "
            "`pip install playwright && playwright install chromium` 또는 "
            "Google Chrome 설치가 필요합니다."
        )

    @staticmethod
    def _png_size(path: Path) -> tuple[int, int]:
        try:
            data = path.read_bytes()[:32]
            if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
                return (0, 0)
            width, height = struct.unpack(">II", data[16:24])
            return int(width), int(height)
        except OSError:
            return (0, 0)

    def _render_chrome(
        self, html_path: Path, png_path: Path, *, binary: str
    ) -> tuple[str, float, str]:
        width, height = self.channel.canvas
        timeout = float(self.channel.get("instagram.renderer.timeout_seconds", 60))
        common = [
            binary,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            f"--window-size={width},{height}",
            "--virtual-time-budget=2000",
        ]
        url = html_path.resolve().as_uri()

        # 1) autofit 결과 읽기 (DOM 덤프)
        try:
            dom = subprocess.run(  # noqa: S603 - 경로는 설정에서 온 바이너리
                [*common, "--dump-dom", url],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            ).stdout
        except subprocess.TimeoutExpired as exc:
            raise RenderError(f"렌더 타임아웃({timeout:.0f}s): {html_path.name}") from exc

        fit = "unknown"
        scale = 1.0
        metrics = ""
        match = re.search(r'data-fit="([^"]*)"', dom)
        if match:
            fit = match.group(1)
        match = re.search(r'data-fit-scale="([^"]*)"', dom)
        if match:
            try:
                scale = float(match.group(1))
            except ValueError:
                scale = 1.0
        match = re.search(r'data-fit-metrics="([^"]*)"', dom)
        if match:
            metrics = match.group(1)

        # 2) 스크린샷
        try:
            completed = subprocess.run(  # noqa: S603
                [*common, f"--screenshot={png_path}", url],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RenderError(f"스크린샷 타임아웃({timeout:.0f}s): {html_path.name}") from exc

        if not png_path.exists() or png_path.stat().st_size == 0:
            raise RenderError(
                f"PNG 생성 실패: {png_path.name}\n"
                f"  stderr: {(completed.stderr or '').strip()[:300]}"
            )
        return fit, scale, metrics

    def _render_playwright(self, html_path: Path, png_path: Path) -> tuple[str, float, str]:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

        width, height = self.channel.canvas
        timeout = float(self.channel.get("instagram.renderer.timeout_seconds", 60)) * 1000
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(
                    viewport={"width": width, "height": height}, device_scale_factor=1
                )
                page.goto(html_path.resolve().as_uri(), timeout=timeout)
                page.wait_for_timeout(120)
                fit = page.evaluate("document.documentElement.dataset.fit || 'unknown'")
                scale = page.evaluate("document.documentElement.dataset.fitScale || '1'")
                metrics = page.evaluate("document.documentElement.dataset.fitMetrics || ''")
                page.screenshot(path=str(png_path))
            finally:
                browser.close()
        try:
            scale_value = float(scale)
        except (TypeError, ValueError):
            scale_value = 1.0
        return str(fit), scale_value, str(metrics)

    # --------------------------------------------------------------------- API
    def render(self, carousel: Carousel, output_dir: Path) -> RenderResult:
        """슬라이드 PNG + preview.html 생성.

        Raises:
            RenderError: 백엔드가 없거나, PNG 크기가 캔버스와 다르거나, 최종
                배율에서도 텍스트가 넘치는 경우.
        """
        backend = self._available_backend()
        # 문구가 바뀌었을 수 있으므로 렌더 때마다 문구의 숫자에서 차트를 다시 만든다(LLM 없음).
        attach_visuals(carousel, self.channel)
        slides_dirname = self.channel.output_filename("slides_dir", "slides")
        slides_dir = output_dir / slides_dirname
        html_dir = output_dir / ".html"
        slides_dir.mkdir(parents=True, exist_ok=True)
        html_dir.mkdir(parents=True, exist_ok=True)

        result = RenderResult(backend=backend)
        name_format = str(
            self.channel.get("instagram.output.slide_filename_format", "{index:02d}.png")
        )
        chrome_binary = self._chrome_binary() if backend == "chrome_cli" else None
        expected = self.channel.canvas

        def render_html(html_path: Path, png_path: Path) -> tuple[str, float, str]:
            if backend == "playwright":
                return self._render_playwright(html_path, png_path)
            return self._render_chrome(html_path, png_path, binary=str(chrome_binary))

        styles: dict[int, SlideStyle] = {}
        for slide in carousel.slides:
            html_path = html_dir / f"{slide.index:02d}.html"
            png_path = slides_dir / name_format.format(index=slide.index)
            style = self._style_for(slide, carousel)
            html_path.write_text(self._slide_html(slide, carousel, style), encoding="utf-8")

            fit, scale, metrics = render_html(html_path, png_path)
            # 넘치면 글자를 줄이다 실패시키지 않고 장식부터 차례로 걷어낸다: 차트 → 레이아웃.
            # 문구는 건드리지 않는다.
            if fit != "ok" and slide.visual:
                logger.warning("[render] slide %02d: 차트 포함 시 넘쳐 차트 없이 렌더", slide.index)
                slide.visual = {}
                html_path.write_text(self._slide_html(slide, carousel, style), encoding="utf-8")
                fit, scale, metrics = render_html(html_path, png_path)
            if fit != "ok" and style.layout != "default":
                logger.warning(
                    "[render] slide %02d: %s 레이아웃에서 넘쳐 기본 레이아웃으로 렌더",
                    slide.index,
                    style.layout,
                )
                style = SlideStyle("default", style.theme)
                html_path.write_text(self._slide_html(slide, carousel, style), encoding="utf-8")
                fit, scale, metrics = render_html(html_path, png_path)
            styles[slide.index] = style

            width, height = self._png_size(png_path)
            if self.channel.get("instagram.renderer.verify_png_size", True) and (
                (width, height) != expected
            ):
                raise RenderError(
                    f"slide {slide.index} PNG 크기 불일치: {width}x{height} "
                    f"(기대 {expected[0]}x{expected[1]})"
                )

            result.slides.append(
                SlideRender(
                    index=slide.index,
                    png_path=png_path,
                    html_path=html_path,
                    fit=fit,
                    fit_scale=scale,
                    metrics=metrics,
                    width=width,
                    height=height,
                )
            )

        # 균일성: 한 포스트의 content·closing 슬라이드는 같은 글자 배율로 맞춘다.
        # 슬라이드마다 autofit 배율이 달라 글자 크기가 들쑥날쑥해 보이는 것을 막는다.
        if bool(self.channel.get("brand.autofit.uniform", True)):
            target = uniform_scale(
                [
                    (slide.type is SlideType.COVER, render.fit, render.fit_scale)
                    for slide, render in zip(carousel.slides, result.slides, strict=True)
                ]
            )
            for slide, render in zip(carousel.slides, result.slides, strict=True):
                if slide.type is SlideType.COVER or render.fit != "ok":
                    continue
                if target is None or render.fit_scale <= target + 1e-9:
                    continue
                html = self._slide_html(slide, carousel, styles[slide.index], scales=[target])
                render.html_path.write_text(html, encoding="utf-8")
                render.fit, render.fit_scale, render.metrics = render_html(
                    render.html_path, render.png_path
                )
                logger.info("[render] slide %02d 글자 배율 %.2f 로 통일", slide.index, target)

        for slide, render in zip(carousel.slides, result.slides, strict=True):
            style = styles[slide.index]
            slide.render = render.to_dict()
            slide.render["layout"] = style.layout
            slide.render["theme"] = style.theme
            logger.info(
                "[render] slide %02d → %s (fit=%s scale=%.2f %s)",
                slide.index,
                render.png_path.name,
                render.fit,
                render.fit_scale,
                render.metrics,
            )

        overflow = result.overflow_slides
        if overflow:
            min_scale = self.channel.get("brand.autofit.min_scale", 0.82)
            raise RenderError(
                f"텍스트 overflow: slide {overflow} — 최소 배율({min_scale})에서도 "
                "영역을 넘칩니다. 자동 잘라내기를 하지 않으므로 문구를 줄여야 합니다."
            )

        result.preview_path = self.write_preview(carousel, output_dir, result)
        return result

    def write_preview(
        self, carousel: Carousel, output_dir: Path, result: RenderResult
    ) -> Path:
        """사람이 한 번에 확인할 preview.html 을 만듭니다."""
        template = self._read(self.shared_dir / "preview.html")
        width, height = self.channel.canvas
        slides_dirname = self.channel.output_filename("slides_dir", "slides")

        figures: list[str] = []
        for render, slide in zip(result.slides, carousel.slides, strict=False):
            label = str(slide.type).upper()
            figures.append(
                "<figure>"
                f'<img src="{slides_dirname}/{render.png_path.name}" '
                f'alt="slide {render.index}" />'
                f"<figcaption><span>{render.index:02d} · {label}</span>"
                f"<span>fit {render.fit} ×{render.fit_scale:g}</span></figcaption>"
                "</figure>"
            )

        values = {
            "HEADLINE": _esc(carousel.headline),
            "TEMPLATE": _esc(carousel.template),
            "SLIDE_COUNT": str(carousel.slide_count),
            "TOPIC": _esc(carousel.topic),
            "CANVAS": f"{width}×{height}",
            "GENERATED_AT": to_iso(utcnow()) or "",
            "FIGURES": "\n".join(figures),
            "HOOKS": hook_preview_html(carousel, output_dir.name),
            "CAPTION": _esc(carousel.caption),
            "HASHTAGS": _esc(" ".join(carousel.hashtags)),
            "BG": str(self.channel.get("brand.colors.background", "#0D0D0D")),
            "FG": str(self.channel.get("brand.colors.foreground", "#F5F5F5")),
            "MUTED": str(self.channel.get("brand.colors.muted", "rgba(245,245,245,0.62)")),
            "FONT_STACK": self._css_variables()["FONT_STACK"],
        }
        rendered = template
        for key, value in values.items():
            rendered = rendered.replace(f"{{{{{key}}}}}", str(value))

        path = output_dir / self.channel.output_filename("preview_html", "preview.html")
        path.write_text(rendered, encoding="utf-8")
        logger.info("[render] preview → %s", path.name)
        return path
