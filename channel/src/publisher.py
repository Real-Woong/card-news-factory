"""Phase 3 Instagram carousel publisher with non-bypassable approval guards.

The module is deliberately separate from generation. It consumes one completed
output directory and will not contact Meta unless every approval, content-hash,
configuration, credential, and public-media prerequisite passes.

CLI::

    python -m channel.src.publisher preflight <output-id>
    python -m channel.src.publisher publish <output-id> --confirm

``publishing_media.json`` is an operator-provided deployment artifact::

    {
      "content_hash": "sha256 from approval.json",
      "image_urls": ["https://cdn.example/01.jpg", "..."]
    }

Meta fetches carousel images itself, so local PNG files cannot be sent directly.
The URLs must point to public HTTPS JPEG files in slide order.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from channel.src.approval import (
    APPROVAL_FILENAME,
    STATUS_APPROVED,
    ApprovalError,
    content_hash,
    latest_output,
    load_record,
    metrics,
    resolve_output,
)
from channel.src.config import ChannelConfig, load_channel_config
from channel.src.freshness import find_stale_dates
from channel.src.token_store import effective_environ
from channel.src.schema import Carousel
from src.core.config import load_dotenv_file
from src.core.errors import ContentOpsError
from src.core.text import parse_datetime, to_iso, utcnow

MEDIA_MANIFEST_FILENAME = "publishing_media.json"
PUBLISHING_RESULT_FILENAME = "publishing.json"
REQUIRED_ENV = ("INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_USER_ID")


class PublishingError(Exception):
    """Publishing input, policy, or remote API failure."""


@dataclass
class MediaManifest:
    content_hash: str
    image_urls: list[str]

    @classmethod
    def load(cls, path: Path) -> MediaManifest:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise PublishingError(f"media manifest가 없습니다: {path}") from exc
        except (OSError, ValueError) as exc:
            raise PublishingError(f"media manifest를 읽을 수 없습니다: {exc}") from exc
        if not isinstance(payload, dict):
            raise PublishingError("media manifest 최상위는 object여야 합니다")
        urls = payload.get("image_urls") or []
        if not isinstance(urls, list):
            raise PublishingError("media manifest image_urls는 list여야 합니다")
        return cls(
            content_hash=str(payload.get("content_hash") or "").strip(),
            image_urls=[str(url).strip() for url in urls if str(url).strip()],
        )


@dataclass
class PublishingPreflight:
    output_id: str
    eligible: bool
    content_hash: str | None
    slide_count: int
    blockers: list[str] = field(default_factory=list)
    media_manifest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_id": self.output_id,
            "eligible": self.eligible,
            "content_hash": self.content_hash,
            "slide_count": self.slide_count,
            "blockers": self.blockers,
            "media_manifest": self.media_manifest,
        }


@dataclass
class PublishingResult:
    output_id: str
    ok: bool
    status: str
    content_hash: str
    started_at: str
    completed_at: str | None = None
    external_id: str | None = None
    external_url: str | None = None
    child_container_ids: list[str] = field(default_factory=list)
    carousel_container_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_id": self.output_id,
            "ok": self.ok,
            "status": self.status,
            "content_hash": self.content_hash,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "external_id": self.external_id,
            "external_url": self.external_url,
            "child_container_ids": self.child_container_ids,
            "carousel_container_id": self.carousel_container_id,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PublishingResult:
        return cls(
            output_id=str(payload.get("output_id") or ""),
            ok=bool(payload.get("ok", False)),
            status=str(payload.get("status") or "failed"),
            content_hash=str(payload.get("content_hash") or ""),
            started_at=str(payload.get("started_at") or ""),
            completed_at=payload.get("completed_at"),
            external_id=payload.get("external_id"),
            external_url=payload.get("external_url"),
            child_container_ids=[
                str(item) for item in (payload.get("child_container_ids") or [])
            ],
            carousel_container_id=payload.get("carousel_container_id"),
            error=payload.get("error"),
        )


def _load_carousel(directory: Path) -> Carousel:
    try:
        payload = json.loads((directory / "carousel.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PublishingError(f"carousel.json을 읽을 수 없습니다: {exc}") from exc
    if not isinstance(payload, dict):
        raise PublishingError("carousel.json 최상위는 object여야 합니다")
    return Carousel.from_dict(payload)


def _is_public_jpeg(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme.lower() == "https"
        and bool(parsed.netloc)
        and Path(parsed.path).suffix.lower() in {".jpg", ".jpeg"}
    )


def _load_previous_result(directory: Path) -> PublishingResult | None:
    path = directory / PUBLISHING_RESULT_FILENAME
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return PublishingResult.from_dict(payload) if isinstance(payload, dict) else None


def save_result(directory: Path, result: PublishingResult) -> Path:
    """Atomically persist a token-free publishing audit record."""
    path = directory / PUBLISHING_RESULT_FILENAME
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)
    return path


def preflight(
    directory: Path,
    config: ChannelConfig,
    *,
    manifest_path: Path | None = None,
    environ: dict[str, str] | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> PublishingPreflight:
    """Evaluate every condition required before the first network request."""
    blockers: list[str] = []
    environment = environ if environ is not None else effective_environ()
    record = load_record(directory)
    actual_hash = content_hash(directory)
    carousel = _load_carousel(directory)
    manifest_file = manifest_path or (directory / MEDIA_MANIFEST_FILENAME)

    if int(config.get("instagram.channel.phase", 2)) < 3:
        blockers.append("channel_phase_not_3")
    if not bool(config.get("instagram.channel.publish", False)):
        blockers.append("channel_publish_disabled")
    if bool(config.get("instagram.publishing.require_phase3_ready", True)):
        if not metrics(directory.parent)["phase3_ready"]:
            blockers.append("phase3_approval_target_not_met")
    if not record.publishable:
        blockers.append("not_publishable")
    if any(warning.startswith("[BLOCK]") for warning in record.warnings):
        blockers.append("quality_block_present")
    if record.status != STATUS_APPROVED:
        blockers.append("approval_not_approved")
    if not actual_hash:
        blockers.append("content_hash_missing")
    elif actual_hash != record.approved_content_hash:
        blockers.append("approval_invalid_for_current_content")
    if not (directory / APPROVAL_FILENAME).exists():
        blockers.append("approval_record_missing")

    # 승인 뒤 시간이 흐르면 내용의 시점이 지났을 수 있어 게시 직전에 다시 확인합니다.
    if find_stale_dates(carousel, today=today):
        blockers.append("stale_content")
    max_age = config.get("instagram.publishing.max_approval_age_days")
    reviewed = parse_datetime(record.reviewed_at)
    if max_age is not None and reviewed is not None:
        if ((now or utcnow()) - reviewed).days > int(max_age):
            blockers.append("approval_too_old")

    if carousel.slide_count not in config.allowed_slide_counts:
        blockers.append("slide_count_not_allowed")
    slides_dir = directory / config.output_filename("slides_dir", "slides")
    slide_pattern = str(
        config.get("instagram.output.slide_filename_format", "{index:02d}.png")
    )
    for slide in carousel.slides:
        if not (slides_dir / slide_pattern.format(index=slide.index)).is_file():
            blockers.append(f"slide_image_missing:{slide.index}")

    manifest: MediaManifest | None = None
    try:
        manifest = MediaManifest.load(manifest_file)
    except PublishingError:
        blockers.append("media_manifest_missing_or_invalid")
    if manifest is not None:
        if manifest.content_hash != actual_hash:
            blockers.append("media_manifest_content_hash_mismatch")
        if len(manifest.image_urls) != carousel.slide_count:
            blockers.append("media_count_mismatch")
        for index, url in enumerate(manifest.image_urls, start=1):
            if not _is_public_jpeg(url):
                blockers.append(f"media_url_not_public_https_jpeg:{index}")

    missing_env = [name for name in REQUIRED_ENV if not environment.get(name, "").strip()]
    blockers.extend(f"missing_environment:{name}" for name in missing_env)

    previous = _load_previous_result(directory)
    if previous and previous.ok and previous.content_hash == actual_hash:
        blockers.append("already_published")
    elif (
        previous
        and previous.content_hash == actual_hash
        and previous.status in {"publishing", "failed"}
    ):
        # A timeout can happen after Meta accepted media_publish. Never retry an
        # ambiguous attempt automatically; an operator must reconcile it first.
        blockers.append("previous_publish_attempt_requires_review")

    # Preserve order while avoiding duplicate codes from malformed fixtures.
    blockers = list(dict.fromkeys(blockers))
    return PublishingPreflight(
        output_id=directory.name,
        eligible=not blockers,
        content_hash=actual_hash,
        slide_count=carousel.slide_count,
        blockers=blockers,
        media_manifest=str(manifest_file),
    )


class InstagramGraphClient:
    """Small Instagram Login client. Access tokens are sent in headers only."""

    def __init__(
        self,
        *,
        access_token: str,
        instagram_user_id: str,
        api_version: str = "v25.0",
        base_url: str = "https://graph.instagram.com",
        timeout: float = 30.0,
        session: Any | None = None,
    ) -> None:
        if not access_token or not instagram_user_id:
            raise PublishingError("Instagram token과 user id가 필요합니다")
        self.access_token = access_token
        self.instagram_user_id = instagram_user_id
        self.api_version = api_version.strip("/")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    def _request(
        self, method: str, path: str, *, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{self.api_version}/{path.lstrip('/')}"
        kwargs: dict[str, Any] = {
            "headers": {"Authorization": f"Bearer {self.access_token}"},
            "timeout": self.timeout,
        }
        if method.upper() == "GET":
            kwargs["params"] = data or {}
        else:
            kwargs["data"] = data or {}
        try:
            response = self.session.request(method, url, **kwargs)
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise PublishingError(f"Instagram API 요청 실패: {exc}") from exc
        if (
            response.status_code >= 400
            or not isinstance(payload, dict)
            or payload.get("error")
        ):
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                message = str(error.get("message") or "unknown error")
                code = error.get("code")
                kind = error.get("type")
                raise PublishingError(
                    f"Instagram API {response.status_code}: {kind}/{code} {message}"
                )
            raise PublishingError(f"Instagram API {response.status_code}: invalid response")
        return payload

    def account_info(self) -> dict[str, Any]:
        """읽기 전용 점검: 토큰이 유효하고 이 앱이 API 에 접근할 수 있는지(게시·쓰기 없음)."""
        return self._request("GET", "me", data={"fields": "user_id,username,account_type"})

    def permalink(self, media_id: str) -> str:
        """게시물 링크(읽기 전용). 알림에 싣는 용도라 실패해도 게시 결과에는 영향이 없다."""
        payload = self._request("GET", media_id, data={"fields": "permalink"})
        return str(payload.get("permalink") or "")

    def create_image_container(self, image_url: str) -> str:
        payload = self._request(
            "POST",
            f"{self.instagram_user_id}/media",
            data={"image_url": image_url, "is_carousel_item": "true"},
        )
        container_id = str(payload.get("id") or "")
        if not container_id:
            raise PublishingError("image container id가 응답에 없습니다")
        return container_id

    def create_carousel_container(self, children: list[str], caption: str) -> str:
        payload = self._request(
            "POST",
            f"{self.instagram_user_id}/media",
            data={
                "media_type": "CAROUSEL",
                "children": ",".join(children),
                "caption": caption,
            },
        )
        container_id = str(payload.get("id") or "")
        if not container_id:
            raise PublishingError("carousel container id가 응답에 없습니다")
        return container_id

    def container_status(self, container_id: str) -> str:
        payload = self._request(
            "GET", container_id, data={"fields": "status_code,status"}
        )
        return str(payload.get("status_code") or "").upper()

    def publish_container(self, container_id: str) -> str:
        payload = self._request(
            "POST",
            f"{self.instagram_user_id}/media_publish",
            data={"creation_id": container_id},
        )
        external_id = str(payload.get("id") or "")
        if not external_id:
            raise PublishingError("published media id가 응답에 없습니다")
        return external_id


def check_access(
    config: ChannelConfig,
    *,
    environ: dict[str, str] | None = None,
    client: InstagramGraphClient | None = None,
) -> tuple[bool, str]:
    """Meta API 접근 점검(읽기 전용). 반환: (성공 여부, 사람이 읽는 메시지). 토큰은 출력하지 않는다."""
    environment = environ if environ is not None else effective_environ()
    missing = [name for name in REQUIRED_ENV if not environment.get(name, "").strip()]
    if missing and client is None:
        return False, "환경변수 없음: " + ", ".join(missing)
    try:
        graph = client or InstagramGraphClient(
            access_token=environment["INSTAGRAM_ACCESS_TOKEN"],
            instagram_user_id=environment["INSTAGRAM_USER_ID"],
            api_version=str(config.get("instagram.publishing.api_version", "v25.0")),
            base_url=str(
                config.get("instagram.publishing.api_base", "https://graph.instagram.com")
            ),
            timeout=float(config.get("instagram.publishing.timeout_seconds", 30)),
        )
        info = graph.account_info()
    except PublishingError as exc:
        token = environment.get("INSTAGRAM_ACCESS_TOKEN", "")
        return False, str(exc).replace(token, "[REDACTED]") if token else str(exc)
    name = info.get("username") or "?"
    return True, f"@{name} ({info.get('account_type') or 'account_type 알 수 없음'})"


def _wait_for_container(
    client: InstagramGraphClient,
    container_id: str,
    *,
    attempts: int,
    interval_seconds: float,
    sleeper: Callable[[float], None],
) -> None:
    for attempt in range(attempts):
        status = client.container_status(container_id)
        if status in {"FINISHED", "PUBLISHED"}:
            return
        if status in {"ERROR", "EXPIRED"}:
            raise PublishingError(f"container {container_id} status={status}")
        if attempt + 1 < attempts:
            sleeper(interval_seconds)
    raise PublishingError(f"container {container_id} 준비 시간 초과")


def _caption(carousel: Carousel) -> str:
    tags = " ".join(tag for tag in carousel.hashtags if tag not in carousel.caption)
    return f"{carousel.caption}\n\n{tags}".strip()


def publish_output(
    directory: Path,
    config: ChannelConfig,
    *,
    manifest_path: Path | None = None,
    environ: dict[str, str] | None = None,
    client: InstagramGraphClient | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> PublishingResult:
    """Publish one approved output, persisting success or diagnostic failure."""
    environment = environ if environ is not None else effective_environ()
    check = preflight(
        directory, config, manifest_path=manifest_path, environ=environment
    )
    if not check.eligible:
        raise PublishingError("게시 preflight 실패: " + ", ".join(check.blockers))
    manifest = MediaManifest.load(
        manifest_path or (directory / MEDIA_MANIFEST_FILENAME)
    )
    carousel = _load_carousel(directory)
    result = PublishingResult(
        output_id=directory.name,
        ok=False,
        status="publishing",
        content_hash=check.content_hash or "",
        started_at=to_iso(utcnow()),
    )
    # Persist intent before the first POST so a crash cannot lead to a blind retry.
    save_result(directory, result)
    graph = client or InstagramGraphClient(
        access_token=environment["INSTAGRAM_ACCESS_TOKEN"],
        instagram_user_id=environment["INSTAGRAM_USER_ID"],
        api_version=str(config.get("instagram.publishing.api_version", "v25.0")),
        base_url=str(
            config.get(
                "instagram.publishing.api_base", "https://graph.instagram.com"
            )
        ),
        timeout=float(config.get("instagram.publishing.timeout_seconds", 30)),
    )
    attempts = int(config.get("instagram.publishing.container_poll_attempts", 10))
    interval = float(
        config.get("instagram.publishing.container_poll_interval_seconds", 2)
    )
    try:
        for image_url in manifest.image_urls:
            child_id = graph.create_image_container(image_url)
            result.child_container_ids.append(child_id)
            save_result(directory, result)
            _wait_for_container(
                graph,
                child_id,
                attempts=attempts,
                interval_seconds=interval,
                sleeper=sleeper,
            )
        result.carousel_container_id = graph.create_carousel_container(
            result.child_container_ids, _caption(carousel)
        )
        save_result(directory, result)
        _wait_for_container(
            graph,
            result.carousel_container_id,
            attempts=attempts,
            interval_seconds=interval,
            sleeper=sleeper,
        )
        result.external_id = graph.publish_container(result.carousel_container_id)
        result.ok = True
        result.status = "published"
        try:  # 이미 올라갔다: 링크를 못 얻어도(어떤 이유로든) 게시는 성공이다
            result.external_url = graph.permalink(result.external_id) or None
        except Exception:  # noqa: BLE001
            result.external_url = None
    except PublishingError as exc:
        result.status = "failed"
        result.error = str(exc).replace(graph.access_token, "[REDACTED]")
        raise
    finally:
        result.completed_at = to_iso(utcnow())
        save_result(directory, result)
    return result


def record_published_post(directory: Path) -> int | None:
    """Ask the engine to mirror a live post into its ``posts`` history.

    The channel never touches engine storage; the boundary is the output directory
    (``carousel.json`` + ``publishing.json``), which the engine CLI reads. Returns the
    engine post id, or None when it recorded nothing.
    """
    done = subprocess.run(
        [sys.executable, "-m", "src.pipeline.record_publication", str(directory)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=Path(__file__).resolve().parents[2],
    )
    if done.returncode != 0:
        raise PublishingError(done.stderr.strip() or f"exit code {done.returncode}")
    out = done.stdout.strip()
    return int(out) if out.isdigit() else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m channel.src.publisher",
        description="승인·해시·공개 media guard가 적용된 Instagram publisher",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Meta API 접근 점검(읽기 전용, 게시 안 함)")
    for name in ("preflight", "publish"):
        command = sub.add_parser(name)
        command.add_argument("output_id")
        command.add_argument("--media-manifest", type=Path, default=None)
        if name == "publish":
            command.add_argument(
                "--confirm",
                action="store_true",
                help="실제 Meta API 게시 호출을 명시적으로 승인",
            )
    return parser


def main(argv: list[str] | None = None) -> int:
    # Keep secrets in the repository-local, gitignored .env file just like the
    # shared ContentOps CLI. Existing shell variables still take precedence.
    load_dotenv_file()
    args = build_parser().parse_args(argv)
    if args.command == "check":
        ok, message = check_access(load_channel_config())
        print(("접근 가능: " if ok else "접근 불가: ") + message)
        return 0 if ok else 1
    try:
        # --confirm 이 없으면 산출물을 찾기 전에 거절한다(운영 산출물이 없는 새 환경에서도 같은 답).
        if args.command == "publish" and not args.confirm:
            raise PublishingError("실제 게시에는 --confirm이 필요합니다")
        directory = (
            latest_output() if args.output_id == "latest" else resolve_output(args.output_id)
        )
        config = load_channel_config()
        if args.command == "preflight":
            check = preflight(
                directory, config, manifest_path=args.media_manifest
            )
            print(json.dumps(check.to_dict(), ensure_ascii=False, indent=2))
            return 0 if check.eligible else 1
        result = publish_output(
            directory, config, manifest_path=args.media_manifest
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        # The post is already live; a history failure must not look like a publish failure.
        try:
            post_id = record_published_post(directory)
        except Exception as exc:  # noqa: BLE001
            print(f"경고: 게시는 완료됐지만 post history 기록 실패: {exc}", file=sys.stderr)
        else:
            if post_id is None:
                print("경고: post history 에 연결할 엔진 run 이 없어 기록하지 않았습니다", file=sys.stderr)
        return 0
    except (PublishingError, ApprovalError, ContentOpsError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
