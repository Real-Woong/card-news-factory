"""카테고리 ↔ 색·해시태그 일관성 테스트.

카테고리를 추가할 때 색·해시태그를 빠뜨리거나, 서로 구분되지 않는 색을 고르면 실패한다
(카테고리 확장이 파이프라인을 어지럽히지 못하게 하는 안전망).
"""

from __future__ import annotations

import itertools
import math
from pathlib import Path

import pytest
import yaml

from channel.src.colors import MIN_TEXT_CONTRAST, palette_contrasts, parse_hex, theme_palette
from channel.src.config import load_channel_config

ROOT = Path(__file__).resolve().parents[1]
MIN_DELTA_E = 40.0  # 이보다 가까우면 두 카테고리 색을 구분하기 어렵다

CATEGORIES = ["blockchain", "ai", "security", "policy", "science", "health"]


def lab(color: str) -> tuple[float, float, float]:
    def linear(channel: float) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in parse_hex(color))
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


@pytest.fixture(scope="module")
def channel():
    return load_channel_config()


def test_every_planned_category_has_an_accent_and_a_hashtag(channel):
    accents = channel.get("brand.colors.accent")
    hashtags = channel.get("instagram.caption.hashtags.base")
    assert "default" in accents  # 분류되지 않은 소식(기타)은 흰색
    for category in CATEGORIES:
        assert category in accents, f"{category}: 포인트 컬러 없음"
        assert category in hashtags, f"{category}: 기본 해시태그 없음"


def test_engine_categories_are_covered_by_the_channel():
    """엔진(config/topics.yaml)에 있는 카테고리는 채널 색·해시태그가 반드시 있어야 한다."""
    engine = yaml.safe_load((ROOT / "config" / "topics.yaml").read_text(encoding="utf-8"))
    channel = load_channel_config()
    accents = channel.get("brand.colors.accent")
    hashtags = channel.get("instagram.caption.hashtags.base")
    for category in engine["categories"]:
        assert category in accents and category in hashtags, f"{category}: 채널 설정 누락"


def test_category_colors_are_distinguishable(channel):
    accents = channel.get("brand.colors.accent")
    colors = {name: accents[name] for name in [*CATEGORIES, "default"]}
    for (a, ca), (b, cb) in itertools.combinations(colors.items(), 2):
        distance = math.dist(lab(ca), lab(cb))
        assert distance >= MIN_DELTA_E, f"{a}/{b} 색이 너무 비슷합니다 (ΔE {distance:.1f})"


@pytest.mark.parametrize("category", [*CATEGORIES, "default"])
@pytest.mark.parametrize("theme", ["dark", "accent", "light"])
def test_every_category_color_is_readable_in_every_theme(channel, category, theme):
    accent = channel.get("brand.colors.accent")[category]
    palette = theme_palette(theme, base_bg="#0D0D0D", base_fg="#F5F5F5", accent=accent)
    for name, ratio in palette_contrasts(palette).items():
        assert ratio >= MIN_TEXT_CONTRAST, f"{category}/{theme} {name} {ratio:.2f}:1"


def test_unknown_category_falls_back_to_white(channel):
    from channel.src.renderer import CarouselRenderer

    renderer = CarouselRenderer(channel)
    assert renderer._accent("BLOCKCHAIN") == "#FFB020"
    assert renderer._accent("something-new") == "#F5F5F5"
    assert renderer._accent("") == "#F5F5F5"
