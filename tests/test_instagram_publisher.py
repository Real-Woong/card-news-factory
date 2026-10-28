"""Phase 3 publisher approval guards and mocked Meta request flow."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from channel.src.approval import content_hash, decide, init_record
from channel.src.config import load_channel_config
from channel.src.publisher import (
    InstagramGraphClient,
    PublishingError,
    preflight,
    publish_output,
)
import channel.src.publisher as publisher_module


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self.payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


@pytest.fixture
def publishing_config():
    config = copy.deepcopy(load_channel_config())
    config.instagram["channel"]["phase"] = 3
    config.instagram["channel"]["publish"] = True
    config.instagram["publishing"]["require_phase3_ready"] = False
    config.instagram["publishing"]["container_poll_interval_seconds"] = 0
    return config


def make_output(tmp_path: Path, *, approve: bool = True) -> tuple[Path, Path]:
    directory = tmp_path / "candidate-1"
    slides = directory / "slides"
    slides.mkdir(parents=True)
    payload = {
        "channel": "instagram",
        "brand": "R_A_Glance",
        "template": "news_short",
        "topic": "테스트",
        "headline": "테스트 캐러셀",
        "slides": [
            {"index": 1, "type": "cover", "headline": "표지"},
            {"index": 2, "type": "content", "title": "근거", "body": ["사실"]},
            {
                "index": 3,
                "type": "closing",
                "title": "Takeaway",
                "body": ["결론"],
            },
        ],
        "caption": "검증된 캡션",
        "hashtags": ["#AI", "#RAGlance"],
    }
    (directory / "carousel.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    (directory / "review.md").write_text(
        "publishable = true, score = 0.91", encoding="utf-8"
    )
    for index in range(1, 4):
        (slides / f"{index:02d}.png").write_bytes(b"fixture")
    init_record(directory, publishable=True, score=0.91, warnings=[])
    if approve:
        decide(directory, "approved", reviewer="jin")
    manifest = directory / "publishing_media.json"
    manifest.write_text(
        json.dumps(
            {
                "content_hash": content_hash(directory),
                "image_urls": [
                    f"https://cdn.example/candidate-1/{index:02d}.jpg"
                    for index in range(1, 4)
                ],
            }
        ),
        encoding="utf-8",
    )
    return directory, manifest


ENV = {"INSTAGRAM_ACCESS_TOKEN": "secret-token", "INSTAGRAM_USER_ID": "1784"}


def test_publisher_cli_loads_repository_dotenv(monkeypatch):
    loaded = False

    def fake_load_dotenv_file():
        nonlocal loaded
        loaded = True

    monkeypatch.setattr(publisher_module, "load_dotenv_file", fake_load_dotenv_file)
    monkeypatch.setattr(
        publisher_module,
        "resolve_output",
        lambda _output_id: (_ for _ in ()).throw(PublishingError("stop")),
    )

    assert publisher_module.main(["preflight", "candidate-1"]) == 2
    assert loaded is True


def test_preflight_accepts_exact_approved_content(tmp_path, publishing_config):
    directory, manifest = make_output(tmp_path)
    result = preflight(
        directory, publishing_config, manifest_path=manifest, environ=ENV
    )
    assert result.eligible is True
    assert result.blockers == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("pending", "approval_not_approved"),
        ("unpublishable", "not_publishable"),
        ("changed", "approval_invalid_for_current_content"),
    ],
)
def test_approval_guards_cannot_be_bypassed(
    tmp_path, publishing_config, mutation, expected
):
    directory, manifest = make_output(tmp_path, approve=mutation != "pending")
    if mutation == "unpublishable":
        record = json.loads((directory / "approval.json").read_text())
        record["publishable"] = False
        (directory / "approval.json").write_text(json.dumps(record))
    if mutation == "changed":
        payload = json.loads((directory / "carousel.json").read_text())
        payload["caption"] = "승인 이후 변경"
        (directory / "carousel.json").write_text(json.dumps(payload))
    result = preflight(
        directory, publishing_config, manifest_path=manifest, environ=ENV
    )
    assert result.eligible is False
    assert expected in result.blockers


def test_disabled_config_blocks_before_network(tmp_path, publishing_config):
    directory, manifest = make_output(tmp_path)
    publishing_config.instagram["channel"]["publish"] = False
    result = preflight(
        directory, publishing_config, manifest_path=manifest, environ=ENV
    )
    assert "channel_publish_disabled" in result.blockers


def test_phase3_approval_target_is_not_bypassable_by_default(
    tmp_path, publishing_config
):
    directory, manifest = make_output(tmp_path)
    publishing_config.instagram["publishing"]["require_phase3_ready"] = True
    result = preflight(
        directory, publishing_config, manifest_path=manifest, environ=ENV
    )
    assert "phase3_approval_target_not_met" in result.blockers


def test_manifest_must_match_hash_count_and_public_jpeg(tmp_path, publishing_config):
    directory, manifest = make_output(tmp_path)
    manifest.write_text(
        json.dumps(
            {
                "content_hash": "stale",
                "image_urls": ["http://localhost/01.png"],
            }
        )
    )
    result = preflight(
        directory, publishing_config, manifest_path=manifest, environ=ENV
    )
    assert "media_manifest_content_hash_mismatch" in result.blockers
    assert "media_count_mismatch" in result.blockers
    assert "media_url_not_public_https_jpeg:1" in result.blockers


def test_missing_credentials_block_preflight(tmp_path, publishing_config):
    directory, manifest = make_output(tmp_path)
    result = preflight(
        directory, publishing_config, manifest_path=manifest, environ={}
    )
    assert "missing_environment:INSTAGRAM_ACCESS_TOKEN" in result.blockers
    assert "missing_environment:INSTAGRAM_USER_ID" in result.blockers


def test_mocked_carousel_publish_sequence_and_audit(tmp_path, publishing_config):
    directory, manifest = make_output(tmp_path)
    responses = []
    for index in range(1, 4):
        responses.extend(
            [FakeResponse({"id": f"child-{index}"}), FakeResponse({"status_code": "FINISHED"})]
        )
    responses.extend(
        [
            FakeResponse({"id": "carousel-1"}),
            FakeResponse({"status_code": "FINISHED"}),
            FakeResponse({"id": "media-1"}),
            FakeResponse({"permalink": "https://www.instagram.com/p/ABC123/"}),
        ]
    )
    session = FakeSession(responses)
    client = InstagramGraphClient(
        access_token=ENV["INSTAGRAM_ACCESS_TOKEN"],
        instagram_user_id=ENV["INSTAGRAM_USER_ID"],
        session=session,
    )

    result = publish_output(
        directory,
        publishing_config,
        manifest_path=manifest,
        environ=ENV,
        client=client,
        sleeper=lambda _seconds: None,
    )

    assert result.ok is True
    assert result.external_url == "https://www.instagram.com/p/ABC123/"  # 알림에 실을 링크
    assert result.external_id == "media-1"
    assert result.child_container_ids == ["child-1", "child-2", "child-3"]
    publish_call = session.calls[-2]  # 마지막은 게시 뒤의 읽기 전용 링크 조회(permalink)
    assert publish_call[1].endswith("/1784/media_publish")
    assert publish_call[2]["data"] == {"creation_id": "carousel-1"}
    assert session.calls[-1][0] == "GET" and session.calls[-1][1].endswith("/media-1")
    assert all(
        call[2]["headers"]["Authorization"] == "Bearer secret-token"
        for call in session.calls
    )
    audit = json.loads((directory / "publishing.json").read_text())
    assert audit["status"] == "published"
    assert "secret-token" not in json.dumps(audit)


def test_successful_publish_is_idempotently_blocked(tmp_path, publishing_config):
    directory, manifest = make_output(tmp_path)
    (directory / "publishing.json").write_text(
        json.dumps(
            {
                "output_id": directory.name,
                "ok": True,
                "status": "published",
                "content_hash": content_hash(directory),
                "started_at": "2026-09-28T00:00:00Z",
                "external_id": "existing-media",
            }
        )
    )
    result = preflight(
        directory, publishing_config, manifest_path=manifest, environ=ENV
    )
    assert "already_published" in result.blockers


def test_remote_failure_is_persisted_without_token(tmp_path, publishing_config):
    directory, manifest = make_output(tmp_path)
    session = FakeSession(
        [
            FakeResponse(
                {
                    "error": {
                        "message": "media rejected",
                        "type": "IGApiException",
                        "code": 9004,
                    }
                },
                status_code=400,
            )
        ]
    )
    client = InstagramGraphClient(
        access_token=ENV["INSTAGRAM_ACCESS_TOKEN"],
        instagram_user_id=ENV["INSTAGRAM_USER_ID"],
        session=session,
    )

    with pytest.raises(PublishingError):
        publish_output(
            directory,
            publishing_config,
            manifest_path=manifest,
            environ=ENV,
            client=client,
            sleeper=lambda _seconds: None,
        )

    audit = (directory / "publishing.json").read_text()
    assert '"status": "failed"' in audit
    assert "secret-token" not in audit
    retry = preflight(
        directory, publishing_config, manifest_path=manifest, environ=ENV
    )
    assert "previous_publish_attempt_requires_review" in retry.blockers


# --------------------------------------------------------------------------- #
# 게시 직전 재검증: 승인 뒤 시간이 흘러 낡은 소식이 되는 경우
# --------------------------------------------------------------------------- #
def test_preflight_passes_fresh_recent_approval(tmp_path, publishing_config):
    directory, manifest = make_output(tmp_path)
    check = preflight(directory, publishing_config, manifest_path=manifest, environ=ENV)

    assert check.eligible and check.blockers == []


def test_preflight_blocks_deadline_that_passed_after_approval(tmp_path, publishing_config):
    from datetime import date

    directory, manifest = make_output(tmp_path)
    payload = json.loads((directory / "carousel.json").read_text(encoding="utf-8"))
    payload["slides"][1]["body"] = ["2026-10-06 마감 전 업그레이드 필요"]
    (directory / "carousel.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    decide(directory, "approved", reviewer="jin")  # 이 문구로 승인
    manifest.write_text(
        json.dumps({"content_hash": content_hash(directory), "image_urls": [
            f"https://cdn.example/candidate-1/{i:02d}.jpg" for i in range(1, 4)]}),
        encoding="utf-8",
    )

    before = preflight(directory, publishing_config, manifest_path=manifest,
                       environ=ENV, today=date(2026, 10, 5))
    after = preflight(directory, publishing_config, manifest_path=manifest,
                      environ=ENV, today=date(2026, 10, 18))

    assert "stale_content" not in before.blockers
    assert "stale_content" in after.blockers and not after.eligible


def test_preflight_blocks_approval_older_than_limit(tmp_path, publishing_config):
    from datetime import timedelta

    from src.core.text import utcnow

    directory, manifest = make_output(tmp_path)
    kwargs = dict(manifest_path=manifest, environ=ENV)

    fresh = preflight(directory, publishing_config, now=utcnow() + timedelta(days=2), **kwargs)
    old = preflight(directory, publishing_config, now=utcnow() + timedelta(days=4), **kwargs)

    assert "approval_too_old" not in fresh.blockers
    assert "approval_too_old" in old.blockers


def test_approval_age_limit_can_be_disabled(tmp_path, publishing_config):
    from datetime import timedelta

    from src.core.text import utcnow

    publishing_config.instagram["publishing"]["max_approval_age_days"] = None
    directory, manifest = make_output(tmp_path)
    check = preflight(directory, publishing_config, manifest_path=manifest,
                      environ=ENV, now=utcnow() + timedelta(days=90))

    assert "approval_too_old" not in check.blockers


def _seed_run(db_path: Path) -> None:
    from src.core.models import RunRecord
    from src.storage.db import open_db
    from src.storage.repository import ArticleRepository

    with open_db(db_path) as conn:
        repo = ArticleRepository(conn)
        record = RunRecord(run_id="run-1", status="success")
        repo.start_run(record)
        repo.finish_run(record)


def _write_published(directory: Path, *, ok: bool = True) -> None:
    carousel = json.loads((directory / "carousel.json").read_text(encoding="utf-8"))
    carousel["meta"] = {"topic_id": "abc123"}
    (directory / "carousel.json").write_text(
        json.dumps(carousel, ensure_ascii=False), encoding="utf-8"
    )
    (directory / "publishing.json").write_text(
        json.dumps(
            {
                "output_id": "candidate-1",
                "ok": ok,
                "status": "published" if ok else "failed",
                "external_id": "1790" if ok else None,
                "external_url": "https://instagram.com/p/abc",
                "completed_at": "2026-10-23T00:01:00Z",
            }
        ),
        encoding="utf-8",
    )


def _load_posts(db_path: Path) -> list[dict]:
    from src.storage.db import open_db

    with open_db(db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM posts")]


def test_record_publication_links_history_and_is_idempotent(tmp_path):
    from src.pipeline.record_publication import record_publication

    directory, _manifest = make_output(tmp_path)
    _write_published(directory)
    db_path = tmp_path / "content.db"
    _seed_run(db_path)

    first = record_publication(directory, db_path=db_path)
    second = record_publication(directory, db_path=db_path)

    assert first is not None and first == second
    rows = _load_posts(db_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "published"
    assert rows[0]["channel_id"] == "instagram"
    assert rows[0]["external_id"] == "1790"
    assert rows[0]["external_url"] == "https://instagram.com/p/abc"
    assert json.loads(rows[0]["article_keys"]) == ["abc123"]


def test_record_publication_skips_unpublished_or_without_run(tmp_path):
    from src.pipeline.record_publication import record_publication

    directory, _manifest = make_output(tmp_path)
    db_path = tmp_path / "content.db"
    _write_published(directory, ok=False)
    assert record_publication(directory, db_path=db_path) is None
    _write_published(directory)
    # published, but no successful engine run to attach to
    assert record_publication(directory, db_path=db_path) is None
    assert _load_posts(db_path) == []


def test_record_publication_default_db_path_is_resolved_not_a_method(tmp_path, monkeypatch):
    """실사용 회귀: db_path 를 안 주면 설정에서 경로를 구해야 한다(메서드 객체를 넘겨 TypeError 가 났다)."""
    from src.pipeline import record_publication as module

    seen = {}

    class FakeConfig:
        def db_path(self):
            return tmp_path / "content.db"

    def fake_open_db(path):
        seen["path"] = path
        raise RuntimeError("stop after the path is chosen")

    monkeypatch.setattr(module, "load_config", lambda: FakeConfig())
    monkeypatch.setattr(module, "open_db", fake_open_db)
    directory = tmp_path / "out"
    directory.mkdir()
    (directory / "publishing.json").write_text(
        json.dumps({"ok": True, "external_id": "1"}), encoding="utf-8")
    (directory / "carousel.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError):
        module.record_publication(directory)
    assert seen["path"] == tmp_path / "content.db"


def test_a_failing_permalink_lookup_never_turns_a_live_post_into_a_failure(tmp_path, publishing_config):
    directory, manifest = make_output(tmp_path)
    responses = []
    for index in range(1, 4):
        responses.extend([FakeResponse({"id": f"child-{index}"}), FakeResponse({"status_code": "FINISHED"})])
    responses.extend([
        FakeResponse({"id": "carousel-1"}), FakeResponse({"status_code": "FINISHED"}),
        FakeResponse({"id": "media-1"}),
        FakeResponse({"error": {"message": "permalink unavailable", "type": "OAuthException", "code": 1}}, 400),
    ])
    client = InstagramGraphClient(access_token=ENV["INSTAGRAM_ACCESS_TOKEN"],
                                  instagram_user_id=ENV["INSTAGRAM_USER_ID"], session=FakeSession(responses))
    result = publish_output(directory, publishing_config, manifest_path=manifest, environ=ENV,
                            client=client, sleeper=lambda _s: None)
    assert result.ok is True and result.status == "published" and result.external_url is None
