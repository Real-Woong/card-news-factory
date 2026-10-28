"""Dedup strategies and ranking behaviour."""

from __future__ import annotations

from src.core.models import DedupReason
from src.dedup.service import DedupService
from src.dedup.strategies import TitleDedup, UrlDedup
from src.rag.embeddings import HashingEmbedder
from src.rag.indexer import RagIndexer
from src.rag.local_store import LocalJsonVectorStore
from src.rag.retriever import RagRetriever
from src.ranking.scorer import ArticleScorer
from src.ranking.selector import TopicSelector
from src.ranking.topics import TopicTagger
from tests.conftest import make_article


class TestUrlDedup:
    def test_same_url_in_one_batch(self, repo):
        strategy = UrlDedup(repo)
        first = make_article()
        assert strategy.check(first, seen_in_run={}) is None
        match = strategy.check(make_article(), seen_in_run={first.key: first})
        assert match is not None and match.reason == DedupReason.URL

    def test_tracking_params_do_not_defeat_dedup(self, repo):
        stored = make_article()
        repo.insert_article(stored)
        again = make_article(url="https://openai.com/index/example/?utm_source=newsletter")
        match = UrlDedup(repo).check(again, seen_in_run={})
        assert match is not None and match.reason == DedupReason.URL

    def test_unseen_article_passes(self, repo):
        assert UrlDedup(repo).check(make_article(url="https://x.dev/new"), seen_in_run={}) is None


class TestTitleDedup:
    def test_near_identical_title_is_caught(self, repo):
        stored = make_article()
        repo.insert_article(stored)
        similar = make_article(
            url="https://example.com/mirror",
            title="Structured outputs are now generally available!",
        )
        match = TitleDedup(repo, threshold=0.82).check(similar, seen_in_run={})
        assert match is not None and match.reason == DedupReason.TITLE

    def test_different_title_passes(self, repo):
        repo.insert_article(make_article())
        other = make_article(
            url="https://blog.ethereum.org/devnet-9", title="Devnet-9 raises blob throughput"
        )
        assert TitleDedup(repo, threshold=0.82).check(other, seen_in_run={}) is None


class TestDedupService:
    def test_second_run_drops_everything(self, config, repo, tmp_path):
        articles = [
            make_article(),
            make_article(url="https://blog.ethereum.org/devnet-9", title="Devnet-9 live"),
        ]
        service = DedupService(config=config, repo=repo)

        first = service.run(articles)
        assert len(first.kept) == 2
        repo.insert_articles(articles)

        second = DedupService(config=config, repo=repo).run(
            [make_article(), make_article(url="https://blog.ethereum.org/devnet-9", title="Devnet-9 live")]
        )
        assert second.kept == []
        assert len(second.duplicates) == 2

    def test_embedding_flag_surfaces_near_duplicate(self, config, repo, tmp_path):
        embedder = HashingEmbedder(dimension=256)
        store = LocalJsonVectorStore(tmp_path / "v")
        indexer = RagIndexer(config=config, embedder=embedder, store=store, repo=repo)
        retriever = RagRetriever(config=config, embedder=embedder, store=store)

        original = make_article()
        repo.insert_article(original)
        indexer.index_articles([original])

        rephrased = make_article(
            url="https://openai.com/index/example-mirror/",
            title="Structured outputs: generally available now",
        )
        summary = DedupService(config=config, repo=repo, retriever=retriever).run([rephrased])
        # Either dropped as duplicate or flagged for review - never silently kept clean.
        assert summary.duplicates or summary.warnings


class TestTopicTagger:
    def test_tags_topics_and_signals(self, config):
        tagger = TopicTagger(config)
        article = make_article(
            content="vLLM release adds inference throughput improvements and a breaking change."
        )
        match = tagger.tag(article)
        assert "inference_infra" in match.topics
        assert match.signals
        assert article.topics == match.topics

    def test_noise_keywords_detected(self, config):
        tagger = TopicTagger(config)
        match = tagger.tag(
            make_article(content="Join our webinar! We're hiring. Price prediction inside.")
        )
        assert match.noise


