"""Research Agent: extract what changed, not a summary."""

from __future__ import annotations

from typing import Any

from src.agents.base import Agent
from src.core.errors import AgentError
from src.core.models import Article, Claim, ResearchNote, RetrievedChunk
from src.core.text import canonicalize_url, to_iso, truncate

PRIMARY_TIERS = {"official", "github", "paper"}


def _authority(trust_tier: str) -> str:
    return "primary" if (trust_tier or "").lower() in PRIMARY_TIERS else "secondary"


def _relation(article: Article, *, primary: bool) -> str:
    tier = (article.trust_tier or "").lower()
    source_type = (article.source_type or "").lower()
    url = article.url.lower()
    if tier == "github" or "github.com/" in url:
        return "implementation_artifact"
    if source_type in {"docs", "documentation"} or "/docs/" in url:
        return "documentation"
    if tier in {"major_news", "community"} and not primary:
        return "independent_secondary"
    return "primary_announcement" if primary else "documentation"


def _source_entry(article: Article, *, primary: bool) -> dict[str, Any]:
    return {
        "title": article.title,
        "url": article.url,
        "source": article.source_name,
        "name": article.source_name,
        "type": article.source_type,
        "authority": _authority(article.trust_tier),
        "trust_tier": article.trust_tier,
        "published_at": to_iso(article.published_at),
        "primary": primary,
        "source_relation": _relation(article, primary=primary),
        "supports_event": True,
    }


def _build_sources(article: Article, cluster: Any | None) -> list[dict[str, Any]]:
    """클러스터가 있으면 authority 순 복수 출처, 없으면 단일 출처."""
    if cluster is not None and hasattr(cluster, "sources"):
        entries = cluster.sources()
        if entries:
            sources = [
                {
                    **entry,
                    "source": entry.get("name"),
                    "published_at": None,
                }
                for entry in entries
            ]
        else:
            sources = [_source_entry(article, primary=True)]
    else:
        sources = [_source_entry(article, primary=True)]

    seen = {
        canonicalize_url(str(source.get("url") or ""))
        for source in sources
        if source.get("url")
    }
    for discovered in article.source_metadata.get("supporting_sources") or []:
        if not isinstance(discovered, dict) or not discovered.get("supports_event"):
            continue
        url = str(discovered.get("url") or "")
        canonical = canonicalize_url(url)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        sources.append(
            {
                **discovered,
                "source": discovered.get("name"),
                "type": "discovered_artifact",
                "trust_tier": "github"
                if discovered.get("source_relation") == "implementation_artifact"
                else "official",
                "primary": False,
            }
        )
    return sources

INSTRUCTION = (
    "Analyse the article below together with the prior context. "
    "Focus on what concretely changed (versions, numbers, dates, availability, "
    "breaking changes) and why a developer should care. "
    "Return JSON with keys: what_changed, why_it_matters, key_facts "
    "(list of {text, kind, source_url, confidence}), technical_details (list), "
    "open_questions (list), prior_context (list), confidence (0-1). "
    "Never invent facts that are absent from the source text."
)


class ResearchAgent(Agent):
    """Turns one article (+ RAG context) into a :class:`ResearchNote`."""

    name = "research"
    task = "research"
    prompt_file = "researcher.md"

    def build_payload(
        self,
        article: Article,
        context: list[RetrievedChunk],
        cluster: Any | None = None,
    ) -> dict[str, Any]:
        priority_words = (
            "release", "availability", "rollout", "benchmark", "evaluation",
            "performance", "reference", "repository", "changelog",
        )
        preserved_sections = [
            {
                "chunk_id": chunk.get("id"),
                "section": chunk.get("section"),
                "excerpt": truncate(str(chunk.get("text") or ""), 900),
            }
            for chunk in (article.source_metadata.get("chunks") or [])
            if isinstance(chunk, dict)
            and (
                chunk.get("kind") in {"list_item", "table"}
                or any(
                    word in str(chunk.get("section") or "").lower()
                    for word in priority_words
                )
            )
        ][:8]
        payload: dict[str, Any] = {
            "article": {
                **article.reference(),
                "summary": article.summary,
                "content_excerpt": truncate(article.body, 6000),
                "structured_evidence": article.reference().get(
                    "source_metadata", {}
                ),
                "preserved_sections": preserved_sections,
            },
            "prior_context": [
                {
                    "title": chunk.source_title,
                    "url": chunk.url,
                    "collection": chunk.metadata.get("collection"),
                    "similarity": round(chunk.score, 3),
                    "excerpt": truncate(chunk.text, 700),
                }
                for chunk in context
            ],
        }
        # 같은 사건을 다룬 다른 기사가 있으면 교차 확인 재료로 함께 전달합니다.
        if cluster is not None and getattr(cluster, "size", 1) > 1:
            payload["corroborating_articles"] = [
                {
                    "source": member.source_name,
                    "authority": _authority(member.trust_tier),
                    "title": member.title,
                    "url": member.url,
                    "excerpt": truncate(member.body, 1500),
                }
                for member in cluster.members
            ]
        return payload

    def run(
        self,
        article: Article,
        context: list[RetrievedChunk] | None = None,
        *,
        cluster: Any | None = None,
    ) -> ResearchNote:
        context = context or []
        data = self.ask_json(
            self.build_payload(article, context, cluster), instruction=INSTRUCTION
        )

        facts: list[Claim] = []
        for raw in data.get("key_facts") or []:
            if isinstance(raw, str):
                facts.append(Claim(text=raw, source_url=article.url))
                continue
            if not isinstance(raw, dict):
                continue
            facts.append(
                Claim(
                    text=str(raw.get("text") or "").strip(),
                    kind=str(raw.get("kind") or "fact"),
                    source_url=str(raw.get("source_url") or article.url),
                    confidence=float(raw.get("confidence") or 0.5),
                )
            )
        facts = [fact for fact in facts if fact.text]

        note = ResearchNote(
            article_key=article.key,
            title=article.title,
            url=article.url,
            category=article.category,
            what_changed=str(data.get("what_changed") or "").strip(),
            why_it_matters=str(data.get("why_it_matters") or "").strip(),
            key_facts=facts,
            technical_details=[str(d) for d in (data.get("technical_details") or []) if d],
            open_questions=[str(q) for q in (data.get("open_questions") or []) if q],
            prior_context=[str(p) for p in (data.get("prior_context") or []) if p],
            context_chunks=context,
            sources=_build_sources(article, cluster),
            confidence=float(data.get("confidence") or 0.5),
        )

        if not note.what_changed:
            raise AgentError(
                f"Research produced no `what_changed` for {article.url}", agent=self.name
            )
        self.log.info("[research] %s -> %d fact(s)", truncate(article.title, 60), len(facts))
        return note
