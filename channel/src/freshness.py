"""시점 지난 소식 감지: 이미 지난 날짜를 앞으로 올 일처럼 쓰는 문장을 찾습니다.

근거 추적(grounding)은 "원문이 그렇게 말하는가"만 봅니다. 마감이 지난 뒤에도 원문은
여전히 그렇게 말하므로, 시간이 지나 가치가 사라진 소식은 이 게이트가 따로 잡아야 합니다.

LLM 없이 결정적으로 동작합니다: 문장에서 연도가 명시된 날짜를 뽑고, 오늘보다 과거이면서
같은 문장에 미래·마감 표현이 있을 때만 지적합니다. 과거 날짜만 있는 문장("9월 24일 공개")은
사실 서술이므로 지적하지 않습니다. 연도가 없는 날짜는 해석이 모호해 보지 않습니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from channel.src.grounding import collect_claims
from channel.src.schema import Carousel

_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}

_ISO = re.compile(r"(?<!\d)(20\d{2})[-./](\d{1,2})[-./](\d{1,2})(?!\d)")
_KOREAN = re.compile(r"(?<!\d)(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_ENGLISH = re.compile(
    r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b"
)

# 같은 문장에 이 표현이 있으면 그 날짜를 "아직 오지 않은 시점"으로 쓴 것으로 봅니다.
FORWARD_CUES = (
    "전 ", "전에", "전까지", "까지", "마감", "기한", "예정", "예약", "앞두", "이후 ",
    "이전 ", "이탈", "필수", "해야", "우선순위", "임박", "개시 예정", "시행 예정",
    "deadline", "before", "until", "by ", "will ", "upcoming", "scheduled",
)


@dataclass
class StaleDate:
    slide: int
    text: str
    day: date
    cue: str
    days_ago: int


def extract_dates(text: str) -> list[date]:
    found: list[date] = []

    def add(y: int, m: int, d: int) -> None:
        try:
            found.append(date(y, m, d))
        except ValueError:
            pass

    for y, m, d in _ISO.findall(text):
        add(int(y), int(m), int(d))
    for y, m, d in _KOREAN.findall(text):
        add(int(y), int(m), int(d))
    for name, d, y in _ENGLISH.findall(text):
        month = _MONTHS.get(name.lower())
        if month:
            add(int(y), month, int(d))
    return found


def _cue(text: str) -> str:
    lowered = text.lower()
    # 끝에 붙은 단어도 잡히도록 공백을 덧붙여 어절 경계를 맞춥니다.
    padded = f"{lowered} "
    for cue in FORWARD_CUES:
        if cue in padded:
            return cue.strip()
    return ""


def find_stale_dates(carousel: Carousel, *, today: date | None = None) -> list[StaleDate]:
    today = today or date.today()
    stale: list[StaleDate] = []
    for claim in collect_claims(carousel):
        cue = _cue(claim.text)
        if not cue:
            continue
        past = [d for d in extract_dates(claim.text) if d < today]
        if past:
            latest = max(past)
            stale.append(StaleDate(claim.slide, claim.text, latest, cue, (today - latest).days))
    return stale
