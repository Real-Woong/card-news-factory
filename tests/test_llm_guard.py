"""Kiro CLI 업그레이드 대비(channel/src/llm_guard.py, src/agents/kiro_cli.py canary) 테스트.

핵심 보장: 버전이 바뀌면 카드를 만들기 전에 호출을 한 번 시험하고, 실패하면 실행을 건너뛰며(가짜 폴백 없음),
같은 버전이면 추가 호출이 없다. 실제 CLI·네트워크는 쓰지 않는다.
"""

from __future__ import annotations

import json
import stat
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agents import kiro_cli
from src.agents.kiro_cli import CANARY_PROMPT, canary, kiro_version

from channel.src.llm_guard import llm_ready


@pytest.fixture(autouse=True)
def isolated_workdir(tmp_path, monkeypatch):
    """테스트가 실제 ~/.r_a_glance 를 만들지 않게 전용 폴더를 임시 위치로 돌린다."""
    monkeypatch.setattr(kiro_cli, "NEUTRAL_CWD", tmp_path / "neutral")


def fake_cli(tmp_path: Path, body: str) -> list[str]:
    """주어진 셸 본문을 실행하는 가짜 kiro-cli(stdin 을 읽고 stdout/stderr 에 쓴다)."""
    script = tmp_path / "kiro-cli"
    script.write_text("#!/bin/bash\n" + textwrap.dedent(body), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return [str(script), "chat", "--no-interactive", "--trust-tools="]


class TestKiroVersion:
    def test_version_is_parsed(self, tmp_path):
        argv = fake_cli(tmp_path, 'echo "kiro-cli 3.0.2"')
        assert kiro_version(argv[0]) == "3.0.2"

    def test_unknown_when_the_binary_misbehaves(self, tmp_path):
        assert kiro_version(str(tmp_path / "없는-바이너리")) == ""
        argv = fake_cli(tmp_path, "echo hello")
        assert kiro_version(argv[0]) == ""


class TestCanary:
    def test_a_healthy_cli_passes_and_credits_footer_is_recognized(self, tmp_path):
        argv = fake_cli(tmp_path, '''
            cat > /dev/null
            echo '{"ok": true, "n": 42}'
            echo "▸ Credits: 0.02 • Time: 3s" 1>&2
        ''')
        result = canary(argv)
        assert result.ok and result.credits_seen  # 일반 텍스트 모드의 푸터도 계속 읽는다(호환)

    def test_unrecognized_footer_is_reported_not_fatal(self, tmp_path):
        argv = fake_cli(tmp_path, '''
            cat > /dev/null
            echo '{"ok": true, "n": 42}'
            echo "Usage this call: 0.02 credits" 1>&2
        ''')
        result = canary(argv)
        assert result.ok and not result.credits_seen  # 호출은 정상, 크레딧만 못 읽음

    def test_output_format_change_is_caught(self, tmp_path):
        argv = fake_cli(tmp_path, "cat > /dev/null\necho 'Sorry, I cannot do that.'")
        result = canary(argv)
        assert not result.ok and "JSON" in result.detail

    def test_wrong_answer_and_nonzero_exit(self, tmp_path):
        assert not canary(fake_cli(tmp_path, "cat > /dev/null\necho '{\"ok\": false}'")).ok
        bad = canary(fake_cli(tmp_path, "cat > /dev/null\necho boom 1>&2\nexit 3"))
        assert not bad.ok and "3" in bad.detail

    def test_the_prompt_is_sent_on_stdin(self, tmp_path):
        seen = tmp_path / "seen.txt"
        argv = fake_cli(tmp_path, f'''
            cat > {seen}
            echo '{{"ok": true, "n": 42}}'
        ''')
        canary(argv)
        assert seen.read_text(encoding="utf-8").strip() == CANARY_PROMPT


class Llm:
    provider = "kiro_cli"

    def __init__(self):
        self.argv = ["/x/kiro-cli", "chat"]


class TestLlmReady:
    def run(self, tmp_path, version, ok=True, state=None):
        store = tmp_path / "kiro_version.json"
        if state:
            store.write_text(json.dumps({"version": state}), encoding="utf-8")
        calls = []

        def canary_fn(argv):
            calls.append(argv)
            return SimpleNamespace(ok=ok, detail="JSON 파싱 실패", credits_seen=True)

        result = llm_ready(Llm(), path=store, version_fn=lambda b: version, canary_fn=canary_fn)
        return result, calls, store

    def test_same_version_costs_nothing(self, tmp_path):
        (ok, why), calls, _ = self.run(tmp_path, "2.35.1", state="2.35.1")
        assert ok and why == "" and calls == []

    def test_first_run_and_a_new_version_are_verified_then_remembered(self, tmp_path):
        (ok, _), calls, store = self.run(tmp_path, "3.0.0", state="2.35.1")
        assert ok and len(calls) == 1
        assert json.loads(store.read_text(encoding="utf-8"))["version"] == "3.0.0"
        (ok, _), calls, _ = self.run(tmp_path, "3.0.0", state="3.0.0")
        assert ok and calls == []

    def test_a_failing_new_version_skips_the_run_and_is_not_remembered(self, tmp_path):
        (ok, why), _, store = self.run(tmp_path, "3.0.0", ok=False, state="2.35.1")
        assert not ok and "2.35.1" in why and "3.0.0" in why and "kiro_cli" in why
        assert json.loads(store.read_text(encoding="utf-8"))["version"] == "2.35.1"  # 그대로

    def test_other_providers_are_not_checked(self, tmp_path):
        mock = SimpleNamespace(provider="mock")
        assert llm_ready(mock, path=tmp_path / "x.json") == (True, "")


class TestGenerationFailureIsNotSilent:
    def test_unattended_generation_failure_alerts_and_exits_2(self, monkeypatch):
        from channel.src import pipeline
        from channel.src.config import load_channel_config

        sent = []
        monkeypatch.setattr(pipeline, "finish_run", lambda ch, **kw: sent.append(kw["held"]))
        args = SimpleNamespace(index=None, topic=None)
        code = pipeline._generation_failed(load_channel_config(), args, RuntimeError("파싱 실패"))
        assert code == 2 and "생성 실패" in sent[0][0]

    def test_manual_runs_stay_quiet_but_still_fail(self, monkeypatch):
        from channel.src import pipeline
        from channel.src.config import load_channel_config

        sent = []
        monkeypatch.setattr(pipeline, "finish_run", lambda ch, **kw: sent.append(kw))
        args = SimpleNamespace(index=1, topic=None)
        assert pipeline._generation_failed(load_channel_config(), args, RuntimeError("x")) == 2
        assert sent == []


class TestNeutralWorkingDirectory:
    """프로젝트 폴더의 .kiro 설정이 매 호출의 맥락으로 섞이지 않도록 빈 폴더에서 실행한다."""

    def test_calls_run_from_the_neutral_folder_not_the_project(self, tmp_path, monkeypatch):
        from src.agents import kiro_cli, llm
        from src.agents.llm import KiroCliLLM, LLMRequest

        monkeypatch.setattr(kiro_cli, "NEUTRAL_CWD", tmp_path / "kiro_cwd")
        seen = {}

        def fake_run_cli(prompt, *, argv, timeout, cwd=None, metering=True):
            seen["cwd"] = cwd
            return '{"ok": true}', 0.0, 1.0

        monkeypatch.setattr(kiro_cli, "run_cli", fake_run_cli)
        fake = fake_cli(tmp_path, "echo kiro-cli 2.35.1")
        client = KiroCliLLM(command=" ".join(fake[:1]))
        client.complete(LLMRequest(task="t", system="s", user="u", payload={}, json_mode=True))
        assert seen["cwd"] == tmp_path / "kiro_cwd" and seen["cwd"].is_dir()
        assert Path(__file__).resolve().parents[1] not in seen["cwd"].parents

    def test_canary_also_uses_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(kiro_cli, "NEUTRAL_CWD", tmp_path / "kiro_cwd")
        argv = fake_cli(tmp_path, '''
            cat > /dev/null
            pwd > %s/where.txt
            echo '{"ok": true, "n": 42}'
        ''' % tmp_path)
        assert canary(argv).ok
        assert (tmp_path / "where.txt").read_text(encoding="utf-8").strip().endswith("kiro_cwd")


class TestUsageProbe:
    def test_only_structure_and_usage_numbers_are_shown_never_the_answer(self):
        lines = [
            json.dumps({"type": "message", "content": "SECRET ANSWER TEXT", "n": 1}),
            json.dumps({"type": "metadata", "usage": {"credits": 0.04, "inputTokens": 120},
                        "info": {"name": "x"}}),
            "not json",
        ]
        shown = kiro_cli.summarize_events(lines)
        text = "\n".join(shown)
        assert "SECRET ANSWER TEXT" not in text
        assert "usage.credits=0.04" in text and "usage.inputTokens=120" in text
        assert "type=metadata" in text and "(JSON 아님)" in text


    def test_message_events_show_their_kind_and_text_length_but_not_the_text(self):
        line = json.dumps({"type": "sessionUpdate", "data": {"update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "SECRET ANSWER"}}}})
        shown = kiro_cli.summarize_events([line])[0]
        assert "update=agent_message_chunk" in shown and "text_len=13" in shown
        assert "SECRET" not in shown


SAMPLE_STREAM = "\n".join(json.dumps(e) for e in [
    {"type": "runStarted", "data": {}},
    {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "available_commands_update"}}},
    {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "agent_message_chunk",
                                                    "content": {"type": "text", "text": '{"ok": tr'}}}},
    {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "agent_message_chunk",
                                                    "content": {"type": "text", "text": 'ue, "n": 42}'}}}},
    {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "session_info_update", "_meta": {
        "kiro": {"promptTurnSummaries": [{"usage": 0.04}, {"usage": 0.01}]}}}}},
    {"type": "runFinished", "data": {}},
])


