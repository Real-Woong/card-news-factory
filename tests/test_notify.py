"""실패 알림: 로그 기록이 핵심이고, 배너 실패는 삼킨다."""

from __future__ import annotations

from channel.src import notify as notify_module
from channel.src.notify import _applescript_string, notify


def test_alert_is_appended_to_log_without_banner(tmp_path):
    log = tmp_path / "logs" / "alerts.log"
    notify("R_A_Glance", "첫째", log_path=log, banner=False)
    notify("R_A_Glance", "둘째\n줄바꿈", log_path=log, banner=False)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "[R_A_Glance] 첫째" in lines[0] and "줄바꿈" in lines[1]


def test_banner_failure_never_breaks_the_log_write(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("no osascript")

    monkeypatch.setattr(notify_module.subprocess, "run", boom)
    log = tmp_path / "alerts.log"

    assert notify("t", "m", log_path=log) == log
    assert log.exists()


def test_applescript_quoting_escapes_quotes_and_backslashes():
    assert _applescript_string('a"b\\c') == '"a\\"b\\\\c"'
