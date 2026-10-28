"""Fact Check Agent: verify claims against the source text."""

from __future__ import annotations

from typing import Any

from src.agents.base import Agent
from src.core.evidence import retrieve_claim_evidence
from src.core.logging_setup import get_logger
from src.core.models import Article, CheckedClaim, FactCheckReport, ResearchNote, Verdict
from src.core.text import truncate

logger = get_logger(__name__)

INSTRUCTION = (
    "Verify every claim against the provided source text. Check dates, company "
    "names, version numbers, metrics, product names and availability wording. "
    "A claim is `verified` only when the source text supports it explicitly; use "
    "`partially_verified` when it is implied, `unverified` when it is absent and "
    "`contradicted` when the source says otherwise. Prefer primary sources. "
    "Return JSON with keys: verdict, confidence (0-1), checked_claims "
    "(list of {claim, verdict, confidence, evidence_url, note}), "
    "unverified_claims (list), corrections (list), primary_source_found (bool), "
    "needs_human_review (bool), notes (list), and partial_reasons (list of "
    "structured reason codes). For checked_claims also return source_id, source_url, "
    "matched_chunk, matched_section, and a short evidence_excerpt from the supplied "
    "claim_evidence."
)

PRIMARY_TIERS = {"official", "github", "paper"}
PARTIAL_REASON_CODES = {
    "source_truncated",
    "missing_secondary_source",
    "ambiguous_availability",
    "unsupported_benchmark_rank",
    "missing_repository_link",
}


