"""Issue the planned requests and write what comes back.

A failed request does not abort the run: it is logged, recorded in the manifest, and the
next one goes out. One dead endpoint should not cost you the other twenty.

Phase 4 issues one request per spec. The pagination walk arrives in phase 5.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from api_extractor.auth.registry import Authenticator
from api_extractor.config.loader import resolve_env_tree
from api_extractor.config.models import Paginate, Source
from api_extractor.http import pagination
from api_extractor.http.client import Client, Request, Response
from api_extractor.logs import get_logger
from api_extractor.persist import envelope, manifest, paths
from api_extractor.plan.binding import RequestSpec, plan_one, plan_order
from api_extractor.providers.registry import ProviderContext, SavedOutput

log = get_logger(__name__)


@dataclass(frozen=True)
class RunResult:
    run_id: str
    manifest_path: Path
    written: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def attempted(self) -> int:
        return self.written + self.skipped + self.failed


def build_request(source: Source, spec: RequestSpec, auth_headers: Mapping[str, str]) -> Request:
    headers = {**resolve_env_tree(dict(source.defaults.headers)), **auth_headers}
    return Request(
        method=spec.method,
        url=f"{source.base_url.rstrip('/')}/{spec.path.lstrip('/')}",
        query=spec.query,
        payload=spec.payload,
        headers=headers,
        timeout=source.defaults.timeout,
    )


def search_root(source: Source, endpoint: str) -> Path:
    """Where this endpoint's envelopes live, from the literal head of its output template.

    `output/{source}/assets/{assetType}_p{page}.json` becomes `output/thingsboard/assets`.
    Every candidate is still checked against its own metadata, so a shared directory or a
    stray file cannot be mistaken for someone else's output.
    """
    partial = source.output_template(endpoint).replace("{source}", source.source)
    partial = partial.replace("{endpoint}", endpoint)
    head = partial.split("{", 1)[0]
    return Path(head) if head.endswith(("/", "\\")) else Path(head).parent


def read_outputs(source: Source, endpoint: str) -> list[SavedOutput]:
    """Envelopes already on disk for one endpoint, whichever run wrote them."""
    root = search_root(source, endpoint)
    if not root.is_dir():
        return []
    found: list[SavedOutput] = []
    for path in sorted(root.rglob("*.json")):
        try:
            data = envelope.read(path)
        except ValueError:  # a stray or half-written file is not this run's problem
            log.warning("skipping unreadable envelope %s", path)
            continue
        metadata = data.get("metadata", {})
        if metadata.get("endpoint") == endpoint and metadata.get("source") == source.source:
            found.append(SavedOutput(path=path, envelope=data))
    return found


def context_for(source: Source, run_id: str, output_root: Path) -> ProviderContext:
    return ProviderContext(
        run_id=run_id,
        output_root=output_root,
        source_name=source.source,
        outputs_for=lambda endpoint: read_outputs(source, endpoint),
    )


def execute(
    source: Source,
    client: Client,
    authenticator: Authenticator,
    *,
    output_root: Path = Path("output"),
    run_id: str | None = None,
    force: bool = False,
    only: Sequence[str] = (),
    limit: int | None = None,
    no_limit: bool = False,
) -> RunResult:
    """Plan and issue one endpoint at a time, in dependency order.

    Planning is interleaved with issuing on purpose: `measures` reads the envelopes that
    `assets` writes, so it cannot be planned until `assets` has run. That is also why
    `--endpoint measures` alone works — the provider reads whatever is already on disk.
    """
    run = run_id or manifest.new_run_id()
    manifest_path = manifest.path_for(output_root, run)
    context = context_for(source, run, output_root)
    cache: dict[str, list[dict[str, Any]]] = {}
    written = skipped = failed = 0

    for name in plan_order(source, only):
        item = plan_one(source, name, context, cache, limit=limit, no_limit=no_limit)
        if item.error is not None:
            log.warning("%s could not be planned: %s", name, item.error)
            manifest.append(
                manifest_path,
                {"endpoint": item.endpoint, "status": "unplanned", "error": item.error},
            )
            failed += 1
            continue

        paginate = source.endpoints[item.endpoint].paginate
        for spec in item.requests:
            counts = _walk(
                source,
                spec,
                client,
                authenticator,
                run,
                manifest_path,
                force=force,
                paginate=paginate,
                max_pages=source.defaults.max_pages,
            )
            written += counts[0]
            skipped += counts[1]
            failed += counts[2]

    return RunResult(
        run_id=run,
        manifest_path=manifest_path,
        written=written,
        skipped=skipped,
        failed=failed,
    )


def _walk(
    source: Source,
    spec: RequestSpec,
    client: Client,
    authenticator: Authenticator,
    run_id: str,
    manifest_path: Path,
    *,
    force: bool,
    paginate: Paginate | None,
    max_pages: int,
) -> tuple[int, int, int]:
    """One request, or one pagination walk. Returns (written, skipped, failed).

    A skipped page is read back off disk rather than re-fetched, so that resuming a walk
    without --force still gets to page 7 instead of stopping at the page 0 it already has.
    """
    written = skipped = failed = 0
    page = 0
    cursor = paginate.start if paginate is not None else 0

    while True:
        outcome, body = _one(
            source,
            spec,
            client,
            authenticator,
            run_id,
            manifest_path,
            force=force,
            page=page,
            paginate=paginate,
            cursor=cursor,
        )
        written += outcome == "written"
        skipped += outcome == "skipped"
        failed += outcome == "failed"

        if paginate is None or outcome == "failed":
            break
        if not pagination.has_next(paginate, body):
            break
        if page + 1 >= max_pages:
            log.warning(
                "%s %s: stopped at max_pages=%s with the API still offering more",
                spec.endpoint,
                dict(spec.params),
                max_pages,
            )
            break
        page += 1
        cursor += 1

    return written, skipped, failed


def _one(
    source: Source,
    spec: RequestSpec,
    client: Client,
    authenticator: Authenticator,
    run_id: str,
    manifest_path: Path,
    *,
    force: bool,
    page: int = 0,
    paginate: Paginate | None = None,
    cursor: int = 0,
) -> tuple[str, Any]:
    """Issue one page. Returns (outcome, parsed body) — the body drives the walk."""
    path = paths.for_request(spec, page)
    record: dict[str, Any] = {
        "run_id": run_id,
        "endpoint": spec.endpoint,
        "params": dict(spec.params),
        "page": page,
        "output": str(path),
        "parents": list(spec.parents),
    }

    if paths.already_written(path) and not force:
        log.info("%s %s skipped (exists) %s", spec.endpoint, dict(spec.params), path)
        manifest.append(manifest_path, {**record, "status": "skipped"})
        return "skipped", envelope.read(path).get("body")

    try:
        response, sent = _send(source, spec, client, authenticator, paginate, cursor)
    except httpx.HTTPError as exc:  # the network is where failure is expected
        log.warning("%s %s failed: %s", spec.endpoint, dict(spec.params), exc)
        manifest.append(
            manifest_path, {**record, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
        )
        return "failed", None

    envelope.write(
        path,
        envelope.build(
            spec=spec,
            request=sent,
            response=response,
            base_url=source.base_url,
            extracted_at=manifest.utc_now(),
            auth_headers=authenticator.header_names(),
        ),
    )
    log.info(
        "%s %s -> %s in %sms %s",
        spec.endpoint,
        dict(spec.params),
        response.status,
        response.elapsed_ms,
        path,
    )
    manifest.append(
        manifest_path,
        {
            **record,
            "status": response.status,
            "elapsed_ms": response.elapsed_ms,
        },
    )
    # An error response is saved, not swallowed — but it is not a success either.
    return ("written" if response.status < 400 else "failed", response.body)


def _dispatch(client: Client, request: Request, auth_headers: Iterable[str]) -> Response:
    """Send one request, tracing it at DEBUG.

    Headers are redacted here rather than left to the log scrubber, so a credential cannot
    reach a log record in the first place — the scrubber is the second line of defence, not
    the only one.
    """
    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "-> %s %s\n     query   %s\n     payload %s\n     headers %s",
            request.method,
            request.url,
            dict(request.query) or "-",
            json.dumps(request.payload, ensure_ascii=False) if request.payload else "-",
            envelope.redact_headers(request.headers, auth_headers),
        )

    response = client.send(request)

    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "<- %s in %sms\n     headers %s\n     body    %s",
            response.status,
            response.elapsed_ms,
            dict(response.headers),
            _preview(response.text),
        )
    return response


def _preview(text: str, limit: int = 500) -> str:
    body = text.strip().replace("\n", " ")
    if not body:
        return "-"
    return body if len(body) <= limit else f"{body[:limit]}... ({len(text)} chars)"


def _send(
    source: Source,
    spec: RequestSpec,
    client: Client,
    authenticator: Authenticator,
    paginate: Paginate | None,
    cursor: int,
) -> tuple[Response, Request]:
    """Send once. On a 401, refresh the credential once and send again, then give up.

    A chained fan-out can run for an hour and outlive its token; this will happen. Returns
    the request that was actually sent, so the envelope records what went out.
    """
    names = authenticator.header_names()
    request = build_request(source, spec, authenticator.headers())
    if paginate is not None:
        request = pagination.with_cursor(request, paginate, cursor)

    response = _dispatch(client, request, names)
    if response.status != 401:
        return response, request

    log.info("401 on %s — refreshing the credential once", spec.endpoint)
    retried = build_request(source, spec, authenticator.refresh())
    if paginate is not None:
        retried = pagination.with_cursor(retried, paginate, cursor)
    return _dispatch(client, retried, authenticator.header_names()), retried
