"""Kiro CLI provider: 바이너리 resolve, 출력 정리, JSON 추출, 폴백 금지 정책."""

from __future__ import annotations

import subprocess

import pytest

from src.agents.kiro_cli import (
    KiroUsage,
    clean_output,
    extract_json_payload,
    parse_footer,
    resolve_command,
    strip_ansi,
)
from src.agents.llm import KiroCliLLM, LLMRequest, MockLLM, build_llm, normalize_provider
from src.agents.schemas import validate_payload
from src.core.errors import LLMError

# 실제 CLI 출력 형태 (ANSI + 프롬프트 프리픽스 + credits 푸터)
REAL_OUTPUT = (
    "\x1b[38;5;252m\x1b[0m\x1b[?25l\x1b[38;5;141m> \x1b[0m"
    '{"what_changed": "Structured outputs are GA", "confidence": 0.9}'
    "\x1b[0m\x1b[0m\n\x1b[38;5;8m\n ▸ Credits: 0.08 • Time: 2s\n\n\x1b[0m\x1b[1G\x1b[0m\x1b[0m\x1b[?25h"
)


class TestResolveCommand:
    def test_env_command_is_used(self, monkeypatch, tmp_path):
        fake = tmp_path / "kiro-cli"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        monkeypatch.setenv("KIRO_CLI_COMMAND", f"{fake} chat --no-interactive")
        argv = resolve_command()
        assert argv[0] == str(fake)
        assert argv[1:] == ["chat", "--no-interactive"]

    def test_env_command_with_missing_binary_raises(self, monkeypatch):
        monkeypatch.setenv("KIRO_CLI_COMMAND", "/nonexistent/kiro-cli chat")
        with pytest.raises(LLMError) as exc:
            resolve_command()
        assert "찾을 수 없습니다" in str(exc.value)

    def test_falls_back_to_which(self, monkeypatch, tmp_path):
        monkeypatch.delenv("KIRO_CLI_COMMAND", raising=False)
        fake = tmp_path / "kiro-cli"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))
        argv = resolve_command()
        assert argv[0] == str(fake)
        assert "--no-interactive" in argv

    def test_missing_everywhere_raises_with_guidance(self, monkeypatch, tmp_path):
        monkeypatch.delenv("KIRO_CLI_COMMAND", raising=False)
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.setattr("src.agents.kiro_cli.FALLBACK_BINARY", tmp_path / "nope")
        with pytest.raises(LLMError) as exc:
            resolve_command()
        assert "KIRO_CLI_COMMAND" in str(exc.value)

    def test_resolves_on_this_machine(self, monkeypatch):
        """실제 설치된 kiro-cli 를 찾을 수 있어야 함 (없으면 skip)."""
        import shutil
        from pathlib import Path

        monkeypatch.delenv("KIRO_CLI_COMMAND", raising=False)
        if not (shutil.which("kiro-cli") or (Path.home() / ".local/bin/kiro-cli").is_file()):
            pytest.skip("kiro-cli 가 설치되어 있지 않음")
        argv = resolve_command()
        assert argv[0].endswith("kiro-cli")
        assert argv[1] == "chat"


class TestOutputCleaning:
    def test_strip_ansi(self):
        assert strip_ansi("\x1b[38;5;141mhello\x1b[0m") == "hello"

    def test_parse_footer(self):
        credits, seconds = parse_footer(" ▸ Credits: 0.08 • Time: 2s")
        assert credits == pytest.approx(0.08)
        assert seconds == pytest.approx(2.0)

    def test_parse_footer_missing(self):
        assert parse_footer("no footer here") == (0.0, 0.0)

    def test_parse_footer_ms_unit(self):
        credits, seconds = parse_footer("▸ Credits: 1.5 • Time: 250ms")
        assert seconds == pytest.approx(0.25)

    def test_clean_output_removes_ui_and_footer(self):
        cleaned = clean_output(REAL_OUTPUT)
        assert cleaned.startswith("{")
        assert cleaned.endswith("}")
        assert "Credits" not in cleaned
        assert "\x1b" not in cleaned
        assert "▸" not in cleaned

    def test_clean_output_keeps_prose(self):
        assert "hello world" in clean_output("\x1b[0m> hello world\n")


