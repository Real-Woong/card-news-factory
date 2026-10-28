"""주간 운영(channel/src/weekly.py) 테스트: 월요일에 2개를 정하고 수·일에 하나씩 올린다."""

from __future__ import annotations

import json
import plistlib
from datetime import date, timedelta
from pathlib import Path

from channel.src.config import load_channel_config
from channel.src.schema import VerifiedTopic
from channel.src.weekly import (
    build_list,
    order_topics,
    research_age_days,
    save_list,
    summary,
)

ROOT = Path(__file__).resolve().parents[1]


def topic(tid, category="ai", verdict="verified"):
    return VerifiedTopic(topic_id=tid, category=category, headline=f"제목 {tid}",
                         fact_check={"verdict": verdict})


class TestOrder:
    def test_only_verified_topics_are_used(self):
        topics = [topic("a"), topic("b", verdict="partially_verified"), topic("c")]
        assert [t.topic_id for t in order_topics(topics)] == ["a", "c"]

    def test_second_slot_prefers_a_different_category(self):
        topics = [topic("a", "ai"), topic("b", "ai"), topic("c", "blockchain"), topic("d", "ai")]
        assert [t.topic_id for t in order_topics(topics)][:2] == ["a", "c"]

    def test_falls_back_to_engine_order_when_everything_is_one_category(self):
        topics = [topic(x) for x in "abcd"]
        assert [t.topic_id for t in order_topics(topics)] == list("abcd")

    def test_order_is_deterministic_and_keeps_every_topic(self):
        topics = [topic("a", "ai"), topic("b", "blockchain"), topic("c", "ai"), topic("d", "ai")]
        assert order_topics(topics) == order_topics(topics)
        assert sorted(t.topic_id for t in order_topics(topics)) == list("abcd")


class TestWeeklyList:
    def test_two_slots_and_reserves(self):
        topics = [topic(x, "ai" if i % 2 else "blockchain") for i, x in enumerate("abcdef")]
        payload = build_list(topics)
        assert len(payload["slots"]) == 2 and len(payload["reserves"]) == 2
        listed = [s["topic_id"] for s in payload["slots"] + payload["reserves"]]
        assert len(set(listed)) == 4

    def test_topics_with_recent_candidates_are_left_out(self):
        payload = build_list([topic("a"), topic("b"), topic("c")], skip_ids={"a"})
        assert [s["topic_id"] for s in payload["slots"]] == ["b", "c"]

    def test_empty_week_is_reported(self):
        payload = build_list([topic("a", verdict="partially_verified")])
        assert payload["slots"] == [] and "없습니다" in summary(payload)

    def test_summary_names_the_slots_and_save_writes_json(self, tmp_path):
        payload = build_list([topic("a"), topic("b"), topic("c")])
        assert "제목 a" in summary(payload) and "수·일" in summary(payload)
        path = save_list(payload, tmp_path)
        assert json.loads(path.read_text(encoding="utf-8"))["slots"][0]["topic_id"] == "a"


class TestWedAndSunPickDifferentTopics:
    def test_the_second_run_continues_where_the_first_left_off(self):
        """수요일에 1번을 만들면(후보 생성) 일요일엔 그 주제를 건너뛰어 2번이 된다."""
        topics = [topic("a", "ai"), topic("b", "blockchain"), topic("c", "ai")]
        wednesday = build_list(topics)["slots"][0]["topic_id"]
        sunday = build_list(topics, skip_ids={wednesday})["slots"][0]["topic_id"]
        assert wednesday == "a" and sunday == "b"


class TestResearchAge:
    def test_age_comes_from_the_folder_date(self, tmp_path):
        folder = tmp_path / "2026-10-26"
        folder.mkdir()
        file = folder / "research.json"
        file.write_text("{}", encoding="utf-8")
        assert research_age_days(file, date(2026, 10, 28)) == 2
        assert research_age_days(file, date(2026, 11, 5)) == 10

    def test_non_dated_folder_falls_back_to_mtime(self, tmp_path):
        file = tmp_path / "research.json"
        file.write_text("{}", encoding="utf-8")
        assert research_age_days(file, date.today() + timedelta(days=3)) == 3

    def test_default_limit_covers_monday_to_the_following_sunday(self):
        assert int(load_channel_config().get("instagram.posting.max_research_age_days")) >= 6


class TestSchedule:
    def load(self, name):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8").replace("{{PROJECT_DIR}}", "/x")
        return plistlib.loads(text.encode("utf-8"))

    def test_engine_runs_monday_and_the_channel_runs_wednesday_and_sunday(self):
        engine = self.load("com.contentops.pipeline.plist")
        channel = self.load("com.contentops.channel.plist")
        assert [(i["Weekday"], i["Hour"], i["Minute"]) for i in engine["StartCalendarInterval"]] == [(1, 8, 30)]
        assert sorted(i["Weekday"] for i in channel["StartCalendarInterval"]) == [0, 3]
        # 인터프리터를 /bin/bash 로 못 박는다(전체 디스크 접근 권한을 이 프로그램에 부여하기 위해)
        assert engine["ProgramArguments"][0] == "/bin/bash"
        assert channel["ProgramArguments"][0] == "/bin/bash"
        assert engine["ProgramArguments"][-1].endswith("run_pipeline.sh")
        assert channel["ProgramArguments"][-1].endswith("run_channel.sh")
        assert engine["Label"] != channel["Label"]

    def test_a_failing_engine_run_is_announced_not_silent(self):
        text = (ROOT / "scripts" / "run_pipeline.sh").read_text(encoding="utf-8")
        assert '|| status=$?' in text  # set -e 아래에서 실패가 조용히 스크립트를 끝내지 않는다
        assert "channel.src.notify" in text and "엔진 실패" in text

    def test_engine_job_no_longer_chains_the_channel(self):
        text = (ROOT / "scripts" / "run_pipeline.sh").read_text(encoding="utf-8")
        assert "channel.src.weekly" in text and 'run_channel.sh" ||' not in text

    def test_config_documents_the_same_days(self):
        assert load_channel_config().get("instagram.posting.days") == ["wednesday", "sunday"]
