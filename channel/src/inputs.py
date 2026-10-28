"""공통 엔진 출력 → 채널 입력 어댑터.

경계는 **파일**입니다. 채널은 엔진의 파이썬 모델을 import 하지 않고
``outputs/drafts/<date>/research.json`` (또는 같은 모양의 dict) 만 읽습니다.

엔진이 바뀌어도 이 파일의 매핑만 고치면 되고, 반대로 채널 요구사항 때문에 엔진을
수정할 필요가 없습니다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.core.errors import ContentOpsError
from src.core.logging_setup import get_logger
from src.core.text import canonicalize_url

from channel.src.schema import Source, VerifiedTopic

logger = get_logger(__name__)

ENGINE_DRAFTS_DIR = Path("outputs/drafts")
RESEARCH_FILENAME = "research.json"


class TopicInputError(ContentOpsError):
    """엔진 출력이 없거나 읽을 수 없음."""


def find_latest_research(root: Path, *, drafts_dir: Path | None = None) -> Path:
    """가장 최근 실행의 ``research.json`` 경로를 찾습니다.

    Raises:
        TopicInputError: 산출물이 하나도 없는 경우.
    """
    base = drafts_dir or (root / ENGINE_DRAFTS_DIR)
    if not base.exists():
        raise TopicInputError(
            f"엔진 산출물 디렉터리가 없습니다: {base} — 먼저 `python -m src.pipeline.run` 실행"
        )
    candidates = sorted(
        (path for path in base.glob(f"*/{RESEARCH_FILENAME}") if path.is_file()),
        key=lambda path: (path.parent.name, path.stat().st_mtime),
        reverse=True,
    )
    if not candidates:
        raise TopicInputError(f"{base} 아래에 {RESEARCH_FILENAME} 이 없습니다")
    return candidates[0]


def _source_from_engine(article: dict[str, Any], verdict: str) -> Source:
    return Source(
        name=str(article.get("source") or article.get("source_name") or "").strip(),
        url=str(article.get("url") or "").strip(),
        verdict=verdict,
        authority="primary",
        source_relation="primary_announcement",
    )


def topics_from_research_payload(payload: dict[str, Any]) -> list[VerifiedTopic]:
    """엔진 ``research.json`` dict → :class:`VerifiedTopic` 목록."""
    topics: list[VerifiedTopic] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        article = dict(item.get("article") or {})
        research = dict(item.get("research") or {})
        fact_check = dict(item.get("fact_check") or {})
        verdict = str(fact_check.get("verdict") or "")

        sources = [_source_from_engine(article, verdict)]
        for extra in research.get("sources") or []:
            if not isinstance(extra, dict):
                continue
            candidate = Source(
                name=str(extra.get("source") or extra.get("name") or "").strip(),
                url=str(extra.get("url") or "").strip(),
                verdict=verdict,
                authority=str(extra.get("authority") or "").strip(),
                source_relation=str(extra.get("source_relation") or "").strip(),
                supports_event=bool(extra.get("supports_event", True)),
            )
            existing = {canonicalize_url(source.url) for source in sources}
            if candidate.url and canonicalize_url(candidate.url) not in existing:
                sources.append(candidate)

        topics.append(
            VerifiedTopic(
                topic_id=str(article.get("key") or article.get("id") or "").strip(),
                category=str(article.get("category") or "").strip(),
                headline=str(article.get("title") or research.get("what_changed") or "").strip(),
                research=research,
                fact_check=fact_check,
                sources=[s for s in sources if s.name or s.url],
            )
        )
    return topics


def load_topics(
    path: Path | None = None,
    *,
    root: Path | None = None,
    allowed_verdicts: set[str] | None = None,
) -> tuple[list[VerifiedTopic], Path]:
    """엔진 출력에서 토픽을 읽어 검증 통과분만 돌려줍니다.

    Args:
        path: ``research.json`` 경로. 없으면 가장 최근 실행을 찾습니다.
        root: 프로젝트 루트 (기본: 이 파일 기준 상위).
        allowed_verdicts: 허용 verdict 집합. 기본 verified/partially_verified.

    Returns:
        ``(토픽 목록, 사용한 research.json 경로)``

    Raises:
        TopicInputError: 파일이 없거나 JSON 이 잘못된 경우.
    """
    project_root = root or Path(__file__).resolve().parents[2]
    research_path = path or find_latest_research(project_root)
    try:
        payload = json.loads(research_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TopicInputError(f"{research_path} 를 읽을 수 없습니다: {exc}") from exc

    topics = topics_from_research_payload(payload)
    allowed = allowed_verdicts or {"verified", "partially_verified"}
    usable = [topic for topic in topics if topic.verdict in allowed]

    logger.info(
        "[input] %s 에서 토픽 %d개 로드, 사용 가능 %d개 (허용 verdict=%s)",
        research_path.name,
        len(topics),
        len(usable),
        sorted(allowed),
    )
    if not usable:
        raise TopicInputError(
            f"{research_path} 에 사용 가능한 토픽이 없습니다 "
            f"(전체 {len(topics)}개, 허용 verdict {sorted(allowed)})"
        )
    return usable, research_path


def select_topic(
    topics: list[VerifiedTopic], *, topic_id: str | None = None, index: int | None = None
) -> VerifiedTopic:
    """토픽 하나를 고릅니다(기본: 첫 번째 = 엔진 랭킹 1위).

    Raises:
        TopicInputError: 지정한 topic_id/index 를 찾을 수 없는 경우.
    """
    if topic_id:
        for topic in topics:
            if topic.topic_id == topic_id or topic.topic_id.startswith(topic_id):
                return topic
        raise TopicInputError(f"topic_id `{topic_id}` 를 찾을 수 없습니다")
    if index is not None:
        if not 0 <= index < len(topics):
            raise TopicInputError(f"index {index} 범위 초과 (0~{len(topics) - 1})")
        return topics[index]
    return topics[0]
