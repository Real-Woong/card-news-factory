"""환경변수 점검 도구 테스트 — 가짜 환경 dict 만 쓰며 .env 를 읽지 않는다.

핵심 보장: 출력에 값(또는 그 일부)이 절대 나오지 않는다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.core.envcheck import BAD_FORMAT, MISSING, OK, PLACEHOLDER, check, render

SECRET = "FAKE-secret-value-for-tests-only-0123"


def status(findings, name):
    return next(f.status for f in findings if f.name == name)


class TestCheck:
    def test_mock_provider_is_not_acceptable_for_auto_operation(self):
        assert status(check({"LLM_PROVIDER": "mock"}), "LLM_PROVIDER") == PLACEHOLDER
        assert status(check({}), "LLM_PROVIDER") == PLACEHOLDER  # 기본값도 mock

    def test_unknown_provider(self):
        assert status(check({"LLM_PROVIDER": "gpt"}), "LLM_PROVIDER") == BAD_FORMAT

    @pytest.mark.parametrize("provider,key", [("openai", "OPENAI_API_KEY"),
                                              ("anthropic", "ANTHROPIC_API_KEY")])
    def test_provider_needs_its_key(self, provider, key):
        assert status(check({"LLM_PROVIDER": provider}), key) == MISSING
        assert status(check({"LLM_PROVIDER": provider, key: SECRET}), key) == OK

    @pytest.mark.parametrize("value", ["your-key-here", "xxxxxxxx", "<token>", "changeme"])
    def test_placeholders_are_caught(self, value):
        found = check({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": value})
        assert status(found, "OPENAI_API_KEY") == PLACEHOLDER

    def test_short_or_spaced_values_are_bad_format(self):
        assert status(check({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "abc"}),
                      "OPENAI_API_KEY") == BAD_FORMAT
        assert status(check({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "a b " * 8}),
                      "OPENAI_API_KEY") == BAD_FORMAT

    def test_publishing_items_only_when_requested(self):
        names = {f.name for f in check({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": SECRET})}
        assert "INSTAGRAM_ACCESS_TOKEN" not in names
        env = {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": SECRET}
        assert status(check(env, publishing=True), "INSTAGRAM_ACCESS_TOKEN") == MISSING
        env.update(INSTAGRAM_ACCESS_TOKEN="IG" + SECRET * 2, INSTAGRAM_USER_ID="17841400000000000")
        found = check(env, publishing=True)
        assert status(found, "INSTAGRAM_ACCESS_TOKEN") == OK and status(found, "INSTAGRAM_USER_ID") == OK

    def test_user_id_must_be_numeric(self):
        env = {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": SECRET, "INSTAGRAM_USER_ID": "abc"}
        assert status(check(env, publishing=True), "INSTAGRAM_USER_ID") == BAD_FORMAT

    def test_optional_github_token_only_checked_when_present(self):
        env = {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": SECRET}
        assert "GITHUB_TOKEN" not in {f.name for f in check(env)}
        assert status(check({**env, "GITHUB_TOKEN": "short"}), "GITHUB_TOKEN") == BAD_FORMAT


class TestNeverPrintsValues:
    def test_output_has_no_secret_text_or_lengths(self):
        env = {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": SECRET,
               "INSTAGRAM_ACCESS_TOKEN": SECRET * 2, "INSTAGRAM_USER_ID": "12345", "GITHUB_TOKEN": SECRET}
        out = render(check(env, publishing=True))
        assert SECRET not in out and SECRET[:6] not in out
        assert str(len(SECRET)) not in out


class TestEnvExampleIsComplete:
    """코드가 읽는 환경변수는 모두 .env.example 에 문서화돼 있어야 한다(값은 보지 않는다)."""

    def test_every_variable_read_by_code_is_documented(self):
        root = Path(__file__).resolve().parents[1]
        documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", (root / ".env.example").read_text(encoding="utf-8"), re.M))
        used: set[str] = set()
        pattern = re.compile(r"""(?:environ\.get|getenv)\(\s*["']([A-Z][A-Z0-9_]+)["']""")
        for folder in ("src", "channel"):
            for path in (root / folder).rglob("*.py"):
                used |= set(pattern.findall(path.read_text(encoding="utf-8")))
        assert not (used - documented), sorted(used - documented)
