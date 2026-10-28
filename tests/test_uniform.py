"""카드뉴스 균일성(uniform)과 로고 테스트.

같은 틀이 모든 슬라이드·모든 포스트 종류에서 유지되도록 구조와 실측 검증을 고정한다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from channel.src.config import load_channel_config
from channel.src.renderer import CarouselRenderer, RenderResult, SlideRender, uniform_scale
from channel.src.schema import Carousel, Slide, SlideType
from channel.src.validator import CarouselValidator

TEMPLATES = Path(__file__).resolve().parents[1] / "channel" / "templates" / "instagram"


@pytest.fixture
def channel():
    return load_channel_config()


def deck(template: str = "deep_dive", topic: str = "AI") -> Carousel:
    body = ["첫째 사실 3x", "둘째 사실", "셋째 사실"]
    count = {"news_short": 3, "news_standard": 5, "deep_dive": 7}[template]
    slides = [Slide(index=1, type=SlideType.COVER, headline="표지", subtitle="부제")]
    for index in range(2, count):
        slides.append(Slide(index=index, type=SlideType.CONTENT, title="제목", body=list(body)))
    slides.append(
        Slide(index=count, type=SlideType.CLOSING, title="Takeaway", body=["핵심"])
    )
    return Carousel(template=template, topic=topic, headline="표지", slides=slides)


def footer_of(html: str) -> str:
    match = re.search(r"<footer.*?</footer>", html, re.S)
    assert match
    return re.sub(r"\s+", " ", match.group(0))


class TestStructureIsUniform:
    @pytest.mark.parametrize("template", ["news_short", "news_standard", "deep_dive"])
    def test_every_slide_has_the_same_footer(self, channel, template):
        carousel = deck(template)
        renderer = CarouselRenderer(channel)
        footers = {
            footer_of(renderer._slide_html(slide, carousel)) for slide in carousel.slides
        }
        assert len(footers) == 1  # 표지·본문·마무리 모두 같은 로고 + 태그라인
        assert "R_A_Glance" in next(iter(footers)) and "바뀐 것만" in next(iter(footers))

    def test_templates_do_not_override_shared_design(self):
        """템플릿별 CSS 는 비워 둔다(제목 크기·간격이 포스트 종류마다 달라지는 것을 막음)."""
        for name in ("news_short", "news_standard", "deep_dive"):
            css = (TEMPLATES / name / "layout.css").read_text(encoding="utf-8")
            assert "{" not in re.sub(r"/\*.*?\*/", "", css, flags=re.S), f"{name} 에 규칙이 있음"

    def test_main_content_is_anchored_not_centered(self):
        css = (TEMPLATES / "shared" / "base.css").read_text(encoding="utf-8")
        main = re.search(r"\.slide__main \{(.*?)\}", css, re.S).group(1)
        assert "justify-content: flex-start" in main and "padding-top: var(--main-top)" in main
        assert "center" not in main.replace("align-items", "")

    def test_same_content_renders_identically_across_templates(self, channel):
        """같은 내용의 슬라이드는 포스트 종류와 관계없이 같은 HTML(클래스 제외 구조)이다."""
        slide_html = []
        for template in ("news_standard", "deep_dive"):
            carousel = deck(template)
            html = CarouselRenderer(channel)._slide_html(carousel.slides[1], carousel)
            body = html.split("<body>")[1]
            body = re.sub(r'(slide__eyebrow">)[^<]*', r"\1", body)  # 역할 이름은 템플릿마다 다르다
            slide_html.append(re.sub(r"slide--layout-\w+|slide--theme-\w+|\d{2} / \d{2}", "", body))
        assert slide_html[0] == slide_html[1]


class TestUniformScale:
    def test_takes_the_tightest_content_scale(self):
        slides = [(True, "ok", 1.0), (False, "ok", 1.0), (False, "ok", 0.88), (False, "ok", 0.94)]
        assert uniform_scale(slides) == 0.88  # 표지는 제외

    def test_cover_scale_never_drags_the_rest(self):
        assert uniform_scale([(True, "ok", 0.82), (False, "ok", 1.0), (False, "ok", 1.0)]) == 1.0

    def test_nothing_to_unify(self):
        assert uniform_scale([(True, "ok", 1.0), (False, "ok", 0.9)]) is None
        assert uniform_scale([(False, "overflow", 0.82), (False, "ok", 1.0)]) is None


def render_of(carousel: Carousel, tmp_path, geoms, scales=None) -> RenderResult:
    scales = scales or [1.0] * carousel.slide_count
    result = RenderResult(backend="test")
    for slide, geom, scale in zip(carousel.slides, geoms, scales, strict=True):
        png = tmp_path / f"{slide.index:02d}.png"
        png.write_bytes(b"x")
        result.slides.append(
            SlideRender(
                index=slide.index,
                png_path=png,
                html_path=png,
                fit_scale=scale,
                metrics=f"1/1 slide 1/1 geom {geom[0]},{geom[1]}",
            )
        )
    return result


class TestUniformityValidation:
    def codes(self, channel, carousel, render):
        report = CarouselValidator(channel).validate(carousel, render=render)
        return {finding.code for finding in report.warnings}

    def test_uniform_render_passes(self, channel, tmp_path):
        carousel = deck("news_standard")
        geoms = [(800, 1200), (200, 1212), (201, 1212), (200, 1212), (200, 1212)]
        render = render_of(carousel, tmp_path, geoms)
        codes = self.codes(channel, carousel, render)
        assert "layout_nonuniform" not in codes and "font_scale_mixed" not in codes

    def test_title_drift_is_flagged(self, channel, tmp_path):
        carousel = deck("news_standard")
        geoms = [(800, 1212), (200, 1212), (260, 1212), (200, 1212), (200, 1212)]
        render = render_of(carousel, tmp_path, geoms)
        assert "layout_nonuniform" in self.codes(channel, carousel, render)

    def test_footer_drift_is_flagged(self, channel, tmp_path):
        carousel = deck("news_standard")
        geoms = [(800, 1212), (200, 1212), (200, 1190), (200, 1212), (200, 1212)]
        render = render_of(carousel, tmp_path, geoms)
        assert "layout_nonuniform" in self.codes(channel, carousel, render)

    def test_cover_is_not_compared(self, channel, tmp_path):
        carousel = deck("news_standard")
        geoms = [(950, 1100)] + [(200, 1212)] * 4  # 표지는 아래 고정이라 y 가 달라도 정상
        render = render_of(carousel, tmp_path, geoms)
        assert "layout_nonuniform" not in self.codes(channel, carousel, render)

    def test_mixed_font_scale_is_flagged(self, channel, tmp_path):
        carousel = deck("news_standard")
        geoms = [(800, 1212)] + [(200, 1212)] * 4
        render = render_of(carousel, tmp_path, geoms, [1.0, 1.0, 0.88, 1.0, 1.0])
        assert "font_scale_mixed" in self.codes(channel, carousel, render)


class TestLogo:
    def html(self, channel):
        carousel = deck("news_standard")
        return CarouselRenderer(channel)._slide_html(carousel.slides[1], carousel)

    def test_project_logo_is_the_transparent_png_painted_in_theme_color(self, channel):
        """저장소의 실제 로고(logo/Basic/raglance_black.png, 1774x887)가 mask 로 렌더된다."""
        html = self.html(channel)
        assert 'class="logo logo--mask"' in html and "data:image/png;base64," in html
        assert "height:60px;width:120px" in html  # 2:1 비율

    def test_jpeg_logo_is_never_masked(self, channel, tmp_path):
        """JPG 는 투명 배경이 없어 mask 로 칠하면 사각형이 된다 → 원본 색으로 쓴다."""
        jpg = tmp_path / "logo.jpg"
        jpg.write_bytes(b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x32\x00\x64" + b"\x00" * 20)
        channel.brand["logo"] = {"path": str(jpg), "mode": "mask", "height": 40}
        html = self.html(channel)
        assert 'class="logo logo--image"' in html and 'class="logo logo--mask"' not in html
        assert "height:40px;width:80px" in html  # 100x50

    def test_default_logo_is_drawn_in_code(self, channel):
        channel.brand["logo"] = {}
        html = self.html(channel)
        assert 'class="logo logo--default"' in html and 'aria-label="R_A_Glance"' in html
        assert "<b>R_A_</b>Glance" in html

    def test_svg_logo_file_is_masked_with_theme_color(self, channel, tmp_path):
        svg = tmp_path / "logo.svg"
        svg.write_text('<svg viewBox="0 0 200 50" xmlns="http://www.w3.org/2000/svg"></svg>')
        channel.brand["logo"] = {"path": str(svg), "mode": "mask", "height": 40}
        html = self.html(channel)
        assert 'class="logo logo--mask"' in html
        assert "mask-image:url('data:image/svg+xml;base64," in html
        assert "height:40px;width:160px" in html  # 200:50 비율

    def test_png_logo_image_mode_keeps_original_colors(self, channel, tmp_path):
        png = tmp_path / "logo.png"
        # 1x1 PNG 헤더만으로 크기를 읽는다(100x50 으로 기록)
        png.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00d\x00\x00\x002\x08\x06\x00\x00\x00"
        )
        channel.brand["logo"] = {"path": str(png), "mode": "image", "height": 40}
        html = self.html(channel)
        assert 'class="logo logo--image"' in html and "data:image/png;base64," in html
        assert "height:40px;width:80px" in html

    def test_missing_or_unreadable_logo_falls_back(self, channel, tmp_path):
        channel.brand["logo"] = {"path": str(tmp_path / "없음.svg"), "mode": "mask", "height": 40}
        assert 'class="logo logo--default"' in self.html(channel)
        broken = tmp_path / "logo.svg"
        broken.write_text("<svg></svg>")  # viewBox·크기 없음
        channel.brand["logo"] = {"path": str(broken), "mode": "mask", "height": 40}
        assert 'class="logo logo--default"' in self.html(channel)

    def test_logo_color_follows_theme(self):
        css = (TEMPLATES / "shared" / "base.css").read_text(encoding="utf-8")
        assert re.search(r"\.logo--mask \{[^}]*background: var\(--fg\)", css)
