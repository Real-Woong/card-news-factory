"""에이전트별 응답 schema 검증.

LLM 이 구조적으로 다른 JSON 을 돌려주면 **mock 으로 폴백하지 않고** 해당 item 을
실패시키기 위한 최소 검증입니다. 목적은 타입 체크가 아니라 "이 응답으로 초안을
만들면 안 되는지" 판단입니다.
"""

from __future__ import annotations

from typing import Any

from src.core.errors import LLMError


def _is_list_of(value: Any, kinds: tuple[type, ...]) -> bool:
    return isinstance(value, list) and all(isinstance(item, kinds) for item in value)


# task -> (필수 키, 타입 검사 함수)
SCHEMAS: dict[str, dict[str, Any]] = {
    "research": {
        "required": {
            "what_changed": lambda v: isinstance(v, str) and v.strip() != "",
        },
        "optional": {
            "why_it_matters": lambda v: isinstance(v, str),
            "key_facts": lambda v: _is_list_of(v, (dict, str)),
            "technical_details": lambda v: _is_list_of(v, (str,)),
            "open_questions": lambda v: _is_list_of(v, (str,)),
            "prior_context": lambda v: _is_list_of(v, (str,)),
            "confidence": lambda v: isinstance(v, (int, float)),
        },
    },
    "fact_check": {
        "required": {
            "verdict": lambda v: isinstance(v, str)
            and v.strip().lower()
            in {"verified", "partially_verified", "unverified", "contradicted"},
        },
        "optional": {
            "confidence": lambda v: isinstance(v, (int, float)),
            "checked_claims": lambda v: _is_list_of(v, (dict,)),
            "unverified_claims": lambda v: _is_list_of(v, (str,)),
            "corrections": lambda v: _is_list_of(v, (str,)),
            "primary_source_found": lambda v: isinstance(v, bool),
            "needs_human_review": lambda v: isinstance(v, bool),
            "notes": lambda v: _is_list_of(v, (str,)),
            "partial_reasons": lambda v: _is_list_of(v, (str,)),
        },
    },
    "write": {
        "required": {
            "body": lambda v: isinstance(v, str) and len(v.strip()) >= 20,
        },
        "optional": {
            "title": lambda v: isinstance(v, str),
            "hashtags": lambda v: _is_list_of(v, (str,)),
            "thread_parts": lambda v: _is_list_of(v, (str,)),
        },
    },
    "edit": {
        "required": {
            "body": lambda v: isinstance(v, str) and len(v.strip()) >= 20,
        },
        "optional": {
            "title": lambda v: isinstance(v, str),
            "hashtags": lambda v: _is_list_of(v, (str,)),
            "thread_parts": lambda v: _is_list_of(v, (str,)),
            "changes": lambda v: _is_list_of(v, (str,)),
        },
    },
}


def validate_payload(task: str, data: Any, *, provider: str = "") -> dict[str, Any]:
    """task 에 맞는 schema 로 응답을 검증합니다.

    Args:
        task: ``research`` / ``fact_check`` / ``write`` / ``edit``.
        data: 파싱된 JSON.
        provider: 오류 메시지용 provider 이름.

    Returns:
        검증을 통과한 dict.

    Raises:
        LLMError: dict 가 아니거나, 필수 키가 없거나, 타입이 맞지 않는 경우.
    """
    if not isinstance(data, dict):
        raise LLMError(
            f"task={task} 응답이 JSON 객체가 아닙니다 (type={type(data).__name__})",
            provider=provider,
        )

    schema = SCHEMAS.get(task)
    if schema is None:  # 검증 규칙이 없는 task 는 통과
        return data

    missing = [key for key in schema["required"] if key not in data]
    if missing:
        raise LLMError(
            f"task={task} 응답에 필수 키가 없습니다: {missing} (받은 키: {sorted(data)[:12]})",
            provider=provider,
        )

    invalid: list[str] = []
    for key, check in schema["required"].items():
        try:
            if not check(data[key]):
                invalid.append(key)
        except Exception:  # noqa: BLE001 - 검증 함수가 예상 못한 타입을 받은 경우
            invalid.append(key)
    if invalid:
        raise LLMError(
            f"task={task} 응답의 필수 필드 형식이 잘못되었습니다: {invalid}",
            provider=provider,
        )

    # 선택 필드는 형식이 틀리면 조용히 버립니다(부분 응답으로도 초안 생성 가능).
    for key, check in schema["optional"].items():
        if key not in data:
            continue
        try:
            if not check(data[key]):
                data.pop(key)
        except Exception:  # noqa: BLE001
            data.pop(key, None)
    return data