class FactCheckAgent(Agent):
    """Produces a :class:`FactCheckReport` for one research note."""

    name = "fact_check"
    task = "fact_check"
    prompt_file = "fact_checker.md"

    def build_payload(self, article: Article, note: ResearchNote) -> dict[str, Any]:
        claims = [fact.text for fact in note.key_facts]
        claims.extend(note.technical_details)
        claim_evidence = [
            {
                "claim": claim,
                "matches": retrieve_claim_evidence(
                    claim, article.source_metadata, top_k=2
                ),
            }
            for claim in claims
        ]
        return {
            "article": {
                **article.reference(),
                "domain": article.domain,
            },
            "research": {
                "what_changed": note.what_changed,
                "why_it_matters": note.why_it_matters,
                "key_facts": [fact.to_dict() for fact in note.key_facts],
                "technical_details": note.technical_details,
            },
            "source_excerpt": truncate(article.body, 8000),
            "claim_evidence": claim_evidence,
            "structured_evidence": {
                key: article.source_metadata.get(key) or []
                for key in (
                    "repository_links",
                    "release_links",
                    "version_strings",
                    "dates",
                    "identifiers",
                    "headings",
                )
            },
            # 같은 사건을 다룬 다른 출처(클러스터). 교차 확인 근거로 사용합니다.
            "sources": [
                {
                    "name": s.get("name") or s.get("source"),
                    "authority": s.get("authority"),
                    "type": s.get("type"),
                    "url": s.get("url"),
                    "source_relation": s.get("source_relation"),
                    "supports_event": s.get("supports_event", True),
                }
                for s in note.sources
            ],
            "prior_context": [
                {"title": chunk.source_title, "excerpt": truncate(chunk.text, 400)}
                for chunk in note.context_chunks[:3]
            ],
            "rules": {
                "require_primary_source": bool(
                    self.config.get("fact_check.require_primary_source", True)
                ),
                "min_confidence": float(self.config.get("fact_check.min_confidence", 0.6)),
            },
        }

    def run(self, article: Article, note: ResearchNote) -> FactCheckReport:
        payload = self.build_payload(article, note)
        data = self.ask_json(payload, instruction=INSTRUCTION)
        evidence_by_claim = {
            str(item.get("claim") or ""): list(item.get("matches") or [])
            for item in payload["claim_evidence"]
        }

        checked: list[CheckedClaim] = []
        for raw in data.get("checked_claims") or []:
            if not isinstance(raw, dict):
                continue
            claim_text = str(raw.get("claim") or "").strip()
            if not claim_text:
                continue
            matches = evidence_by_claim.get(claim_text) or retrieve_claim_evidence(
                claim_text, article.source_metadata, top_k=1
            )
            best = matches[0] if matches else {}
            allowed_urls = {
                article.url,
                *(str(source.get("url") or "") for source in note.sources),
            }
            requested_url = str(
                raw.get("source_url") or raw.get("evidence_url") or article.url
            )
            evidence_url = requested_url if requested_url in allowed_urls else article.url
            checked.append(
                CheckedClaim(
                    claim=claim_text,
                    verdict=_verdict(raw.get("verdict")),
                    confidence=float(raw.get("confidence") or 0.0),
                    evidence_url=evidence_url,
                    source_id=article.source_id,
                    source_url=evidence_url,
                    # Trace fields come from collected chunks, not free-form output.
                    matched_chunk=str(best.get("chunk_id") or ""),
                    matched_section=str(best.get("section") or ""),
                    evidence_excerpt=str(best.get("excerpt") or ""),
                    note=str(raw.get("note") or ""),
                )
            )

        primary = bool(data.get("primary_source_found"))
        if article.trust_tier in PRIMARY_TIERS:
            primary = True
        if any(s.get("authority") == "primary" for s in note.sources):
            primary = True

        report = FactCheckReport(
            article_key=article.key,
            title=article.title,
            verdict=_verdict(data.get("verdict")),
            confidence=float(data.get("confidence") or 0.0),
            checked_claims=checked,
            unverified_claims=[str(c) for c in (data.get("unverified_claims") or []) if c],
            corrections=[str(c) for c in (data.get("corrections") or []) if c],
            primary_source_found=primary,
            needs_human_review=bool(data.get("needs_human_review", True)),
            notes=[str(n) for n in (data.get("notes") or []) if n],
            partial_reasons=[
                str(reason)
                for reason in (data.get("partial_reasons") or [])
                if str(reason) in PARTIAL_REASON_CODES
            ],
        )

        # Policy enforcement lives here, not in the prompt: a model cannot be
        # trusted to apply our publishing rules.
        min_confidence = float(self.config.get("fact_check.min_confidence", 0.6))
        if self.config.get("fact_check.require_primary_source", True) and not primary:
            report.verdict = Verdict.UNVERIFIED
            report.needs_human_review = True
            report.notes.append("1차 출처가 확인되지 않아 unverified로 강등됨")
        if report.confidence < min_confidence and report.verdict == Verdict.VERIFIED:
            report.verdict = Verdict.PARTIALLY_VERIFIED
            report.notes.append(
                f"신뢰도 {report.confidence:.2f} < 기준 {min_confidence:.2f} → partially_verified"
            )

        meaningful_sources = [
            source
            for source in note.sources
            if source.get("source_relation") != "mirror"
            and source.get("supports_event", True)
        ]
        if report.verdict == Verdict.PARTIALLY_VERIFIED:
            missing_trace = any(
                claim.verdict != Verdict.VERIFIED and not claim.matched_chunk
                for claim in report.checked_claims
            ) or bool(report.unverified_claims and not report.checked_claims)
            if article.source_metadata.get("content_truncated") and missing_trace:
                report.partial_reasons.append("source_truncated")
            if (
                len(meaningful_sources) < 2
                and "source_truncated" not in report.partial_reasons
            ):
                report.partial_reasons.append("missing_secondary_source")
            unresolved = " ".join(report.unverified_claims).lower()
            if any(word in unresolved for word in ("availability", "rollout", "available")):
                report.partial_reasons.append("ambiguous_availability")
            if "benchmark" in unresolved and any(
                word in unresolved for word in ("rank", "best", "top", "score")
            ):
                report.partial_reasons.append("unsupported_benchmark_rank")
            if any(word in unresolved for word in ("repository", "github", "repo url")):
                report.partial_reasons.append("missing_repository_link")
        report.partial_reasons = list(dict.fromkeys(report.partial_reasons))
        report.quality_metrics = {
            "partially_verified_truncation": (
                report.verdict == Verdict.PARTIALLY_VERIFIED
                and "source_truncated" in report.partial_reasons
            ),
            "partially_verified_source_scarcity": (
                report.verdict == Verdict.PARTIALLY_VERIFIED
                and "missing_secondary_source" in report.partial_reasons
            ),
            "fully_verified_single_source": (
                report.verdict == Verdict.VERIFIED and len(meaningful_sources) == 1
            ),
            "fully_verified_multi_source": (
                report.verdict == Verdict.VERIFIED and len(meaningful_sources) > 1
            ),
        }

        self.log.info(
            "[fact_check] %s -> %s (%.2f, %d claim(s))",
            truncate(article.title, 55),
            report.verdict,
            report.confidence,
            len(checked),
        )
        return report


def _verdict(value: Any) -> Verdict:
    try:
        return Verdict(str(value).strip().lower())
    except ValueError:
        return Verdict.UNVERIFIED
