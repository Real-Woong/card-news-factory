"""Instagram carousel 콘텐츠 스키마.

표준 라이브러리 dataclass 로 구현했습니다(공통 엔진과 동일한 스타일, 의존성 0).
JSON 표현은 ``carousel.json`` 과 1:1 로 대응합니다.

구조::

    Carousel
    ├── channel / brand / template / topic / headline
    ├── slides[]  : CoverSlide | ContentSlide | ClosingSlide
    ├── caption
    ├── hashtags[]
    └── meta      : 출처 URL, topic_id, fact_check verdict 등 (public 노출 안 함)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SlideType(StrEnum):
    COVER = "cover"
    CONTENT = "content"
    CLOSING = "closing"


@dataclass
class Source:
    """출처. slide 에는 ``name`` 만, URL 은 JSON/metadata 에만 남습니다."""

    name: str
    url: str = ""
    verdict: str = ""
    authority: str = ""
    source_relation: str = ""
    supports_event: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "verdict": self.verdict,
            "authority": self.authority,
            "source_relation": self.source_relation,
            "supports_event": self.supports_event,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Source:
        return cls(
            name=str(data.get("name") or "").strip(),
            url=str(data.get("url") or "").strip(),
            verdict=str(data.get("verdict") or "").strip(),
            authority=str(data.get("authority") or "").strip(),
            source_relation=str(data.get("source_relation") or "").strip(),
            supports_event=bool(data.get("supports_event", True)),
        )


@dataclass
class Slide:
    """캐러셀 한 장.

    ``type`` 에 따라 쓰이는 필드가 다릅니다.

    * cover   : ``eyebrow``, ``headline``, ``subtitle``
    * content : ``title``, ``body``
    * closing : ``title``, ``body``, ``sources``
    """

    index: int
    type: SlideType
    eyebrow: str = ""
    headline: str = ""
    subtitle: str = ""
    title: str = ""
    body: list[str] = field(default_factory=list)
    # closing variant A 의 두 번째 블록 (예: "What to watch next").
    # 다른 슬라이드 타입에서는 사용하지 않습니다.
    extra_title: str = ""
    extra_body: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    # 문구의 숫자로 만든 차트 블록(stat / compare). channel/src/visual.py 가 렌더 직전에 채운다.
    visual: dict[str, Any] = field(default_factory=dict)
    # 렌더러가 기록하는 정보(스키마 검증 대상 아님)
    render: dict[str, Any] = field(default_factory=dict)

    @property
    def text_parts(self) -> list[str]:
        """이 슬라이드에 실제로 보이는 모든 텍스트."""
        parts = [
            self.eyebrow,
            self.headline,
            self.subtitle,
            self.title,
            *self.body,
            self.extra_title,
            *self.extra_body,
        ]
        parts.extend(source.name for source in self.sources)
        return [part for part in parts if part]

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"index": self.index, "type": str(self.type)}
        if self.type is SlideType.COVER:
            data.update(
                {"eyebrow": self.eyebrow, "headline": self.headline, "subtitle": self.subtitle}
            )
        else:
            data.update({"title": self.title, "body": list(self.body)})
        if self.extra_title or self.extra_body:
            data["extra_title"] = self.extra_title
            data["extra_body"] = list(self.extra_body)
        if self.sources:
            data["sources"] = [source.to_dict() for source in self.sources]
        if self.visual:
            data["visual"] = self.visual
        if self.render:
            data["render"] = self.render
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Slide:
        raw_type = str(data.get("type") or "content").lower()
        try:
            slide_type = SlideType(raw_type)
        except ValueError:
            slide_type = SlideType.CONTENT
        body = data.get("body") or []
        if isinstance(body, str):
            body = [body]
        extra_body = data.get("extra_body") or []
        if isinstance(extra_body, str):
            extra_body = [extra_body]
        return cls(
            index=int(data.get("index") or 0),
            type=slide_type,
            eyebrow=str(data.get("eyebrow") or "").strip(),
            headline=str(data.get("headline") or "").strip(),
            subtitle=str(data.get("subtitle") or "").strip(),
            title=str(data.get("title") or "").strip(),
            body=[str(item).strip() for item in body if str(item).strip()],
            extra_title=str(data.get("extra_title") or "").strip(),
            extra_body=[str(item).strip() for item in extra_body if str(item).strip()],
            sources=[Source.from_dict(s) for s in (data.get("sources") or []) if isinstance(s, dict)],
            visual=dict(data.get("visual") or {}),
            render=dict(data.get("render") or {}),
        )


@dataclass
class Carousel:
    """Instagram 캐러셀 한 건 = 한 포스트."""

    template: str
    topic: str
    headline: str
    slides: list[Slide] = field(default_factory=list)
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    channel: str = "instagram"
    brand: str = "R_A_Glance"
    meta: dict[str, Any] = field(default_factory=dict)

    # -- 조회 ---------------------------------------------------------------
    @property
    def slide_count(self) -> int:
        return len(self.slides)

    @property
    def cover(self) -> Slide | None:
        return next((s for s in self.slides if s.type is SlideType.COVER), None)

    @property
    def closing(self) -> Slide | None:
        return next((s for s in self.slides if s.type is SlideType.CLOSING), None)

    @property
    def sources(self) -> list[Source]:
        """closing slide 의 출처(없으면 전체 슬라이드에서 수집)."""
        closing = self.closing
        if closing and closing.sources:
            return closing.sources
        collected: list[Source] = []
        seen: set[str] = set()
        for slide in self.slides:
            for source in slide.sources:
                key = source.url or source.name
                if key not in seen:
                    seen.add(key)
                    collected.append(source)
        return collected

    def all_text(self) -> str:
        """슬라이드 + 캡션의 모든 공개 텍스트 (내부 표현 검사용)."""
        chunks = [part for slide in self.slides for part in slide.text_parts]
        chunks.append(self.caption)
        chunks.extend(self.hashtags)
        return "\n".join(chunk for chunk in chunks if chunk)

    # -- 직렬화 -------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "brand": self.brand,
            "template": self.template,
            "topic": self.topic,
            "headline": self.headline,
            "slides": [slide.to_dict() for slide in self.slides],
            "caption": self.caption,
            "hashtags": list(self.hashtags),
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Carousel:
        return cls(
            channel=str(data.get("channel") or "instagram"),
            brand=str(data.get("brand") or "R_A_Glance"),
            template=str(data.get("template") or ""),
            topic=str(data.get("topic") or ""),
            headline=str(data.get("headline") or "").strip(),
            slides=[Slide.from_dict(s) for s in (data.get("slides") or []) if isinstance(s, dict)],
            caption=str(data.get("caption") or "").strip(),
            hashtags=[str(h).strip() for h in (data.get("hashtags") or []) if str(h).strip()],
            meta=dict(data.get("meta") or {}),
        )


# --------------------------------------------------------------------------- #
# 공통 엔진 → 채널 입력 계약
# --------------------------------------------------------------------------- #
@dataclass
class VerifiedTopic:
    """공통 엔진이 검증까지 끝낸 토픽 하나.

    이것이 shared engine 과 채널 레이어 사이의 **유일한** 인터페이스입니다.
    채널은 엔진의 내부 모델(Article, ResearchNote …)을 import 하지 않습니다.
    """

    topic_id: str
    category: str
    headline: str
    research: dict[str, Any] = field(default_factory=dict)
    fact_check: dict[str, Any] = field(default_factory=dict)
    sources: list[Source] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        return str(self.fact_check.get("verdict") or "").lower()

    @property
    def what_changed(self) -> str:
        return str(self.research.get("what_changed") or "")

    @property
    def why_it_matters(self) -> str:
        return str(self.research.get("why_it_matters") or "")

    @property
    def key_facts(self) -> list[str]:
        facts: list[str] = []
        for fact in self.research.get("key_facts") or []:
            if isinstance(fact, dict):
                text = str(fact.get("text") or "").strip()
            else:
                text = str(fact).strip()
            if text:
                facts.append(text)
        return facts

    @property
    def technical_details(self) -> list[str]:
        return [str(d) for d in (self.research.get("technical_details") or []) if str(d).strip()]

    @property
    def unverified_claims(self) -> list[str]:
        return [
            str(c) for c in (self.fact_check.get("unverified_claims") or []) if str(c).strip()
        ]

    @property
    def open_questions(self) -> list[str]:
        """원문이 답하지 않은 항목. closing 의 "what to watch next" 재료로 씁니다."""
        return [str(q) for q in (self.research.get("open_questions") or []) if str(q).strip()]

    @property
    def primary_source(self) -> Source | None:
        return self.sources[0] if self.sources else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "category": self.category,
            "headline": self.headline,
            "research": self.research,
            "fact_check": self.fact_check,
            "sources": [s.to_dict() for s in self.sources],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VerifiedTopic:
        return cls(
            topic_id=str(data.get("topic_id") or "").strip(),
            category=str(data.get("category") or "").strip(),
            headline=str(data.get("headline") or "").strip(),
            research=dict(data.get("research") or {}),
            fact_check=dict(data.get("fact_check") or {}),
            sources=[
                Source.from_dict(s) for s in (data.get("sources") or []) if isinstance(s, dict)
            ],
        )
