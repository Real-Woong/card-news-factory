"""Instagram 액세스 토큰 자동 갱신.

Instagram Login 으로 만든 장기 토큰은 약 60일 뒤 만료되며, 만료 전에(발급 24시간 뒤부터) 갱신하면 다시 60일이
된다. 이 모듈은 무인 실행 중에 토큰을 주기적으로 갱신해 만료로 게시가 멈추는 일을 막는다.

**비밀 취급 원칙**

* ``.env`` 를 읽거나 고치지 않는다. 갱신된 토큰은 저장소 밖의 전용 파일
  (``~/.r_a_glance/instagram_token.json``, 권한 600)에 둔다.
* 이 파일에는 갱신 토큰과 만료 추정 시각, 그리고 *기준이 된 .env 토큰의 해시*(지문)만 있다. 운영자가
  ``.env`` 의 토큰을 새 값으로 바꾸면 지문이 달라져서 저장본은 무시되고 ``.env`` 가 우선한다.
* 토큰 값은 로그·알림·예외 메시지 어디에도 내보내지 않는다.

흐름: ``maintain()`` 이 무인 실행 시작 시 호출돼 (필요하면) 갱신하고, 게시 코드는 ``effective_environ()`` 으로
저장본이 반영된 환경을 쓴다.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from src.core.logging_setup import get_logger

from channel.src.config import ChannelConfig
from channel.src.notify import notify as default_notify

logger = get_logger(__name__)

STORE_PATH = Path.home() / ".r_a_glance" / "instagram_token.json"
TOKEN_ENV = "INSTAGRAM_ACCESS_TOKEN"
REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
LIFETIME_DAYS = 60  # 장기 토큰 수명(문서 기준). 갱신 응답의 expires_in 이 있으면 그것을 쓴다.


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fingerprint(token: str) -> str:
    """토큰의 SHA-256 앞 16자(비교 전용; 토큰을 복원할 수 없다)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _parse(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_store(path: Path | None = None) -> dict[str, Any] | None:
    target = path or STORE_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_store(data: dict[str, Any], path: Path | None = None) -> None:
    target = path or STORE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(target)


def effective_environ(
    environ: Mapping[str, str] | None = None, path: Path | None = None
) -> dict[str, str]:
    """``environ`` 에 (유효한) 갱신 토큰을 반영한 사본. 기준 .env 토큰이 바뀌었으면 .env 가 우선."""
    env = dict(os.environ if environ is None else environ)
    base = env.get(TOKEN_ENV, "").strip()
    stored = read_store(path)
    if not (base and stored and stored.get("access_token")):
        return env
    if stored.get("base_fingerprint") != fingerprint(base):
        return env  # 운영자가 .env 토큰을 교체했다 → 저장본은 낡은 것
    expires = _parse(stored.get("expires_at"))
    if expires is not None and expires <= _now():
        return env
    env[TOKEN_ENV] = str(stored["access_token"])
    return env


@dataclass
class RefreshOutcome:
    status: str  # refreshed | skipped | failed
    message: str = ""
    days_left: int | None = None


def _settings(channel: ChannelConfig) -> dict[str, Any]:
    return channel.get("instagram.publishing.token_refresh", {}) or {}


def refresh_token(
    channel: ChannelConfig,
    *,
    environ: Mapping[str, str] | None = None,
    path: Path | None = None,
    http: Any = None,
    force: bool = False,
    now: datetime | None = None,
) -> RefreshOutcome:
    """필요하면 토큰을 갱신하고 저장본을 쓴다. 토큰 값은 어디에도 내보내지 않는다."""
    settings = _settings(channel)
    now = now or _now()
    env = dict(os.environ if environ is None else environ)
    base = env.get(TOKEN_ENV, "").strip()
    if not bool(settings.get("enabled", True)):
        return RefreshOutcome("skipped", "토큰 자동 갱신이 꺼져 있습니다")
    if not base:
        return RefreshOutcome("skipped", f"{TOKEN_ENV} 가 설정되지 않았습니다")

    stored = read_store(path)
    if not stored or stored.get("base_fingerprint") != fingerprint(base):
        stored = None  # 처음이거나 운영자가 토큰을 교체했다
    lifetime = int(settings.get("assumed_lifetime_days", LIFETIME_DAYS))
    first_seen = _parse((stored or {}).get("base_first_seen")) or now
    current = effective_environ(env, path)[TOKEN_ENV]
    expires = _parse((stored or {}).get("expires_at")) or first_seen + timedelta(days=lifetime)
    refreshed = _parse((stored or {}).get("refreshed_at"))
    every = int(settings.get("refresh_every_days", 7))
    due = force or refreshed is None or (now - refreshed) >= timedelta(days=every)
    days_left = (expires - now).days
    if not due:
        return RefreshOutcome("skipped", "최근에 갱신했습니다", days_left)

    session = http or requests
    try:
        response = session.get(
            REFRESH_URL,
            params={"grant_type": "ig_refresh_token", "access_token": current},
            timeout=float(channel.get("instagram.publishing.timeout_seconds", 30)),
        )
        payload = response.json()
    except (requests.RequestException, ValueError):
        return RefreshOutcome("failed", "갱신 요청 실패(네트워크 오류)", days_left)
    if response.status_code >= 400 or not isinstance(payload, dict) or payload.get("error"):
        error = payload.get("error") if isinstance(payload, dict) else None
        detail = (
            f"{error.get('type')}/{error.get('code')} {error.get('message')}"
            if isinstance(error, dict)
            else f"HTTP {response.status_code}"
        )
        for secret in (current, base):
            detail = detail.replace(secret, "[REDACTED]")
        return RefreshOutcome("failed", f"갱신 거절: {detail}", days_left)
    new_token = str(payload.get("access_token") or "")
    if not new_token:
        return RefreshOutcome("failed", "갱신 응답에 토큰이 없습니다", days_left)
    seconds = int(payload.get("expires_in") or lifetime * 86400)
    expires_at = now + timedelta(seconds=seconds)
    write_store(
        {
            "access_token": new_token,
            "base_fingerprint": fingerprint(base),
            "base_first_seen": first_seen.isoformat(),
            "refreshed_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        },
        path,
    )
    return RefreshOutcome("refreshed", "토큰을 갱신했습니다", (expires_at - now).days)


def maintain(
    channel: ChannelConfig,
    *,
    notifier: Callable[..., Any] = default_notify,
    **kwargs: Any,
) -> RefreshOutcome:
    """무인 실행 시작 시 호출: 갱신하고, 만료가 가까운데 갱신이 안 되면 알린다. 예외를 던지지 않는다."""
    try:
        outcome = refresh_token(channel, **kwargs)
    except Exception as exc:  # noqa: BLE001 — 갱신 실패가 실행 전체를 막으면 안 된다
        outcome = RefreshOutcome("failed", f"갱신 중 오류: {type(exc).__name__}")
    warn_days = int(_settings(channel).get("warn_days", 14))
    soon = outcome.days_left is not None and outcome.days_left <= warn_days
    if outcome.status == "failed" or (outcome.status != "refreshed" and soon):
        left = f" (만료까지 약 {outcome.days_left}일)" if outcome.days_left is not None else ""
        try:
            notifier(
                "R_A_Glance 토큰 갱신 필요" if soon or outcome.status == "failed" else "R_A_Glance 토큰",
                f"{outcome.message}{left} — 만료되면 게시가 멈춥니다. Meta 대시보드에서 토큰을 새로 만들어 "
                f".env 에 넣어 주세요",
                banner=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[token] 알림 실패: %s", exc)
    logger.info("[token] %s%s", outcome.status,
                f" (남은 약 {outcome.days_left}일)" if outcome.days_left is not None else "")
    return outcome
