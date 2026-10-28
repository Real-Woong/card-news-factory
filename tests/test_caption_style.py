"""캡션 쉬운 말 규칙(channel/src/caption_style.py)과 작성 단계 재시도 테스트."""

from __future__ import annotations

import json

import pytest

from src.agents.llm import LLMClient, LLMRequest, LLMResponse

from channel.src.caption_style import caption_problems, known_names
from channel.src.config import load_channel_config
from channel.src.schema import Carousel, Slide, SlideType, Source, VerifiedTopic
from channel.src.writer import InstagramCaptionWriter

# 2026-10-28-3 에 실제로 게시된 캡션(개발자용 표현이 그대로 있던 사례)
POSTED = (
    "문서이해 OCR MiniCPM-OCR, visual-retrieval UltraVBert, MoE 모델 Flint가 커스텀 통합 코드 없이 "
    "바로 쓸 수 있게 들어왔다.\n"
    "Kyutai의 100M Pocket TTS는 CPU에서 real-time보다 빠르게 구동되고, Gradium Phonon TTS는 "
    "step마다 연속 audio latent를 방출한다.\n"
    "QuantizedCache에는 fp8 백엔드가 더해져 KV state를 torch.float8_e4m3fn로 저장하고 "
    "KV-cache 메모리를 줄인다."
)
PLAIN = (
    "새 소식이에요. 글자를 읽어 주는 모델과 음성을 만드는 모델이 한꺼번에 추가됐어요.\n"
    "Pocket TTS는 그래픽카드 없이 CPU만으로도 실시간보다 빠르게 말해요.\n"
    "메모리를 덜 쓰는 저장 방식이 더해져 긴 대화도 가볍게 처리해요."
)


@pytest.fixture
def channel():
    return load_channel_config()


NAMES = known_names("transformers v5.21.0 신규 모델 6종", "Hugging Face Transformers Releases")


class TestCaptionProblems:
    def test_the_real_posted_caption_is_flagged(self, channel):
        problems = caption_problems(POSTED, channel, NAMES)
        text = " ".join(problems)
        assert "torch.float8_e4m3fn" in text  # 코드 식별자
        assert any("영어가 너무 많음" in p for p in problems)

    def test_plain_korean_with_product_names_passes(self, channel):
        assert caption_problems(PLAIN, channel, NAMES) == []

    def test_code_identifiers_are_flagged_but_versions_are_not(self, channel):
        assert caption_problems("v5.21.0 버전이 나왔어요", channel) == []
        assert caption_problems("max_image_tiles 옵션이 생겼어요", channel)
        assert caption_problems("config.json 파일을 읽어요", channel)

    def test_glossary_terms_are_flagged_by_name(self, channel):
        problems = caption_problems("이제 allowlist 로 접근을 통제해요", channel)
        assert any("allowlist" in p for p in problems)

    def test_acronyms_and_known_product_names_do_not_count_as_english(self, channel):
        text = "Kyutai의 Pocket TTS가 CPU에서 돌아요. API 없이도 쓸 수 있어요."
        assert caption_problems(text, channel, known_names("Kyutai Pocket TTS")) == []

    def test_disabled(self, channel):
        channel.content["caption_style"]["enabled"] = False
        assert caption_problems(POSTED, channel, NAMES) == []

    def test_empty_caption(self, channel):
        assert caption_problems("", channel) == []


class FakeLLM(LLMClient):
    provider = "fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        # 마지막 답은 계속 반복한다(파싱 실패 시 재시도해도 같은 응답이 오는 상황)
        text = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return LLMResponse(text=text, provider=self.provider)


def reply(body, hook="한 줄 소개", take="한 줄 정리"):
    return json.dumps({"hook": hook, "body": body, "takeaway": take,
                       "hashtags": ["#AI"]}, ensure_ascii=False)


