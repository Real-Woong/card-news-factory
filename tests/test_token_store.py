"""토큰 자동 갱신(channel/src/token_store.py) 테스트.

핵심 보장: 토큰 값은 어디에도 출력되지 않고, .env 는 건드리지 않으며(저장본은 별도 파일, 권한 600),
운영자가 .env 토큰을 바꾸면 저장본보다 우선하고, 갱신 실패·만료 임박은 알림이 된다. 네트워크는 가짜로 대체한다.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from channel.src.config import load_channel_config
from channel.src.token_store import (
    TOKEN_ENV,
    effective_environ,
    fingerprint,
    maintain,
    read_store,
    refresh_token,
)

BASE = "BASE-token-0123456789-abcdefghijklmnop"
FRESH = "FRESH-token-9876543210-qrstuvwxyz"
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def channel():
    return load_channel_config()


class Http:
    def __init__(self, payload=None, status=200, boom=None):
        self.payload = payload if payload is not None else {
            "access_token": FRESH, "token_type": "bearer", "expires_in": 5183944}
        self.status, self.boom, self.calls = status, boom, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if self.boom:
            raise self.boom
        return SimpleNamespace(status_code=self.status, json=lambda: self.payload)


def env(token=BASE):
    return {TOKEN_ENV: token} if token else {}


class TestRefresh:
    def test_first_refresh_writes_a_private_store_and_the_new_token_is_used(self, channel, tmp_path):
        store = tmp_path / "x" / "token.json"
        http = Http()
        outcome = refresh_token(channel, environ=env(), path=store, http=http, now=NOW)
        assert outcome.status == "refreshed" and 55 <= outcome.days_left <= 60
        assert http.calls[0][1] == {"grant_type": "ig_refresh_token", "access_token": BASE}
        assert stat.S_IMODE(os.stat(store).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(store.parent).st_mode) == 0o700
        assert effective_environ(env(), store)[TOKEN_ENV] == FRESH

    def test_the_store_never_contains_the_original_token_only_its_fingerprint(self, channel, tmp_path):
        store = tmp_path / "token.json"
        refresh_token(channel, environ=env(), path=store, http=Http(), now=NOW)
        raw = store.read_text(encoding="utf-8")
        assert BASE not in raw and fingerprint(BASE) in raw

    def test_not_due_means_no_network_call(self, channel, tmp_path):
        store = tmp_path / "token.json"
        refresh_token(channel, environ=env(), path=store, http=Http(), now=NOW)
        http = Http()
        outcome = refresh_token(channel, environ=env(), path=store, http=http,
                                now=NOW + timedelta(days=3))
        assert outcome.status == "skipped" and http.calls == []
        again = refresh_token(channel, environ=env(), path=store, http=http,
                              now=NOW + timedelta(days=8))
        assert again.status == "refreshed" and len(http.calls) == 1

    def test_the_refreshed_token_is_used_for_the_next_refresh(self, channel, tmp_path):
        store = tmp_path / "token.json"
        refresh_token(channel, environ=env(), path=store, http=Http(), now=NOW)
        http = Http({"access_token": "THIRD-token-aaaaaaaaaaaaaaaaaaaaaa", "expires_in": 5184000})
        refresh_token(channel, environ=env(), path=store, http=http, now=NOW + timedelta(days=8))
        assert http.calls[0][1]["access_token"] == FRESH  # .env 의 낡은 토큰이 아니라 직전 갱신본

    def test_rotating_the_env_token_wins_over_the_store(self, channel, tmp_path):
        store = tmp_path / "token.json"
        refresh_token(channel, environ=env(), path=store, http=Http(), now=NOW)
        assert effective_environ(env("OPERATOR-NEW-token-zzzzzzzzzzzzzzzzzzzz"), store)[TOKEN_ENV] \
            == "OPERATOR-NEW-token-zzzzzzzzzzzzzzzzzzzz"
        # 새 토큰으로는 저장본을 새로 만든다(옛 갱신본을 쓰지 않는다)
        http = Http()
        refresh_token(channel, environ=env("OPERATOR-NEW-token-zzzzzzzzzzzzzzzzzzzz"), path=store,
                      http=http, now=NOW + timedelta(days=1))
        assert http.calls[0][1]["access_token"] == "OPERATOR-NEW-token-zzzzzzzzzzzzzzzzzzzz"

    def test_an_expired_store_is_ignored(self, channel, tmp_path):
        store = tmp_path / "token.json"
        refresh_token(channel, environ=env(), path=store, http=Http(), now=NOW)
        data = read_store(store)
        data["expires_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        store.write_text(json.dumps(data), encoding="utf-8")
        assert effective_environ(env(), store)[TOKEN_ENV] == BASE

    def test_no_token_or_disabled_does_nothing(self, channel, tmp_path):
        http = Http()
        assert refresh_token(channel, environ={}, path=tmp_path / "t.json", http=http).status == "skipped"
        channel.instagram["publishing"]["token_refresh"]["enabled"] = False
        assert refresh_token(channel, environ=env(), path=tmp_path / "t.json", http=http).status == "skipped"
        assert http.calls == []


class TestFailures:
    def test_rejection_is_reported_without_leaking_the_token(self, channel, tmp_path):
        http = Http({"error": {"message": f"bad token {BASE}", "type": "OAuthException", "code": 190}},
                    status=400)
        outcome = refresh_token(channel, environ=env(), path=tmp_path / "t.json", http=http, now=NOW)
        assert outcome.status == "failed" and "OAuthException/190" in outcome.message
        assert BASE not in outcome.message and "[REDACTED]" in outcome.message
        assert not (tmp_path / "t.json").exists()  # 실패하면 저장본을 만들지 않는다

    def test_network_error_and_missing_token_in_response(self, channel, tmp_path):
        import requests

        out = refresh_token(channel, environ=env(), path=tmp_path / "a.json",
                            http=Http(boom=requests.ConnectionError("x")), now=NOW)
        assert out.status == "failed"
        out = refresh_token(channel, environ=env(), path=tmp_path / "b.json",
                            http=Http({"expires_in": 1}), now=NOW)
        assert out.status == "failed"

    def test_a_failed_refresh_keeps_the_old_store_usable(self, channel, tmp_path):
        store = tmp_path / "token.json"
        refresh_token(channel, environ=env(), path=store, http=Http(), now=NOW)
        refresh_token(channel, environ=env(), path=store, http=Http({"error": {"message": "x"}}, 400),
                      now=NOW + timedelta(days=8))
        assert effective_environ(env(), store)[TOKEN_ENV] == FRESH


class TestMaintainAlerts:
    def run(self, channel, tmp_path, http, *, now=NOW, token=BASE):
        sent = []
        out = maintain(channel, notifier=lambda t, m, **k: sent.append((t, m)),
                       environ=env(token), path=tmp_path / "t.json", http=http, now=now)
        return out, sent

    def test_success_is_quiet(self, channel, tmp_path):
        out, sent = self.run(channel, tmp_path, Http())
        assert out.status == "refreshed" and sent == []

    def test_failure_alerts_and_never_contains_the_token(self, channel, tmp_path):
        out, sent = self.run(channel, tmp_path, Http({"error": {"message": "too new"}}, 400))
        assert out.status == "failed" and sent and "토큰 갱신 필요" in sent[0][0]
        assert BASE not in sent[0][1]

    def test_expiry_close_and_not_refreshable_alerts(self, channel, tmp_path):
        # 처음 본 날로부터 55일 뒤, 갱신도 실패 → 곧 만료
        refresh_token(channel, environ=env(), path=tmp_path / "t.json", http=Http(), now=NOW)
        data = read_store(tmp_path / "t.json")
        data["expires_at"] = (NOW + timedelta(days=5)).isoformat()
        data["refreshed_at"] = (NOW - timedelta(days=1)).isoformat()
        (tmp_path / "t.json").write_text(json.dumps(data), encoding="utf-8")
        out, sent = self.run(channel, tmp_path, Http(), now=NOW)
        assert out.status == "skipped" and out.days_left <= 14 and sent  # 최근 갱신했지만 만료가 가깝다

    def test_a_crash_inside_refresh_does_not_stop_the_run(self, channel, tmp_path):
        class Bad:
            def get(self, *a, **k):
                raise RuntimeError("boom")

        out, sent = self.run(channel, tmp_path, Bad())
        assert out.status == "failed" and sent
