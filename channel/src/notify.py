"""무인 실행의 실패 알림.

launchd 로 돌리면 아무도 터미널을 보지 않으므로, 실패가 조용히 묻히지 않게 두 곳에 남깁니다.
1. `logs/channel_alerts.log` 에 한 줄 추가 (항상, 확실한 기록)
2. macOS 알림 배너 (best effort: 로그인 세션 밖이거나 권한이 없으면 건너뜁니다)

비밀값은 받지도 남기지도 않습니다. 메시지는 호출하는 쪽이 정합니다.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ALERT_LOG = Path(__file__).resolve().parents[2] / "logs" / "channel_alerts.log"


def _banner(title: str, message: str) -> bool:
    script = "display notification {} with title {}".format(
        _applescript_string(message), _applescript_string(title)
    )
    try:
        done = subprocess.run(
            ["osascript", "-e", script], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def notify(
    title: str,
    message: str,
    *,
    log_path: Path | None = None,
    banner: bool = True,
    telegram: bool = True,
) -> Path:
    """Record an alert. Returns the log path; the log write is the part that must work.

    로그가 기본이고, 배너(맥미니 화면)와 텔레그램(휴대폰)은 best effort 다. 텔레그램은 ``RAG_TELEGRAM_*``
    환경변수가 있을 때만 보낸다.
    """
    path = log_path or ALERT_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{stamp} [{title}] {message}".replace("\n", " ")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    if banner:
        _banner(title, message)
    if telegram:
        from channel.src import telegram_notify

        try:
            telegram_notify.send_text(f"{title}\n{message}")
        except Exception:  # noqa: BLE001 — 알림 실패가 실행을 깨면 안 된다
            pass
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R_A_Glance 실패 알림")
    parser.add_argument("title")
    parser.add_argument("message")
    args = parser.parse_args(argv)
    print(notify(args.title, args.message))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
