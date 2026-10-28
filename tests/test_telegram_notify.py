"""텔레그램 알림(channel/src/telegram_notify.py) 테스트.

핵심 보장: 설정이 없으면 네트워크를 건드리지 않고, 실패는 예외가 아니라 False 이며, 토큰은 어디에도 새지
않고, 게시 완료는 표지 사진+링크로 가며 사진이 안 되면 글로 대체된다. 실제 텔레그램에는 접속하지 않는다.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from channel.src import telegram_notify as tg
from channel.src.autopublish import FAILED, PUBLISHED, AutoPublishResult, finish_run
from channel.src.config import load_channel_config
from channel.src.notify import notify
from src.core.envcheck import BAD_FORMAT, MISSING, OK, PLACEHOLDER, check

TOKEN = "123456789:FAKE_TOKEN_FOR_TESTS_ONLY"
ENV = {tg.TOKEN_ENV: TOKEN, tg.CHAT_ENV: "424242"}


class Http:
    def __init__(self, ok=True, status=200, boom=None, updates=None):
        self.ok, self.status, self.boom, self.updates = ok, status, boom, updates
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, data=None, timeout=None):
        self.posts.append((url, dict(data or {})))
        if self.boom:
            raise self.boom
        return SimpleNamespace(status_code=self.status, json=lambda: {"ok": self.ok})

    def get(self, url, timeout=None):
        return SimpleNamespace(json=lambda: {"result": self.updates or []})


class TestSend:
    def test_unconfigured_never_touches_the_network(self):
        http = Http()
        assert tg.send_text("hi", environ={}, http=http) is False and http.posts == []
        assert tg.send_photo("https://x/a.jpg", "c", environ={tg.TOKEN_ENV: TOKEN}, http=http) is False
        assert http.posts == [] and not tg.configured({}) and tg.configured(ENV)

    def test_text_goes_to_the_configured_chat_without_markup(self):
        http = Http()
        assert tg.send_text("게시 완료", environ=ENV, http=http)
        url, data = http.posts[0]
        assert url.endswith("/sendMessage") and data["chat_id"] == "424242"
        assert data["text"] == "게시 완료" and "parse_mode" not in data

    def test_long_text_is_cut_to_the_api_limit(self):
        http = Http()
        tg.send_text("가" * 9000, environ=ENV, http=http)
        assert len(http.posts[0][1]["text"]) <= tg.TEXT_LIMIT

    @pytest.mark.parametrize("http", [Http(ok=False), Http(status=500), Http(boom=requests.ConnectionError("x"))])
    def test_failures_return_false_never_raise(self, http):
        assert tg.send_text("hi", environ=ENV, http=http) is False

    def test_the_token_never_appears_in_logs_or_errors(self, caplog):
        http = Http(boom=requests.ConnectionError(f"https://api.telegram.org/bot{TOKEN}/sendMessage failed"))
        with caplog.at_level("DEBUG"):
            assert tg.send_text("hi", environ=ENV, http=http) is False
        assert TOKEN not in caplog.text

    def test_photo_with_caption_and_a_text_fallback(self):
        http = Http()
        assert tg.send_photo("https://host/01.jpg", "캡션", environ=ENV, http=http)
        assert http.posts[0][0].endswith("/sendPhoto") and http.posts[0][1]["photo"] == "https://host/01.jpg"
        broken = Http(ok=False)
        assert tg.send_photo("https://host/01.jpg", "캡션", environ=ENV, http=broken) is False
        assert [u.rsplit("/", 1)[1] for u, _ in broken.posts] == ["sendPhoto", "sendMessage"]

    def test_non_https_photo_url_is_sent_as_text(self):
        http = Http()
        assert tg.send_photo("", "글만", environ=ENV, http=http)
        assert http.posts[0][0].endswith("/sendMessage")

    def test_discover_lists_chats_without_printing_the_token(self):
        updates = [{"message": {"chat": {"id": 424242, "type": "private", "first_name": "Jin"}}}]
        found = tg.discover_chats(environ=ENV, http=Http(updates=updates))
        assert found == ["424242  Jin (private)"] and all(TOKEN not in line for line in found)
        assert tg.discover_chats(environ={}, http=Http()) == []


class TestNotifyFansOut:
    def test_log_always_telegram_only_when_asked(self, tmp_path, monkeypatch):
        sent = []
        monkeypatch.setattr(tg, "send_text", lambda text, **kw: sent.append(text) or True)
        notify("제목", "내용", log_path=tmp_path / "a.log", banner=False)
        assert sent == ["제목\n내용"] and "제목" in (tmp_path / "a.log").read_text(encoding="utf-8")
        notify("제목2", "내용2", log_path=tmp_path / "b.log", banner=False, telegram=False)
        assert len(sent) == 1 and (tmp_path / "b.log").exists()

    def test_a_crashing_telegram_never_breaks_the_log(self, tmp_path, monkeypatch):
        def boom(text, **kw):
            raise RuntimeError("down")

        monkeypatch.setattr(tg, "send_text", boom)
        path = notify("t", "m", log_path=tmp_path / "c.log", banner=False)
        assert path.read_text(encoding="utf-8").strip()


class TestPublishedMessage:
    def run(self, outcome, photo_ok=True, **cfg):
        channel = load_channel_config()
        channel.instagram["notify"]["telegram"].update(cfg)
        texts, photos = [], []

        def photo_sender(url, caption):
            photos.append((url, caption))
            return photo_ok and bool(url)

        status = finish_run(
            channel, approved_dir=None if outcome is None else __import__("pathlib").Path("/x/2026-11-01"),
            title="Sui testnet", held=["Genie: material_too_thin"],
            notifier=lambda t, m, **kw: texts.append((t, m, kw)),
            publisher=lambda d, c: outcome, photo_sender=photo_sender)
        return status, texts, photos

    def test_publish_sends_cover_photo_and_link_not_a_duplicate_text(self):
        outcome = AutoPublishResult(PUBLISHED, url="https://www.instagram.com/p/ABC/",
                                    cover_url="https://host/2026-11-01/h/01.jpg")
        status, texts, photos = self.run(outcome)
        assert status == PUBLISHED
        assert photos == [("https://host/2026-11-01/h/01.jpg",
                           "게시 완료\nSui testnet\nhttps://www.instagram.com/p/ABC/")]
        assert texts[0][2]["telegram"] is False  # 로그·배너만 — 텔레그램은 사진 한 통

    def test_without_a_cover_or_when_the_photo_fails_it_falls_back_to_text(self):
        outcome = AutoPublishResult(PUBLISHED, url="", cover_url="")
        _, _, photos = self.run(outcome)
        handle = load_channel_config().get("instagram.channel.handle")
        assert photos[-1][0] == "" and f"instagram.com/{handle}" in photos[-1][1]
        outcome = AutoPublishResult(PUBLISHED, url="https://i/p/1", cover_url="https://host/01.jpg")
        _, _, photos = self.run(outcome, photo_ok=False)
        assert photos[0][0] == "https://host/01.jpg" and photos[1][0] == ""

    def test_cover_photo_can_be_turned_off(self):
        outcome = AutoPublishResult(PUBLISHED, url="https://i/p/1", cover_url="https://host/01.jpg")
        _, _, photos = self.run(outcome, cover_photo=False)
        assert [p[0] for p in photos] == [""]

    def test_failures_and_no_candidate_go_through_the_normal_telegram_path(self):
        out = AutoPublishResult(FAILED, reason="게시 실패: API", details=["x"])
        status, texts, photos = self.run(out)
        assert status == FAILED and photos == [] and texts[0][2]["telegram"] is True
        status, texts, photos = self.run(None)
        assert status == "none" and "Genie" in texts[0][1] and texts[0][2]["telegram"] is True

    def test_telegram_can_be_disabled_in_config(self):
        status, texts, photos = self.run(None, enabled=False)
        assert texts[0][2]["telegram"] is False and photos == []


class TestEnvcheck:
    def names(self, env):
        return {f.name: f.status for f in check(env, telegram=True)}

    BASE = {"LLM_PROVIDER": "kiro_cli", "KIRO_CLI_COMMAND": "/bin/sh"}

    def test_valid_values_pass(self):
        s = self.names({**self.BASE, **ENV})
        assert s[tg.TOKEN_ENV] == OK and s[tg.CHAT_ENV] == OK

    def test_group_chat_ids_may_be_negative(self):
        assert self.names({**self.BASE, tg.TOKEN_ENV: TOKEN, tg.CHAT_ENV: "-1001234567890"})[tg.CHAT_ENV] == OK

    @pytest.mark.parametrize("value,status", [("", MISSING), ("your-token", PLACEHOLDER), ("abc", BAD_FORMAT)])
    def test_bad_token(self, value, status):
        assert self.names({**self.BASE, tg.TOKEN_ENV: value, tg.CHAT_ENV: "424242"})[tg.TOKEN_ENV] == status

    def test_bad_chat_id_and_not_checked_unless_asked(self):
        assert self.names({**self.BASE, tg.TOKEN_ENV: TOKEN, tg.CHAT_ENV: "me"})[tg.CHAT_ENV] == BAD_FORMAT
        assert tg.TOKEN_ENV not in {f.name for f in check({**self.BASE, **ENV})}


class TestShortEnvNames:
    """운영자가 쓰는 짧은 이름(TELEGRAM_BOT, TELEGRAM_ALLOWED_USER_ID)도 인식한다."""

    SHORT = {"TELEGRAM_BOT": TOKEN, "TELEGRAM_ALLOWED_USER_ID": "424242"}

    def test_short_names_work_and_send_to_that_user(self):
        http = Http()
        assert tg.configured(self.SHORT)
        assert tg.send_text("hi", environ=self.SHORT, http=http)
        assert http.posts[0][1]["chat_id"] == "424242" and f"/bot{TOKEN}/" in http.posts[0][0]

    def test_the_long_names_take_precedence(self):
        env = {**self.SHORT, tg.TOKEN_ENV: "999999999:FAKE_OTHER_TOKEN_FOR_TESTS", tg.CHAT_ENV: "777777"}
        http = Http()
        tg.send_text("hi", environ=env, http=http)
        assert http.posts[0][1]["chat_id"] == "777777" and "/bot999999999:" in http.posts[0][0]

    def test_only_one_of_the_two_is_not_configured(self):
        assert not tg.configured({"TELEGRAM_BOT": TOKEN})
        assert not tg.configured({"TELEGRAM_ALLOWED_USER_ID": "424242"})

    def test_envcheck_reports_the_name_that_is_actually_used(self):
        names = {f.name: f.status for f in check({"LLM_PROVIDER": "kiro_cli", "KIRO_CLI_COMMAND": "/bin/sh",
                                                  **self.SHORT}, telegram=True)}
        assert names["TELEGRAM_BOT"] == OK and names["TELEGRAM_ALLOWED_USER_ID"] == OK
