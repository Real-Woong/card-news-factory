"""환경변수 점검 — 값은 절대 출력하지 않고, 있는지·형식이 맞는지만 알려 준다.

운영자가 ``.env`` 에 넣은 상수가 파이프라인이 요구하는 조건을 채우는지 확인합니다. 출력에는 변수
**이름**과 상태(OK / 없음 / 자리표시자 / 형식 오류)만 나오고, 값·길이·일부 글자도 나오지 않습니다.
네트워크 요청은 하지 않습니다(토큰이 실제로 유효한지는 이 도구가 확인하지 않음).

사용::

    python -m src.core.envcheck                 # 실행에 필요한 항목
    python -m src.core.envcheck --publishing    # + Instagram 게시용 항목
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

OK = "OK"
MISSING = "없음"
PLACEHOLDER = "자리표시자"
BAD_FORMAT = "형식 오류"
WARN = "주의"

# 예시 문서에서 그대로 복사된 값으로 보이는 패턴(비밀값을 비교하는 것이 아니라 흔한 자리표시자 모양만 본다).
_PLACEHOLDER = re.compile(
    r"^(your[-_ ]|<.*>$|xxx+|x{4,}|changeme|replace|todo|none$|null$|test$|example)", re.I
)

LLM_PROVIDERS = {"mock", "kiro_cli", "openai", "anthropic"}

# 텔레그램 알림 변수 이름(앞의 것이 우선). 채널의 telegram_notify 가 같은 목록을 쓴다(channel → engine 방향).
TELEGRAM_TOKEN_NAMES = ("RAG_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT")
TELEGRAM_CHAT_NAMES = ("RAG_TELEGRAM_CHAT_ID", "TELEGRAM_ALLOWED_USER_ID")


def first_set(env: Mapping[str, str], names: tuple[str, ...]) -> tuple[str, str]:
    """(변수 이름, 값) — 값이 있는 첫 번째 이름. 없으면 ('', '')."""
    for name in names:
        value = str(env.get(name, "") or "").strip()
        if value:
            return name, value
    return "", ""


@dataclass(frozen=True)
class Finding:
    name: str
    status: str
    note: str = ""

    @property
    def is_error(self) -> bool:
        return self.status in {MISSING, PLACEHOLDER, BAD_FORMAT}


def _raw(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name, "") or "").strip()


def _secret(env: Mapping[str, str], name: str, *, min_len: int = 16, note: str = "") -> Finding:
    """비밀값 하나를 점검한다. 값은 비교만 하고 어디에도 담지 않는다."""
    value = _raw(env, name)
    if not value:
        return Finding(name, MISSING, note)
    if _PLACEHOLDER.match(value):
        return Finding(name, PLACEHOLDER, "예시 값으로 보입니다")
    if re.search(r"\s", value) or len(value) < min_len:
        return Finding(name, BAD_FORMAT, "공백이 있거나 너무 짧습니다")
    return Finding(name, OK)


def _numeric_id(env: Mapping[str, str], name: str) -> Finding:
    value = _raw(env, name)
    if not value:
        return Finding(name, MISSING)
    if _PLACEHOLDER.match(value):
        return Finding(name, PLACEHOLDER, "예시 값으로 보입니다")
    if not value.isdigit():
        return Finding(name, BAD_FORMAT, "숫자만 있어야 합니다")
    return Finding(name, OK)


def _telegram_token(env: Mapping[str, str], name: str) -> Finding:
    """BotFather 토큰은 ``<숫자>:<영문·숫자·_-> `` 모양이다(값은 비교만 하고 담지 않는다)."""
    value = _raw(env, name)
    if not value:
        return Finding(name, MISSING)
    if _PLACEHOLDER.match(value):
        return Finding(name, PLACEHOLDER, "예시 값으로 보입니다")
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", value):
        return Finding(name, BAD_FORMAT, "'숫자:문자열' 모양이어야 합니다")
    return Finding(name, OK)


def _chat_id(env: Mapping[str, str], name: str) -> Finding:
    value = _raw(env, name)
    if not value:
        return Finding(name, MISSING)
    if not re.fullmatch(r"-?\d{5,}", value):
        return Finding(name, BAD_FORMAT, "숫자만 있어야 합니다(그룹은 -로 시작)")
    return Finding(name, OK)


def check(
    env: Mapping[str, str] | None = None, *, publishing: bool = False, telegram: bool = False
) -> list[Finding]:
    """``env``(기본 ``os.environ``)를 점검한 결과 목록."""
    env = os.environ if env is None else env
    findings: list[Finding] = []

    provider = (_raw(env, "LLM_PROVIDER") or "mock").lower()
    if provider not in LLM_PROVIDERS:
        findings.append(Finding("LLM_PROVIDER", BAD_FORMAT, f"허용: {', '.join(sorted(LLM_PROVIDERS))}"))
    elif provider == "mock":
        findings.append(
            Finding("LLM_PROVIDER", PLACEHOLDER, "mock 은 검증용입니다 — 자동 운영에는 쓰지 않습니다")
        )
    else:
        findings.append(Finding("LLM_PROVIDER", OK))

    if provider == "openai":
        findings.append(_secret(env, "OPENAI_API_KEY"))
    elif provider == "anthropic":
        findings.append(_secret(env, "ANTHROPIC_API_KEY"))
    elif provider == "kiro_cli":
        command = _raw(env, "KIRO_CLI_COMMAND")
        first = command.split()[0] if command else ""
        found = (
            bool(first)
            and (shutil.which(os.path.expanduser(first)) or Path(os.path.expanduser(first)).exists())
        ) or bool(shutil.which("kiro-cli") or shutil.which("kiro"))
        findings.append(
            Finding("KIRO_CLI_COMMAND", OK if found else MISSING, "" if found else "kiro-cli 를 찾지 못함")
        )

    if (_raw(env, "EMBEDDING_PROVIDER") or "hashing").lower() == "openai" and provider != "openai":
        findings.append(_secret(env, "OPENAI_API_KEY", note="EMBEDDING_PROVIDER=openai"))

    token = _raw(env, "GITHUB_TOKEN")
    if token:  # 선택 항목: 넣었다면 모양만 확인
        findings.append(_secret(env, "GITHUB_TOKEN", min_len=20))

    if telegram:
        token_name = first_set(env, TELEGRAM_TOKEN_NAMES)[0] or TELEGRAM_TOKEN_NAMES[0]
        chat_name = first_set(env, TELEGRAM_CHAT_NAMES)[0] or TELEGRAM_CHAT_NAMES[0]
        findings.append(_telegram_token(env, token_name))
        findings.append(_chat_id(env, chat_name))
    if publishing:
        findings.append(_secret(env, "INSTAGRAM_ACCESS_TOKEN", min_len=30))
        findings.append(_numeric_id(env, "INSTAGRAM_USER_ID"))
    return findings


def render(findings: list[Finding]) -> str:
    lines = []
    for item in findings:
        mark = "ok  " if not item.is_error and item.status == OK else ("WARN" if item.status == WARN else "FAIL")
        note = f" — {item.note}" if item.note else ""
        lines.append(f"  [{mark}] {item.name}: {item.status}{note}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from src.core.config import load_dotenv_file

    parser = argparse.ArgumentParser(prog="src.core.envcheck", description=__doc__)
    parser.add_argument("--publishing", action="store_true", help="Instagram 게시용 항목도 점검")
    parser.add_argument("--telegram", action="store_true", help="텔레그램 알림용 항목도 점검")
    args = parser.parse_args(argv)

    load_dotenv_file()  # 프로젝트 자신의 로더 — 이 도구는 값을 출력하지 않는다
    findings = check(publishing=args.publishing, telegram=args.telegram)
    print(render(findings))
    errors = [f for f in findings if f.is_error]
    print(f"\n{'문제 없음' if not errors else f'문제 {len(errors)}건'} (값은 출력하지 않습니다)")
    return 1 if errors else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
