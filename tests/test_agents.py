"""Agents and the LLM abstraction (mock provider only - no network)."""

from __future__ import annotations

import pytest

from src.agents.editor import EditorAgent
from src.agents.fact_check import FactCheckAgent
from src.agents.llm import LLMClient, LLMRequest, LLMResponse, MockLLM, build_llm, extract_json
from src.agents.research import ResearchAgent
from src.agents.writer import WriterAgent
from src.core.errors import AgentError, LLMError
from src.core.models import Verdict
from tests.conftest import make_article


class TestExtractJson:
    def test_bare_json(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        assert extract_json('text\n```json\n{"a": 2}\n```\nmore') == {"a": 2}

    def test_json_with_prose_around_it(self):
        assert extract_json('Sure! {"a": 3} hope that helps') == {"a": 3}

    def test_list_is_wrapped(self):
        assert extract_json("[1, 2]") == {"items": [1, 2]}

    @pytest.mark.parametrize("bad", ["", "no json here"])
    def test_failures_raise(self, bad):
        with pytest.raises(LLMError):
            extract_json(bad)


class TestBuildLLM:
    def test_default_is_mock(self, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        assert build_llm().provider == "mock"

    def test_missing_key_raises_instead_of_silent_mock(self, monkeypatch):
        """실패 시 mock 으로 폴백하면 가짜 문장이 게시 후보가 되므로 반드시 예외."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(LLMError):
            build_llm("openai")

    def test_unknown_provider_raises(self):
        with pytest.raises(LLMError):
            build_llm("does-not-exist")


class TestMockLLM:
    def test_is_deterministic(self):
        llm = MockLLM()
        request = LLMRequest(
            task="research",
            system="s",
            user="u",
            payload={"article": {"title": "t", "content_excerpt": "v1.2.0 released. 30% faster."}},
        )
        assert llm.complete(request).text == llm.complete(request).text

    def test_unknown_task_returns_note(self):
        response = MockLLM().complete(LLMRequest(task="unknown", system="", user=""))
        assert "mock provider has no builder" in response.text


class TestAgents:
    @pytest.fixture
    def llm(self) -> MockLLM:
        return MockLLM()

    def test_research_extracts_change_and_facts(self, config, llm):
        agent = ResearchAgent(config=config, llm=llm)
        note = agent.run(make_article())
        assert note.what_changed
        assert note.article_key
        assert note.sources and note.sources[0]["primary"] is True

    def test_fact_check_verifies_grounded_claims(self, config, llm):
        article = make_article()
        note = ResearchAgent(config=config, llm=llm).run(article)
        report = FactCheckAgent(config=config, llm=llm).run(article, note)
        assert report.verdict in set(Verdict)
        assert report.primary_source_found is True

    def test_fact_check_demotes_non_primary_source(self, config, llm):
        article = make_article(
            url="https://forum.example.com/thread",
            source_id="techcrunch_ai",
            trust_tier="major_news",
            trust_score=3,
        )
        note = ResearchAgent(config=config, llm=llm).run(article)
        report = FactCheckAgent(config=config, llm=llm).run(article, note)
        assert report.verdict == Verdict.UNVERIFIED
        assert report.needs_human_review is True
        assert any("1차 출처" in n for n in report.notes)

    def test_writer_respects_channel_limit(self, config, llm):
        article = make_article()
        note = ResearchAgent(config=config, llm=llm).run(article)
        report = FactCheckAgent(config=config, llm=llm).run(article, note)

        short = next(c for c in config.channels if c.format == "shortform" and c.enabled)
        draft = WriterAgent(config=config, llm=llm).run(
            channel=short, items=[(article, note, report)]
        )
        assert draft.body
        assert draft.char_count <= short.max_chars
        assert draft.over_limit is False
        assert len(draft.hashtags) <= short.hashtag_limit
        assert draft.sources

    def test_writer_produces_thread_for_thread_channel(self, config, llm):
        article = make_article()
        note = ResearchAgent(config=config, llm=llm).run(article)
        report = FactCheckAgent(config=config, llm=llm).run(article, note)
        channel = next(c for c in config.channels if c.thread and c.enabled)
        draft = WriterAgent(config=config, llm=llm).run(
            channel=channel, items=[(article, note, report)]
        )
        assert draft.thread_parts
        assert all(len(part) <= channel.max_chars for part in draft.thread_parts)

    def test_editor_removes_hype_and_records_changes(self, config, llm):
        from src.core.models import Draft

        channel = next(c for c in config.channels if c.format == "longform" and c.enabled)
        draft = Draft(
            channel_id=channel.id,
            format=channel.format,
            title="t",
            body="이것은 혁명적인 game-changing 업데이트입니다.\n\n같은 문장\n같은 문장",
            sources=[{"title": "x", "url": "https://example.com", "verdict": "verified"}],
        )
        edited = EditorAgent(config=config, llm=llm).run(draft=draft, channel=channel)
        assert "혁명적" not in edited.body
        assert "game-changing" not in edited.body.lower()
        assert edited.editor_changes

    def test_agent_error_on_broken_provider(self, config):
        class Broken(LLMClient):
            provider = "broken"

            def complete(self, request: LLMRequest) -> LLMResponse:
                return LLMResponse(text="not json at all", provider=self.provider)

        with pytest.raises(AgentError):
            ResearchAgent(config=config, llm=Broken()).run(make_article())

    def test_prompt_files_are_loaded(self, config, llm):
        for agent_cls in (ResearchAgent, FactCheckAgent, WriterAgent, EditorAgent):
            agent = agent_cls(config=config, llm=llm)
            assert len(agent.system_prompt) > 200, f"{agent.name} prompt looks empty"
