"""Editor Agent: remove hype, unsourced claims and repetition."""

from __future__ import annotations

from typing import Any

from src.agents.base import Agent
from src.core.config import ChannelConfig
from src.core.models import Draft, FactCheckReport

INSTRUCTION = (
    "Edit the draft. Remove hype and superlatives, delete or hedge any claim not "
    "backed by the listed sources, remove repetition, and make the tone read like "
    "a human engineer wrote it. Keep every factual detail that is verified, keep "
    "the source links, and stay within max_chars. Return JSON with keys: title, "
    "body, hashtags (list), thread_parts (list), changes (list of short strings "
    "describing what you changed)."
)


class EditorAgent(Agent):
    """Returns a cleaned :class:`Draft` with an audit list of changes."""

    name = "editor"
    task = "edit"
    prompt_file = "editor.md"

    def build_payload(
        self,
        *,
        draft: Draft,
        channel: ChannelConfig,
        reports: list[FactCheckReport],
    ) -> dict[str, Any]:
        return {
            "draft": {
                "title": draft.title,
                "body": draft.body,
                "hashtags": draft.hashtags,
                "thread_parts": draft.thread_parts,
                "format": draft.format,
            },
            "max_chars": channel.max_chars,
            "language": channel.language,
            "style": channel.style,
            "allowed_sources": draft.sources,
            "unverified_claims": [
                claim for report in reports for claim in report.unverified_claims
            ],
            "corrections": [
                correction for report in reports for correction in report.corrections
            ],
        }

    def run(
        self,
        *,
        draft: Draft,
        channel: ChannelConfig,
        reports: list[FactCheckReport] | None = None,
    ) -> Draft:
        payload = self.build_payload(draft=draft, channel=channel, reports=reports or [])
        data = self.ask_json(payload, instruction=INSTRUCTION)

        body = str(data.get("body") or draft.body).strip()
        edited = Draft(
            channel_id=draft.channel_id,
            format=draft.format,
            title=str(data.get("title") or draft.title).strip(),
            body=body,
            hashtags=[str(t) for t in (data.get("hashtags") or draft.hashtags) if t][
                : channel.hashtag_limit
            ],
            thread_parts=[
                str(p) for p in (data.get("thread_parts") or draft.thread_parts) if str(p).strip()
            ],
            sources=draft.sources,
            char_count=len(body),
            over_limit=len(body) > channel.max_chars,
            editor_changes=[str(c) for c in (data.get("changes") or []) if c],
        )
        self.log.info(
            "[editor] %s: %d -> %d chars, %d change(s)",
            channel.id,
            draft.char_count,
            edited.char_count,
            len(edited.editor_changes),
        )
        return edited
