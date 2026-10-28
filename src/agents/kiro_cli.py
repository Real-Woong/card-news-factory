"""Kiro CLI provider.

`kiro-cli chat --no-interactive` 를 subprocess 로 실행하고, 터미널 UI 출력에서
JSON payload 만 안정적으로 뽑아냅니다.

설계 원칙: **조용한 mock 폴백은 없습니다.** `LLM_PROVIDER=kiro_cli` 로 지정했는데
CLI 가 없거나 실패하면 :class:`LLMError` 를 던집니다. 자동 실행 중 Kiro 가 죽었을 때
가짜 문장이 게시 후보로 만들어지는 상황을 막는 것이 이 모듈의 핵심 목적입니다.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.core.errors import LLMError
from src.core.logging_setup import get_logger

logger = get_logger(__name__)

PROVIDER = "kiro_cli"
DEFAULT_ARGS = ("chat", "--no-interactive", "--trust-tools=")
FALLBACK_BINARY = Path.home() / ".local" / "bin" / "kiro-cli"
# Kiro 는 실행한 폴더의 설정(.kiro/steering·agents·hooks)을 읽을 수 있다. 프롬프트는 stdin 으로 전부 전달하므로
# 프로젝트 폴더가 아니라 빈 전용 폴더에서 실행한다: 프로젝트 문서가 매 호출의 맥락(토큰)으로 섞이거나, Kiro 3.0 의
# "이 작업 공간을 신뢰하시겠습니까?" 확인이 무인 실행을 막는 일을 피한다.
NEUTRAL_CWD = Path.home() / ".r_a_glance" / "kiro_cwd"


def neutral_cwd() -> Path:
    """Kiro CLI 를 실행할 빈 폴더(없으면 만든다). 홈에 만들 수 없으면 임시 폴더로 대신한다."""
    try:
        NEUTRAL_CWD.mkdir(parents=True, exist_ok=True)
        return NEUTRAL_CWD
    except OSError:
        import tempfile

        fallback = Path(tempfile.gettempdir()) / "r_a_glance_kiro_cwd"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
BINARY_CANDIDATES = ("kiro-cli", "kiro")

# ANSI CSI/OSC 시퀀스 (색상, 커서 제어, 화면 지우기 등)
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
# 진행 표시/스피너/박스 드로잉 문자
UI_CHARS = re.compile(r"[\u2500-\u257f\u2580-\u259f\u25a0-\u25ff\u2800-\u28ff⠁-⣿]")
# "▸ Credits: 0.08 • Time: 2s" 푸터
FOOTER_PATTERN = re.compile(
    r"^\s*[▸▪►>]?\s*Credits:\s*(?P<credits>[\d.]+)\s*[•·|]\s*Time:\s*(?P<time>[\d.]+)\s*(?P<unit>ms|s|m)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# 프롬프트 프리픽스("> ")와 UI 잔여 라인
PROMPT_PREFIX = re.compile(r"^\s*>\s?", re.MULTILINE)


@dataclass
class KiroUsage:
    """CLI 호출 누적 사용량 (run_metadata.json 에 기록)."""

    calls: int = 0
    failures: int = 0
    credits: float = 0.0
    seconds: float = 0.0
    per_task: dict[str, int] = field(default_factory=dict)

    def record(self, *, task: str, credits: float, seconds: float, ok: bool = True) -> None:
        self.calls += 1
        if not ok:
            self.failures += 1
        self.credits += credits
        self.seconds += seconds
        self.per_task[task] = self.per_task.get(task, 0) + 1

    def to_dict(self) -> dict[str, object]:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "credits": round(self.credits, 4),
            "seconds": round(self.seconds, 2),
            "calls_per_task": dict(self.per_task),
        }


def resolve_command(command: str | None = None) -> list[str]:
    """실행할 argv 를 결정합니다.

    우선순위:

    1. 인자로 받은 ``command`` 또는 환경변수 ``KIRO_CLI_COMMAND``
    2. ``shutil.which("kiro-cli")`` → ``shutil.which("kiro")``
    3. ``~/.local/bin/kiro-cli``

    Raises:
        LLMError: 어느 경로에서도 실행 가능한 바이너리를 찾지 못한 경우.
    """
    configured = (command or os.environ.get("KIRO_CLI_COMMAND") or "").strip()
    if configured:
        argv = shlex.split(configured)
        binary = argv[0]
        resolved = shutil.which(binary) or (binary if Path(binary).expanduser().is_file() else None)
        if resolved is None:
            raise LLMError(
                f"KIRO_CLI_COMMAND 의 바이너리를 찾을 수 없습니다: {binary!r} "
                f"(command={configured!r})",
                provider=PROVIDER,
            )
        return [str(Path(resolved).expanduser()), *argv[1:]]

    for name in BINARY_CANDIDATES:
        found = shutil.which(name)
        if found:
            return [found, *DEFAULT_ARGS]

    if FALLBACK_BINARY.is_file() and os.access(FALLBACK_BINARY, os.X_OK):
        return [str(FALLBACK_BINARY), *DEFAULT_ARGS]

    raise LLMError(
        "Kiro CLI 를 찾을 수 없습니다. "
        f"PATH 에서 {list(BINARY_CANDIDATES)} 를 찾지 못했고 {FALLBACK_BINARY} 도 없습니다. "
        "KIRO_CLI_COMMAND 로 직접 지정하세요.",
        provider=PROVIDER,
    )


def strip_ansi(text: str) -> str:
    """ANSI 이스케이프 시퀀스 제거."""
    return ANSI_PATTERN.sub("", text or "")


def parse_footer(text: str) -> tuple[float, float]:
    """``▸ Credits: 0.08 • Time: 2s`` 에서 (credits, seconds) 추출. 없으면 (0, 0)."""
    match = None
    for match in FOOTER_PATTERN.finditer(text):  # 마지막 푸터를 사용
        pass
    if match is None:
        return 0.0, 0.0
    credits = float(match.group("credits"))
    value = float(match.group("time"))
    unit = match.group("unit").lower()
    seconds = value / 1000 if unit == "ms" else value * 60 if unit == "m" else value
    return credits, seconds


def clean_output(raw: str) -> str:
    """CLI stdout 에서 UI 요소를 제거하고 모델 응답 본문만 남깁니다.

    순서: ANSI strip → 푸터 제거 → 프롬프트 프리픽스/스피너 문자 제거 → 공백 정리.
    """
    text = strip_ansi(raw)
    text = FOOTER_PATTERN.sub("", text)
    text = PROMPT_PREFIX.sub("", text)
    text = UI_CHARS.sub("", text)
    text = text.replace("\r", "\n").replace("\x07", "")
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line.strip()).strip()


def _balanced_slice(text: str, open_ch: str, close_ch: str) -> str | None:
    """문자열 리터럴을 고려해 괄호 균형이 맞는 첫 블록을 반환."""
    start = text.find(open_ch)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == open_ch:
            depth += 1
        elif char == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def extract_json_payload(raw: str) -> dict | list:
    """정리된 CLI 출력에서 JSON 객체/배열을 찾아 파싱합니다.

    Raises:
        LLMError: JSON 을 찾지 못했거나 파싱에 실패한 경우.
    """
    cleaned = clean_output(raw)
    if not cleaned:
        raise LLMError("Kiro CLI 출력이 비어 있습니다", provider=PROVIDER)

    candidates: list[str] = []

    # 1) ```json fence
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL):
        candidates.append(match.group(1).strip())
    # 2) 전체 문자열
    candidates.append(cleaned)
    # 3) 괄호 균형이 맞는 첫 블록
    for pair in (("{", "}"), ("[", "]")):
        block = _balanced_slice(cleaned, *pair)
        if block:
            candidates.append(block)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, (dict, list)):
            return parsed

    preview = cleaned[:300].replace("\n", "\\n")
    raise LLMError(
        f"Kiro CLI 출력에서 JSON 을 찾지 못했습니다. 정리된 출력 앞부분: {preview!r}",
        provider=PROVIDER,
    )


def parse_stream(stdout: str) -> tuple[str | None, float]:
    """``--output-format stream-json`` 출력(JSON Lines)에서 (모델 답변 본문, 크레딧)을 뽑는다.

    실측한 형식(Kiro CLI 2.35.1): ``sessionUpdate`` 이벤트의 ``data.update`` 에서
    ``sessionUpdate == "agent_message_chunk"`` 인 ``content.text`` 조각을 이어 붙이면 답변이고, 호출 크레딧은
    ``_meta.kiro.promptTurnSummaries[].usage`` 의 합이다. JSON 줄이 하나도 없으면 ``(None, 0)`` — 호출하는 쪽이
    일반 텍스트 출력으로 취급한다.
    """
    parts: list[str] = []
    credits = 0.0
    seen_json = False
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        seen_json = True
        update = ((event.get("data") or {}).get("update") or {}) if isinstance(event, dict) else {}
        if not isinstance(update, dict):
            continue
        content = update.get("content")
        if (
            update.get("sessionUpdate") == "agent_message_chunk"
            and isinstance(content, dict)
            and content.get("type") == "text"
            and isinstance(content.get("text"), str)
        ):
            parts.append(content["text"])
        summaries = ((update.get("_meta") or {}).get("kiro") or {}).get("promptTurnSummaries")
        if isinstance(summaries, list):
            credits = sum(
                float(item.get("usage") or 0) for item in summaries if isinstance(item, dict)
            )
    if not seen_json:
        return None, 0.0
    return ("".join(parts) if parts else None), credits


def run_cli(
    prompt: str,
    *,
    argv: list[str],
    timeout: float,
    cwd: Path | None = None,
    metering: bool = True,
) -> tuple[str, float, float]:
    """CLI 를 실행하고 (stdout, credits, seconds) 를 반환합니다.

    Raises:
        LLMError: 실행 실패, 타임아웃, 0 이 아닌 종료 코드.
    """
    # 호출별 크레딧을 알려면 stream-json 으로 받아야 한다(일반 텍스트 모드는 크레딧을 출력하지 않는다, 실측).
    # 이미 --output-format 이 있거나 metering=False 면 그대로 둔다. 본문을 못 찾으면 원래 출력으로 되돌아간다.
    streaming = metering and not any(arg.startswith("--output-format") for arg in argv)
    command = [*argv, "--output-format", "stream-json"] if streaming else argv
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 - argv 는 resolve_command 로 검증된 값
            command,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            check=False,
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
        )
    except FileNotFoundError as exc:
        raise LLMError(
            f"Kiro CLI 실행 실패(바이너리 없음): argv={argv}", provider=PROVIDER
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise LLMError(
            f"Kiro CLI 타임아웃 {timeout:.0f}s: argv={argv}", provider=PROVIDER
        ) from exc

    elapsed = time.monotonic() - started
    if completed.returncode != 0:
        stderr = strip_ansi(completed.stderr or "").strip()[:500]
        stdout = strip_ansi(completed.stdout or "").strip()[:300]
        raise LLMError(
            "Kiro CLI 가 0 이 아닌 코드로 종료했습니다\n"
            f"  command   : {' '.join(argv)}\n"
            f"  returncode: {completed.returncode}\n"
            f"  stderr    : {stderr or '(없음)'}\n"
            f"  stdout    : {stdout or '(없음)'}",
            provider=PROVIDER,
        )

    # credits/time 푸터는 TERM=dumb 환경에서 stderr 로 나옵니다(실측). 두 스트림을
    # 모두 살펴봐야 사용량이 0 으로 집계되지 않습니다.
    combined = strip_ansi((completed.stdout or "") + "\n" + (completed.stderr or ""))
    credits, reported = parse_footer(combined)
    if completed.stderr and completed.stderr.strip():
        logger.debug("[kiro] stderr: %s", strip_ansi(completed.stderr).strip()[:300])
    text = completed.stdout or ""
    if streaming:
        answer, stream_credits = parse_stream(text)
        if answer is not None:
            text, credits = answer, stream_credits or credits
        else:
            logger.warning("[kiro] stream-json 에서 답변 본문을 찾지 못해 원본 출력을 사용합니다")
    return text, credits, reported or elapsed


# --------------------------------------------------------------------------- #
# 버전 확인과 호출 시험(canary) — Kiro CLI 가 업그레이드돼도 파이프라인이 조용히 깨지지 않게
# --------------------------------------------------------------------------- #
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
CANARY_PROMPT = 'Reply with exactly this JSON object and nothing else: {"ok": true, "n": 42}'


def kiro_version(binary: str, *, timeout: float = 20.0) -> str:
    """``kiro-cli --version`` 의 버전 문자열(예: ``2.35.1``). 알 수 없으면 빈 문자열."""
    try:
        done = subprocess.run(  # noqa: S603 - binary 는 resolve_command 로 검증된 값
            [binary, "--version"], capture_output=True, text=True, timeout=timeout,
            check=False, env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    match = _VERSION.search(strip_ansi((done.stdout or "") + (done.stderr or "")))
    return ".".join(match.groups()) if match else ""


@dataclass
class CanaryResult:
    ok: bool
    detail: str
    credits_seen: bool = False  # 사용량 푸터를 읽었는가(False 면 credits=0 으로만 집계된다)
    footer_lines: list[str] = field(default_factory=list)


def canary(argv: list[str], *, timeout: float = 180.0, metering: bool = True) -> CanaryResult:
    """아주 작은 프롬프트로 호출 → JSON 파싱까지 확인한다(크레딧 약 0.05~0.1).

    파이프라인이 실제로 쓰는 경로(``run_cli``: stream-json, 출력 정리, JSON 추출)를 그대로 지나가므로, CLI 출력
    형식이 바뀌어 파싱이 깨졌는지를 카드 한 장을 만들어 보기 전에 알 수 있다.
    """
    try:
        raw, credits, _ = run_cli(
            CANARY_PROMPT, argv=argv, timeout=timeout, cwd=neutral_cwd(), metering=metering
        )
    except (LLMError, OSError) as exc:
        return CanaryResult(False, "호출 실패: " + " ".join(str(exc).split())[:240])
    try:
        payload = extract_json_payload(raw)
    except LLMError as exc:
        return CanaryResult(False, f"JSON 파싱 실패: {str(exc)[:160]}", credits > 0)
    if not (isinstance(payload, dict) and payload.get("ok") is True and payload.get("n") == 42):
        return CanaryResult(False, "응답 내용이 지시와 다릅니다", credits > 0)
    return CanaryResult(True, "정상", credits > 0)


_USAGE_KEY = re.compile(r"credit|usage|token|cost|metering", re.I)


def summarize_events(lines: list[str], *, limit: int = 40) -> list[str]:
    """``--output-format stream-json`` 의 JSON Lines 에서 구조와 사용량 관련 숫자만 요약한다.

    모델의 답변 본문은 출력하지 않는다(키 이름과, 키에 credit/usage/token/cost 가 들어간 숫자 값만).
    """
    out: list[str] = []

    def walk(node: object, path: str, found: list[str]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}" if path else str(key)
                if isinstance(value, (dict, list)):
                    walk(value, here, found)
                elif _USAGE_KEY.search(str(key)) and isinstance(value, (int, float)):
                    found.append(f"{here}={value}")
        elif isinstance(node, list):
            for item in node[:5]:
                walk(item, f"{path}[]", found)

    for number, line in enumerate(lines[:limit], start=1):
        try:
            event = json.loads(line)
        except ValueError:
            out.append(f"{number}. (JSON 아님) {line[:60]!r}")
            continue
        keys = ",".join(list(event)[:6]) if isinstance(event, dict) else type(event).__name__
        kind = event.get("type") or event.get("event") or event.get("method") if isinstance(event, dict) else ""
        found: list[str] = []
        walk(event, "", found)
        extra = ""
        update = ((event.get("data") or {}).get("update") or {}) if isinstance(event, dict) else {}
        if isinstance(update, dict) and update.get("sessionUpdate"):
            extra = f" update={update['sessionUpdate']}"
            content = update.get("content")
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                extra += f" content.type={content.get('type')} text_len={len(content['text'])}"  # 본문은 출력하지 않는다
        out.append(
            f"{number}. type={kind or '?'}{extra} keys=[{keys}]"
            + (f" 사용량: {', '.join(found[:6])}" if found else "")
        )
    return out


def probe_usage(argv: list[str], *, timeout: float = 180.0) -> list[str]:
    """작은 호출을 stream-json 으로 실행해 크레딧·토큰 사용량이 어느 이벤트에 있는지 보여 준다."""
    command = argv if "--output-format" in argv else [*argv, "--output-format", "stream-json"]
    try:
        done = subprocess.run(  # noqa: S603
            command, input=CANARY_PROMPT, capture_output=True, text=True, timeout=timeout,
            check=False, cwd=str(neutral_cwd()), env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"실행 실패: {type(exc).__name__}"]
    lines = [ln for ln in strip_ansi(done.stdout or "").splitlines() if ln.strip()]
    if done.returncode != 0:
        return [f"종료 코드 {done.returncode}: {strip_ansi(done.stderr or '').strip()[:200]}"]
    return summarize_events(lines) or ["출력이 비어 있습니다"]


def main(argv: list[str] | None = None) -> int:
    """``python -m src.agents.kiro_cli`` — 버전, 호출·파싱, 크레딧 푸터 인식 여부를 점검한다.

    ``--probe-usage``: 크레딧·토큰 사용량이 stream-json 이벤트의 어디에 있는지 구조만 보여 준다.
    """
    import sys

    from src.core.config import load_dotenv_file

    load_dotenv_file()
    command = resolve_command()
    if "--probe-usage" in (argv if argv is not None else sys.argv[1:]):
        print(f"kiro-cli 버전: {kiro_version(command[0]) or '알 수 없음'}")
        for line in probe_usage(command):
            print(line)
        return 0
    version = kiro_version(command[0])
    print(f"kiro-cli 버전: {version or '알 수 없음'}")
    print(f"실행 명령    : {' '.join(command)}")
    result = canary(command)
    print(f"호출·파싱    : {'정상' if result.ok else '실패 — ' + result.detail}")
    print(f"크레딧 집계  : {'가능(호출별 크레딧을 읽음)' if result.credits_seen else '못 읽음(사용량이 0 으로 집계됨)'}")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