class TestJsonExtraction:
    def test_real_cli_output(self):
        payload = extract_json_payload(REAL_OUTPUT)
        assert payload["what_changed"] == "Structured outputs are GA"
        assert payload["confidence"] == 0.9

    def test_fenced_json(self):
        raw = 'Sure!\n```json\n{"body": "text"}\n```\n ▸ Credits: 0.1 • Time: 1s'
        assert extract_json_payload(raw) == {"body": "text"}

    def test_json_with_trailing_prose(self):
        raw = '{"a": 1}\nThat is the result.\n ▸ Credits: 0.1 • Time: 1s'
        assert extract_json_payload(raw) == {"a": 1}

    def test_nested_braces_in_strings(self):
        raw = '{"body": "use {braces} and \\"quotes\\" here", "n": 2} trailing'
        payload = extract_json_payload(raw)
        assert payload["n"] == 2
        assert "{braces}" in payload["body"]

    def test_json_array(self):
        assert extract_json_payload("[1, 2, 3]") == [1, 2, 3]

    @pytest.mark.parametrize("raw", ["", "   ", "\x1b[0m\n ▸ Credits: 0.0 • Time: 0s\n"])
    def test_empty_output_raises(self, raw):
        with pytest.raises(LLMError):
            extract_json_payload(raw)

    def test_non_json_output_raises_with_preview(self):
        with pytest.raises(LLMError) as exc:
            extract_json_payload("I cannot help with that request.")
        assert "JSON" in str(exc.value)


class TestUsage:
    def test_accumulates(self):
        usage = KiroUsage()
        usage.record(task="research", credits=0.08, seconds=2.0)
        usage.record(task="research", credits=0.12, seconds=3.0)
        usage.record(task="write", credits=0.0, seconds=0.0, ok=False)
        data = usage.to_dict()
        assert data["calls"] == 3
        assert data["failures"] == 1
        assert data["credits"] == pytest.approx(0.20)
        assert data["calls_per_task"] == {"research": 2, "write": 1}


