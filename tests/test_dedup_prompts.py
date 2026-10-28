"""Prompts must keep telling the model not to restate facts across slides.

Prompt text is behaviour here. Two live failures drove it: the closing slide
paraphrasing a body bullet (go-ethereum), and "Why it matters" re-listing the
"What changed" bullets (ollama), with the duplicate repair merely rewording both.
These checks pin the instructions that prevent it; they cannot prove model behaviour.
"""

from __future__ import annotations

from pathlib import Path

from channel.src.editor import REPAIR_INSTRUCTION

PROMPTS = Path(__file__).resolve().parents[1] / "channel" / "prompts"


def read(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def test_writer_closing_is_synthesis_not_restatement():
    text = read("instagram_writer.md")
    closing = text.split("### closing", 1)[1].split("## 캡션", 1)[0]
    assert "요약이 아니라 종합" in closing
    assert "하한은 1줄" in closing
    assert "형식 설명용" in closing


def test_writer_no_longer_asks_for_constraint_or_action_restating():
    # The old wording invited the model to re-list body facts as a "next action".
    assert "이후 줄은 제약이나" not in read("instagram_writer.md")


def test_editor_has_closing_specific_repair_rules():
    text = read("instagram_editor.md")
    assert "closing(Takeaway)일 때" in text
    assert "1줄이면 충분" in text
    assert "형식 설명용" in text


def test_repair_instruction_covers_closing_slide():
    assert "closing" in REPAIR_INSTRUCTION
    assert "do not paraphrase" in REPAIR_INSTRUCTION
    assert "One closing line is enough" in REPAIR_INSTRUCTION
    # Existing contract must survive the addition.
    assert "Do NOT invent facts" in REPAIR_INSTRUCTION
    assert '"slides"' in REPAIR_INSTRUCTION


def test_writer_distinguishes_slide_roles():
    section = read("instagram_writer.md").split("## 슬라이드 역할 구분", 1)[1].split("## 금지", 1)[0]
    assert "Why it matters" in section
    assert "변화를 요약하는 장이 아닙니다" in section
    assert "상한을 지킵니다" in section
    assert "형식 설명용" in section


def test_writer_role_example_is_not_a_test_topic_fact():
    # Examples can leak into unrelated posts; keep them off real source topics.
    section = read("instagram_writer.md").split("## 슬라이드 역할 구분", 1)[1].split("## 금지", 1)[0]
    for leaked in ("MLX", "Ollama", "Sepolia", "go-ethereum", "Holesky"):
        assert leaked not in section


def test_editor_has_body_repair_rules():
    text = read("instagram_editor.md")
    assert 'Why it matters' in text
    assert "표현만 바꾼 것은 수정이 아닙니다" in text
    assert "상한을 넘으면" in text


def test_repair_instruction_rejects_rewording_and_caps_items():
    assert "NOT a fix" in REPAIR_INSTRUCTION
    assert "Why it matters" in REPAIR_INSTRUCTION
    assert "maximum item count" in REPAIR_INSTRUCTION


def test_impact_must_come_from_material_not_inference():
    # Asking for "consequences" invites the model to invent behaviour (a fabricated
    # fallback path passed every gate in a live run), so each prompt caps it.
    writer = read("instagram_writer.md").split("## 슬라이드 역할 구분", 1)[1].split("## 금지", 1)[0]
    assert "영향은 재료가 말한 것만 씁니다" in writer
    assert "추론해서 사실처럼 쓰지 않습니다" in writer
    assert "명시한 것만" in read("instagram_editor.md")
    assert "never infer behaviour" in REPAIR_INSTRUCTION
