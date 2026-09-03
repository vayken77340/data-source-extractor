from __future__ import annotations

import json
import logging
import re

import httpx
import pytest

from api_extractor.auth.registry import Authenticator
from api_extractor.config.models import Source
from api_extractor.http.client import Client
from api_extractor.logs import REDACTED
from api_extractor.persist import envelope, manifest
from api_extractor.providers import registry
from api_extractor.runner import execute

BASE = "https://demo.example.com"


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))

    def add(name: str, rows: list[dict]) -> None:
        @registry.provider(name)
        def _fn(ctx, **args):
            return rows

    return add


@pytest.fixture
def client():
    with Client(retries=0) as made:
        yield made


def source_with(endpoints: dict, providers: dict | None = None, auth: dict | None = None) -> Source:
    body: dict = {
        "source": "demo",
        "base_url": BASE,
        "providers": providers or {},
        "endpoints": endpoints,
        "defaults": {"headers": {"Accept": "application/json"}, "retries": 0},
    }
    if auth is not None:
        body["auth"] = auth
    return Source.model_validate(body)


def run(source: Source, client, tmp_path, **kwargs):
    authenticator = Authenticator(source.auth, client, source.base_url)
    return execute(source, client, authenticator, output_root=tmp_path, **kwargs)


PING = {"ping": {"method": "GET", "path": "/ping", "output": "{source}/{endpoint}.json"}}


