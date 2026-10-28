"""Phase 3 media preparation: PNG -> JPEG staging and manifest generation."""

from __future__ import annotations

import copy
import json
import struct
from pathlib import Path

import pytest

from channel.src.approval import content_hash, decide, init_record
from channel.src.config import load_channel_config
from channel.src.media import (
    MediaError,
    hosting_blockers,
    main,
    png_size,
    prepare_media,
    publish_media,
    wait_until_served,
)
from channel.src.publisher import MediaManifest, _is_public_jpeg

BASE_URL = "https://example.github.io/media"


def fake_png(width: int = 1080, height: int = 1350) -> bytes:
    header = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
    return header + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"


def fake_converter(source: Path, target: Path, quality: int) -> None:
    target.write_bytes(b"\xff\xd8\xff\xe0" + b"jpeg" * 8)


@pytest.fixture
def config(tmp_path):
    cfg = copy.deepcopy(load_channel_config())
    cfg.root = tmp_path / "channel"
    return cfg


def make_output(tmp_path: Path, slides: int = 3, headline: str = "테스트") -> Path:
    directory = tmp_path / "candidate-1"
    (directory / "slides").mkdir(parents=True)
    payload = {
        "channel": "instagram",
        "brand": "R_A_Glance",
        "template": "news_short",
        "topic": "테스트",
        "headline": headline,
        "caption": "캡션",
        "hashtags": ["#test"],
        "slides": [{"index": i, "type": "content", "title": f"s{i}"} for i in range(1, slides + 1)],
    }
    (directory / "carousel.json").write_text(json.dumps(payload, ensure_ascii=False))
    for i in range(1, slides + 1):
        (directory / "slides" / f"{i:02d}.png").write_bytes(fake_png())
    return directory


