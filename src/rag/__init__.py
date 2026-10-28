"""RAG: embeddings, vector stores, indexing and retrieval."""

from src.rag.base import Embedder, VectorStore, cosine_similarity
from src.rag.embeddings import HashingEmbedder, build_embedder
from src.rag.factory import build_rag, build_vector_store
from src.rag.indexer import RagIndexer
from src.rag.local_store import LocalJsonVectorStore
from src.rag.retriever import RagRetriever

__all__ = [
    "Embedder",
    "HashingEmbedder",
    "LocalJsonVectorStore",
    "RagIndexer",
    "RagRetriever",
    "VectorStore",
    "build_embedder",
    "build_rag",
    "build_vector_store",
    "cosine_similarity",
]