def test_a_single_request_is_issued_and_written(httpx_mock, client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    httpx_mock.add_response(url=f"{BASE}/ping", json={"pong": True})
    result = run(source_with(PING), client, tmp_path)
    assert (result.written, result.skipped, result.failed) == (1, 0, 0)
    written = envelope.read(tmp_path / "demo" / "ping.json")
    assert written["body"] == {"pong": True}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", written["metadata"]["extracted_at"])
    # The run id lives in the manifest, which is keyed by it — not in every file.
    assert manifest.read(result.manifest_path)[0]["run_id"] == result.run_id


def test_defaults_headers_are_sent(httpx_mock, client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    httpx_mock.add_response(json={})
    run(source_with(PING), client, tmp_path)
    assert httpx_mock.get_requests()[0].headers["Accept"] == "application/json"


def test_auth_headers_are_applied_to_every_request(httpx_mock, client, tmp_path, monkeypatch, fake):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("K", "key-abcdef123")
    fake("rows", [{"a": 1}, {"a": 2}])
    source = source_with(
        {
            "e": {
                "method": "GET",
                "path": "/e",
                "query": {"a": {"from": "p"}},
                "output": "{source}/{endpoint}_{a}.json",
            }
        },
        providers={"p": {"fn": "rows"}},
        auth={"type": "header", "headers": {"X-API-Key": "env:K"}},
    )
    httpx_mock.add_response(json={}, is_reusable=True)
    run(source, client, tmp_path)
    assert all(sent.headers["X-API-Key"] == "key-abcdef123" for sent in httpx_mock.get_requests())


def test_rerunning_is_a_no_op_without_force(httpx_mock, client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    httpx_mock.add_response(url=f"{BASE}/ping", json={"pong": True})
    source = source_with(PING)
    assert run(source, client, tmp_path).written == 1
    second = run(source, client, tmp_path)
    assert (second.written, second.skipped) == (0, 1)
    assert len(httpx_mock.get_requests()) == 1


def test_force_rewrites(httpx_mock, client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    httpx_mock.add_response(url=f"{BASE}/ping", json={"pong": True})
    httpx_mock.add_response(url=f"{BASE}/ping", json={"pong": False})
    source = source_with(PING)
    run(source, client, tmp_path)
    assert run(source, client, tmp_path, force=True).written == 1
    assert envelope.read(tmp_path / "demo" / "ping.json")["body"] == {"pong": False}


def test_a_skip_is_recorded_in_the_manifest(httpx_mock, client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    httpx_mock.add_response(url=f"{BASE}/ping", json={})
    source = source_with(PING)
    run(source, client, tmp_path)
    result = run(source, client, tmp_path)
    records = manifest.read(result.manifest_path)
    assert records[0]["status"] == "skipped"
    assert records[0]["output"].endswith("ping.json")


def test_an_error_response_is_saved_not_dropped(httpx_mock, client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    httpx_mock.add_response(url=f"{BASE}/ping", status_code=403, json={"error": "forbidden"})
    result = run(source_with(PING), client, tmp_path)
    assert result.failed == 1
    saved = envelope.read(tmp_path / "demo" / "ping.json")
    assert saved["metadata"]["response"]["status"] == 403
    assert saved["body"] == {"error": "forbidden"}


def test_one_dead_endpoint_does_not_cost_the_others(httpx_mock, client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    httpx_mock.add_exception(httpx.ConnectError("no route"), url=f"{BASE}/dead")
    httpx_mock.add_response(url=f"{BASE}/alive", json={"ok": True})
    source = source_with(
        {
            "dead": {"method": "GET", "path": "/dead", "output": "{source}/dead.json"},
            "alive": {"method": "GET", "path": "/alive", "output": "{source}/alive.json"},
        }
    )
    result = run(source, client, tmp_path)
    assert (result.written, result.failed) == (1, 1)
    assert (tmp_path / "demo" / "alive.json").is_file()
    assert not (tmp_path / "demo" / "dead.json").is_file()
    errors = [r for r in manifest.read(result.manifest_path) if r.get("status") == "error"]
    assert "ConnectError" in errors[0]["error"]


def test_an_unplannable_endpoint_is_recorded(httpx_mock, client, tmp_path, monkeypatch, fake):
    monkeypatch.chdir(tmp_path)
    httpx_mock.add_response(json={})

    @registry.provider("explodes")
    def _boom(ctx, **args):
        raise FileNotFoundError("input/missing.xlsx")

    source = source_with(
        {
            "broken": {"method": "GET", "path": "/broken", "query": {"a": {"from": "p"}}},
            "ping": PING["ping"],
        },
        providers={"p": {"fn": "explodes"}},
    )
    result = run(source, client, tmp_path)
    assert result.failed == 1
    unplanned = [r for r in manifest.read(result.manifest_path) if r.get("status") == "unplanned"]
    assert "missing.xlsx" in unplanned[0]["error"]


LOGIN = {
    "type": "login_token",
    "request": {"method": "POST", "path": "/auth/login", "payload": {"u": "env:U"}},
    "token_path": "$.token",
    "apply": {"header": "X-Authorization", "template": "Bearer {token}"},
}


def test_a_mid_run_401_refreshes_once_then_succeeds(httpx_mock, client, tmp_path, monkeypatch):
    """A chained fan-out can outlive its token; this will happen."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("U", "svc-account")
    httpx_mock.add_response(url=f"{BASE}/auth/login", json={"token": "stale-abc123"})
    httpx_mock.add_response(url=f"{BASE}/ping", status_code=401, json={})
    httpx_mock.add_response(url=f"{BASE}/auth/login", json={"token": "fresh-abc123"})
    httpx_mock.add_response(url=f"{BASE}/ping", json={"pong": True})

    result = run(source_with(PING, auth=LOGIN), client, tmp_path)
    assert result.written == 1
    pings = [r for r in httpx_mock.get_requests() if r.url.path == "/ping"]
    assert [r.headers["X-Authorization"] for r in pings] == [
        "Bearer stale-abc123",
        "Bearer fresh-abc123",
    ]


def test_a_persistent_401_gives_up_after_one_refresh(httpx_mock, client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("U", "svc-account")
    httpx_mock.add_response(url=f"{BASE}/auth/login", json={"token": "one-abc123"})
    httpx_mock.add_response(url=f"{BASE}/ping", status_code=401, json={})
    httpx_mock.add_response(url=f"{BASE}/auth/login", json={"token": "two-abc123"})
    httpx_mock.add_response(url=f"{BASE}/ping", status_code=401, json={"nope": True})

    result = run(source_with(PING, auth=LOGIN), client, tmp_path)
    assert result.failed == 1
    assert len([r for r in httpx_mock.get_requests() if r.url.path == "/ping"]) == 2
    assert envelope.read(tmp_path / "demo" / "ping.json")["metadata"]["response"]["status"] == 401


def test_no_secret_reaches_any_output_file(httpx_mock, client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("U", "svc-account")
    httpx_mock.add_response(url=f"{BASE}/auth/login", json={"token": "issued-abc123"})
    httpx_mock.add_response(url=f"{BASE}/ping", json={"pong": True})
    result = run(source_with(PING, auth=LOGIN), client, tmp_path)
    for path in [tmp_path / "demo" / "ping.json", result.manifest_path]:
        assert "issued-abc123" not in path.read_text(encoding="utf-8")


PAGED = {
    "things": {
        "method": "GET",
        "path": "/things",
        "query": {"per_page": 100},
        "paginate": {"style": "page_number", "at": "query.page", "has_more": "$.hasNext"},
        "output": "{source}/things_p{page}.json",
    }
}


def page(rows: list, has_next: bool) -> dict:
    return {"data": rows, "hasNext": has_next}


def test_a_walk_writes_one_file_per_page(httpx_mock, client, tmp_path, monkeypatch):
    """Never merged into one array — that would destroy which page a row came from."""
    monkeypatch.chdir(tmp_path)
    httpx_mock.add_response(url=f"{BASE}/things?per_page=100&page=0", json=page([1, 2], True))
    httpx_mock.add_response(url=f"{BASE}/things?per_page=100&page=1", json=page([3, 4], True))
    httpx_mock.add_response(url=f"{BASE}/things?per_page=100&page=2", json=page([5], False))

    result = run(source_with(PAGED), client, tmp_path)
    assert result.written == 3
    bodies = [
        envelope.read(tmp_path / "demo" / f"things_p{n}.json")["body"]["data"] for n in range(3)
    ]
    assert bodies == [[1, 2], [3, 4], [5]]
    # No top-level page number: the cursor is already in the query, verbatim, where the
    # API saw it — and the filename carries it too.
    assert [
        envelope.read(tmp_path / "demo" / f"things_p{n}.json")["metadata"]["request"]["query"]
        for n in range(3)
    ] == [{"per_page": 100, "page": 0}, {"per_page": 100, "page": 1}, {"per_page": 100, "page": 2}]


def test_the_cursor_increments_from_start(httpx_mock, client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    httpx_mock.add_response(json=page([1], True))
    httpx_mock.add_response(json=page([], False))
    run(source_with(PAGED), client, tmp_path)
    assert [r.url.params["page"] for r in httpx_mock.get_requests()] == ["0", "1"]


def test_a_walk_stops_on_an_empty_page_without_has_more(httpx_mock, client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    endpoints = {
        "things": {
            **PAGED["things"],
            "paginate": {"style": "page_number", "at": "query.page"},
        }
    }
    httpx_mock.add_response(json={"data": [1]})
    httpx_mock.add_response(json={"data": []})
    assert run(source_with(endpoints), client, tmp_path).written == 2


def test_max_pages_caps_a_stop_condition_that_never_fires(
    httpx_mock, client, tmp_path, monkeypatch, caplog
):
    """A misread stop condition must not become an unbounded loop on a production API."""
    monkeypatch.chdir(tmp_path)
    httpx_mock.add_response(json=page([1], True), is_reusable=True)
    source = Source.model_validate(
        {
            "source": "demo",
            "base_url": BASE,
            "endpoints": PAGED,
            "defaults": {"retries": 0, "max_pages": 4},
        }
    )
    result = run(source, client, tmp_path)
    assert result.written == 4
    assert len(httpx_mock.get_requests()) == 4
    assert "max_pages=4" in caplog.text


def test_the_cursor_goes_into_a_nested_payload(httpx_mock, client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    endpoints = {
        "search": {
            "method": "POST",
            "path": "/search",
            "payload": {"pageLink": {"pageSize": 100}},
            "paginate": {
                "style": "page_number",
                "at": "payload.pageLink.page",
                "has_more": "$.hasNext",
            },
            "output": "{source}/search_p{page}.json",
        }
    }
    httpx_mock.add_response(json=page([1], True))
    httpx_mock.add_response(json=page([2], False))
    run(source_with(endpoints), client, tmp_path)
    sent = [json.loads(request.content)["pageLink"] for request in httpx_mock.get_requests()]
    assert sent == [{"pageSize": 100, "page": 0}, {"pageSize": 100, "page": 1}]


def test_a_resumed_walk_continues_past_the_pages_it_already_has(
    httpx_mock, client, tmp_path, monkeypatch
):
    """Reading the skipped page back off disk is what lets the walk get to page 2."""
    monkeypatch.chdir(tmp_path)
    httpx_mock.add_response(url=f"{BASE}/things?per_page=100&page=0", json=page([1], True))
    httpx_mock.add_response(url=f"{BASE}/things?per_page=100&page=1", json=page([2], True))
    httpx_mock.add_response(url=f"{BASE}/things?per_page=100&page=2", json=page([3], False))
    source = source_with(PAGED)
    assert run(source, client, tmp_path).written == 3

    (tmp_path / "demo" / "things_p2.json").unlink()
    httpx_mock.add_response(url=f"{BASE}/things?per_page=100&page=2", json=page([3], False))
    second = run(source, client, tmp_path)
    assert (second.skipped, second.written) == (2, 1)


def test_a_failed_page_ends_the_walk(httpx_mock, client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    httpx_mock.add_response(url=f"{BASE}/things?per_page=100&page=0", json=page([1], True))
    httpx_mock.add_exception(
        httpx.ConnectError("dropped"), url=f"{BASE}/things?per_page=100&page=1"
    )
    result = run(source_with(PAGED), client, tmp_path)
    assert (result.written, result.failed) == (1, 1)
    assert len(httpx_mock.get_requests()) == 2


def test_each_page_is_its_own_manifest_line(httpx_mock, client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    httpx_mock.add_response(json=page([1], True))
    httpx_mock.add_response(json=page([2], False))
    result = run(source_with(PAGED), client, tmp_path)
    records = manifest.read(result.manifest_path)
    assert [r["page"] for r in records] == [0, 1]
    assert [r["output"].endswith(f"things_p{n}.json") for n, r in enumerate(records)] == [
        True,
        True,
    ]


def test_verbose_traces_the_request_without_leaking_the_credential(
    httpx_mock, client, tmp_path, monkeypatch, caplog
):
    """-v is for debugging a new source: you need the body you actually sent."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("K", "sk-live-9f3c8a21b4")
    source = source_with(
        {
            "search": {
                "method": "POST",
                "path": "/things/search",
                "payload": {"status": "ACTIVE"},
                "output": "{source}/search.json",
            }
        },
        auth={
            "type": "header",
            "headers": {"X-Authorization": {"value": "env:K", "template": "Api-Key {value}"}},
        },
    )
    httpx_mock.add_response(json={"data": [1, 2]})

    with caplog.at_level(logging.DEBUG, logger="api_extractor.runner"):
        run(source, client, tmp_path)

    assert "-> POST https://demo.example.com/things/search" in caplog.text
    assert '"status": "ACTIVE"' in caplog.text
    assert "<- 200" in caplog.text
    assert '{"data":[1,2]}' in caplog.text

    # Redacted at the call site, so the secret never reaches a log record at all —
    # the scrubbing formatter is the second line of defence, not the only one.
    assert "sk-live-9f3c8a21b4" not in caplog.text
    assert REDACTED in caplog.text


def test_a_long_body_is_truncated_in_the_trace(httpx_mock, client, tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    httpx_mock.add_response(json={"padding": "x" * 5000})
    with caplog.at_level(logging.DEBUG, logger="api_extractor.runner"):
        run(source_with(PING), client, tmp_path)
    assert "chars)" in caplog.text
    assert len(caplog.text) < 3000


def test_the_manifest_indexes_every_attempt(httpx_mock, client, tmp_path, monkeypatch, fake):
    monkeypatch.chdir(tmp_path)
    fake("rows", [{"a": 1}, {"a": 2}, {"a": 3}])
    httpx_mock.add_response(json={"ok": True}, is_reusable=True)
    source = source_with(
        {
            "e": {
                "method": "GET",
                "path": "/e",
                "query": {"a": {"from": "p"}},
                "output": "{source}/{endpoint}_{a}.json",
            }
        },
        providers={"p": {"fn": "rows"}},
    )
    result = run(source, client, tmp_path)
    records = manifest.read(result.manifest_path)
    assert len(records) == 3
    assert [r["params"]["a"] for r in records] == [1, 2, 3]
    assert all(r["status"] == 200 and r["run_id"] == result.run_id for r in records)
