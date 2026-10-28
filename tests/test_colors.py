"""테마 색 대비 보장 테스트 (흰 바탕에 흰 글씨 같은 조합이 나오지 않게 한다)."""

from __future__ import annotations

import colorsys
import re

import pytest

from channel.src.colors import (
    MIN_TEXT_CONTRAST,
    contrast,
    ensure_contrast,
    palette_contrasts,
    parse_hex,
    theme_palette,
    to_hex,
)
from channel.src.config import load_channel_config
from channel.src.renderer import CarouselRenderer
from channel.src.schema import Carousel, Slide, SlideType

BASE = {"base_bg": "#0D0D0D", "base_fg": "#F5F5F5"}
THEMES = ("dark", "accent", "light")


def test_contrast_reference_values():
    assert contrast(parse_hex("#000000"), parse_hex("#FFFFFF")) == pytest.approx(21.0)
    assert contrast(parse_hex("#777777"), parse_hex("#777777")) == pytest.approx(1.0)


def test_ensure_contrast_darkens_on_light_and_lightens_on_dark():
    light_bg, dark_bg = parse_hex("#F5F5F5"), parse_hex("#0D0D0D")
    orange = parse_hex("#FFB020")
    assert contrast(orange, light_bg) < 2  # 흰 바탕의 주황은 거의 안 보인다
    fixed = ensure_contrast(orange, light_bg)
    assert contrast(fixed, light_bg) >= MIN_TEXT_CONTRAST
    dim = parse_hex("#202020")
    assert contrast(ensure_contrast(dim, dark_bg), dark_bg) >= MIN_TEXT_CONTRAST


ACCENTS = list(load_channel_config().get("brand.colors.accent").values())


@pytest.mark.parametrize("accent", ACCENTS)
@pytest.mark.parametrize("theme", THEMES)
def test_brand_accents_meet_contrast_in_every_theme(accent, theme):
    palette = theme_palette(theme, accent=accent, **BASE)
    for name, ratio in palette_contrasts(palette).items():
        assert ratio >= MIN_TEXT_CONTRAST, f"{theme}/{accent} {name} {ratio:.2f}:1"
    assert palette.bg != palette.fg


def test_any_accent_color_stays_readable():
    """색상환을 훑어도 글자색·보조색·강조색이 항상 읽힌다(포인트 컬러를 바꿔도 안전)."""
    worst = 99.0
    for hue in range(0, 360, 15):
        for lightness in (0.3, 0.5, 0.7):
            r, g, b = colorsys.hls_to_rgb(hue / 360, lightness, 0.9)
            accent = to_hex((r * 255, g * 255, b * 255))
            for theme in THEMES:
                ratios = palette_contrasts(theme_palette(theme, accent=accent, **BASE))
                worst = min(worst, *ratios.values())
    assert worst >= MIN_TEXT_CONTRAST


def deck(topic: str) -> Carousel:
    body = ["첫째 사실 3x", "둘째 사실", "셋째 사실"]
    slides = [Slide(index=1, type=SlideType.COVER, headline="표지", subtitle="부제")]
    for index in range(2, 7):
        slides.append(Slide(index=index, type=SlideType.CONTENT, title="제목", body=list(body)))
    slides.append(
        Slide(
            index=7,
            type=SlideType.CLOSING,
            title="Takeaway",
            body=["핵심"],
            extra_title="What to watch next",
            extra_body=["지켜볼 것"],
        )
    )
    return Carousel(template="deep_dive", topic=topic, headline="표지", slides=slides)


def declared(html: str, name: str) -> str:
    return re.search(rf"--{name}: ([^;]+);", html.split("</style>")[0]).group(1).strip()


@pytest.mark.parametrize("topic", ["AI", "BLOCKCHAIN"])
def test_rendered_slides_never_have_unreadable_text(topic):
    """실제 렌더 HTML 에 주입된 색으로 검사한다 — 7장 덱의 모든 슬라이드."""
    channel = load_channel_config()
    renderer = CarouselRenderer(channel)
    carousel = deck(topic)
    seen_themes = set()
    for slide in carousel.slides:
        html = renderer._slide_html(slide, carousel)
        bg = parse_hex(declared(html, "bg"))
        for name in ("fg", "muted", "accent"):
            ratio = contrast(parse_hex(declared(html, name)), bg)
            assert ratio >= MIN_TEXT_CONTRAST, f"slide {slide.index} --{name} {ratio:.2f}:1"
        seen_themes.add(re.search(r"slide--theme-(\w+)", html).group(1))
    assert seen_themes == {"dark", "accent", "light"}


def test_slide_sets_its_own_text_color():
    """테마의 --fg 가 상속되는 글자색에도 적용돼야 한다(예전 흰 바탕·흰 글씨 버그의 원인)."""
    css = (load_channel_config().templates_dir / "shared" / "base.css").read_text(encoding="utf-8")
    slide_rule = re.search(r"\.slide \{(.*?)\}", css, re.S).group(1)
    assert "color: var(--fg)" in slide_rule
