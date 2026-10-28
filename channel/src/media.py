"""Phase 3 media preparation: slide PNG -> public-ready JPEG + media manifest.

Meta fetches carousel images itself and only accepts public HTTPS JPEG URLs, so
the locally rendered 1080x1350 PNG slides must be converted and hosted. This
module covers the conversion and the manifest; uploading the staged files to the
host stays an explicit operator step. It never contacts Meta and never publishes.

CLI::

    python -m channel.src.media prepare <output-id> [--base-url https://...]
    python -m channel.src.media publish <output-id> --confirm

``publish`` pushes the staged JPEGs of an approved output to the static-host
repository and waits until every URL actually serves a JPEG. It is the only
step here that leaves the machine, so it needs ``--confirm`` and refuses any
output that is not approved for its current content. It still never contacts
Meta.

Staged layout (the stage directory is meant to be the checkout of a static-host
repository such as GitHub Pages)::

    <stage_dir>/<output-id>/<content_hash[:12]>/01.jpg ...

The content hash is part of the URL, so edited content never reuses a cached or
already-fetched image URL. ``publishing_media.json`` is written next to the
output's ``carousel.json`` in the format ``publisher.MediaManifest`` loads.
"""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import requests

from channel.src.approval import (
    STATUS_APPROVED,
    ApprovalError,
    content_hash,
    latest_output,
    load_record,
    resolve_output,
)
from channel.src.config import ChannelConfig, load_channel_config
from channel.src.publisher import MEDIA_MANIFEST_FILENAME
from src.core.errors import ContentOpsError

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
EXPECTED_SIZE = (1080, 1350)
# Instagram rejects image files larger than 8 MB.
MAX_JPEG_BYTES = 8 * 1024 * 1024

Converter = Callable[[Path, Path, int], None]


class MediaError(Exception):
    """Media conversion or manifest input failure."""


@dataclass
class MediaPlan:
    output_id: str
    content_hash: str
    stage_directory: Path
    files: list[Path]
    image_urls: list[str]
    manifest: Path


def png_size(path: Path) -> tuple[int, int]:
    """Read width/height from the PNG IHDR chunk without extra dependencies."""
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or not header.startswith(PNG_SIGNATURE):
        raise MediaError(f"PNG 파일이 아닙니다: {path}")
    return struct.unpack(">II", header[16:24])


