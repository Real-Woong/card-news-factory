# Architecture

Two layers with a file boundary between them:

```
engine (src/)                                   channel (channel/)
collect → normalize → dedupe → cluster          plan → write → edit → ground → repair
   → RAG → rank → research → fact_check            → review → render → validate → gate → publish
            │                                                       ▲
            └── outputs/drafts/<date>/research.json ────────────────┘
```

The dependency is one way: `channel` imports `src`, never the reverse (tested).

## Engine

Collects from RSS, arXiv and GitHub releases, de-duplicates and clusters items into topics, adds
context from a local RAG store, ranks them and researches each selected topic. `fact_check`
produces a verdict (`verified`, `partially_verified`, ...) with sources and unverified claims.
The output is `research.json`: `key_facts`, `what_changed`, `why_it_matters`, `sources`, `verdict`.

## Channel

| Step | Who | Output |
|---|---|---|
| Material gate | code | Skips topics with too little verified material (no filler is invented) |
| Plan | LLM | Number of slides (3/5/7) and the narrative |
| Write, edit | LLM | `carousel.json`: slide text only, no design information |
| Ground | LLM judge + code | Per-sentence verdicts re-validated by code (cited evidence must exist) |
| Repair | LLM + code | Narrow weak sentences; code rejects new numbers, URLs, internal wording |
| Review | LLM + code | Independent check for overclaims; severity derived from issue kind by code |
| Render | code + Chrome | Layout/theme chosen by rules, HTML templates, 1080×1350 PNG |
| Validate | code | Text budgets, duplicates, tone, freshness, contrast, uniformity |
| Gate | code | Approve only if every check passes; otherwise hold with reasons |
| Publish | code | Host JPEGs, re-check guards, post through the Instagram API |

Design rules: the LLM only writes text; uncertain means hold; a failed LLM never falls back to a mock;
approvals are bound to a hash of the public text, so edited content needs a new approval.

## Configuration

- `channel/config/brand.yaml`: identity, palette, typography, logos, layout geometry
- `channel/config/content.yaml`: budgets, layout and visual rules, glossary, gate thresholds
- `channel/config/instagram.yaml`: rhythm, switches, notifications
- `channel/prompts/`: one instruction file per LLM step

## Extending

- **A new category:** add it to `config/topics.yaml`, `config/sources.yaml` and `knowledge/<id>/`, give it a color in
  `brand.yaml` (`colors.accent`) and a cover logo, and add a hashtag base in `instagram.yaml`.
- **A new LLM provider:** implement `LLMClient.complete` in `src/agents/llm.py`.
- **A new layout:** add a rule in `channel/src/layout.py`, CSS in `channel/templates/instagram/shared/base.css`, and a test.
