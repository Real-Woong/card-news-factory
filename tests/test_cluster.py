"""토픽 클러스터링: 다중 신호 판정과 authority 정렬."""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.core.text import utcnow
from src.ranking.cluster import (
    AUTHORITY_ORDER,
    ClusterService,
    TopicCluster,
    authority_label,
    authority_rank,
    is_primary_tier,
)
from tests.conftest import make_article


@pytest.fixture
def service(config):
    return ClusterService(config)


def article(**kwargs):
    return make_article(**kwargs)


class TestAuthority:
    def test_order(self):
        assert AUTHORITY_ORDER["official"] < AUTHORITY_ORDER["github"]
        assert AUTHORITY_ORDER["github"] < AUTHORITY_ORDER["paper"]
        assert AUTHORITY_ORDER["paper"] < AUTHORITY_ORDER["major_news"]

    @pytest.mark.parametrize(
        ("tier", "expected"),
        [("official", "primary"), ("github", "primary"), ("paper", "primary"),
         ("major_news", "secondary"), ("community", "secondary"), ("social", "secondary")],
    )
    def test_labels(self, tier, expected):
        assert authority_label(tier) == expected
        assert is_primary_tier(tier) is (expected == "primary")

    def test_unknown_tier_is_last(self):
        assert authority_rank("bogus") > authority_rank("social")


class TestSignals:
    def test_same_story_different_outlet(self, service):
        official = article(
            url="https://openai.com/index/structured-ga/",
            title="Structured outputs are now generally available",
            source_id="openai_news",
            trust_tier="official",
            content="Structured outputs are now generally available. Median latency drops "
            "from 340 ms to 120 ms. The beta header is removed in v2.1.0.",
        )
        news = article(
            url="https://techcrunch.com/structured-ga/",
            title="OpenAI makes structured outputs generally available",
            source_id="techcrunch_ai",
            trust_tier="major_news",
            trust_score=3,
            content="Structured outputs are now generally available, OpenAI said. Median "
            "latency drops from 340 ms to 120 ms and the beta header is removed in v2.1.0.",
        )
        matched, signals = service.same_event(official, news)
        assert matched, f"신호: {signals.to_dict()}"
        assert len(signals.agreed) >= 2

    def test_unrelated_articles_do_not_match(self, service):
        left = article(title="Structured outputs are now generally available")
        right = article(
            url="https://blog.ethereum.org/devnet-9",
            title="Devnet-9 raises blob throughput",
            source_id="ethereum_blog",
            category="blockchain",
            content="Devnet-9 raises the blob target from 6 to 12 per block.",
        )
        matched, signals = service.same_event(left, right)
        assert not matched
        assert len(signals.agreed) < 2

    def test_time_window_is_mandatory(self, service):
        left = article(days_ago=0.5)
        right = article(url="https://mirror.example.com/x", source_id="techcrunch_ai", days_ago=30)
        matched, signals = service.same_event(left, right)
        assert not matched
        assert signals.hours_apart > service.window_hours

    def test_different_releases_of_same_repo_are_separate(self, service):
        older = article(
            url="https://github.com/ollama/ollama/releases/tag/v0.34.1",
            title="ollama/ollama: v0.34.1",
            source_id="gh_ollama",
            trust_tier="github",
            content="Release notes for v0.34.1. Bug fixes.",
        )
        newer = article(
            url="https://github.com/ollama/ollama/releases/tag/v0.34.3",
            title="ollama/ollama: v0.34.3",
            source_id="gh_ollama",
            trust_tier="github",
            content="Release notes for v0.34.3. Bug fixes.",
        )
        matched, _ = service.same_event(older, newer)
        assert not matched, "서로 다른 릴리스를 같은 사건으로 묶으면 안 됩니다"

    def test_single_signal_is_not_enough(self, service):
        """제목만 비슷하고 본문이 전혀 다르면 묶지 않습니다."""
        left = article(title="Gemini 3.8 Live 공개", content="A" * 400)
        right = article(
            url="https://other.example.com/b",
            source_id="techcrunch_ai",
            title="Gemini 3.8 Live 공개",
            content="완전히 다른 주제의 본문입니다. " * 20,
        )
        _, signals = service.same_event(left, right)
        # 제목이 동일하므로 title/entity 는 동의하지만 body 는 동의하지 않아야 함
        assert "body" not in signals.agreed