class TestNoSilentFallback:
    """가장 중요한 정책: mock 은 명시했을 때만 쓰인다."""

    def test_explicit_mock_returns_mock(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        assert build_llm().provider == "mock"

    def test_missing_openai_key_raises_instead_of_mock(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(LLMError):
            build_llm("openai")

    def test_missing_anthropic_key_raises_instead_of_mock(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(LLMError):
            build_llm("anthropic")

    def test_unknown_provider_raises(self):
        with pytest.raises(LLMError):
            build_llm("does-not-exist")

    def test_kiro_without_binary_raises(self, monkeypatch, tmp_path):
        monkeypatch.delenv("KIRO_CLI_COMMAND", raising=False)
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.setattr("src.agents.kiro_cli.FALLBACK_BINARY", tmp_path / "nope")
        with pytest.raises(LLMError):
            build_llm("kiro_cli")

    @pytest.mark.parametrize(
        ("alias", "expected"),
        [("kiro", "kiro_cli"), ("kiro-cli", "kiro_cli"), ("KIRO_CLI", "kiro_cli"), ("", "mock")],
    )
    def test_provider_aliases(self, alias, expected):
        assert normalize_provider(alias) == expected


class TestKiroCliLLMBehaviour:
    @pytest.fixture
    def stub_cli(self, monkeypatch, tmp_path):
        """resolve 는 통과시키고 subprocess 만 가로챕니다."""
        fake = tmp_path / "kiro-cli"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        monkeypatch.setenv("KIRO_CLI_COMMAND", f"{fake} chat --no-interactive")
        return fake

    def _patch_run(self, monkeypatch, *, stdout: str = "", returncode: int = 0, stderr: str = ""):
        def fake_run(argv, **kwargs):  # noqa: ANN001, ANN202
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

        monkeypatch.setattr("src.agents.kiro_cli.subprocess.run", fake_run)

    def test_successful_call_records_usage(self, stub_cli, monkeypatch):
        self._patch_run(monkeypatch, stdout=REAL_OUTPUT)
        llm = KiroCliLLM()
        result = llm.complete_json(
            LLMRequest(task="research", system="s", user="u"), retries=0
        )
        assert result["what_changed"] == "Structured outputs are GA"
        assert llm.usage_summary()["calls"] == 1
        assert llm.usage_summary()["credits"] == pytest.approx(0.08)

    def test_credits_footer_on_stderr_is_counted(self, stub_cli, monkeypatch):
        """실측: TERM=dumb 에서 credits 푸터가 stderr 로 나온다."""
        self._patch_run(
            monkeypatch,
            stdout='\x1b[m> \x1b[0m{"what_changed": "x"}',
            stderr="\x1b[m\n ▸ Credits: 0.04 • Time: 2s\n\n",
        )
        llm = KiroCliLLM()
        llm.complete_json(LLMRequest(task="research", system="s", user="u"), retries=0)
        assert llm.usage_summary()["credits"] == pytest.approx(0.04)

    def test_nonzero_exit_raises_with_diagnostics(self, stub_cli, monkeypatch):
        self._patch_run(monkeypatch, returncode=3, stderr="boom: not logged in")
        llm = KiroCliLLM()
        with pytest.raises(LLMError) as exc:
            llm.complete(LLMRequest(task="research", system="s", user="u"))
        message = str(exc.value)
        assert "returncode: 3" in message
        assert "boom: not logged in" in message
        assert "command" in message
        assert llm.usage_summary()["failures"] == 1

    def test_timeout_raises(self, stub_cli, monkeypatch):
        def fake_run(argv, **kwargs):  # noqa: ANN001, ANN202
            raise subprocess.TimeoutExpired(argv, 1)

        monkeypatch.setattr("src.agents.kiro_cli.subprocess.run", fake_run)
        with pytest.raises(LLMError) as exc:
            KiroCliLLM().complete(LLMRequest(task="research", system="s", user="u"))
        assert "타임아웃" in str(exc.value)

    def test_schema_mismatch_raises_after_retries(self, stub_cli, monkeypatch):
        # what_changed 가 없는 응답 -> research schema 위반
        self._patch_run(monkeypatch, stdout='{"summary": "nope"}')
        llm = KiroCliLLM()
        with pytest.raises(LLMError) as exc:
            llm.complete_json(LLMRequest(task="research", system="s", user="u"), retries=1)
        assert "what_changed" in str(exc.value)
        assert llm.usage_summary()["calls"] == 2  # 재시도 포함

    def test_non_json_output_raises(self, stub_cli, monkeypatch):
        self._patch_run(monkeypatch, stdout="I'll help you with that!\n ▸ Credits: 0.1 • Time: 1s")
        with pytest.raises(LLMError):
            KiroCliLLM().complete_json(
                LLMRequest(task="write", system="s", user="u"), retries=0
            )


class TestSchemaValidation:
    def test_research_requires_what_changed(self):
        with pytest.raises(LLMError):
            validate_payload("research", {"why_it_matters": "x"})

    def test_research_rejects_empty_what_changed(self):
        with pytest.raises(LLMError):
            validate_payload("research", {"what_changed": "   "})

    def test_fact_check_rejects_unknown_verdict(self):
        with pytest.raises(LLMError):
            validate_payload("fact_check", {"verdict": "probably fine"})

    def test_fact_check_accepts_known_verdict(self):
        assert validate_payload("fact_check", {"verdict": "partially_verified"})

    def test_write_requires_body(self):
        with pytest.raises(LLMError):
            validate_payload("write", {"title": "t"})

    def test_write_rejects_too_short_body(self):
        with pytest.raises(LLMError):
            validate_payload("write", {"body": "짧음"})

    def test_bad_optional_field_is_dropped_not_fatal(self):
        data = validate_payload(
            "write", {"body": "x" * 40, "hashtags": "should-be-a-list"}
        )
        assert "hashtags" not in data

    def test_non_dict_raises(self):
        with pytest.raises(LLMError):
            validate_payload("research", ["not", "a", "dict"])

    def test_unknown_task_passes_through(self):
        assert validate_payload("unknown_task", {"anything": 1}) == {"anything": 1}

    def test_mock_output_satisfies_every_schema(self, config):
        """mock provider 응답도 실제 schema 를 통과해야 한다(회귀 방지)."""
        from tests.conftest import make_article

        llm = MockLLM()
        article = make_article()
        payloads = {
            "research": {
                "article": {
                    **article.reference(),
                    "content_excerpt": article.content,
                    "summary": article.summary,
                }
            },
            "fact_check": {
                "article": article.reference(),
                "research": {"key_facts": [{"text": article.content[:80]}]},
                "source_excerpt": article.content,
            },
            "write": {
                "channel": {"format": "longform", "max_chars": 2000, "hashtag_limit": 3},
                "items": [
                    {
                        "article": article.reference(),
                        "research": {"what_changed": "x" * 60, "why_it_matters": "y" * 40},
                        "fact_check": {"verdict": "verified"},
                    }
                ],
            },
            "edit": {"draft": {"body": "본문 " * 30, "title": "t"}, "max_chars": 2000},
        }
        for task, payload in payloads.items():
            request = LLMRequest(task=task, system="s", user="u", payload=payload)
            validate_payload(task, llm.parse_json(llm.complete(request).text))
