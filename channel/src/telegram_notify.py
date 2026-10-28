"""텔레그램 알림 — R_A_Glance 전용 봇이 사람 대신 올린 결과를 휴대폰으로 알린다.

맥미니에서 무인으로 도는 시스템이라 화면 배너(맥미니 앞에 있어야 보임)만으로는 부족하다. 게시 완료·보류·실패·
토큰 문제 같은 알림을 텔레그램 봇으로 보낸다. **봇은 이 채널 전용**이다(다른 봇과 섞지 않는다).

설정(둘 다 있어야 켜진다; 값은 ``.env`` 에 운영자가 직접 넣는다 — 에이전트는 읽지 않는다)::

    RAG_TELEGRAM_BOT_TOKEN=   # BotFather 가 준 토큰   (짧은 이름 TELEGRAM_BOT 도 인식)
    RAG_TELEGRAM_CHAT_ID=     # 알림을 받을 채팅 ID(숫자) (TELEGRAM_ALLOWED_USER_ID 도 인식)

원칙: 알림 실패가 실행을 깨면 안 된다(예외 없이 False 반환), 토큰은 로그·메시지 어디에도 내보내지 않는다.

도우미(터미널에서 직접)::

    python -m channel.src.telegram_notify --discover   # 봇에게 말을 건 뒤 채팅 ID 찾기
    python -m channel.src.telegram_notify --test       # 시험 메시지 보내기
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from typing import Any

import requests

from src.core.envcheck import TELEGRAM_CHAT_NAMES, TELEGRAM_TOKEN_NAMES, first_set
from src.core.logging_setup import get_logger

logger = get_logger(__name__)

API = "https://api.telegram.org"
TOKEN_ENV = "RAG_TELEGRAM_BOT_TOKEN"
CHAT_ENV = "RAG_TELEGRAM_CHAT_ID"
# 운영자가 이미 쓰는 짧은 이름도 받는다(앞의 이름이 우선). 개인 채팅에서는 사용자 ID 가 곧 채팅 ID 라서
# TELEGRAM_ALLOWED_USER_ID 를 채팅 ID 로 쓸 수 있다(단, 그 사용자가 봇에게 먼저 /start 를 눌러야 한다).
TOKEN_ENVS = TELEGRAM_TOKEN_NAMES
CHAT_ENVS = TELEGRAM_CHAT_NAMES
TEXT_LIMIT = 4000  # sendMessage 상한 4096 에서 여유를 둔다
CAPTION_LIMIT = 1000  # sendPhoto 캡션 상한 1024


def _credentials(environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    env = os.environ if environ is None else environ
    return first_set(env, TOKEN_ENVS)[1], first_set(env, CHAT_ENVS)[1]


def configured(environ: Mapping[str, str] | None = None) -> bool:
    token, chat = _credentials(environ)
    return bool(token and chat)


def _redact(text: str, token: str) -> str:
    return text.replace(token, "[REDACTED]") if token else text


def _post(method: str, data: dict[str, Any], *, environ: Mapping[str, str] | None, http: Any) -> bool:
    token, chat = _credentials(environ)
    if not (token and chat):
        return False
    session = http or requests
    try:
        response = session.post(
            f"{API}/bot{token}/{method}", data={"chat_id": chat, **data}, timeout=15
        )
        ok = response.status_code == 200 and bool((response.json() or {}).get("ok"))
    except (requests.RequestException, ValueError) as exc:
        logger.warning("[telegram] %s 실패: %s", method, _redact(str(exc), token)[:200])
        return False
    if not ok:
        logger.warning("[telegram] %s 거절됨(HTTP %s)", method, getattr(response, "status_code", "?"))
    return ok


def send_text(
    text: str, *, environ: Mapping[str, str] | None = None, http: Any = None
) -> bool:
    """텍스트 알림. 설정이 없거나 실패하면 False(예외 없음). 서식(parse_mode)은 쓰지 않는다."""
    if not text.strip():
        return False
    return _post("sendMessage", {"text": text[:TEXT_LIMIT], "disable_web_page_preview": "true"},
                 environ=environ, http=http)


def send_photo(
    photo_url: str,
    caption: str,
    *,
    environ: Mapping[str, str] | None = None,
    http: Any = None,
) -> bool:
    """사진(공개 URL)과 캡션. 사진 전송이 실패하면 텍스트로 대신 보낸다."""
    if not configured(environ):
        return False
    if photo_url.startswith("https://") and _post(
        "sendPhoto", {"photo": photo_url, "caption": caption[:CAPTION_LIMIT]},
        environ=environ, http=http,
    ):
        return True
    return send_text(caption, environ=environ, http=http)


def discover_chats(*, environ: Mapping[str, str] | None = None, http: Any = None) -> list[str]:
    """봇에게 최근에 말을 건 채팅(ID, 이름)을 찾는다. 토큰은 환경에서만 읽고 출력하지 않는다."""
    token, _ = _credentials(environ)
    if not token:
        return []
    session = http or requests
    try:
        payload = session.get(f"{API}/bot{token}/getUpdates", timeout=15).json()
    except (requests.RequestException, ValueError):
        return []
    found: dict[str, str] = {}
    for update in payload.get("result", []) if isinstance(payload, dict) else []:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if "id" in chat:
            name = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
            found[str(chat["id"])] = f"{name} ({chat.get('type', '?')})"
    return [f"{chat_id}  {label}" for chat_id, label in found.items()]


def main(argv: list[str] | None = None) -> int:
    from src.core.config import load_dotenv_file

    parser = argparse.ArgumentParser(prog="channel.src.telegram_notify", description=__doc__)
    parser.add_argument("--discover", action="store_true", help="봇에게 말을 건 채팅 ID 찾기")
    parser.add_argument("--test", action="store_true", help="시험 메시지 보내기")
    args = parser.parse_args(argv)

    load_dotenv_file()
    token, chat = _credentials()
    if args.discover:
        if not token:
            print(f"{TOKEN_ENV}(또는 TELEGRAM_BOT) 가 설정되지 않았습니다")
            return 1
        chats = discover_chats()
        if not chats:
            print("찾은 채팅이 없습니다 — 텔레그램에서 봇에게 아무 메시지나 보낸 뒤 다시 실행하세요")
            return 1
        print("채팅 ID  이름 (이 중 알림을 받을 ID 를 .env 의 RAG_TELEGRAM_CHAT_ID 에 넣으세요):")
        for line in chats:
            print(f"  {line}")
        return 0
    if args.test:
        if not (token and chat):
            print(f"토큰({TOKEN_ENV} 또는 TELEGRAM_BOT)과 채팅 ID({CHAT_ENV} 또는 TELEGRAM_ALLOWED_USER_ID)가 모두 필요합니다")
            return 1
        ok = send_text("R_A_Glance 알림 시험 — 이 메시지가 보이면 설정이 끝난 것입니다.")
        print("보냄" if ok else "실패 — 토큰·채팅 ID 를 확인하고, 봇에게 먼저 말을 건 적이 있는지 확인하세요")
        return 0 if ok else 1
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
