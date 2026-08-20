"""The envelope: one response, one file.

`body` is the parsed JSON, semantically unmodified — no key reordering, no coercion, no
unwrapping. Nothing here validates, cleans, flattens or profiles it. When the response is
not JSON, `body` is null and `body_raw` holds the text verbatim.

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


def build(
    *,
    run_id: str,
    spec: RequestSpec,
    request: Request,
    response: Response,
    page: int,
    fetched_at: str,
    auth_headers: Iterable[str] = (),
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "metadata": {
            "run_id": run_id,
            "source": spec.source,
            "endpoint": spec.endpoint,
            "params": dict(spec.params),
            "request": {
                "method": request.method,
                "url": request.url,
                "query": dict(request.query),
                "payload": request.payload,
                "headers": redact_headers(request.headers, auth_headers),
            },
            "response": {
                "status": response.status,
                "headers": dict(response.headers),
                "elapsed_ms": response.elapsed_ms,
            },
            "page": page,
            "parents": list(spec.parents),
            "fetched_at": fetched_at,
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