def test_png_size_reads_ihdr(tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(fake_png(1080, 1350))
    assert png_size(path) == (1080, 1350)


def test_prepare_writes_manifest_loadable_by_publisher(tmp_path, config):
    directory = make_output(tmp_path)
    plan = prepare_media(directory, config, base_url=BASE_URL, converter=fake_converter)

    manifest = MediaManifest.load(directory / "publishing_media.json")
    assert manifest.content_hash == content_hash(directory)
    assert manifest.image_urls == plan.image_urls
    assert len(manifest.image_urls) == 3
    assert all(_is_public_jpeg(url) for url in manifest.image_urls)
    assert all(path.exists() for path in plan.files)
    expected = f"{BASE_URL}/candidate-1/{content_hash(directory)[:12]}/01.jpg"
    assert manifest.image_urls[0] == expected


def test_base_url_falls_back_to_config(tmp_path, config):
    config.instagram["publishing"]["media"]["base_url"] = BASE_URL + "/"
    plan = prepare_media(make_output(tmp_path), config, converter=fake_converter)
    assert plan.image_urls[0].startswith(BASE_URL + "/candidate-1/")


@pytest.mark.parametrize("bad", ["", "http://example.com/m", "example.com/m", "ftp://x/y"])
def test_rejects_non_https_base_url(tmp_path, config, bad):
    config.instagram["publishing"]["media"]["base_url"] = ""
    with pytest.raises(MediaError):
        prepare_media(make_output(tmp_path), config, base_url=bad, converter=fake_converter)


def test_changed_content_gets_new_url_path(tmp_path, config):
    first = prepare_media(make_output(tmp_path), config, base_url=BASE_URL, converter=fake_converter)
    carousel = tmp_path / "candidate-1" / "carousel.json"
    payload = json.loads(carousel.read_text())
    payload["headline"] = "수정된 제목"
    carousel.write_text(json.dumps(payload, ensure_ascii=False))
    second = prepare_media(tmp_path / "candidate-1", config, base_url=BASE_URL, converter=fake_converter)
    assert first.image_urls[0] != second.image_urls[0]


def test_rerun_removes_stale_staged_files(tmp_path, config):
    directory = make_output(tmp_path)
    plan = prepare_media(directory, config, base_url=BASE_URL, converter=fake_converter)
    stale = plan.stage_directory / "99.jpg"
    stale.write_bytes(b"old")
    prepare_media(directory, config, base_url=BASE_URL, converter=fake_converter)
    assert not stale.exists()


def test_rejects_missing_slide_png(tmp_path, config):
    directory = make_output(tmp_path)
    (directory / "slides" / "02.png").unlink()
    with pytest.raises(MediaError, match="02.png"):
        prepare_media(directory, config, base_url=BASE_URL, converter=fake_converter)
    assert not (directory / "publishing_media.json").exists()


def test_rejects_wrong_canvas_size(tmp_path, config):
    directory = make_output(tmp_path)
    (directory / "slides" / "01.png").write_bytes(fake_png(1080, 1080))
    with pytest.raises(MediaError, match="01.png"):
        prepare_media(directory, config, base_url=BASE_URL, converter=fake_converter)


def test_rejects_converter_output_that_is_not_jpeg(tmp_path, config):
    directory = make_output(tmp_path)

    def bad(source: Path, target: Path, quality: int) -> None:
        target.write_bytes(b"not a jpeg")

    with pytest.raises(MediaError, match="JPEG"):
        prepare_media(directory, config, base_url=BASE_URL, converter=bad)
    assert not (directory / "publishing_media.json").exists()


# --------------------------------------------------------------------------- #
# publish: approval guard -> push -> verify public URLs
# --------------------------------------------------------------------------- #
JPEG_BODY = b"\xff\xd8\xff\xe0jpeg"


def approve(directory: Path, *, publishable: bool = True, warnings=None) -> None:
    init_record(directory, publishable=publishable, score=0.9, warnings=warnings or [])
    # `--force` approvals exist in practice, so the hosting guard must catch them.
    decide(directory, "approved", reviewer="jin", force=not publishable)


class FakeGit:
    def __init__(self, dirty: bool = True) -> None:
        self.dirty = dirty
        self.calls: list[list[str]] = []

    def __call__(self, repo: Path, args: list[str]) -> str:
        self.calls.append(args)
        if args[0] == "status":
            return "A  staged.jpg" if self.dirty else ""
        return ""

    @property
    def verbs(self) -> list[str]:
        return [call[0] for call in self.calls]


def ok_fetch(url: str) -> tuple[int, bytes]:
    return 200, JPEG_BODY


@pytest.fixture
def hosted(tmp_path, config):
    """An approved output plus a stage directory that is its own git repo."""
    config.instagram["publishing"]["media"]["base_url"] = BASE_URL
    directory = make_output(tmp_path)
    approve(directory)
    (config.root / "media_site" / ".git").mkdir(parents=True)
    return directory, config


def test_hosting_blockers_empty_for_approved_output(hosted):
    directory, _ = hosted
    assert hosting_blockers(directory) == []


def test_hosting_blockers_for_unapproved_output(tmp_path):
    directory = make_output(tmp_path)
    init_record(directory, publishable=True, score=0.9, warnings=[])
    assert "approval_not_approved" in hosting_blockers(directory)


def test_hosting_blockers_when_edited_after_approval(hosted):
    directory, _ = hosted
    carousel = directory / "carousel.json"
    payload = json.loads(carousel.read_text())
    payload["headline"] = "승인 후 수정"
    carousel.write_text(json.dumps(payload, ensure_ascii=False))
    assert "approval_invalid_for_current_content" in hosting_blockers(directory)


def test_hosting_blockers_for_unpublishable_or_blocked(tmp_path):
    directory = make_output(tmp_path)
    approve(directory, publishable=False, warnings=["[BLOCK] 같은 사실 반복"])
    blockers = hosting_blockers(directory)
    assert "not_publishable" in blockers
    assert "quality_block_present" in blockers


def test_publish_pushes_and_verifies(hosted):
    directory, config = hosted
    git = FakeGit()
    plan, pushed = publish_media(
        directory, config, converter=fake_converter, git=git, fetch=ok_fetch
    )
    assert pushed is True
    assert git.verbs == ["add", "status", "commit", "push"]
    assert "media: candidate-1" in git.calls[2][2]
    assert (directory / "publishing_media.json").exists()
    assert len(plan.image_urls) == 3


def test_publish_skips_commit_when_already_hosted(hosted):
    directory, config = hosted
    git = FakeGit(dirty=False)
    _, pushed = publish_media(
        directory, config, converter=fake_converter, git=git, fetch=ok_fetch
    )
    assert pushed is False
    assert git.verbs == ["add", "status"]


def test_publish_refuses_unapproved_before_any_side_effect(tmp_path, config):
    config.instagram["publishing"]["media"]["base_url"] = BASE_URL
    (config.root / "media_site" / ".git").mkdir(parents=True)
    directory = make_output(tmp_path)
    init_record(directory, publishable=True, score=0.9, warnings=[])
    git = FakeGit()
    with pytest.raises(MediaError, match="approval_not_approved"):
        publish_media(directory, config, converter=fake_converter, git=git, fetch=ok_fetch)
    assert git.calls == []
    assert not (directory / "publishing_media.json").exists()
    assert list((config.root / "media_site").iterdir()) == [config.root / "media_site" / ".git"]


def test_publish_requires_stage_to_be_its_own_git_repo(tmp_path, config):
    config.instagram["publishing"]["media"]["base_url"] = BASE_URL
    directory = make_output(tmp_path)
    approve(directory)
    git = FakeGit()
    with pytest.raises(MediaError, match="호스팅 repo"):
        publish_media(directory, config, converter=fake_converter, git=git, fetch=ok_fetch)
    assert git.calls == []


def test_wait_until_served_retries_until_available():
    answers = iter([(404, b""), (404, b""), (200, JPEG_BODY)])
    sleeps: list[float] = []
    wait_until_served(
        ["https://h/a.jpg"], attempts=5, interval=7,
        fetch=lambda url: next(answers), sleep=sleeps.append,
    )
    assert sleeps == [7, 7]


def test_wait_until_served_rejects_non_jpeg_body():
    with pytest.raises(MediaError, match="응답하지 않습니다"):
        wait_until_served(
            ["https://h/a.jpg"], attempts=2, interval=0,
            fetch=lambda url: (200, b"<html>"), sleep=lambda s: None,
        )


def test_wait_until_served_gives_up_after_attempts():
    calls = []
    with pytest.raises(MediaError):
        wait_until_served(
            ["https://h/a.jpg"], attempts=3, interval=0,
            fetch=lambda url: calls.append(url) or (404, b""), sleep=lambda s: None,
        )
    assert len(calls) == 3


def test_cli_publish_requires_confirm(capsys):
    assert main(["publish", "latest"]) == 2
    assert "--confirm" in capsys.readouterr().err
