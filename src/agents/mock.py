"""Deterministic mock responses.

These are not "fake" placeholders: each task derives its output from the request
payload with plain text processing, so ``--dry-run`` produces realistic,
inspectable artefacts and exercises every downstream code path (JSON parsing,
length limits, review checklist) with no network access.

When a real provider is configured, none of this code runs.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from src.core.text import truncate

if TYPE_CHECKING:
    from src.agents.llm import LLMRequest

_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_NUMBERISH = re.compile(
    r"\d|\bv\d|%|\bx\b|GA\b|generally available|deprecat|breaking|removed|added", re.IGNORECASE
)
_VERSION = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?(?:-[\w.]+)?\b")
_HYPE = [
    "revolutionary",
    "game-changing",
    "게임체인저",
    "혁명적",
    "엄청난",
    "충격",
    "미쳤다",
    "역사상 최고",
    "must-have",
    "insane",
]

_TRUST_CONFIDENCE = {"official": 0.9, "github": 0.88, "paper": 0.75, "major_news": 0.6}


def _sentences(text: str, *, limit: int = 40) -> list[str]:
    parts = [s.strip(" -•\t") for s in _SENTENCE.split(text or "") if s and s.strip()]
    return [p for p in parts if len(p) > 25][:limit]


def _bullets(text: str, *, limit: int = 6) -> list[str]:
    out: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("-", "*", "•")) and len(stripped) > 6:
            out.append(stripped.lstrip("-*• ").strip())
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# research
# --------------------------------------------------------------------------- #
def _mock_research(payload: dict[str, Any]) -> dict[str, Any]:
    article = payload.get("article") or {}
    body = str(article.get("content_excerpt") or article.get("summary") or "")
    title = str(article.get("title") or "")
    trust = str(article.get("trust_tier") or "community")
    sentences = _sentences(body)
    bullets = _bullets(body)

    changed = [s for s in sentences if _NUMBERISH.search(s)]
    what_changed = " ".join(changed[:2]) or (sentences[0] if sentences else title)

    facts: list[dict[str, Any]] = []
    for sentence in (bullets or changed)[:5]:
        kind = "version" if _VERSION.search(sentence) else ("number" if re.search(r"\d", sentence) else "fact")
        facts.append(
            {
                "text": truncate(sentence, 240),
                "kind": kind,
                "source_url": article.get("url", ""),
                "confidence": _TRUST_CONFIDENCE.get(trust, 0.5),
            }
        )

    prior = [
        f"{chunk.get('title')}: {truncate(str(chunk.get('excerpt') or ''), 160)}"
        for chunk in (payload.get("prior_context") or [])[:3]
    ]

    open_questions: list[str] = []
    if "TODO" in body or "TBD" in body:
        open_questions.append("원문에 확정되지 않은 항목이 있음 (TODO/TBD)")
    if not facts:
        open_questions.append("본문에서 구체적 수치/버전을 찾지 못함 - 원문 확인 필요")
    if trust not in {"official", "github", "paper"}:
        open_questions.append("1차 출처가 아님 - 공식 발표 확인 필요")

    return {
        "what_changed": truncate(what_changed, 700),
        "why_it_matters": truncate(
            " ".join(sentences[1:3])
            or f"{article.get('source', 'source')} 발표 내용이 개발자 워크플로에 영향을 줄 수 있음.",
            700,
        ),
        "key_facts": facts,
        "technical_details": [truncate(b, 200) for b in bullets[:5]],
        "open_questions": open_questions,
        "prior_context": prior,
        "confidence": _TRUST_CONFIDENCE.get(trust, 0.5),
    }


# --------------------------------------------------------------------------- #
# fact check
# --------------------------------------------------------------------------- #
def _grounded(claim: str, source_text: str) -> bool:
    """Token-overlap check: is the claim actually supported by the source text?"""
    claim_tokens = {t for t in re.findall(r"[\w.]+", claim.lower()) if len(t) > 3}
    if not claim_tokens:
        return False
    source_tokens = set(re.findall(r"[\w.]+", source_text.lower()))
    overlap = len(claim_tokens & source_tokens) / len(claim_tokens)
    return overlap >= 0.6


def _mock_fact_check(payload: dict[str, Any]) -> dict[str, Any]:
    article = payload.get("article") or {}
    research = payload.get("research") or {}
    source_text = str(payload.get("source_excerpt") or "")
    trust = str(article.get("trust_tier") or "community")
    primary = trust in {"official", "github", "paper"}

    checked: list[dict[str, Any]] = []
    unverified: list[str] = []
    for fact in research.get("key_facts") or []:
        claim = str(fact.get("text") or "")
        if not claim:
            continue
        grounded = _grounded(claim, source_text)
        verdict = "verified" if (grounded and primary) else (
            "partially_verified" if grounded else "unverified"
        )
        if verdict == "unverified":
            unverified.append(claim)
        checked.append(
            {
                "claim": claim,
                "verdict": verdict,
                "confidence": 0.9 if verdict == "verified" else 0.55 if grounded else 0.2,
                "evidence_url": article.get("url", "") if grounded else "",
                "note": "원문 문장과 토큰 일치"
                if grounded
                else "원문에서 동일 표현을 찾지 못함 - 사람 확인 필요",
            }
        )

    verified_count = sum(1 for c in checked if c["verdict"] == "verified")
    if not checked:
        verdict = "unverified"
        confidence = 0.2
    elif verified_count == len(checked) and primary:
        verdict = "verified"
        confidence = 0.9
    elif verified_count >= max(1, len(checked) // 2):
        verdict = "partially_verified"
        confidence = 0.68
    else:
        verdict = "unverified"
        confidence = 0.35

    notes = [f"출처 신뢰도: {trust} ({'1차 출처' if primary else '2차 출처'})"]
    if not primary:
        notes.append("공식 발표 링크를 추가로 확인할 것")

    return {
        "verdict": verdict,
        "confidence": confidence,
        "checked_claims": checked,
        "unverified_claims": unverified,
        "corrections": [],
        "primary_source_found": primary,
        "needs_human_review": verdict != "verified" or bool(unverified),
        "notes": notes,
    }


# --------------------------------------------------------------------------- #
# writer
# --------------------------------------------------------------------------- #
def _hashtags(items: list[dict[str, Any]], limit: int) -> list[str]:
    tags: list[str] = []
    for item in items:
        category = str((item.get("article") or {}).get("category") or "")
        if category == "ai" and "#AI" not in tags:
            tags.append("#AI")
        if category == "blockchain" and "#Blockchain" not in tags:
            tags.append("#Blockchain")
        for topic in ((item.get("article") or {}).get("topics") or [])[:2]:
            tag = "#" + "".join(part.capitalize() for part in str(topic).split("_"))
            if tag not in tags:
                tags.append(tag)
    return tags[:limit] if limit > 0 else []


def _mock_write(payload: dict[str, Any]) -> dict[str, Any]:
    channel = payload.get("channel") or {}
    items = payload.get("items") or []
    max_chars = int(channel.get("max_chars") or 1000)
    is_short = str(channel.get("format")) == "shortform"

    if not items:
        return {"title": "초안 없음", "body": "선별된 항목이 없습니다.", "hashtags": [], "thread_parts": []}

    headline_topic = str((items[0].get("research") or {}).get("what_changed") or "")
    title = truncate(
        f"이번 주 AI/블록체인 업데이트: {truncate(headline_topic, 60)}", 110
    )

    blocks: list[str] = []
    thread_parts: list[str] = []
    for index, item in enumerate(items, 1):
        article = item.get("article") or {}
        research = item.get("research") or {}
        fact = item.get("fact_check") or {}
        what = truncate(str(research.get("what_changed") or ""), 220 if is_short else 420)
        why = truncate(str(research.get("why_it_matters") or ""), 160 if is_short else 320)
        details = [truncate(d, 120) for d in (research.get("technical_details") or [])[:2]]
        caveat = (
            " (검증 필요)"
            if fact.get("verdict") not in {"verified", "partially_verified"}
            else ""
        )

        if is_short:
            part = f"{index}/ {truncate(str(article.get('title') or ''), 80)}{caveat}\n{what}"
            thread_parts.append(truncate(part, max_chars))
            blocks.append(part)
        else:
            lines = [f"**{index}. {article.get('title')}**{caveat}", "", f"바뀐 점: {what}"]
            if why:
                lines.append(f"왜 중요한가: {why}")
            if details:
                lines.extend([f"- {d}" for d in details])
            lines.append(f"출처: {article.get('source')} — {article.get('url')}")
            blocks.append("\n".join(lines))

    body = "\n\n".join(blocks)
    if is_short:
        # A short-form body must stand alone; the rest goes into thread_parts.
        body = thread_parts[0] if thread_parts else truncate(body, max_chars)
        if len(thread_parts) > 1:
            body = truncate(body, max_chars)
    else:
        body = (
            "이번 주 눈에 띈 변화만 정리했습니다. 각 항목은 1차 출처 기준입니다.\n\n"
            + body
            + "\n\n각 항목의 원문 링크를 함께 확인하시길 권합니다."
        )
        body = truncate(body, max_chars)

    return {
        "title": title,
        "body": body,
        "hashtags": _hashtags(items, int(channel.get("hashtag_limit") or 3)),
        "thread_parts": thread_parts if is_short else [],
    }


# --------------------------------------------------------------------------- #
# editor
# --------------------------------------------------------------------------- #
def _mock_edit(payload: dict[str, Any]) -> dict[str, Any]:
    draft = payload.get("draft") or {}
    body = str(draft.get("body") or "")
    max_chars = int(payload.get("max_chars") or 1000)
    changes: list[str] = []

    for word in _HYPE:
        if word.lower() in body.lower():
            body = re.sub(re.escape(word), "", body, flags=re.IGNORECASE)
            changes.append(f"과장 표현 제거: {word}")

    lines = body.splitlines()
    deduped: list[str] = []
    for line in lines:
        if line.strip() and deduped and line.strip() == deduped[-1].strip():
            changes.append("중복 문장 제거")
            continue
        deduped.append(line)
    body = "\n".join(deduped)

    body = re.sub(r"[ \t]{2,}", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    if len(body) > max_chars:
        body = truncate(body, max_chars)
        changes.append(f"{max_chars}자 제한에 맞춰 축약")

    if not changes:
        changes.append("수정 없음 (문제 없음)")

    return {
        "title": draft.get("title") or "",
        "body": body,
        "hashtags": draft.get("hashtags") or [],
        "thread_parts": draft.get("thread_parts") or [],
        "changes": changes,
    }


_BUILDERS = {
    "research": _mock_research,
    "fact_check": _mock_fact_check,
    "write": _mock_write,
    "edit": _mock_edit,
}


def build_mock_response(request: LLMRequest) -> dict[str, Any]:
    """Dispatch on ``request.task``; unknown tasks echo the payload."""
    builder = _BUILDERS.get(request.task)
    if builder is None:
        return {"note": f"mock provider has no builder for task={request.task}"}
    return builder(request.payload)
