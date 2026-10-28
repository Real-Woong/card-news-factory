"""LLM(Kiro CLI) 준비 상태 점검 — 버전이 바뀌면 카드를 만들기 전에 호출을 한 번 시험한다.

Kiro CLI 는 자동 업그레이드된다(예: 2.x → 3.0). 새 버전에서 출력 형식이나 옵션이 바뀌면 파이프라인의 출력
파싱이 깨질 수 있다. 사람이 안 보는 운영에서 그 사실을 실행 한가운데(LLM 호출 수십 번 뒤)에서야 알면 늦으므로:

* 마지막으로 정상이라고 확인한 버전을 ``~/.r_a_glance/kiro_version.json`` 에 기록하고,
* 버전이 다르면 아주 작은 호출(canary)로 파싱까지 확인한 뒤 통과하면 새 버전을 기록한다.
* 실패하면 이 실행을 건너뛰고 알린다(가짜 문장으로 폴백하지 않는다).

같은 버전이면 추가 호출 없이 통과한다(비용 0).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agents import kiro_cli
from src.core.logging_setup import get_logger

logger = get_logger(__name__)

STATE_PATH = Path.home() / ".r_a_glance" / "kiro_version.json"


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)


def llm_ready(
    llm: Any,
    *,
    path: Path | None = None,
    version_fn: Callable[[str], str] | None = None,
    canary_fn: Callable[..., Any] | None = None,
) -> tuple[bool, str]:
    """(준비 여부, 알림에 쓸 메시지). Kiro CLI 가 아니면 점검하지 않는다."""
    argv = getattr(llm, "argv", None)
    if getattr(llm, "provider", "") != "kiro_cli" or not argv:
        return True, ""
    target = path or STATE_PATH
    version = (version_fn or kiro_cli.kiro_version)(argv[0])
    known = _read(target)
    if version and known.get("version") == version:
        return True, ""  # 이미 확인한 버전
    result = (canary_fn or kiro_cli.canary)(argv)
    previous = known.get("version") or "없음"
    if not result.ok:
        return False, (
            f"Kiro CLI 버전이 {previous} → {version or '알 수 없음'} 로 바뀐 뒤 호출 시험에 실패했습니다"
            f"({result.detail}). 이번 실행은 건너뜁니다 — 터미널에서 "
            f"`python -m src.agents.kiro_cli` 로 확인하세요"
        )
    if version:
        _write(target, {
            "version": version,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "credits_footer_seen": bool(getattr(result, "credits_seen", False)),
        })
    logger.info("[llm_guard] Kiro CLI %s 호출 시험 통과(이전 %s)", version or "?", previous)
    return True, ""