def sips_convert(source: Path, target: Path, quality: int) -> None:
    """Convert with macOS ``sips`` (built in, so no new dependency)."""
    if shutil.which("sips") is None:
        raise MediaError("sips 를 찾을 수 없습니다 (macOS 전용 변환기)")
    result = subprocess.run(
        [
            "sips",
            "-s", "format", "jpeg",
            "-s", "formatOptions", str(quality),
            str(source),
            "--out", str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise MediaError(f"JPEG 변환 실패 ({source.name}): {result.stderr.strip()}")


def normalize_base_url(value: str) -> str:
    base = (value or "").strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise MediaError(
            "media base_url 은 공개 HTTPS URL 이어야 합니다 "
            "(--base-url 또는 instagram.yaml 의 publishing.media.base_url)"
        )
    return base


def _verify_jpeg(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        raise MediaError(f"JPEG 가 생성되지 않았습니다: {path.name}")
    if path.stat().st_size > MAX_JPEG_BYTES:
        raise MediaError(f"JPEG 가 8MB 를 넘습니다: {path.name}")
    with path.open("rb") as handle:
        if not handle.read(3) == JPEG_SIGNATURE:
            raise MediaError(f"JPEG 형식이 아닙니다: {path.name}")


def prepare_media(
    directory: Path,
    config: ChannelConfig,
    *,
    base_url: str | None = None,
    converter: Converter = sips_convert,
) -> MediaPlan:
    """Convert slides, stage them under a hash-addressed path, write the manifest."""
    base = normalize_base_url(
        base_url or str(config.get("instagram.publishing.media.base_url", "") or "")
    )
    quality = int(config.get("instagram.publishing.media.jpeg_quality", 92))
    stage_root = config.root / str(
        config.get("instagram.publishing.media.stage_dir", "media_site")
    )

    digest = content_hash(directory)
    if not digest:
        raise MediaError("carousel.json 을 읽을 수 없어 content hash 를 계산하지 못했습니다")
    try:
        payload = json.loads((directory / "carousel.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MediaError(f"carousel.json 을 읽을 수 없습니다: {exc}") from exc
    slide_count = len(payload.get("slides") or [])
    if slide_count == 0:
        raise MediaError("슬라이드가 없습니다")

    sources = [directory / "slides" / f"{i:02d}.png" for i in range(1, slide_count + 1)]
    missing = [p.name for p in sources if not p.exists()]
    if missing:
        raise MediaError(f"슬라이드 PNG 가 없습니다 (재렌더 필요): {', '.join(missing)}")
    for path in sources:
        size = png_size(path)
        if size != EXPECTED_SIZE:
            raise MediaError(f"{path.name} 크기가 {size} 입니다 (기대 {EXPECTED_SIZE})")

    relative = Path(directory.name) / digest[:12]
    stage = stage_root / relative
    # Rebuild from scratch so stale files from a previous run never get hosted.
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    files: list[Path] = []
    for index, source in enumerate(sources, start=1):
        target = stage / f"{index:02d}.jpg"
        converter(source, target, quality)
        _verify_jpeg(target)
        files.append(target)

    urls = [f"{base}/{relative.as_posix()}/{p.name}" for p in files]
    manifest = directory / MEDIA_MANIFEST_FILENAME
    temporary = manifest.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps({"content_hash": digest, "image_urls": urls}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(manifest)
    return MediaPlan(directory.name, digest, stage, files, urls, manifest)


def hosting_blockers(directory: Path) -> list[str]:
    """Why this output must not be uploaded to the public host yet.

    A subset of the publisher preflight: hosting makes images public, so only
    content a human approved (and has not edited since) may leave the machine.
    The 10-approval posting target is deliberately not required here.
    """
    blockers: list[str] = []
    record = load_record(directory)
    actual = content_hash(directory)
    if not record.publishable:
        blockers.append("not_publishable")
    if any(warning.startswith("[BLOCK]") for warning in record.warnings):
        blockers.append("quality_block_present")
    if record.status != STATUS_APPROVED:
        blockers.append("approval_not_approved")
    if not actual:
        blockers.append("content_hash_missing")
    elif actual != record.approved_content_hash:
        blockers.append("approval_invalid_for_current_content")
    return blockers


GitRunner = Callable[[Path, list[str]], str]
UrlFetcher = Callable[[str], tuple[int, bytes]]


def run_git(repo: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise MediaError(f"git {' '.join(args)} 실패: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def fetch_url(url: str) -> tuple[int, bytes]:
    try:
        response = requests.get(url, timeout=15)
    except requests.RequestException:
        return 0, b""
    return response.status_code, response.content


def ensure_host_repo(stage_root: Path) -> None:
    """The stage directory must be its own git repo.

    It sits inside this project's (gitignored) tree, so without its own ``.git``
    a ``git -C`` call would silently act on the parent project repository.
    """
    if not (stage_root / ".git").exists():
        raise MediaError(f"호스팅 repo 가 아닙니다 (git clone 필요): {stage_root}")


def push_stage(
    stage_root: Path, plan: MediaPlan, *, git: GitRunner = run_git
) -> bool:
    """Commit and push the staged files. Returns False when nothing changed."""
    relative = plan.stage_directory.relative_to(stage_root).as_posix()
    git(stage_root, ["add", "--", relative])
    if not git(stage_root, ["status", "--porcelain", "--", relative]).strip():
        return False
    git(
        stage_root,
        ["commit", "-m", f"media: {plan.output_id} {plan.content_hash[:12]}", "--", relative],
    )
    git(stage_root, ["push"])
    return True


def wait_until_served(
    urls: list[str],
    *,
    attempts: int,
    interval: float,
    fetch: UrlFetcher = fetch_url,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Block until each URL answers 200 with JPEG bytes (Pages needs a build)."""
    def served(url: str) -> bool:
        status, body = fetch(url)
        return status == 200 and body.startswith(JPEG_SIGNATURE)

    pending = list(urls)
    for attempt in range(max(attempts, 1)):
        pending = [url for url in pending if not served(url)]
        if not pending:
            return
        if attempt + 1 < attempts:
            sleep(interval)
    raise MediaError(f"공개 URL 이 응답하지 않습니다 ({len(pending)}개): {pending[0]} ...")


def publish_media(
    directory: Path,
    config: ChannelConfig,
    *,
    base_url: str | None = None,
    converter: Converter = sips_convert,
    git: GitRunner = run_git,
    fetch: UrlFetcher = fetch_url,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[MediaPlan, bool]:
    """Approved output -> JPEG -> pushed to the host -> verified publicly served."""
    blockers = hosting_blockers(directory)
    if blockers:
        raise MediaError(f"업로드 차단: {', '.join(blockers)}")
    stage_root = config.root / str(
        config.get("instagram.publishing.media.stage_dir", "media_site")
    )
    ensure_host_repo(stage_root)
    plan = prepare_media(directory, config, base_url=base_url, converter=converter)
    pushed = push_stage(stage_root, plan, git=git)
    wait_until_served(
        plan.image_urls,
        attempts=int(config.get("instagram.publishing.media.wait_attempts", 30)),
        interval=float(config.get("instagram.publishing.media.wait_interval_seconds", 10)),
        fetch=fetch,
        sleep=sleep,
    )
    return plan, pushed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m channel.src.media",
        description="슬라이드 PNG 를 공개 호스팅용 JPEG 와 media manifest 로 준비하고 올립니다 (Meta 게시 없음).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="변환과 manifest 생성만 (업로드 없음)")
    publish = sub.add_parser("publish", help="승인된 산출물을 호스팅 repo 에 push")
    for command in (prepare, publish):
        command.add_argument("output_id")
        command.add_argument("--base-url", default=None)
    publish.add_argument(
        "--confirm", action="store_true", help="공개 호스팅 repo 로의 push 를 명시적으로 승인"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # 확인(--confirm)이 없으면 산출물을 찾기도 전에 거절한다(운영 산출물이 없는 새 환경에서도 같은 답)
        if args.command == "publish" and not args.confirm:
            raise MediaError("공개 호스팅에 올리려면 --confirm 이 필요합니다")
        directory = (
            latest_output() if args.output_id == "latest" else resolve_output(args.output_id)
        )
        config = load_channel_config()
        if args.command == "publish":
            plan, pushed = publish_media(directory, config, base_url=args.base_url)
        else:
            plan, pushed = prepare_media(directory, config, base_url=args.base_url), None
    except (MediaError, ApprovalError, ContentOpsError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    print(f"staged   : {plan.stage_directory}")
    print(f"manifest : {plan.manifest}")
    for url in plan.image_urls:
        print(f"  {url}")
    if pushed is None:
        print("다음 단계: media publish <output-id> --confirm 으로 호스팅에 올리세요.")
    else:
        print("push 완료, 공개 URL 확인됨." if pushed else "변경 없음(이미 호스팅됨), 공개 URL 확인됨.")
        print("다음 단계: publisher preflight 를 실행하세요.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
