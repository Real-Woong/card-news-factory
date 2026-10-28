"""캡션 쉬운 말 규칙 판정(writer 재시도와 validator 경고가 같은 기준을 쓴다).

카드 제목처럼 캡션도 처음 보는 사람이 읽을 수 있어야 한다. 내용의 전문성은 유지하되:

* 코드 식별자(``torch.float8_e4m3fn``, ``max_image_tiles`` 같은 것)를 그대로 쓰지 않는다.
* 풀이 대상 영어 전문용어(``content.yaml → glossary``)를 그대로 늘어놓지 않는다.
* 글자의 대부분이 영어가 되지 않게 한다(제품명·고유명사는 세지 않는다).

기준은 ``content.yaml → caption_style``. 문구는 고치지 않고 문제만 알려 준다.
"""

from __future__ import annotations

import re

from channel.src.config import ChannelConfig

# torch.float8_e4m3fn · max_image_tiles · KV_cache.v2 처럼 점·밑줄로 이어진 식별자
# (\b 는 뒤에 한글이 붙으면 경계로 보지 않으므로 직접 경계를 둔다: "torch.float8_e4m3fn로")
_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_.])[A-Za-z][A-Za-z0-9]*(?:[._][A-Za-z0-9]+)+(?![A-Za-z0-9_])")
_VERSION = re.compile(r"^v?\d+(?:\.\d+)+$")
_LATIN = re.compile(r"[A-Za-z]")
_HANGUL = re.compile(r"[가-힣]")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9\-]*")


def known_names(*texts: str) -> frozenset[str]:
    """제품명·고유명사로 쓸 수 있는 영어 낱말(카드 표지·출처 등에 이미 나온 것, 소문자)."""
    return frozenset(w.lower() for text in texts for w in _WORD.findall(text or ""))


def caption_problems(
    text: str, channel: ChannelConfig, names: frozenset[str] = frozenset()
) -> list[str]:
    """사람이 읽는 문제 설명 목록. 비어 있으면 규칙을 지킨 캡션."""
    style = channel.get("content.caption_style", {}) or {}
    if not style.get("enabled", True) or not text:
        return []
    problems: list[str] = []

    identifiers = [i for i in dict.fromkeys(_IDENTIFIER.findall(text)) if not _VERSION.match(i)]
    for identifier in identifiers:
        problems.append(f"코드 식별자 `{identifier}` — 쉬운 말로 풀어 쓰세요")

    glossary = [str(t) for t in (channel.get("content.glossary.terms", {}) or {})]
    lowered = text.lower()
    for term in glossary:
        if re.search(rf"(?<![A-Za-z]){re.escape(term.lower())}(?![A-Za-z])", lowered):
            problems.append(f"전문용어 `{term}` — 쉬운 말로 바꾸거나 뜻을 덧붙이세요")

    body = _IDENTIFIER.sub("", text)
    allowed = {str(w).lower() for w in style.get("allow_latin", [])} | set(names)
    latin = hangul = 0
    for token in re.split(r"\s+", body):
        words = _WORD.findall(token)
        if words and all(w.lower() in allowed for w in words):
            continue  # 제품명·약어는 세지 않는다
        latin += len(_LATIN.findall(token))
        hangul += len(_HANGUL.findall(token))
    ratio = latin / (latin + hangul) if latin + hangul else 0.0
    limit = float(style.get("max_latin_ratio", 0.30))
    if ratio > limit:
        problems.append(f"영어가 너무 많음({ratio:.0%}, 기준 {limit:.0%}) — 한국어로 풀어 쓰세요")
    return problems
