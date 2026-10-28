"""무인 실행 마무리(channel/src/autopublish.py) 테스트.

핵심 보장: 최후 스위치(설정)가 꺼져 있으면 네트워크·호스팅을 절대 건드리지 않고, 모든 단계의 실패는
예외가 아니라 알림이 되며, 일시정지 파일·일일 상한이 게시를 막는다. 외부 호출은 모두 가짜로 대체한다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from channel.src.autopublish import (
    PAUSE_FILE,
    FAILED,
    HELD,
    PUBLISHED,
    SKIPPED,
    auto_publish,
    finish_run,
    published_today,
)
from channel.src.config import load_channel_config


@pytest.fixture
def channel(tmp_path):
    cfg = load_channel_config()
    cfg.instagram["output"]["dir"] = str(tmp_path / "review")
    cfg.instagram["publishing"]["auto_publish"] = True
    cfg.instagram["publishing"]["max_posts_per_day"] = 1
    return cfg


class Calls:
    def __init__(self, *, media=None, eligible=True, blockers=(), publish=None):
        self.log = []
        self.media, self.eligible, self.blockers, self.publish = media, eligible, blockers, publish

    def media_fn(self, directory, channel):
        self.log.append("media")
        if self.media:
            raise self.media

    def preflight_fn(self, directory, channel):
        self.log.append("preflight")
        return SimpleNamespace(eligible=self.eligible, blockers=list(self.blockers))

    def publish_fn(self, directory, channel):
        self.log.append("publish")
        if self.publish:
            raise self.publish
        return SimpleNamespace(external_id="1789", external_url="https://instagram.com/p/x")

    def record_fn(self, directory):
        self.log.append("record")


def run(channel, tmp_path, calls, **kw):
    return auto_publish(
        tmp_path, channel, media_fn=calls.media_fn, preflight_fn=calls.preflight_fn,
        publish_fn=calls.publish_fn, record_fn=calls.record_fn,
        pause_file=kw.pop("pause_file", tmp_path / "no-pause"), **kw)


class TestAutoPublish:
    def test_happy_path_runs_every_step_in_order(self, channel, tmp_path):
        calls = Calls()
        result = run(channel, tmp_path, calls)
        assert calls.log == ["media", "preflight", "publish", "record"]
        assert result.status == PUBLISHED and result.url.endswith("/p/x")

    def test_switch_off_touches_nothing(self, channel, tmp_path):
        channel.instagram["publishing"]["auto_publish"] = False
        calls = Calls()
        result = run(channel, tmp_path, calls)
        assert result.status == SKIPPED and calls.log == []

    def test_the_safety_nets_are_configured(self):
        """스위치가 켜져 있어도 일일 상한과 일시정지 파일 경로가 있어야 한다."""
        cfg = load_channel_config()
        assert int(cfg.get("instagram.publishing.max_posts_per_day")) == 1
        assert PAUSE_FILE.name == "PAUSE_PUBLISHING" and PAUSE_FILE.parent.name == "logs"

    def test_pause_file_stops_publishing(self, channel, tmp_path):
        pause = tmp_path / "PAUSE"
        pause.write_text("", encoding="utf-8")
        calls = Calls()
        result = run(channel, tmp_path, calls, pause_file=pause)
        assert result.status == SKIPPED and "일시정지" in result.reason and calls.log == []

    def test_daily_cap(self, channel, tmp_path):
        done = tmp_path / "review" / "2026-10-28"
        done.mkdir(parents=True)
        stamp = datetime(2026, 10, 28, 12, 0).astimezone().isoformat()
        (done / "publishing.json").write_text(
            json.dumps({"ok": True, "completed_at": stamp}), encoding="utf-8")
        assert published_today(channel, datetime(2026, 10, 28, 15, 0).astimezone()) == 1
        calls = Calls()
        result = run(channel, tmp_path, calls, today=datetime(2026, 10, 28, 15, 0).astimezone())
        assert result.status == SKIPPED and "상한" in result.reason and calls.log == []
        assert published_today(channel, datetime(2026, 10, 29, 9, 0).astimezone()) == 0

    def test_failed_publishes_do_not_count_against_the_cap(self, channel, tmp_path):
        done = tmp_path / "review" / "x"
        done.mkdir(parents=True)
        (done / "publishing.json").write_text(
            json.dumps({"ok": False, "completed_at": datetime.now().astimezone().isoformat()}),
            encoding="utf-8")
        assert published_today(channel) == 0

    def test_hosting_failure_is_a_result_not_an_exception(self, channel, tmp_path):
        calls = Calls(media=RuntimeError("git push 실패"))
        result = run(channel, tmp_path, calls)
        assert result.status == FAILED and "호스팅" in result.reason and calls.log == ["media"]

    def test_preflight_blockers_hold_the_post(self, channel, tmp_path):
        calls = Calls(eligible=False, blockers=["stale_content", "approval_too_old"])
        result = run(channel, tmp_path, calls)
        assert result.status == HELD and result.details == ["stale_content", "approval_too_old"]
        assert "publish" not in calls.log

    def test_api_failure_is_reported_and_not_retried(self, channel, tmp_path):
        calls = Calls(publish=RuntimeError("Instagram API 400"))
        result = run(channel, tmp_path, calls)
        assert result.status == FAILED and calls.log.count("publish") == 1 and "record" not in calls.log

    def test_history_failure_after_a_live_post_is_not_a_failure(self, channel, tmp_path):
        calls = Calls()
        calls.record_fn = lambda d: (_ for _ in ()).throw(RuntimeError("history"))
        assert run(channel, tmp_path, calls).status == PUBLISHED


class TestFinishRun:
    def finish(self, channel, outcome, *, approved=True, held=(), notifier=None):
        sent = []
        notifier = notifier or (lambda title, msg, **kw: sent.append((title, msg, kw)))
        status = finish_run(
            channel, approved_dir=Path("/x/2026-10-28-3") if approved else None,
            title="transformers v5.21.0", held=list(held), notifier=notifier,
            publisher=lambda d, c: outcome)
        return status, sent

    def test_no_approved_candidate_is_announced_with_reasons(self, channel):
        status, sent = self.finish(channel, None, approved=False,
                                   held=["Genie 4: material_too_thin", "Sui: duplicate_topic"])
        assert status == "none" and "승인된 후보 없음" in sent[0][0]
        assert "Genie 4" in sent[0][1] and sent[0][2]["banner"] is True

    def test_published(self, channel):
        status, sent = self.finish(channel, SimpleNamespace(status=PUBLISHED, url="https://i/p/1",
                                                            reason="", details=[]))
        assert status == PUBLISHED and "게시 완료" in sent[0][0] and "https://i/p/1" in sent[0][1]

    def test_switch_off_still_reports_the_approval(self, channel):
        status, sent = self.finish(channel, SimpleNamespace(status=SKIPPED, url="", details=[],
                                                            reason="자동 게시가 꺼져 있습니다"))
        assert status == "approved" and "승인됨" in sent[0][0] and "2026-10-28-3" in sent[0][1]

    @pytest.mark.parametrize("status,word", [(FAILED, "게시 실패"), (HELD, "게시 보류")])
    def test_failures_are_announced_with_details(self, channel, status, word):
        out = SimpleNamespace(status=status, url="", reason="사유", details=["stale_content"])
        result, sent = self.finish(channel, out)
        assert result == status and word in sent[0][0] and "stale_content" in sent[0][1]

    def test_notifications_can_be_disabled(self, channel):
        channel.instagram["notify"]["enabled"] = False
        _, sent = self.finish(channel, None, approved=False)
        assert sent == []

    def test_a_broken_notifier_never_breaks_the_run(self, channel):
        def boom(*a, **k):
            raise OSError("osascript 없음")

        status, _ = self.finish(channel, None, approved=False, notifier=boom)
        assert status == "none"


class TestAccessCheck:
    """카드를 만들기 전에 Meta API 접근을 읽기 전용으로 점검한다."""

    def test_publisher_check_reports_the_account_and_never_posts(self):
        from channel.src.publisher import InstagramGraphClient, check_access

        calls = []

        class Session:
            def request(self, method, url, **kw):
                calls.append((method, url))
                return SimpleNamespace(status_code=200, json=lambda: {
                    "user_id": "1", "username": "r_a_glance", "account_type": "BUSINESS"})

        client = InstagramGraphClient(access_token="tok-secret", instagram_user_id="1",
                                      session=Session())
        ok, message = check_access(load_channel_config(), client=client)
        assert ok and "@r_a_glance" in message and "BUSINESS" in message
        assert [m for m, _ in calls] == ["GET"] and calls[0][1].endswith("/me")  # 읽기 전용

    def test_blocked_api_is_reported_without_leaking_the_token(self):
        from channel.src.publisher import InstagramGraphClient, check_access

        class Session:
            def request(self, method, url, **kw):
                return SimpleNamespace(status_code=400, json=lambda: {"error": {
                    "message": "API access blocked.", "type": "OAuthException", "code": 200}})

        client = InstagramGraphClient(access_token="tok-secret", instagram_user_id="1",
                                      session=Session())
        ok, message = check_access(load_channel_config(), client=client)
        assert not ok and "OAuthException/200" in message and "tok-secret" not in message

    def test_missing_credentials_are_reported_by_name_only(self):
        from channel.src.publisher import check_access

        ok, message = check_access(load_channel_config(), environ={})
        assert not ok and "INSTAGRAM_ACCESS_TOKEN" in message

    def test_unreachable_api_stops_the_run_before_any_card_is_made(self, channel, tmp_path):
        from channel.src.autopublish import access_ok

        ok, why = access_ok(channel, check_fn=lambda c: (False, "OAuthException/200"),
                            pause_file=tmp_path / "none")
        assert not ok and "200" in why

    def test_no_check_when_auto_publish_is_off_or_paused(self, channel, tmp_path):
        from channel.src.autopublish import access_ok

        boom = lambda c: (_ for _ in ()).throw(AssertionError("호출되면 안 됨"))  # noqa: E731
        channel.instagram["publishing"]["auto_publish"] = False
        assert access_ok(channel, check_fn=boom, pause_file=tmp_path / "none")[0] is True
        channel.instagram["publishing"]["auto_publish"] = True
        pause = tmp_path / "PAUSE"
        pause.write_text("", encoding="utf-8")
        assert access_ok(channel, check_fn=boom, pause_file=pause)[0] is True

    def test_a_crashing_check_counts_as_unreachable(self, channel, tmp_path):
        from channel.src.autopublish import access_ok

        def crash(c):
            raise RuntimeError("DNS")

        ok, why = access_ok(channel, check_fn=crash, pause_file=tmp_path / "none")
        assert not ok and "DNS" in why
