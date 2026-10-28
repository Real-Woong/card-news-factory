"""HTML 본문 추출: boilerplate 제거와 semantic 영역 우선 선택."""

from __future__ import annotations

import pytest

from src.collectors.extract import _extract_with_stdlib, html_to_text

ARTICLE_PAGE = """
<html><head><title>T</title><style>.a{color:red}</style>
<script>window.analytics=1</script></head>
<body>
  <div class="site-header"><a href="/">Home</a><a href="/blog">Blog</a></div>
  <nav class="navbar"><ul><li>Products</li><li>Pricing</li><li>Docs</li></ul></nav>
  <div id="cookie-consent">We use cookies to improve your experience. Accept all</div>
  <main>
    <article class="post-content">
      <h1>Structured outputs are now generally available</h1>
      <p>Median added latency drops from 340 ms to 120 ms, and pricing is unchanged.</p>
      <p>The beta header is no longer required and will be ignored after November 1.</p>
      <div class="share-buttons">Share on X Share on LinkedIn Copy link</div>
      <aside class="related-posts"><h3>Related posts</h3><a>Another article</a></aside>
    </article>
  </main>
  <div class="newsletter-signup">Subscribe to our newsletter</div>
  <footer class="site-footer">© 2026 Example Inc. All rights reserved. Privacy Policy</footer>
</body></html>
"""


class TestBoilerplateRemoval:
    @pytest.fixture
    def text(self) -> str:
        return html_to_text(ARTICLE_PAGE)

    def test_keeps_body_sentences(self, text):
        assert "Median added latency drops from 340 ms to 120 ms" in text
        assert "beta header is no longer required" in text

    def test_drops_script_and_style(self, text):
        assert "window.analytics" not in text
        assert "color:red" not in text

    @pytest.mark.parametrize(
        "noise",
        [
            "Products",             # nav (본문에 'pricing' 이 있으므로 다른 토큰으로 검사)
            "We use cookies",       # cookie banner
            "Share on X",           # share buttons
            "Related posts",        # related posts block
            "Subscribe to our",     # newsletter
            "All rights reserved",  # footer
            "Home",                 # site header links
        ],
    )
    def test_drops_boilerplate(self, text, noise):
        assert noise.lower() not in text.lower(), f"{noise!r} 가 남아 있음"


class TestSemanticContainer:
    def test_prefers_article_over_page_chrome(self):
        html = """
        <body>
          <div class="promo">BUY NOW BUY NOW BUY NOW</div>
          <article><p>%s</p></article>
        </body>
        """ % ("Real body sentence with enough length to be picked as content. " * 5)
        text = html_to_text(html)
        assert "Real body sentence" in text
        assert "BUY NOW" not in text

    def test_content_ancestor_is_not_dropped(self):
        """상위 wrapper class 가 패턴에 걸려도 본문이 사라지면 안 됨.

        실측 회귀: ``openai.com/news`` 가 0자로 추출되던 케이스.
        """
        html = """
        <body>
          <div class="search-wrapper">
            <div class="nav-container">
              <main><p>%s</p></main>
            </div>
          </div>
        </body>
        """ % ("Important announcement body text that must survive extraction. " * 5)
        text = html_to_text(html)
        assert "Important announcement body text" in text

    def test_stub_article_falls_back_to_body(self):
        """JS 렌더링 페이지처럼 <article> 이 껍데기면 body 를 사용."""
        html = """
        <body>
          <article><p>Loading…</p></article>
          <div class="wrapper"><p>%s</p></div>
        </body>
        """ % ("The actual rendered paragraph that holds the article body. " * 6)
        text = html_to_text(html)
        assert "actual rendered paragraph" in text


class TestRobustness:
    def test_empty_input(self):
        assert html_to_text("") == ""
        assert html_to_text("   ") == ""

    def test_malformed_html_does_not_raise(self):
        assert isinstance(html_to_text("<div><p>unclosed <b>tags"), str)

    def test_plain_text_input_survives(self):
        assert "hello world" in html_to_text("hello world")

    def test_repeated_boilerplate_line_is_deduplicated(self):
        html = "<body>" + "<p>Sign in to continue reading this page</p>" * 4 + "</body>"
        text = html_to_text(html)
        assert text.lower().count("sign in to continue") <= 1

    def test_stdlib_fallback_also_filters(self):
        """bs4 가 없는 환경에서도 같은 규칙이 최소한 적용되어야 함."""
        text = _extract_with_stdlib(ARTICLE_PAGE)
        assert "Median added latency" in text
        assert "window.analytics" not in text
        assert "we use cookies" not in text.lower()


class TestRealWorldPattern:
    def test_solana_style_header_noise_is_removed(self):
        """실제 오염 사례: 제목 앞에 붙던 사이트 헤더/바이라인 연결 문자열."""
        html = """
        <body>
          <div class="nav-back"><a href="/news">Back to Changelog</a></div>
          <main>
            <article>
              <h1>Solana Changelog: September 18, 2026</h1>
              <p>Notable feature gates: Transaction V1, rent reduction to 5080 lamports.</p>
              <p>Slot time reduction to 250ms is now active on testnet.</p>
            </article>
          </main>
        </body>
        """
        text = html_to_text(html)
        assert "Back to Changelog" not in text
        assert "Transaction V1" in text
        assert "250ms" in text
