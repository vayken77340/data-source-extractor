"""Auth strategies behind one interface: acquire a credential, get headers back.

Acquired once per run per source and cached. A chained fan-out can run for an hour and
outlive a token, so a 401 mid-run refreshes the credential once and retries — after that
the 401 is the API's actual answer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from api_extractor.http.client import Request, Response

# Heterogeneous by nature: each strategy takes its own auth model, same as ProviderFn.
AcquireFn = Callable[..., dict[str, str]]


class Sender(Protocol):
    """Just enough of the HTTP client for a token exchange."""

    def send(self, request: Request) -> Response: ...


_STRATEGIES: dict[str, AcquireFn] = {}


def strategy(auth_type: str) -> Callable[[AcquireFn], AcquireFn]:
    def register(fn: AcquireFn) -> AcquireFn:
        if auth_type in _STRATEGIES:
            raise RuntimeError(f"auth strategy {auth_type!r} is already registered")
        _STRATEGIES[auth_type] = fn
        return fn

    return register


def registered() -> list[str]:
    return sorted(_STRATEGIES)


def acquire(auth: Any, sender: Sender, base_url: str) -> dict[str, str]:
    if auth.type not in _STRATEGIES:
        raise RuntimeError(f"no strategy for auth type {auth.type!r}")
    return _STRATEGIES[auth.type](auth, BoundSender(sender, base_url))


class BoundSender:
    """A sender that resolves an auth request's `path` against the source base_url."""

    def __init__(self, sender: Sender, base_url: str) -> None:
        self.sender = sender
        self.base_url = base_url.rstrip("/")

    def send(self, request: Request) -> Response:
        return self.sender.send(request)

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"


class Authenticator:
    """Caches the credential for a run and refreshes it at most once per 401."""

    def __init__(self, auth: Any | None, sender: Sender, base_url: str) -> None:
        self._auth = auth
        self._sender = sender
        self._base_url = base_url
        self._headers: dict[str, str] | None = None

    def headers(self) -> dict[str, str]:
        if self._auth is None:
            return {}
        if self._headers is None:
            self._headers = acquire(self._auth, self._sender, self._base_url)
        return dict(self._headers)

    def refresh(self) -> dict[str, str]:
        """Drop the cached credential and acquire again."""
        self._headers = None
        return self.headers()

    def header_names(self) -> set[str]:
        """The headers this strategy sets, so persist can redact them by construction."""
        return set(self.headers())
