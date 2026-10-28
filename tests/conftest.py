"""Shared pytest fixtures."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from src.core.config import AppConfig, load_config
from src.core.models import Article
from src.core.text import utcnow
from src.storage.db import connect, init_schema
from src.storage.repository import ArticleRepository


@pytest.fixture(scope="session")
def config() -> AppConfig:
    """The real project configuration (config/*.yaml)."""
    return load_config(load_env=False)


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    init_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def repo(conn: sqlite3.Connection, config: AppConfig) -> ArticleRepository:
    repository = ArticleRepository(conn)
    repository.upsert_sources(config.sources, config.ranking.trust_scores)
    return repository


def make_article(
    *,
    url: str = "https://openai.com/index/example/",
    title: str = "Structured outputs are now generally available",
    category: str = "ai",
    source_id: str = "openai_news",
    trust_tier: str = "official",
    trust_score: int = 5,
    days_ago: float = 1.0,
    content: str = (
        "Structured outputs are now generally available. Median latency drops from "
        "340 ms to 120 ms. The beta header is removed in v2.1.0 and pricing is unchanged."
    ),
) -> Article:
    """Build a realistic article for tests."""
    return Article(
        url=url,
        title=title,
        source_id=source_id,
        source_name=source_id.replace("_", " ").title(),
        source_type="rss",
        trust_tier=trust_tier,
        trust_score=trust_score,
        category=category,
        summary=content[:120],
        content=content,
        published_at=utcnow() - timedelta(days=days_ago),
    )


@pytest.fixture
def article() -> Article:
    return make_article()


@pytest.fixture(autouse=True)
def _kiro_workdir_in_tmp(tmp_path_factory, monkeypatch):
    """테스트가 실제 홈의 ~/.r_a_glance 를 만들지 않게, Kiro 전용 작업 폴더를 임시 위치로 돌린다."""
    from src.agents import kiro_cli

    monkeypatch.setattr(kiro_cli, "NEUTRAL_CWD", tmp_path_factory.mktemp("kiro_cwd"))


@pytest.fixture(autouse=True)
def _never_reach_real_telegram(monkeypatch):
    """테스트 프로세스에 운영 텔레그램 값이 들어 있어도(예: .env 를 읽는 CLI 테스트) 실제로 보내지 않는다."""
    from src.core.envcheck import TELEGRAM_CHAT_NAMES, TELEGRAM_TOKEN_NAMES

    for name in (*TELEGRAM_TOKEN_NAMES, *TELEGRAM_CHAT_NAMES):
        monkeypatch.delenv(name, raising=False)
