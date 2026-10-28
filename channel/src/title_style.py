"""제목 규칙 판정(validator 경고와 자동 수정이 같은 기준을 쓴다).

제목은 직관적인 한국어여야 한다: 영어 전문용어는 본문에 두고, 길면 줄인다.
기준값은 content.yaml → ``title_style``.
"""

from __future__ import annotations

import re

from channel.src.config import ChannelConfig

JARGON = "title_jargon"
LONG = "title_long"


def title_problems(
    title: str, channel: ChannelConfig, known_names: frozenset[str] = frozenset()
) -> list[tuple[str, str]]:
    """(코드, 설명) 목록. 비어 있으면 규칙을 지킨 제목.

    ``known_names`` 는 근거에 그대로 나오는 이름(제품명 등). 영어여도 전문용어로 세지 않는다 —
    정확하게 쓰려면 이름을 써야 할 때가 있다. 풀이 대상 용어(glossary)는 여전히 센다.
    """
    style = channel.get("content.title_style", {}) or {}
    if not style.get("enabled", True) or not title:
        return []
    allowed = {str(word).lower() for word in style.get("allow_latin", [])}
    glossary = {
        str(term).lower() for term in (channel.get("content.glossary.terms", {}) or {})
    }
    words = re.findall(r"[A-Za-z][A-Za-z\-]*", title)
    jargon = [w for w in words if w.lower() not in allowed and w not in known_names]
    terms = [t for t in glossary if t in title.lower()]
    problems: list[tuple[str, str]] = []
    if len(jargon) > int(style.get("max_latin_words", 1)) or terms:
        problems.append((JARGON, f"영어 전문용어가 많습니다({', '.join(jargon or terms)})"))
    max_chars = int(style.get("max_chars", 30))
    if len(title) > max_chars:
        problems.append((LONG, f"제목 {len(title)}자 (권장 {max_chars}자 이하)"))
    return problems
