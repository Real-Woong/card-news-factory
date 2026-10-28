"""무인 실행의 마지막 단계: 승인된 후보 게시 + 결과 알림.

자동 승인 게이트(auto_gate)가 승인한 후보를 사람 없이 올리기 위한 연결입니다. 새 판단을 하지 않고 기존
부품만 순서대로 잇습니다::

    media.publish_media   PNG → JPEG → 정적 호스팅에 push → 공개 URL 확인   (media.py)
    publisher.preflight   승인·해시·최신성·매체 URL·자격증명 등 모든 조건 재검사 (publisher.py)
    publisher.publish_output   Instagram Graph API 게시

**최후 스위치는 설정이다.** 아래가 모두 켜져야 실제로 게시한다:
``instagram.publishing.auto_publish: true`` + ``instagram.channel.phase: 3`` + ``publish: true``
(뒤의 둘은 publisher.preflight 가 다시 확인한다). 추가 안전장치:

* 일시정지 파일 ``logs/PAUSE_PUBLISHING`` 이 있으면 게시하지 않는다(``touch`` 한 번으로 즉시 중지).
* 하루 게시 상한(``max_posts_per_day``, 기본 1).
* 이 모듈은 예외를 밖으로 내보내지 않는다 — 실패는 알림으로 남기고 후보는 그대로 보존한다.

모든 결과(승인·게시·보류·실패)는 알림(로그 + macOS 배너)으로 남긴다. 사람이 화면을 보지 않기 때문이다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.core.logging_setup import get_logger

from channel.src.config import ChannelConfig
from channel.src.notify import notify as default_notify

logger = get_logger(__name__)

PAUSE_FILE = Path(__file__).resolve().parents[2] / "logs" / "PAUSE_PUBLISHING"
TITLE = "R_A_Glance"

PUBLISHED = "published"
SKIPPED = "skipped"
HELD = "held"
FAILED = "failed"


@dataclass
class AutoPublishResult:
    status: str  # published | skipped | held | failed
    reason: str = ""
    url: str = ""
    cover_url: str = ""  # 게시한 첫 장(공개 JPEG URL) — 텔레그램 알림에 사진으로 붙인다
    details: list[str] = field(default_factory=list)


def _settings(channel: ChannelConfig) -> dict[str, Any]:
    return channel.get("instagram.publishing", {}) or {}


def published_today(channel: ChannelConfig, today: datetime | None = None) -> int:
    """오늘(로컬 날짜) 게시 성공한 출력 개수."""
    day = (today or datetime.now().astimezone()).date()
    count = 0
    root = channel.outputs_dir
    if not root.is_dir():
        return 0
    for folder in root.iterdir():
        path = folder / "publishing.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            done = datetime.fromisoformat(str(data.get("completed_at")).replace("Z", "+00:00"))
        except (OSError, ValueError):
            continue
        if data.get("ok") and done.astimezone().date() == day:
            count += 1
    return count


def auto_publish(
    directory: Path,
    channel: ChannelConfig,
    *,
    media_fn: Callable[..., Any] | None = None,
    preflight_fn: Callable[..., Any] | None = None,
    publish_fn: Callable[..., Any] | None = None,
    record_fn: Callable[..., Any] | None = None,
    pause_file: Path | None = None,
    today: datetime | None = None,
) -> AutoPublishResult:
    """승인된 출력 하나를 게시한다. 절대 예외를 던지지 않는다."""
    settings = _settings(channel)
    if not bool(settings.get("auto_publish", False)):
        return AutoPublishResult(SKIPPED, "자동 게시가 꺼져 있습니다(instagram.publishing.auto_publish)")
    if (pause_file or PAUSE_FILE).exists():
        return AutoPublishResult(SKIPPED, "일시정지 파일이 있습니다(logs/PAUSE_PUBLISHING)")
    cap = int(settings.get("max_posts_per_day", 1))
    if published_today(channel, today) >= cap:
        return AutoPublishResult(SKIPPED, f"오늘 게시 상한({cap}건)에 도달했습니다")

    # 지연 import: 설정이 꺼진 일반 실행은 requests·git 관련 코드를 불러오지 않는다.
    from channel.src import media as media_mod
    from channel.src import publisher as publisher_mod

    media_fn = media_fn or media_mod.publish_media
    preflight_fn = preflight_fn or publisher_mod.preflight
    publish_fn = publish_fn or publisher_mod.publish_output
    record_fn = record_fn or publisher_mod.record_published_post

    try:
        media_fn(directory, channel)
    except Exception as exc:  # noqa: BLE001 — MediaError·git·네트워크 모두 알림으로
        return AutoPublishResult(FAILED, f"이미지 호스팅 실패: {exc}")

    try:
        check = preflight_fn(directory, channel)
    except Exception as exc:  # noqa: BLE001
        return AutoPublishResult(FAILED, f"게시 사전 점검 오류: {exc}")
    if not check.eligible:
        return AutoPublishResult(HELD, "게시 사전 점검 실패", details=list(check.blockers))

    try:
        result = publish_fn(directory, channel)
    except Exception as exc:  # noqa: BLE001 — 모호한 실패는 재시도하지 않고 사람이 확인한다
        return AutoPublishResult(FAILED, f"게시 실패: {exc}")

    try:
        record_fn(directory)
    except Exception as exc:  # noqa: BLE001 — 이미 게시됨; 기록 실패는 경고일 뿐
        logger.warning("[autopublish] 게시는 완료됐지만 post history 기록 실패: %s", exc)
    return AutoPublishResult(PUBLISHED, url=str(getattr(result, "external_url", "") or ""),
                             cover_url=_cover_url(directory),
                             details=[str(getattr(result, "external_id", "") or "")])


def access_ok(
    channel: ChannelConfig,
    *,
    check_fn: Callable[..., tuple[bool, str]] | None = None,
    pause_file: Path | None = None,
) -> tuple[bool, str]:
    """카드를 만들기 전에 Meta API 에 접근할 수 있는지 본다(LLM 비용을 쓰기 전에 막는다).

    자동 게시가 꺼져 있거나 일시정지 중이면 점검하지 않고 통과한다(어차피 게시하지 않는다).
    """
    if not bool(_settings(channel).get("auto_publish", False)):
        return True, ""
    if (pause_file or PAUSE_FILE).exists():
        return True, ""
    if check_fn is None:
        from channel.src.publisher import check_access

        check_fn = check_access
    try:
        return check_fn(channel)
    except Exception as exc:  # noqa: BLE001 — 점검 자체의 오류도 "게시 불가"로 본다
        return False, f"접근 점검 오류: {exc}"


def _cover_url(directory: Path) -> str:
    """게시에 쓴 첫 장의 공개 URL(매니페스트). 없으면 빈 문자열."""
    try:
        data = json.loads((directory / "publishing_media.json").read_text(encoding="utf-8"))
        return str((data.get("image_urls") or [""])[0])
    except (OSError, ValueError, AttributeError, IndexError):
        return ""


def _notify_settings(channel: ChannelConfig) -> tuple[bool, bool]:
    config = channel.get("instagram.notify", {}) or {}
    return bool(config.get("enabled", True)), bool(config.get("banner", True))


def _telegram_settings(channel: ChannelConfig) -> tuple[bool, bool]:
    config = (channel.get("instagram.notify", {}) or {}).get("telegram", {}) or {}
    return bool(config.get("enabled", True)), bool(config.get("cover_photo", True))


def finish_run(
    channel: ChannelConfig,
    *,
    approved_dir: Path | None,
    title: str,
    held: list[str],
    notifier: Callable[..., Any] = default_notify,
    publisher: Callable[..., AutoPublishResult] = auto_publish,
    photo_sender: Callable[..., bool] | None = None,
) -> str:
    """실행 결과를 정리해 게시하고 알린다. 반환: published|approved|held|failed|none."""
    enabled, banner = _notify_settings(channel)
    telegram, cover_photo = _telegram_settings(channel)

    def tell(heading: str, message: str, *, via_telegram: bool = True) -> None:
        if not enabled:
            return
        try:
            notifier(f"{TITLE} {heading}", message, banner=banner, telegram=telegram and via_telegram)
        except Exception as exc:  # noqa: BLE001 — 알림 실패가 실행을 깨면 안 된다
            logger.warning("[autopublish] 알림 실패: %s", exc)

    if approved_dir is None:
        reasons = "; ".join(held[:3]) or "후보 없음"
        tell("승인된 후보 없음", f"이번 실행에서 게시할 후보가 없습니다 — {reasons}")
        return "none"

    outcome = publisher(approved_dir, channel)
    if outcome.status == PUBLISHED:
        # 휴대폰에는 표지 사진과 링크를 한 통으로 보낸다(배너·로그는 텍스트만).
        message = f"{title} {outcome.url or ''}".strip()
        tell("게시 완료", message, via_telegram=False)
        if enabled and telegram:
            if photo_sender is None:
                from channel.src import telegram_notify

                photo_sender = telegram_notify.send_photo
            handle = str(channel.get("instagram.channel.handle", "") or "").lstrip("@")
            profile = f"instagram.com/{handle}" if handle else "Instagram"
            caption = f"게시 완료\n{title}\n{outcome.url or profile}"
            try:
                if not (cover_photo and outcome.cover_url and photo_sender(outcome.cover_url, caption)):
                    photo_sender("", caption)  # 사진이 없거나 실패하면 텍스트로
            except Exception as exc:  # noqa: BLE001
                logger.warning("[autopublish] 텔레그램 알림 실패: %s", type(exc).__name__)
        return PUBLISHED
    if outcome.status == SKIPPED:
        tell("승인됨(게시 대기)", f"{title} — {outcome.reason} [{approved_dir.name}]")
        return "approved"
    extra = f" ({', '.join(outcome.details)})" if outcome.details else ""
    tell("게시 실패" if outcome.status == FAILED else "게시 보류",
         f"{title} — {outcome.reason}{extra} [{approved_dir.name}]")
    return outcome.status
