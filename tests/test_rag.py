"""RAG: embeddings, local vector store, indexer and retriever."""

from __future__ import annotations

import pytest

from src.core.models import VectorRecord
from src.rag.base import cosine_similarity
from src.rag.embeddings import HashingEmbedder, build_embedder
from src.rag.indexer import RagIndexer
from src.rag.local_store import LocalJsonVectorStore
from src.rag.retriever import RagRetriever
from tests.conftest import make_article


@pytest.fixture
def store(tmp_path) -> LocalJsonVectorStore:
    return LocalJsonVectorStore(tmp_path / "vectors")


@pytest.fixture
def embedder() -> HashingEmbedder:
    return HashingEmbedder(dimension=256)


class TestHashingEmbedder:
    def test_deterministic_and_normalised(self, embedder):
        first = embedder.embed_one("Ethereum devnet-9 raises blob throughput")
        second = embedder.embed_one("Ethereum devnet-9 raises blob throughput")
        assert first == second
        assert len(first) == 256
        assert cosine_similarity(first, second) == pytest.approx(1.0, abs=1e-6)

    def test_similar_text_scores_higher_than_unrelated(self, embedder):
        base = embedder.embed_one("vLLM release improves inference throughput on GPUs")
        near = embedder.embed_one("vLLM release improves GPU inference throughput")
        far = embedder.embed_one("Coinbase launches an onchain payments SDK")
        assert cosine_similarity(base, near) > cosine_similarity(base, far)

    def test_handles_korean(self, embedder):
        vector = embedder.embed_one("이더리움 데브넷 처리량 증가")
        assert any(value != 0.0 for value in vector)

    def test_build_embedder_defaults_to_hashing(self, monkeypatch):
        monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
        assert build_embedder().name == "hashing"

    def test_unknown_provider_falls_back(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "does-not-exist")
        assert build_embedder().name == "hashing"


class TestLocalVectorStore:
    def test_upsert_query_delete(self, store, embedder):
        records = [
            VectorRecord(
                id="article:a:0",
                text="vLLM release improves throughput",
                embedding=embedder.embed_one("vLLM release improves throughput"),
                metadata={"doc_key": "a", "title": "vLLM"},
            ),
            VectorRecord(
                id="article:b:0",
                text="Coinbase payments SDK",
                embedding=embedder.embed_one("Coinbase payments SDK"),
                metadata={"doc_key": "b", "title": "Coinbase"},
            ),
        ]
        store.upsert("articles", records)
        assert store.count("articles") == 2

        hits = store.query("articles", embedder.embed_one("vLLM throughput"), top_k=1)
        assert hits and hits[0].metadata["doc_key"] == "a"
        assert hits[0].metadata["collection"] == "articles"

        store.delete("articles", ["article:a:0"])
        assert store.count("articles") == 1

    def test_persists_across_instances(self, tmp_path, embedder):
        first = LocalJsonVectorStore(tmp_path / "v")
        first.upsert(
            "articles",
            [VectorRecord(id="x:0", text="hello", embedding=embedder.embed_one("hello"))],
        )
        second = LocalJsonVectorStore(tmp_path / "v")
        assert second.count("articles") == 1

    def test_ephemeral_store_does_not_write(self, tmp_path, embedder):
        store = LocalJsonVectorStore(tmp_path / "v", ephemeral=True)
        store.upsert(
            "articles",
            [VectorRecord(id="x:0", text="hello", embedding=embedder.embed_one("hello"))],
        )
        assert store.count("articles") == 1  # in memory
        assert not list((tmp_path / "v").glob("*.jsonl"))

    def test_query_with_empty_embedding(self, store):
        assert store.query("articles", [], top_k=3) == []


class TestIndexerAndRetriever:
    def test_index_and_retrieve_article(self, config, store, embedder, repo):
        indexer = RagIndexer(config=config, embedder=embedder, store=store, repo=repo)
        article = make_article()
        repo.insert_article(article)

        chunks = indexer.index_articles([article])
        assert chunks >= 1

        retriever = RagRetriever(config=config, embedder=embedder, store=store)
        hits = retriever.search("structured outputs generally available")
        assert hits
        assert hits[0].metadata["doc_key"] == article.key

    def test_reindexing_unchanged_article_is_skipped(self, config, store, embedder, repo):
        indexer = RagIndexer(config=config, embedder=embedder, store=store, repo=repo)
        article = make_article()
        repo.insert_article(article)
        assert indexer.index_articles([article]) >= 1
        assert indexer.index_articles([article]) == 0

    def test_index_knowledge_files(self, config, store, embedder, repo):
        indexer = RagIndexer(config=config, embedder=embedder, store=store, repo=repo)
        assert indexer.index_knowledge() >= 3  # three seeded knowledge files

    def test_context_for_article_excludes_itself(self, config, store, embedder, repo):
        indexer = RagIndexer(config=config, embedder=embedder, store=store, repo=repo)
        article = make_article()
        other = make_article(
            url="https://huggingface.co/blog/structured-outputs",
            title="Structured outputs reach GA: what it means for serving",
            source_id="huggingface_blog",
        )
        repo.insert_articles([article, other])
        indexer.index_articles([article, other])

        retriever = RagRetriever(config=config, embedder=embedder, store=store)
        context = retriever.context_for_article(article)
        assert all(chunk.metadata.get("doc_key") != article.key for chunk in context)

    def test_best_match_on_empty_collection(self, config, store, embedder):
        retriever = RagRetriever(config=config, embedder=embedder, store=store)
        assert retriever.best_match("anything", collection="posts") is None
