"""The HTTP client: retry, rate limiting, and nothing else.

It takes a `Request` and returns a `Response`. It has never heard of an endpoint, a
provider or a YAML file, which is what keeps it testable and keeps the layers honest.

An error response is a `Response`, not an exception — a 403 or an HTML error page is real
information about the API, and the caller wants it on disk rather than swallowed.
"""

from __future__ import annotations

import email.utils
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from api_extractor.logs import get_logger

log = get_logger(__name__)

RETRY_STATUSES = frozenset({429})
MAX_RETRY_AFTER = 300.0


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    query: Mapping[str, Any] = field(default_factory=dict)
    payload: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout: float = 30.0


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    elapsed_ms: int
    text: str
    body: Any = None
    parsed: bool = True

    @property
    def retryable(self) -> bool:
        """429 and 5xx only. Any other 4xx is an answer, not a hiccup."""
        return self.status in RETRY_STATUSES or 500 <= self.status < 600


class _Retryable(Exception):
    """Carries the response through tenacity so the last one can still be saved."""

    def __init__(self, response: Response) -> None:
        self.response = response
        super().__init__(f"retryable status {response.status}")


def retry_after_seconds(response: Response) -> float | None:
    """`Retry-After`, either as seconds or as an HTTP date."""
    raw = None
    for name, value in response.headers.items():
        if name.lower() == "retry-after":
            raw = value.strip()
            break
    if not raw:
        return None
    if raw.isdigit():
        return min(float(raw), MAX_RETRY_AFTER)
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        # A malformed header is the server's problem; fall back to normal backoff
        # rather than letting it take the run down.
        return None
    delay = parsed.timestamp() - time.time()
    return min(max(delay, 0.0), MAX_RETRY_AFTER)


class Client:
    """One per source. Holds the connection pool and the source-wide rate limit."""

    def __init__(
        self,
        *,
        retries: int = 2,
        rate_limit: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._retries = retries
        self._min_interval = 1.0 / rate_limit if rate_limit else 0.0
        self._client = client or httpx.Client(follow_redirects=True)
        self._owns_client = client is None
        self._last_sent: float | None = None

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def send(self, request: Request) -> Response:
        """Send, retrying 429 and 5xx. Returns the last response either way."""
        retryer = Retrying(
            retry=retry_if_exception_type(_Retryable),
            stop=stop_after_attempt(self._retries + 1),
            wait=self._wait,
            reraise=True,
        )
        try:
            return retryer(self._attempt, request)
        except _Retryable as exhausted:
            return exhausted.response

    def _wait(self, state: Any) -> float:
        """Honour Retry-After when the server sent one, else back off with jitter."""
        exc = state.outcome.exception() if state.outcome is not None else None
        if isinstance(exc, _Retryable):
            explicit = retry_after_seconds(exc.response)
            if explicit is not None:
                return explicit
        wait: float = wait_exponential_jitter(initial=0.5, max=30.0)(state)
        return wait

    def _attempt(self, request: Request) -> Response:
        response = self._send_once(request)
        if response.retryable:
            raise _Retryable(response)
        return response

    def _send_once(self, request: Request) -> Response:
        self._throttle()
        started = time.perf_counter()
        raw = self._client.request(
            request.method,
            request.url,
            params=dict(request.query) or None,
            json=request.payload,
            headers=dict(request.headers),
            timeout=request.timeout,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self._last_sent = time.monotonic()
        return _read(raw, elapsed_ms)

    def _throttle(self) -> None:
        if not self._min_interval or self._last_sent is None:
            return
        remaining = self._min_interval - (time.monotonic() - self._last_sent)
        if remaining > 0:
            time.sleep(remaining)


def _read(raw: httpx.Response, elapsed_ms: int) -> Response:
    text = raw.text
    try:
        body = json.loads(text)
    except ValueError:
        # Not JSON — an HTML error page, say. The caller keeps the text verbatim.
        return Response(
            status=raw.status_code,
            headers=dict(raw.headers),
            elapsed_ms=elapsed_ms,
            text=text,
            body=None,
            parsed=False,
        )
    return Response(
        status=raw.status_code,
        headers=dict(raw.headers),
        elapsed_ms=elapsed_ms,
        text=text,
        body=body,
        parsed=True,
    )
