from __future__ import annotations

import json

from api_extractor.http.client import Request, Response
from api_extractor.logs import REDACTED
from api_extractor.persist import envelope, manifest
from api_extractor.plan.binding import RequestSpec

SPEC = RequestSpec(
    source="thingsboard",
    endpoint="measures",
    method="GET",
    path="/assets/9f3c/measures",
    query={"keys": "temperature,humidity"},
    payload=None,
    params={"id": "9f3c"},
    parents=("output/thingsboard/assets/PUMP_p0.json",),
    output_template="output/{source}/measures/{id}.json",
)

REQUEST = Request(
    method="GET",
    url="https://tb.example.com/api/assets/9f3c/measures",
    query={"keys": "temperature,humidity"},
    headers={
        "Accept": "application/json",
        "X-Authorization": "Bearer super-secret-token",
        "X-EDF-APIKey": "another-secret",
    },
)


def response(**overrides) -> Response:
    base = {
        "status": 200,
        "headers": {"content-type": "application/json"},
        "elapsed_ms": 143,
        "text": '{"ok":true}',
        "body": {"ok": True},
        "parsed": True,
    }
    return Response(**{**base, **overrides})


def build(**overrides) -> dict:
    kwargs = {
        "run_id": "20260820T101203Z-a1b2c3",
        "spec": SPEC,
        "request": REQUEST,
        "response": response(),
        "page": 0,
        "fetched_at": "2026-08-20T10:12:03Z",
        "auth_headers": ("X-EDF-APIKey",),
    }
    return envelope.build(**{**kwargs, **overrides})


def test_envelope_shape():
    metadata = build()["metadata"]
    assert metadata["run_id"] == "20260820T101203Z-a1b2c3"
    assert metadata["source"] == "thingsboard"
    assert metadata["endpoint"] == "measures"
    assert metadata["params"] == {"id": "9f3c"}
    assert metadata["page"] == 0
    assert metadata["parents"] == ["output/thingsboard/assets/PUMP_p0.json"]
    assert metadata["response"] == {
        "status": 200,
        "headers": {"content-type": "application/json"},
        "elapsed_ms": 143,
    }


def test_a_known_sensitive_header_is_redacted():
    headers = build()["metadata"]["request"]["headers"]
    assert headers["X-Authorization"] == REDACTED
    assert headers["Accept"] == "application/json"


def test_a_header_the_auth_layer_applied_is_redacted_by_construction():
    """X-EDF-APIKey is in no standard list — it is redacted because auth set it."""
    assert build()["metadata"]["request"]["headers"]["X-EDF-APIKey"] == REDACTED


def test_redaction_is_case_insensitive():
    assert envelope.redact_headers({"AUTHORIZATION": "x"})["AUTHORIZATION"] == REDACTED
    assert envelope.redact_headers({"x-edf-apikey": "x"}, ["X-EDF-APIKey"])["x-edf-apikey"] == (
        REDACTED
    )


def test_the_body_is_untouched():
    """No reordering, no coercion, no unwrapping."""
    body = {"z": 1, "a": {"nested": [1, "2", None, True]}, "m": None}
    written = build(response=response(body=body))["body"]
    assert written == body
    assert list(written) == ["z", "a", "m"]


def test_a_non_json_body_lands_in_body_raw():
    result = build(response=response(body=None, parsed=False, text="<html>nope</html>"))
    assert result["body"] is None
    assert result["body_raw"] == "<html>nope</html>"


def test_an_error_response_is_still_an_envelope():
    """A 403 is real information about the API and belongs on disk."""
    result = build(response=response(status=403, body={"error": "forbidden"}))
    assert result["metadata"]["response"]["status"] == 403
    assert result["body"] == {"error": "forbidden"}


def test_write_and_read_round_trip(tmp_path):
    path = tmp_path / "nested" / "deep" / "out.json"
    envelope.write(path, build())
    assert path.is_file()
    assert envelope.read(path)["metadata"]["endpoint"] == "measures"


def test_no_secret_reaches_the_file(tmp_path):
    path = tmp_path / "out.json"
    envelope.write(path, build())
    text = path.read_text(encoding="utf-8")
    assert "super-secret-token" not in text
    assert "another-secret" not in text
    assert REDACTED in text


# --- manifest -----------------------------------------------------------------------


def test_run_ids_are_unique_and_sortable():
    ids = {manifest.new_run_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(run_id.split("-")) == 2 for run_id in ids)


def test_manifest_is_one_json_line_per_record(tmp_path):
    path = manifest.path_for(tmp_path, "run-1")
    assert path == tmp_path / "_runs" / "run-1.jsonl"
    manifest.append(path, {"endpoint": "assets", "status": 200})
    manifest.append(path, {"endpoint": "assets", "status": "skipped"})
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert [json.loads(line)["status"] for line in lines] == [200, "skipped"]
    assert manifest.read(path)[0]["endpoint"] == "assets"


def test_reading_a_manifest_that_does_not_exist(tmp_path):
    assert manifest.read(tmp_path / "_runs" / "nope.jsonl") == []