class TestStreamJson:
    def test_answer_chunks_are_joined_and_credits_summed(self):
        text, credits = kiro_cli.parse_stream(SAMPLE_STREAM)
        assert text == '{"ok": true, "n": 42}' and abs(credits - 0.05) < 1e-9

    def test_plain_text_and_empty_streams_fall_back(self):
        assert kiro_cli.parse_stream("그냥 텍스트 답변") == (None, 0.0)
        assert kiro_cli.parse_stream("") == (None, 0.0)
        text, credits = kiro_cli.parse_stream('{"type": "runStarted"}\n{"type": "runFinished"}')
        assert text is None and credits == 0.0  # JSON 은 있으나 답변 조각이 없다

    def test_chunks_of_other_kinds_are_not_part_of_the_answer(self):
        events = [
            {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "agent_thought_chunk",
                                                            "content": {"type": "text", "text": "생각"}}}},
            {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "agent_message_chunk",
                                                            "content": {"type": "text", "text": "답"}}}},
        ]
        assert kiro_cli.parse_stream("\n".join(json.dumps(e) for e in events))[0] == "답"

    def test_run_cli_asks_for_stream_json_and_returns_text_and_credits(self, tmp_path):
        out = tmp_path / "args.txt"
        sample = tmp_path / "sample.jsonl"
        sample.write_text(SAMPLE_STREAM, encoding="utf-8")
        argv = fake_cli(tmp_path, f'''
            cat > /dev/null
            echo "$@" > {out}
            cat {sample}
        ''')
        text, credits, _ = kiro_cli.run_cli("p", argv=argv, timeout=20)
        assert "--output-format stream-json" in out.read_text(encoding="utf-8")
        assert text == '{"ok": true, "n": 42}' and credits > 0.04
        assert kiro_cli.extract_json_payload(text) == {"ok": True, "n": 42}

    def test_metering_off_keeps_the_plain_text_mode(self, tmp_path):
        out = tmp_path / "args.txt"
        argv = fake_cli(tmp_path, f'''
            cat > /dev/null
            echo "$@" > {out}
            echo '{{"ok": true, "n": 42}}'
        ''')
        text, credits, _ = kiro_cli.run_cli("p", argv=argv, timeout=20, metering=False)
        assert "stream-json" not in out.read_text(encoding="utf-8") and credits == 0.0
        assert "ok" in text

    def test_a_cli_that_ignores_the_flag_still_works(self, tmp_path):
        argv = fake_cli(tmp_path, "cat > /dev/null\necho '{\"ok\": true, \"n\": 42}'")
        text, _, _ = kiro_cli.run_cli("p", argv=argv, timeout=20)  # stream-json 을 모르는 CLI
        assert kiro_cli.extract_json_payload(text) == {"ok": True, "n": 42}

    def test_canary_reports_credits_when_the_stream_has_them(self, tmp_path):
        sample = tmp_path / "sample.jsonl"
        sample.write_text(SAMPLE_STREAM, encoding="utf-8")
        argv = fake_cli(tmp_path, f"cat > /dev/null\ncat {sample}")
        result = canary(argv)
        assert result.ok and result.credits_seen
