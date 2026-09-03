"""The envelope: one response, one file.

`body` is the parsed JSON, semantically unmodified — no key reordering, no coercion, no
unwrapping. Nothing here validates, cleans, flattens or profiles it. When the response is
not JSON, `body` is null and `body_raw` holds the text verbatim.

`metadata` is a contract: the specification handed to whoever lands these files in a
warehouse describes it attribute by attribute, and a test pins that description to what
this module writes. Every field is here for a reason, and nothing is here for free:

- `params` is load-bearing — a chained provider joins on it to recover which request
  produced a record, including `label` values that were never sent.
- `request` is what actually went out. `base_url` and `path` are two honest fields rather
  than one URL, because a hand-encoded query string can disagree with what the client
  sent; `query` and `payload` are verbatim and carry the page cursor where the API saw it.
- `response.status` is non-negotiable in an immutable zone: 403s and HTML error pages are
  landed on purpose, and the status is how they are told apart later.
- `parents` is the only cross-file provenance: which files this request's params were read
  from. A list, because an asset can surface under two asset types.

Not here, deliberately: the run id (the manifest is keyed by it), the page number (it is
already in `query` or `payload`, verbatim, and in the filename), response headers and
timing (diagnostics — `-v` traces them, the manifest keeps the timing).

Sensitive request headers are redacted at write time, not at read time.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from api_extractor.http.client import Request, Response
from api_extractor.logs import REDACTED
from api_extractor.plan.binding import RequestSpec

# Names that are credentials wherever they appear. The headers the auth layer applied are
# redacted too, and known by construction rather than by guessing at names.
SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "apikey",
        "x-auth-token",
        "x-authorization",
    }
)


def redact_headers(headers: Mapping[str, str], also: Iterable[str] = ()) -> dict[str, str]:
    sensitive = SENSITIVE_HEADERS | {name.lower() for name in also}
    return {
        name: (REDACTED if name.lower() in sensitive else value) for name, value in headers.items()
    }


def split_url(url: str, base_url: str) -> tuple[str, str]:
    """`base_url` and the path that was sent under it, so the two reassemble the URL.

    The path is taken off the request that actually went out rather than off the spec, and
    the base is checked against it: recording a base and a path that do not add up to what
    was sent would be the one lie this file must never tell.
    """
    base = base_url.rstrip("/")
    if not url.startswith(base + "/") and url != base:
        raise ValueError(f"request url {url!r} was not sent under base_url {base_url!r}")
    return base, url[len(base) :] or "/"


def build(
    *,
    spec: RequestSpec,
    request: Request,
    response: Response,
    base_url: str,
    extracted_at: str,
    auth_headers: Iterable[str] = (),
) -> dict[str, Any]:
    base, path = split_url(request.url, base_url)
    envelope: dict[str, Any] = {
        "metadata": {
            "source": spec.source,
            "endpoint": spec.endpoint,
            "extracted_at": extracted_at,
            "params": dict(spec.params),
            "request": {
                "method": request.method,
                "base_url": base,
                "path": path,
                "query": dict(request.query),
                "payload": request.payload,
                "headers": redact_headers(request.headers, auth_headers),
            },
            "response": {"status": response.status},
            "parents": list(spec.parents),
        },
        "body": response.body if response.parsed else None,
    }
    if not response.parsed:
        envelope["body_raw"] = response.text
    return envelope


def write(path: Path, envelope: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(envelope, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def read(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        loaded: dict[str, Any] = json.load(handle)
    return loaded


def parents_of(envelopes: Sequence[Mapping[str, Any]]) -> list[str]:
    return [str(item["metadata"]["source"]) for item in envelopes]
