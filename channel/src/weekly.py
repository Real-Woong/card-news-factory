"""주간 운영: 월요일에 이번 주 주제 2개를 정하고, 수·일에 하나씩 올린다.

흐름(복잡한 대기열 없이, 엔진 출력과 중복 건너뛰기만으로 굴러간다)::

    월 08:30  엔진이 최신 소식을 수집·RAG·검증 → research.json
              이 모듈이 "이번 주 주제 2개(+예비)"를 정해 목록으로 저장하고 알린다
    수 08:30  채널 파이프라인이 같은 순서에서 1번을 만들어(근거 추적·자동 수정·게이트) 게시
    일 08:30  수요일에 쓴 주제는 건너뛰므로(dedupe) 자연스럽게 2번이 올라간다

주제 순서는 한 곳(``order_topics``)에서 정하므로 목록과 실제 게시가 어긋나지 않는다. 승인된 후보가
(게이트 보류·재료 부족으로) 안 나오면 예비 주제로 넘어간다(``posting.max_attempts``).

CLI::

    python -m channel.src.weekly [--research path] [--no-notify]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.core.logging_setup import get_logger

from channel.src.config import ChannelConfig, load_channel_config
from channel.src.schema import VerifiedTopic

logger = get_logger(__name__)

SLOTS = 2
RESERVES = 2
WEEKLY_DIR = Path(__file__).resolve().parents[2] / "outputs" / "weekly"


def order_topics(topics: list[VerifiedTopic]) -> list[VerifiedTopic]:
    """무인 게시 순서: 검증 통과(verified)만, 엔진 순위대로. 두 번째 자리는 가능하면 다른 카테고리.

    partially_verified 는 자동 승인 게이트가 어차피 보류하므로 무인 실행에서는 시도하지 않는다.
    """
    verified = [topic for topic in topics if topic.verdict == "verified"]
    if len(verified) < 3:
        return verified
    first, rest = verified[0], verified[1:]
    second = next((t for t in rest[:3] if t.category != first.category), rest[0])
    rest.remove(second)
    return [first, second, *rest]


def build_list(
    topics: list[VerifiedTopic], *, skip_ids: set[str] | None = None, research: str = ""
) -> dict[str, Any]:
    """이번 주 목록: 게시 슬롯 2개 + 예비. 이미 후보가 있는 주제(``skip_ids``)는 뺀다."""
    skip = skip_ids or set()
    ordered = [t for t in order_topics(topics) if t.topic_id not in skip]

    def entry(topic: VerifiedTopic) -> dict[str, str]:
        return {"topic_id": topic.topic_id, "category": topic.category, "title": topic.headline}

    return {
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "research": research,
        "slots": [entry(t) for t in ordered[:SLOTS]],
        "reserves": [entry(t) for t in ordered[SLOTS : SLOTS + RESERVES]],
    }


def research_age_days(research_path: Path, today: date | None = None) -> int:
    """research.json 이 몇 일 전 실행 결과인가(폴더 이름의 날짜, 없으면 수정 시각)."""
    today = today or date.today()
    try:
        return (today - date.fromisoformat(research_path.parent.name[:10])).days
    except ValueError:
        return (today - date.fromtimestamp(research_path.stat().st_mtime)).days


def save_list(payload: dict[str, Any], directory: Path | None = None) -> Path:
    folder = directory or WEEKLY_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{date.today().isoformat()}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def summary(payload: dict[str, Any]) -> str:
    slots = payload.get("slots") or []
    if not slots:
        return "이번 주 게시할 검증된 주제가 없습니다"
    names = " / ".join(f"{s['title'][:40]}" for s in slots)
    return f"이번 주 주제 {len(slots)}개(수·일): {names} — 예비 {len(payload.get('reserves') or [])}개"


def main(argv: list[str] | None = None) -> int:
    from src.core.config import load_dotenv_file

    from channel.src.inputs import TopicInputError, load_topics
    from channel.src.notify import notify
    from channel.src.pipeline import existing_topic_ids

    parser = argparse.ArgumentParser(prog="channel.src.weekly", description=__doc__)
    parser.add_argument("--research", type=Path, default=None)
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args(argv)

    load_dotenv_file()
    channel: ChannelConfig = load_channel_config()
    try:
        topics, research_path = load_topics(args.research)
    except TopicInputError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        if not args.no_notify:
            notify("R_A_Glance 주간 목록 실패", str(exc))
        return 2
    skip = existing_topic_ids(channel, int(channel.get("instagram.posting.dedupe_days", 14)))
    payload = build_list(topics, skip_ids=skip, research=str(research_path))
    path = save_list(payload)
    message = summary(payload)
    print(message)
    for label in ("slots", "reserves"):
        for item in payload[label]:
            print(f"  [{label}] {item['category']:<10} {item['title']}")
    print(f"saved: {path}")
    if not args.no_notify:
        notify("R_A_Glance 주간 목록", message)
    return 0 if payload["slots"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