class TestScorer:
    def test_official_release_outranks_noise(self, config):
        scorer = ArticleScorer(config)
        good = scorer.score(
            make_article(
                content=(
                    "vLLM v1.2.0 released. Throughput improves from 2410 to 3180 tok/s. "
                    "Breaking change: max_new_tokens renamed. Inference serving updated."
                ),
                days_ago=0.5,
            )
        )
        bad = scorer.score(
            make_article(
                url="https://forum.example.com/thread/1",
                title="Price prediction webinar",
                source_id="techcrunch_ai",
                trust_tier="community",
                trust_score=1,
                days_ago=13,
                content="Join our webinar for a price prediction. We're hiring.",
            )
        )
        assert good.score > bad.score
        assert good.components["trust"] == 1.0
        assert "noise" in bad.penalties

    def test_novelty_reduces_score(self, config):
        scorer = ArticleScorer(config)
        fresh = scorer.score(make_article(), novelty=1.0)
        stale = scorer.score(make_article(), novelty=0.0)
        assert fresh.score > stale.score

    def test_score_is_bounded(self, config):
        scorer = ArticleScorer(config)
        for article in (make_article(), make_article(days_ago=100)):
            assert 0.0 <= scorer.score(article).score <= 1.0


class TestSelector:
    def _scored(self, config, count: int, category: str):
        scorer = ArticleScorer(config)
        return [
            scorer.score(
                make_article(
                    url=f"https://example.com/{category}/{index}",
                    title=f"{category} release v1.{index}.0 now generally available",
                    category=category,
                    source_id="openai_news" if category == "ai" else "ethereum_blog",
                    content="Release adds throughput. Breaking change. Now available. v1.0.0",
                )
            )
            for index in range(count)
        ]

    def test_collapses_two_releases_of_the_same_repo(self, config):
        scorer = ArticleScorer(config)
        newer = scorer.score(
            make_article(
                url="https://github.com/ollama/ollama/releases/tag/v0.34.3",
                title="ollama/ollama: v0.34.3",
                source_id="gh_ollama",
                trust_tier="github",
                content="Release notes. Breaking change. Now available.",
                days_ago=0.5,
            )
        )
        older = scorer.score(
            make_article(
                url="https://github.com/ollama/ollama/releases/tag/v0.34.1",
                title="ollama/ollama: v0.34.1",
                source_id="gh_ollama",
                trust_tier="github",
                content="Release notes. Breaking change. Now available.",
                days_ago=3,
            )
        )
        selector = TopicSelector(config)
        selected = selector.select([newer, older])
        assert len(selected) == 1
        assert selected[0].article.title.endswith("v0.34.3")
        assert selector.skipped_overlap and selector.skipped_overlap[0].kind == "series"

    def test_keeps_releases_from_different_repos(self, config):
        scorer = ArticleScorer(config)
        items = [
            scorer.score(
                make_article(
                    url="https://github.com/vllm-project/vllm/releases/tag/v0.9.0",
                    title="vllm-project/vllm: v0.9.0",
                    source_id="gh_vllm",
                    trust_tier="github",
                    content="Release adds throughput. Breaking change. Now available.",
                )
            ),
            scorer.score(
                make_article(
                    url="https://github.com/ethereum/go-ethereum/releases/tag/v1.9.0",
                    title="ethereum/go-ethereum: v1.9.0",
                    source_id="gh_go_ethereum",
                    category="blockchain",
                    trust_tier="github",
                    content="Release adds throughput. Breaking change. Now available.",
                )
            ),
        ]
        selector = TopicSelector(config)
        assert len(selector.select(items)) == 2
        assert selector.skipped_overlap == []

    def test_respects_max_items_and_source_cap(self, config):
        scored = self._scored(config, 6, "ai") + self._scored(config, 6, "blockchain")
        selected = TopicSelector(config).select(scored)
        assert len(selected) <= config.ranking.max_items
        per_source: dict[str, int] = {}
        for item in selected:
            per_source[item.article.source_id] = per_source.get(item.article.source_id, 0) + 1
        assert all(count <= config.ranking.max_per_source for count in per_source.values())

    def test_drops_below_min_score(self, config):
        scorer = ArticleScorer(config)
        weak = scorer.score(
            make_article(
                url="https://forum.example.com/1",
                title="hiring thread",
                trust_tier="social",
                trust_score=1,
                days_ago=30,
                content="We're hiring. Giveaway. Airdrop farming.",
            )
        )
        assert TopicSelector(config).select([weak]) == []

    def test_empty_input(self, config):
        assert TopicSelector(config).select([]) == []