def carousel():
    slides = [Slide(index=1, type=SlideType.COVER, headline="transformers v5.21.0 신규 모델 6종",
                    subtitle="OCR·음성합성 기본 지원"),
              Slide(index=2, type=SlideType.CONTENT, title="제목", body=["가", "나"]),
              Slide(index=3, type=SlideType.CLOSING, title="Takeaway", body=["결론"])]
    return Carousel(template="news_short", topic="AI", headline="transformers v5.21.0",
                    slides=slides, meta={})


def topic():
    return VerifiedTopic(topic_id="t", category="ai", headline="huggingface/transformers",
                         research={"key_facts": [{"text": "fact"}], "what_changed": "x",
                                   "why_it_matters": "y"})


class TestWriterRetry:
    def writer(self, config, channel, answers):
        llm = FakeLLM(answers)
        return InstagramCaptionWriter(config=config, llm=llm, channel=channel), llm

    def test_hard_caption_is_rewritten_once_with_the_concrete_problems(self, config, channel):
        hard = reply(POSTED.split("\n"))
        easy = reply(PLAIN.split("\n"))
        writer, llm = self.writer(config, channel, [hard, easy])
        caption, _ = writer.run(topic(), carousel())
        assert "torch.float8" not in caption and "그래픽카드" in caption
        assert len(llm.requests) == 2
        fix = llm.requests[1].payload["fix"]
        assert any("torch.float8_e4m3fn" in p for p in fix["problems"])
        assert fix["previous"]["body"] and "plain Korean" in llm.requests[1].user

    def test_plain_caption_costs_no_extra_call(self, config, channel):
        writer, llm = self.writer(config, channel, [reply(PLAIN.split("\n"))])
        writer.run(topic(), carousel())
        assert len(llm.requests) == 1

    def test_a_rewrite_that_is_not_better_is_discarded(self, config, channel):
        hard = reply(POSTED.split("\n"))
        worse = reply(POSTED.split("\n") + ["max_image_tiles 옵션"])
        writer, llm = self.writer(config, channel, [hard, worse])
        caption, _ = writer.run(topic(), carousel())
        assert "torch.float8_e4m3fn" in caption and "max_image_tiles" not in caption
        assert len(llm.requests) == 2  # 개선되지 않으면 더 시도하지 않는다

    def test_a_failed_rewrite_keeps_the_first_caption(self, config, channel):
        writer, _ = self.writer(config, channel, [reply(POSTED.split("\n")), "json 아님"])
        caption, _ = writer.run(topic(), carousel())
        assert "torch.float8_e4m3fn" in caption

    def test_retry_can_be_turned_off(self, config, channel):
        channel.content["caption_style"]["retry"] = 0
        writer, llm = self.writer(config, channel, [reply(POSTED.split("\n"))])
        writer.run(topic(), carousel())
        assert len(llm.requests) == 1

    def test_facts_are_not_added_by_the_pipeline(self, config, channel):
        """재작성은 문구만 바꾼다: 출처·해시태그 줄은 코드가 그대로 붙인다."""
        carousel_ = carousel()
        carousel_.slides[-1].sources = [Source("Hugging Face Transformers Releases")]
        writer, _ = self.writer(config, channel, [reply(POSTED.split("\n")), reply(PLAIN.split("\n"))])
        caption, tags = writer.run(topic(), carousel_)
        assert "Sources: Hugging Face Transformers Releases" in caption and "#AI" in tags


class TestValidatorWarning:
    def test_hard_caption_warns_without_blocking(self, channel):
        from channel.src.validator import CarouselValidator, ValidationReport

        c = carousel()
        c.caption = POSTED
        report = ValidationReport()
        CarouselValidator(channel)._check_caption_plain(c, report)
        assert report.findings and all(f.code == "caption_jargon" and f.severity == "warn"
                                        for f in report.findings)
        c.caption = PLAIN
        clean = ValidationReport()
        CarouselValidator(channel)._check_caption_plain(c, clean)
        assert clean.findings == []
