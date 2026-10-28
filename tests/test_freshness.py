"""시점 지난 소식 감지: 지난 날짜 + 미래·마감 표현만 지적한다."""

from __future__ import annotations

import copy
from datetime import date

import pytest

from channel.src.config import load_channel_config
from channel.src.freshness import extract_dates, find_stale_dates
from channel.src.validator import CarouselValidator
from tests.test_ra_glance import make_carousel

TODAY = date(2026, 10, 18)


@pytest.fixture(scope="module")
def channel():
    return load_channel_config()


def carousel_with(line: str):
    carousel = make_carousel()
    carousel.slides[2].body[0] = line
    return carousel


def test_extracts_iso_korean_and_english_dates_with_year_only():
    assert extract_dates("2026-10-06 13:53:36 UTC") == [date(2026, 10, 6)]
    assert extract_dates("2026년 9월 24일 공개") == [date(2026, 9, 24)]
    assert extract_dates("Oct 6, 2026 deadline") == [date(2026, 10, 6)]
    assert extract_dates("10월 6일 마감") == []  # 연도 없음: 해석이 모호해 보지 않는다
    assert extract_dates("2026-13-40") == []  # 존재하지 않는 날짜


def test_past_deadline_phrased_as_upcoming_is_flagged():
    items = find_stale_dates(carousel_with("2026-10-06 마감 전 업그레이드 필요"), today=TODAY)

    assert len(items) == 1
    assert items[0].slide == 3 and items[0].days_ago == 12


def test_past_date_stated_as_fact_is_not_flagged():
    assert find_stale_dates(carousel_with("2026년 9월 24일 Hugging Face 공개"), today=TODAY) == []


def test_future_deadline_is_not_flagged():
    assert find_stale_dates(carousel_with("2026-11-06 마감 전 업그레이드 필요"), today=TODAY) == []


def test_the_deadline_day_itself_is_not_yet_stale():
    assert find_stale_dates(carousel_with("2026-10-18 마감 전 업그레이드"), today=TODAY) == []


def test_stale_deadline_blocks_publishing(channel):
    report = CarouselValidator(channel).validate(
        carousel_with("2026-10-06 마감 전 업그레이드 필요"), today=TODAY
    )
    finding = next(f for f in report.findings if f.code == "stale_deadline")

    assert finding.severity == "block" and finding.where == "slide 3"
    assert not report.publishable


def test_clean_carousel_has_no_stale_finding(channel):
    report = CarouselValidator(channel).validate(make_carousel(), today=TODAY)
    assert "stale_deadline" not in {f.code for f in report.findings}
    assert "freshness" in report.checked


def test_disabled_freshness_stays_silent(channel):
    cfg = copy.deepcopy(channel)
    cfg.content.setdefault("freshness", {})["enabled"] = False
    report = CarouselValidator(cfg).validate(
        carousel_with("2026-10-06 마감 전 업그레이드 필요"), today=TODAY
    )
    assert "freshness" not in report.checked