class TestBuild:
    def test_official_becomes_primary(self, service):
        news = article(
            url="https://techcrunch.com/x",
            title="OpenAI makes structured outputs generally available",
            source_id="techcrunch_ai",
            trust_tier="major_news",
            trust_score=3,
            content="Structured outputs are now generally available. Median latency drops "
            "from 340 ms to 120 ms. Beta header removed in v2.1.0.",
        )
        official = article(
            title="Structured outputs are now generally available",
            content="Structured outputs are now generally available. Median latency drops "
            "from 340 ms to 120 ms. Beta header removed in v2.1.0.",
        )
        clusters = service.build([news, official])
        multi = [c for c in clusters if c.size > 1]
        assert len(multi) == 1
        cluster = multi[0]
        assert cluster.primary.trust_tier == "official"
        assert cluster.has_primary_source

    def test_sources_are_authority_ordered(self, service):
        official = article(content="Same event body text with numbers 340 ms and v2.1.0.")
        gh = article(
            url="https://github.com/openai/x/releases/tag/v2.1.0",
            title="openai/x: v2.1.0",
            source_id="gh_vllm",
            trust_tier="github",
            content="Same event body text with numbers 340 ms and v2.1.0.",
        )
        news = article(
            url="https://techcrunch.com/y",
            title="OpenAI ships v2.1.0",
            source_id="techcrunch_ai",
            trust_tier="major_news",
            trust_score=3,
            content="Same event body text with numbers 340 ms and v2.1.0.",
        )
        cluster = TopicCluster(primary=official, members=[news, gh])
        authorities = [s["authority"] for s in cluster.sources()]
        assert authorities[0] == "primary"
        # secondary 는 뒤로 밀려야 함
        assert authorities[-1] == "secondary"
        assert [s["trust_tier"] for s in cluster.sources()][:2] == ["official", "github"]

    def test_sources_include_type_and_url(self, service):
        cluster = TopicCluster(primary=article())
        entry = cluster.sources()[0]
        assert {"name", "url", "type", "authority"} <= set(entry)

    def test_every_article_is_assigned_once(self, service):
        articles = [
            article(url=f"https://example.com/a{i}", title=f"완전히 다른 제목 {i}", content=f"본문 {i}")
            for i in range(5)
        ]
        clusters = service.build(articles)
        assert sum(c.size for c in clusters) == len(articles)

    def test_max_members_is_respected(self, config):
        service = ClusterService(config)
        base = "Structured outputs are now generally available. Median latency 340 ms to 120 ms."
        articles = [article(title="Structured outputs are now generally available", content=base)]
        for i in range(8):
            articles.append(
                article(
                    url=f"https://news{i}.example.com/x",
                    title="Structured outputs now generally available",
                    source_id="techcrunch_ai",
                    trust_tier="major_news",
                    trust_score=3,
                    content=base,
                )
            )
        clusters = service.build(articles)
        assert all(c.size <= service.max_size for c in clusters)

    def test_disabled_service_returns_singletons(self, config):
        config.pipeline.setdefault("cluster", {})["enabled"] = False
        service = ClusterService(config)
        articles = [article(), article(url="https://x.dev/2", title="Another")]
        clusters = service.build(articles)
        assert all(c.size == 1 for c in clusters)
        config.pipeline["cluster"]["enabled"] = True

    def test_cluster_for_finds_member(self, service):
        primary = article()
        member = article(url="https://news.example.com/x", source_id="techcrunch_ai")
        cluster = TopicCluster(primary=primary, members=[member])
        assert service.cluster_for(member, [cluster]) is cluster
        assert service.cluster_for(primary, [cluster]) is cluster
        assert service.cluster_for(article(url="https://nope.dev/1"), [cluster]) is None


class TestResearchIntegration:
    def test_note_carries_cluster_sources(self, config):
        from src.agents.llm import MockLLM
        from src.agents.research import ResearchAgent

        primary = article()
        member = article(
            url="https://techcrunch.com/x",
            title="OpenAI makes structured outputs GA",
            source_id="techcrunch_ai",
            trust_tier="major_news",
            trust_score=3,
        )
        cluster = TopicCluster(primary=primary, members=[member])
        note = ResearchAgent(config=config, llm=MockLLM()).run(primary, [], cluster=cluster)
        assert len(note.sources) == 2
        assert note.sources[0]["authority"] == "primary"
        assert any(s["authority"] == "secondary" for s in note.sources)

    def test_note_without_cluster_has_single_source(self, config):
        from src.agents.llm import MockLLM
        from src.agents.research import ResearchAgent

        note = ResearchAgent(config=config, llm=MockLLM()).run(article(), [])
        assert len(note.sources) == 1
        assert note.sources[0]["primary"] is True


