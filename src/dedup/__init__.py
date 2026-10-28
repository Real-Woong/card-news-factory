"""Deduplication: URL, title-similarity and embedding-similarity checks."""

from src.dedup.service import DedupService, DedupSummary, resolve_embedding_profile
from src.dedup.strategies import DedupStrategy, EmbeddingDedup, Match, TitleDedup, UrlDedup

__all__ = [
    "DedupService",
    "DedupStrategy",
    "DedupSummary",
    "EmbeddingDedup",
    "Match",
    "TitleDedup",
    "UrlDedup",
    "resolve_embedding_profile",
]
