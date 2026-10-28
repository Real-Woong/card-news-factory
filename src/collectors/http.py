"""HTTP client used by every collector.

Prefers ``requests`` when installed and falls back to ``urllib`` so the project
runs on a bare interpreter. Adds retries, a polite per-host delay and a
configurable User-Agent.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from src.core.errors import CollectorError
from src.core.logging_setup import get_logger

logger = get_logger(__name__)

DEFAULT_USER_AGENT = "Project-ContentOps/0.1 (+https://github.com/; personal research bot)"


@dataclass
class HttpSettings:
    timeout: float = 20.0
    max_retries: int = 2
    delay_seconds: float = 1.0
    user_agent: str = ""
    max_bytes: int = 4_000_000


class HttpClient:
    """Small, dependency-optional HTTP GET client."""

    def __init__(self, settings: HttpSettings | None = None) -> None:
        self.settings = settings or HttpSettings()
        if not self.settings.user_agent:
            self.settings.user_agent = os.environ.get("HTTP_USER_AGENT") or DEFAULT_USER_AGENT
        self._last_request_at: dict[str, float] = {}
        self._session: Any | None = None
        try:
            import requests  # type: ignore[import-not-found]

            self._session = requests.Session()
            self._session.headers.update({"User-Agent": self.settings.user_agent})
        except ImportError:
            logger.debug("requests not installed; using urllib")

    # ------------------------------------------------------------------ throttle
    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        delay = self.settings.delay_seconds
        if delay <= 0:
            return
        last = self._last_request_at.get(host)
        if last is not None:
            wait = delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_request_at[host] = time.monotonic()

    # ----------------------------------------------------------------------- get
    def get_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        """GET a URL and return the decoded body.

        Raises:
            CollectorError: after exhausting retries.
        """
        merged = {"User-Agent": self.settings.user_agent, **(headers or {})}
        attempts = max(self.settings.max_retries, 0) + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            self._throttle(url)
            try:
                if self._session is not None:
                    response = self._session.get(
                        url, headers=merged, timeout=timeout or self.settings.timeout
                    )
                    if response.status_code >= 400:
                        raise CollectorError(f"HTTP {response.status_code} for {url}")
                    return response.text
                request = urllib.request.Request(url, headers=merged)  # noqa: S310
                with urllib.request.urlopen(  # noqa: S310
                    request, timeout=timeout or self.settings.timeout
                ) as raw:
                    payload = raw.read(self.settings.max_bytes)
                    charset = raw.headers.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
            except Exception as exc:  # noqa: BLE001 - normalised into CollectorError
                last_error = exc
                if attempt < attempts:
                    backoff = min(2.0 * attempt, 8.0)
                    logger.debug(
                        "GET %s failed (%s); retrying in %.1fs [%d/%d]",
                        url,
                        exc,
                        backoff,
                        attempt,
                        attempts,
                    )
                    time.sleep(backoff)

        raise CollectorError(f"GET {url} failed after {attempts} attempts: {last_error}")

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        merged = {"Accept": "application/json", **(headers or {})}
        body = self.get_text(url, headers=merged, timeout=timeout)
        try:
            return json.loads(body)
        except ValueError as exc:
            raise CollectorError(f"Invalid JSON from {url}: {exc}") from exc

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001 - best effort
                pass
