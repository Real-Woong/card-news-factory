"""Content Writer Agent: verified material -> channel-specific draft."""

from __future__ import annotations

from typing import Any

from src.agents.base import Agent
from src.core.config import ChannelConfig
from src.core.models import Article, Draft, FactCheckReport, ResearchNote
from src.core.text import truncate

INSTRUCTION = (
    "Write a post for the given channel using ONLY the verified material below. "
    "Lead with what changed, then why it matters to developers. No press-release "
    "phrasing, no hype, no invented numbers. Claims marked unverified must either "
    "be omitted or explicitly hedged. Respect max_chars and the channel style. "
    "Return JSON with keys: title, body, hashtags (list), thread_parts (list; "
    "shortform channels only)."
)


class WriterAgent(Agent):
    """Produces one :class:`Draft` per channel."""

    name = "writer"
    task = "write"
    prompt_file = "writer.md"

    def build_payload(
        self,
        *,
        channel: ChannelConfig,
        items: list[tuple[Article, ResearchNote, FactCheckReport]],
    ) -> dict[str, Any]:
        return {
            "channel": {
                "id": channel.id,
                "name": channel.name,
                "format": channel.format,
                "max_chars": channel.max_chars,
                "hashtag_limit": channel.hashtag_limit,
                "thread": channel.thread,
                "max_thread_items": channel.max_thread_items,
                "style": channel.style,
                "language": channel.language,
                "secondary_language": channel.secondary_language,
                "include_sources": channel.include_sources,
            },
            "items": [
                {
                    "article": article.reference(),
                    "research": {
                        "what_changed": note.what_changed,
                        "why_it_matters": note.why_it_matters,
                        "technical_details": note.technical_details,
                        "key_facts": [fact.to_dict() for fact in note.key_facts],
                        "prior_context": note.prior_context,
                    },
                    "fact_check": {
                        "verdict": str(report.verdict),
                        "confidence": report.confidence,
                        "unverified_claims": report.unverified_claims,
                        "corrections": report.corrections,
                    },
                }
                for article, note, report in items
            ],
        }

    def run(
        self,
        *,
        channel: ChannelConfig,
        items: list[tuple[Article, ResearchNote, FactCheckReport]],
    ) -> Draft:
        payload = self.build_payload(channel=channel, items=items)
        data = self.ask_json(payload, instruction=INSTRUCTION)

        body = str(data.get("body") or "").strip()
        thread_parts = [str(p).strip() for p in (data.get("thread_parts") or []) if str(p).strip()]
        if channel.thread and thread_parts:
            thread_parts = [
                truncate(part, channel.max_chars) for part in thread_parts[: channel.max_thread_items]
            ]

        sources = [
            {
                "title": article.title,
                "url": article.url,
                "source": article.source_name,
                "trust_tier": article.trust_tier,
                "verdict": str(report.verdict),
            }
            for article, _note, report in items
        ]

        draft = Draft(
            channel_id=channel.id,
            format=channel.format,
            title=str(data.get("title") or "").strip(),
            body=body,
            hashtags=[
                str(tag) for tag in (data.get("hashtags") or [])[: channel.hashtag_limit] if tag
            ],
            thread_parts=thread_parts,
            sources=sources,
            char_count=len(body),
            over_limit=len(body) > channel.max_chars,
        )
        self.log.info(
            "[writer] %s draft: %d chars%s",
            channel.id,
            draft.char_count,
            " (over limit)" if draft.over_limit else "",
        )
        return draft
