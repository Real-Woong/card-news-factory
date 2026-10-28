"""LLM provider 추상화.

하나의 인터페이스, 네 개 provider:

* ``mock``      - 오프라인 결정론적 응답. **명시적으로 지정했을 때만** 사용됩니다.
* ``kiro_cli``  - Kiro CLI 를 subprocess 로 호출 (API 키 불필요)
* ``openai``    - Chat Completions
* ``anthropic`` - Messages API

**조용한 폴백은 없습니다.** 지정한 provider 를 만들 수 없으면 :class:`LLMError` 를
던집니다. 자동 실행 중 provider 가 죽었는데 mock 문장이 게시 후보로 만들어지는
상황을 막는 것이 이 정책의 목적입니다.
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from src.agents.schemas import validate_payload
from src.core.errors import LLMError
from src.core.logging_setup import get_logger

logger = get_logger(__name__)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

MOCK_PROVIDERS = frozenset({"mock"})
KNOWN_PROVIDERS = frozenset({"mock", "kiro_cli", "openai", "anthropic"})


@dataclass
class LLMRequest:
    """한 번의 completion 요청.

    ``payload`` 는 구조화된 입력입니다. 실제 provider 는 이를 user 메시지로
    직렬화하고, :class:`MockLLM` 은 이를 이용해 결정론적 응답을 합성합니다.
    """

    task: str
    system: str
    user: str
    payload: dict[str, Any] = field(default_factory=dict)
    temperature: float = 0.3
    max_tokens: int = 2000
    json_mode: bool = True


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str = ""
    raw: Any = None


def extract_json(text: str) -> dict[str, Any]:
    """모델 응답에서 첫 JSON 객체를 추출합니다.

    bare JSON, ```json fence, 앞뒤 산문을 모두 처리합니다.

    Raises:
        LLMError: 파싱 가능한 JSON 이 없는 경우.
    """
    if not text or not text.strip():
        raise LLMError("Empty LLM response")

    candidates: list[str] = []
    fenced = _JSON_FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate.strip())
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"items": parsed}
    raise LLMError(f"No JSON object in LLM response: {text[:200]!r}")


class LLMClient(ABC):
    """Provider 인터페이스."""

    provider: str = "base"
    model: str = ""

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:
        """한 번의 completion 실행.

        Raises:
            LLMError: provider/네트워크/인증 실패.
        """

    def parse_json(self, text: str) -> dict[str, Any] | list[Any]:
        """응답 텍스트 → JSON. provider 별 출력 정리는 여기서 override 합니다."""
        return extract_json(text)

    def complete_json(self, request: LLMRequest, *, retries: int = 2) -> dict[str, Any]:
        """completion 실행 후 JSON 파싱 + schema 검증.

        파싱/검증 실패 시 같은 요청을 ``retries`` 번까지 재시도하고, 그래도 실패하면
        :class:`LLMError` 를 던집니다. **mock 으로 폴백하지 않습니다** — 호출한
        에이전트가 해당 item 을 실패 처리해야 합니다.
        """
        last_error: Exception | None = None
        attempts = max(retries, 0) + 1
        current = request

        for attempt in range(1, attempts + 1):
            response = self.complete(current)
            try:
                parsed = self.parse_json(response.text)
                return validate_payload(current.task, parsed, provider=self.provider)
            except LLMError as exc:
                last_error = exc
                logger.warning(
                    "[llm] %s task=%s 응답 파싱/검증 실패 (%d/%d): %s",
                    self.provider,
                    current.task,
                    attempt,
                    attempts,
                    exc,
                )
                if attempt < attempts:
                    current = LLMRequest(
                        **{
                            **current.__dict__,
                            "user": current.user
                            + "\n\nIMPORTANT: respond with a single valid JSON object only, "
                            "no prose before or after it.",
                            "temperature": 0.0,
                        }
                    )

        raise LLMError(
            f"{self.provider} 에서 task={request.task} 의 유효한 JSON 을 얻지 못했습니다: "
            f"{last_error}",
            provider=self.provider,
        )

    def usage_summary(self) -> dict[str, Any]:
        """run_metadata.json 에 기록할 사용량 (provider 별로 override)."""
        return {}


class MockLLM(LLMClient):
    """오프라인 결정론적 provider.

    payload 를 규칙으로 변환해 구조적으로 유효한 응답을 만듭니다. ``--dry-run`` 에서
    네트워크 없이 파이프라인 전체(파싱, 길이 제한, 검수 로직)를 검증하는 용도입니다.
    **명시적으로 ``LLM_PROVIDER=mock`` 일 때만** 선택됩니다.
    """

    provider = "mock"
    model = "mock-deterministic"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        from src.agents.mock import build_mock_response

        self.calls += 1
        payload = build_mock_response(request)
        return LLMResponse(
            text=json.dumps(payload, ensure_ascii=False),
            provider=self.provider,
            model=self.model,
            raw=payload,
        )

    def usage_summary(self) -> dict[str, Any]:
        return {"calls": self.calls, "credits": 0.0}


class KiroCliLLM(LLMClient):
    """Kiro CLI provider.

    바이너리 resolve 순서: ``KIRO_CLI_COMMAND`` → ``which kiro-cli`` / ``which kiro``
    → ``~/.local/bin/kiro-cli``. 찾지 못하면 생성 시점에 :class:`LLMError`.
    """

    provider = "kiro_cli"

    def __init__(self, command: str | None = None, *, timeout: float | None = None) -> None:
        from src.agents.kiro_cli import KiroUsage, resolve_command

        self.argv = resolve_command(command)
        self.model = " ".join(self.argv)
        self.timeout = timeout or float(os.environ.get("KIRO_CLI_TIMEOUT", "300"))
        self.usage = KiroUsage()
        # 호출별 크레딧을 읽기 위해 stream-json 으로 받는다. 문제가 생기면 KIRO_STREAM_JSON=0 으로 끈다.
        self.metering = os.environ.get("KIRO_STREAM_JSON", "1").strip() != "0"
        logger.info("[llm] kiro_cli command: %s (timeout %.0fs)", self.model, self.timeout)

    def complete(self, request: LLMRequest) -> LLMResponse:
        from src.agents.kiro_cli import neutral_cwd, run_cli

        prompt = f"{request.system}\n\n---\n\n{request.user}"
        if request.json_mode:
            prompt += (
                "\n\nOutput requirement: reply with a single JSON object only. "
                "No markdown fence, no explanation, no tool use."
            )

        try:
            raw, credits, seconds = run_cli(
                prompt, argv=self.argv, timeout=self.timeout, cwd=neutral_cwd(),
                metering=self.metering,
            )
        except LLMError as exc:
            self.usage.record(task=request.task, credits=0.0, seconds=0.0, ok=False)
            logger.error("[llm] kiro_cli 호출 실패 (task=%s): %s", request.task, exc)
            raise

        self.usage.record(task=request.task, credits=credits, seconds=seconds)
        logger.info(
            "[llm] kiro_cli task=%s credits=%.3f time=%.1fs (누적 %d회 / %.3f credits)",
            request.task,
            credits,
            seconds,
            self.usage.calls,
            self.usage.credits,
        )
        return LLMResponse(text=raw, provider=self.provider, model=self.model, raw=raw)

    def parse_json(self, text: str) -> dict[str, Any] | list[Any]:
        from src.agents.kiro_cli import extract_json_payload

        return extract_json_payload(text)

    def usage_summary(self) -> dict[str, Any]:
        return self.usage.to_dict()


class OpenAILLM(LLMClient):
    """OpenAI Chat Completions."""

    provider = "openai"

    def __init__(self, model: str | None = None) -> None:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise LLMError("OPENAI_API_KEY 가 설정되지 않았습니다", provider=self.provider)
        try:
            from openai import OpenAI  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise LLMError(
                "openai 패키지가 없습니다 (pip install -r requirements-optional.txt)",
                provider=self.provider,
            ) from exc
        self._client = OpenAI(api_key=api_key)
        self.model = model or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - SDK 예외 종류가 많음
            raise LLMError(f"OpenAI 요청 실패: {exc}", provider=self.provider) from exc
        self.calls += 1
        return LLMResponse(
            text=response.choices[0].message.content or "",
            provider=self.provider,
            model=self.model,
            raw=response,
        )

    def usage_summary(self) -> dict[str, Any]:
        return {"calls": self.calls}


class AnthropicLLM(LLMClient):
    """Anthropic Messages API."""

    provider = "anthropic"

    def __init__(self, model: str | None = None) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY 가 설정되지 않았습니다", provider=self.provider)
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise LLMError(
                "anthropic 패키지가 없습니다 (pip install -r requirements-optional.txt)",
                provider=self.provider,
            ) from exc
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model or os.environ.get("ANTHROPIC_MODEL") or "claude-3-5-sonnet-latest"
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        user = request.user
        if request.json_mode:
            user += "\n\nRespond with a single JSON object and nothing else."
        try:
            message = self._client.messages.create(
                model=self.model,
                system=request.system,
                messages=[{"role": "user", "content": user}],
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Anthropic 요청 실패: {exc}", provider=self.provider) from exc
        self.calls += 1
        text = "".join(
            block.text for block in message.content if getattr(block, "type", "") == "text"
        )
        return LLMResponse(text=text, provider=self.provider, model=self.model, raw=message)

    def usage_summary(self) -> dict[str, Any]:
        return {"calls": self.calls}


def normalize_provider(name: str | None) -> str:
    """provider 별칭을 정규화합니다."""
    value = (name or "").strip().lower().replace("-", "_")
    if value in {"kiro", "kirocli", "kiro_cli"}:
        return "kiro_cli"
    if value in {"", "none"}:
        return "mock"
    return value


def build_llm(provider: str | None = None) -> LLMClient:
    """지정한 provider 를 생성합니다.

    mock 은 ``LLM_PROVIDER=mock`` 을 명시했을 때만 사용됩니다. 다른 provider 를
    지정했는데 사용할 수 없으면 :class:`LLMError` 를 던집니다(폴백 없음).

    Raises:
        LLMError: 알 수 없는 provider 이거나, 생성에 실패한 경우.
    """
    name = normalize_provider(provider or os.environ.get("LLM_PROVIDER") or "mock")

    if name not in KNOWN_PROVIDERS:
        raise LLMError(
            f"알 수 없는 LLM provider `{name}` (사용 가능: {sorted(KNOWN_PROVIDERS)})"
        )

    if name in MOCK_PROVIDERS:
        logger.warning(
            "[llm] mock provider 사용 중 — 초안 문장은 규칙 기반 재조합입니다. "
            "실제 문장 생성에는 LLM_PROVIDER=kiro_cli 등을 설정하세요."
        )
        return MockLLM()

    if name == "kiro_cli":
        client: LLMClient = KiroCliLLM()
    elif name == "openai":
        client = OpenAILLM()
    else:
        client = AnthropicLLM()

    logger.info("[llm] provider=%s model=%s", client.provider, client.model)
    return client