class TestAntiFalsePositive:
    """실측 오탐을 회귀 테스트로 고정."""

    def test_same_source_is_never_clustered(self, service):
        """같은 피드의 두 기사는 서로 다른 사건이다.

        실측: arxiv_ai 의 서로 다른 논문 2편, openai_news 의 고객 사례 2건이
        공통 bigram 때문에 묶였다.
        """
        body = "Coding agents with an obstacle aware harness for software tasks."
        left = article(
            url="http://arxiv.org/abs/2599.1", title="Coding Agents with an Obstacle-Aware Harness",
            source_id="arxiv_ai", trust_tier="paper", trust_score=4, content=body,
        )
        right = article(
            url="http://arxiv.org/abs/2599.2", title="An Empirical Study of Harness Design for Coding Agents",
            source_id="arxiv_ai", trust_tier="paper", trust_score=4, content=body,
        )
        matched, _ = service.same_event(left, right)
        assert not matched

    def test_bare_number_does_not_count_as_version(self, service):
        """'GPT-6' 의 6 같은 맨 숫자로 version 신호가 켜지면 안 된다."""
        left = article(title="Hex turns complex analysis into visual reports with GPT-6")
        right = article(
            url="https://openai.com/index/perplexity/",
            title="Perplexity trusts GPT-6 with end-to-end research",
            source_id="anthropic_news",
        )
        _, signals = service.same_event(left, right)
        assert signals.version == 0.0

    def test_release_channel_difference_blocks_cluster(self, service):
        left = article(
            url="https://github.com/MystenLabs/sui/releases/tag/testnet-v1.80.0",
            title="MystenLabs/sui: testnet-v1.80.0", source_id="gh_sui",
            trust_tier="github", content="Sui release notes v1.80.0 for the network.",
        )
        right = article(
            url="https://github.com/MystenLabs/sui/releases/tag/devnet-v1.80.0",
            title="MystenLabs/sui: devnet-v1.80.0", source_id="gh_reth",
            trust_tier="github", content="Sui release notes v1.80.0 for the network.",
        )
        matched, _ = service.same_event(left, right)
        assert not matched, "devnet/testnet 은 서로 다른 아티팩트"

    def test_anchor_required(self, config, monkeypatch):
        """공통 구절/버전 anchor 가 없으면 다른 신호가 있어도 묶지 않는다.

        자연스러운 한국어 문장으로는 'bigram 없이 title/body 만 높은' 상황을 만들기
        어려워, 게이트 자체를 신호 주입으로 검증한다.
        """
        from src.ranking.cluster import ClusterSignals

        service = ClusterService(config)
        assert service.require_anchor

        left = article()
        right = article(url="https://other.dev/x", source_id="techcrunch_ai")

        no_anchor = ClusterSignals(
            title=0.9, entity=0.9, body=0.9, phrase=0.0, version=0.0, hours_apart=1.0,
            agreed=["title", "entity", "body"],
        )
        monkeypatch.setattr(service, "signals", lambda a, b: no_anchor)
        matched, _ = service.same_event(left, right)
        assert not matched, "anchor 없이 묶였습니다"

        with_anchor = ClusterSignals(
            title=0.6, entity=0.4, body=0.2, phrase=0.5, version=0.0, hours_apart=1.0,
            agreed=["title", "phrase"],
        )
        monkeypatch.setattr(service, "signals", lambda a, b: with_anchor)
        matched, _ = service.same_event(left, right)
        assert matched, "anchor + 2신호인데 묶이지 않았습니다"


class TestClusterAwareSelection:
    """같은 사건의 두 기사가 각각 선택되면 같은 내용이 두 번 게시된다."""

    def test_selector_takes_one_per_cluster(self, config):
        from src.ranking.scorer import ArticleScorer
        from src.ranking.selector import TopicSelector

        body = "Structured outputs are now generally available. Latency 340 ms to 120 ms."
        vendor = article(title="Structured outputs are now generally available", content=body)
        recap = article(
            url="https://huggingface.co/blog/recap",
            title="Structured outputs reach GA: what it means for serving",
            source_id="huggingface_blog", content=body,
        )
        other = article(
            url="https://blog.ethereum.org/devnet-9", title="Devnet-9 raises blob throughput",
            source_id="ethereum_blog", category="blockchain",
            content="Blob target moves from 6 to 12 per block.",
        )
        scorer = ArticleScorer(config)
        scored = [scorer.score(a) for a in (vendor, recap, other)]

        service = ClusterService(config)
        clusters = service.build([vendor, recap, other])
        lookup = {a.key: c for c in clusters for a in c.articles}

        def cluster_key(art):
            cluster = lookup.get(art.key)
            return cluster.primary.key if cluster and cluster.size > 1 else None

        selector = TopicSelector(config)
        selected = selector.select(scored, cluster_key_fn=cluster_key)
        titles = [s.article.title for s in selected]
        assert not ("Structured outputs are now generally available" in titles
                    and "Structured outputs reach GA: what it means for serving" in titles), \
            "같은 사건의 두 기사가 모두 선택되었습니다"
        assert any(s.kind == "cluster" for s in selector.skipped_overlap)

    def test_vendor_announcement_becomes_primary(self, config):
        """원 발표(먼저 발행 + 본문 긴 쪽)가 primary 가 되어야 한다."""
        service = ClusterService(config)
        body_long = "Structured outputs are now generally available. " * 12
        vendor = article(title="Structured outputs are now generally available",
                         content=body_long, days_ago=1.2)
        recap = article(url="https://huggingface.co/blog/recap",
                        title="Structured outputs reach GA: what it means for serving",
                        source_id="huggingface_blog",
                        content="Structured outputs are now generally available. Recap.",
                        days_ago=1.0)
        clusters = [c for c in service.build([recap, vendor]) if c.size > 1]
        assert clusters, "클러스터가 만들어지지 않았습니다"
        assert clusters[0].primary.source_id == "openai_news"
        assert clusters[0].sources()[0]["primary"] is True
