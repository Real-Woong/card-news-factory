"""배경 테마별 색 계산과 대비(contrast) 보장.

CSS ``color-mix`` 같은 트릭에 기대지 않고, 테마마다 배경·글자·보조·강조 색을 **코드에서 확정**합니다.
WCAG 상대 휘도로 대비를 재서 기준(기본 4.5:1)에 못 미치면 색을 자동으로 조정합니다.

* 글자색은 배경과 대비가 큰 쪽(브랜드 검정/흰색)을 고릅니다.
* 보조(muted) 글자는 불투명도를 올려 대비를 맞추고, 미리 섞은 단색으로 내보냅니다.
* 강조(accent) 색은 배경 위에서 대비가 모자라면 배경 반대쪽(검정/흰색)으로 어둡게/밝게 조정합니다.
  (예: 주황 ``#FFB020`` 은 흰 바탕에서 1.7:1 이라 어두운 호박색으로 내려갑니다.)
"""

from __future__ import annotations

from dataclasses import dataclass

RGB = tuple[float, float, float]

MIN_TEXT_CONTRAST = 4.5


def parse_hex(value: str) -> RGB:
    """``#RGB`` / ``#RRGGBB`` → (r, g, b) 0~255."""
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError(f"색은 #RRGGBB 형식이어야 합니다: {value!r}")
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def to_hex(rgb: RGB) -> str:
    r, g, b = (max(0, min(255, round(channel))) for channel in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def _linear(channel: float) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb: RGB) -> float:
    r, g, b = (_linear(channel) for channel in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: RGB, b: RGB) -> float:
    """WCAG 대비 비율 (1~21)."""
    la, lb = luminance(a), luminance(b)
    hi, lo = (la, lb) if la >= lb else (lb, la)
    return (hi + 0.05) / (lo + 0.05)


def mix(a: RGB, b: RGB, t: float) -> RGB:
    """a→b 로 t(0~1) 만큼 섞기. 결과는 정수 RGB 로 반올림한다(hex 로 내보낼 값과 같아야
    대비를 정확히 잴 수 있다 — 반올림 오차로 4.49:1 이 되는 것을 막는다)."""
    return tuple(round(a[i] * (1 - t) + b[i] * t) for i in range(3))  # type: ignore[return-value]


def blend(fg: RGB, bg: RGB, alpha: float) -> RGB:
    """투명도 alpha 의 fg 를 bg 위에 올린 단색."""
    return mix(bg, fg, alpha)


def readable_on(bg: RGB, candidates: list[RGB]) -> RGB:
    """배경과 대비가 가장 큰 후보."""
    return max(candidates, key=lambda candidate: contrast(candidate, bg))


def ensure_contrast(color: RGB, bg: RGB, minimum: float = MIN_TEXT_CONTRAST) -> RGB:
    """배경 위에서 대비가 minimum 이상이 되도록 색을 배경 반대쪽으로 조정한다(색상은 유지)."""
    if contrast(color, bg) >= minimum:
        return color
    target: RGB = (0, 0, 0) if luminance(bg) > 0.179 else (255, 255, 255)
    for step in range(1, 101):
        candidate = mix(color, target, step / 100)
        if contrast(candidate, bg) >= minimum:
            return candidate
    return target


@dataclass(frozen=True)
class Palette:
    """테마 하나의 색 집합(모두 CSS 에 그대로 쓰는 문자열)."""

    bg: str
    fg: str
    muted: str
    hairline: str
    accent: str


def theme_palette(
    theme: str, *, base_bg: str, base_fg: str, accent: str, minimum: float = MIN_TEXT_CONTRAST
) -> Palette:
    """``dark`` / ``accent`` / ``light`` 테마의 색을 확정한다."""
    brand_bg, brand_fg, brand_accent = parse_hex(base_bg), parse_hex(base_fg), parse_hex(accent)

    if theme == "accent":
        bg = brand_accent
        # 브랜드 검정/흰색으로 기준을 못 맞추는 중간 밝기 포인트 컬러면 순수 검정/흰색 쪽으로 보정
        fg = ensure_contrast(readable_on(bg, [brand_bg, brand_fg]), bg, minimum)
        deco = fg  # 포인트 컬러 바탕에서는 강조·장식도 글자색으로(바탕과 같은 색 금지)
    elif theme == "light":
        bg, fg = brand_fg, brand_bg
        deco = ensure_contrast(brand_accent, bg, minimum)
    else:  # dark
        bg, fg = brand_bg, brand_fg
        deco = ensure_contrast(brand_accent, bg, minimum)

    # 보조 글자: 62% 에서 시작해 대비 기준을 넘을 때까지 불투명도를 올린다.
    alpha = 0.62
    muted = blend(fg, bg, alpha)
    while contrast(muted, bg) < minimum and alpha < 1.0:
        alpha = min(1.0, alpha + 0.04)
        muted = blend(fg, bg, alpha)

    r, g, b = (round(channel) for channel in fg)
    return Palette(
        bg=to_hex(bg),
        fg=to_hex(fg),
        muted=to_hex(muted),
        hairline=f"rgba({r}, {g}, {b}, 0.16)",
        accent=to_hex(deco),
    )


def palette_contrasts(palette: Palette) -> dict[str, float]:
    """검증·테스트용: 배경 대비 비율."""
    bg = parse_hex(palette.bg)
    return {
        "fg": contrast(parse_hex(palette.fg), bg),
        "muted": contrast(parse_hex(palette.muted), bg),
        "accent": contrast(parse_hex(palette.accent), bg),
    }
