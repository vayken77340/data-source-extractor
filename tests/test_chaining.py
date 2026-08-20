"""Phase 6: `from_output`, and the reference source running end to end."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from api_extractor.auth.registry import Authenticator
from api_extractor.config.loader import load_source
from api_extractor.config.models import Source
from api_extractor.http.client import Client
from api_extractor.persist import envelope, manifest
from api_extractor.providers.builtin import field_name, from_output
from api_extractor.providers.registry import ProviderContext, SavedOutput
from api_extractor.runner import execute, read_outputs, search_root
from tests.conftest import REFERENCE_BASE_URL, REFERENCE_SOURCE

BASE = "https://demo.example.com"
TB = REFERENCE_BASE_URL


@pytest.fixture
def client():
    with Client(retries=0) as made:
        yield made


def ctx_with(outputs: dict[str, list[SavedOutput]]) -> ProviderContext:
    return ProviderContext(
        run_id="t",
        output_root=Path("output"),
        source_name="demo",
        outputs_for=lambda endpoint: outputs.get(endpoint, []),
    )


def saved(path: str, body: object) -> SavedOutput:
    return SavedOutput(path=Path(path), envelope={"body": body})


# --- from_output as a pure provider -------------------------------------------------


def test_reads_a_jsonpath_out_of_saved_envelopes():
    ctx = ctx_with({"assets": [saved("output/assets_p0.json", {"data": [{"id": {"id": "a1"}}]})]})
    assert from_output(ctx, endpoint="assets", path="$.data[*].id.id") == [
        {"id": "a1", "__parents__": [str(Path("output/assets_p0.json"))]}
    ]


def test_every_page_contributes():
    ctx = ctx_with(
        {
            "assets": [
                saved("p0.json", {"data": [{"id": "a1"}, {"id": "a2"}]}),
                saved("p1.json", {"data": [{"id": "a3"}]}),
            ]
        }
    )
    rows = from_output(ctx, endpoint="assets", path="$.data[*].id")
    assert [row["id"] for row in rows] == ["a1", "a2", "a3"]
    assert [row["__parents__"] for row in rows] == [["p0.json"], ["p0.json"], ["p1.json"]]


def test_a_value_in_two_envelopes_is_one_row_with_two_parents():
    """An asset can surface under two asset types; both paths are worth keeping."""
    ctx = ctx_with(
        {
            "assets": [
                saved("pump_p0.json", {"data": [{"id": "shared"}]}),
                saved("valve_p0.json", {"data": [{"id": "shared"}]}),
            ]
        }
    )
    assert from_output(ctx, endpoint="assets", path="$.data[*].id") == [
        {"id": "shared", "__parents__": ["pump_p0.json", "valve_p0.json"]}
    ]


def test_nulls_are_skipped():
    ctx = ctx_with({"assets": [saved("p0.json", {"data": [{"id": "a1"}, {"id": None}]})]})
    assert [row["id"] for row in from_output(ctx, endpoint="assets", path="$.data[*].id")] == ["a1"]


def test_no_outputs_means_no_rows():
    assert from_output(ctx_with({}), endpoint="assets", path="$.data[*].id") == []


def test_a_path_that_matches_nothing_means_no_rows():
    ctx = ctx_with({"assets": [saved("p0.json", {"data": [{"id": "a1"}]})]})
    assert from_output(ctx, endpoint="assets", path="$.nope[*].id") == []


def test_values_of_different_types_do_not_collide():
    ctx = ctx_with({"assets": [saved("p0.json", {"data": [1, "1"]})]})
    assert len(from_output(ctx, endpoint="assets", path="$.data[*]")) == 2


@pytest.mark.parametrize(
    ("json_path", "expected"),
    [("$.data[*].id.id", "id"), ("$.token", "token"), ("$.items[*].assetType", "assetType")],
)
def test_the_row_field_is_named_after_the_path(json_path, expected):
    assert field_name(json_path) == expected


# --- finding envelopes on disk ------------------------------------------------------


def test_search_root_comes_from_the_literal_head_of_the_template():
    source = Source.model_validate(
        {
            "source": "thingsboard",
            "base_url": BASE,
            "endpoints": {
                "assets": {
                    "method": "GET",
                    "path": "/a",
                    "output": "output/{source}/assets/{assetType}_p{page}.json",
                },
                "plain": {"method": "GET", "path": "/p"},
            },
        }
    )
    assert search_root(source, "assets") == Path("output/thingsboard/assets")
    assert search_root(source, "plain") == Path("output/thingsboard/plain")


def test_only_this_endpoint_and_source_are_read(tmp_path, monkeypatch):
    """A shared directory or a stray file must not be mistaken for someone else's output."""
    monkeypatch.chdir(tmp_path)
    source = Source.model_validate(
        {
            "source": "demo",
            "base_url": BASE,
            "endpoints": {"assets": {"method": "GET", "path": "/a", "output": "out/{slug}.json"}},
        }
    )
    envelope.write(
        Path("out/mine.json"), {"metadata": {"endpoint": "assets", "source": "demo"}, "body": {}}
    )
    envelope.write(
        Path("out/theirs.json"), {"metadata": {"endpoint": "other", "source": "demo"}, "body": {}}
    )
    envelope.write(
        Path("out/elsewhere.json"),
        {"metadata": {"endpoint": "assets", "source": "another"}, "body": {}},
    )
    Path("out/garbage.json").write_text("not json at all", encoding="utf-8")

    found = read_outputs(source, "assets")
    assert [item.path.name for item in found] == ["mine.json"]


def test_reading_a_directory_that_does_not_exist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = Source.model_validate(
        {"source": "demo", "base_url": BASE, "endpoints": {"a": {"method": "GET", "path": "/a"}}}
    )
    assert read_outputs(source, "a") == []


# --- the reference source, end to end -----------------------------------------------


def asset_page(asset_type: str, ids: list[str], has_next: bool) -> dict:
    return {"data": [{"id": {"id": f"{asset_type}-{i}"}} for i in ids], "hasNext": has_next}


@pytest.fixture
def reference(monkeypatch, tmp_path, reference_env):
    """The suite's own source: asset types come from `literal`, so no file is needed."""
    monkeypatch.chdir(tmp_path)
    return load_source(REFERENCE_SOURCE)


def test_the_reference_source_runs_end_to_end(reference, httpx_mock, client, tmp_path):
    """§12: envelopes for all four endpoint shapes, measures chained off assets."""
    source = reference
    httpx_mock.add_response(url=f"{TB}/tenant/info", json={"tenant": "acme"})
    httpx_mock.add_response(url=f"{TB}/alarms?pageSize=100&searchStatus=ACTIVE&type=PUMP", json=[])
    httpx_mock.add_response(url=f"{TB}/alarms?pageSize=100&searchStatus=ACTIVE&type=VALVE", json=[])

    def per_asset_type(request: httpx.Request) -> httpx.Response:
        asset_type = json.loads(request.content)["assetType"]
        return httpx.Response(200, json=asset_page(asset_type, ["1", "2"], False))

    httpx_mock.add_callback(per_asset_type, url=f"{TB}/assets/search", is_reusable=True)
    for asset_type in ("PUMP", "VALVE"):
        for index in ("1", "2"):
            httpx_mock.add_response(
                url=f"{TB}/assets/{asset_type}-{index}/measures?keys=temperature%2Chumidity",
                json={"values": []},
            )

    result = execute(
        source, client, Authenticator(source.auth, client, source.base_url), output_root=tmp_path
    )

    # 1 tenant_info + 2 alarms + 2 assets + 4 measures
    assert (result.written, result.failed) == (9, 0)
    assert (tmp_path / f"output/{source.source}/tenant_info/all.json").is_file()
    assert (tmp_path / f"output/{source.source}/alarms/PUMP.json").is_file()
    assert (tmp_path / f"output/{source.source}/assets/VALVE_p0.json").is_file()

    measures = envelope.read(tmp_path / f"output/{source.source}/measures/PUMP-1.json")
    assert measures["metadata"]["params"] == {"id": "PUMP-1"}
    assert measures["metadata"]["parents"] == [str(Path(f"output/{source.source}/assets/PUMP_p0.json"))]


def test_an_asset_under_two_types_carries_both_parents(reference, httpx_mock, client, tmp_path):
    """The reason `parents` is a list at all."""
    source = reference
    httpx_mock.add_response(url=f"{TB}/tenant/info", json={})
    httpx_mock.add_response(url=f"{TB}/alarms?pageSize=100&searchStatus=ACTIVE&type=PUMP", json=[])
    httpx_mock.add_response(url=f"{TB}/alarms?pageSize=100&searchStatus=ACTIVE&type=VALVE", json=[])
    httpx_mock.add_response(
        url=f"{TB}/assets/search", json=asset_page("SHARED", ["1"], False), is_reusable=True
    )
    httpx_mock.add_response(
        url=f"{TB}/assets/SHARED-1/measures?keys=temperature%2Chumidity", json={}
    )

    execute(
        source, client, Authenticator(source.auth, client, source.base_url), output_root=tmp_path
    )

    measures = envelope.read(tmp_path / f"output/{source.source}/measures/SHARED-1.json")
    assert measures["metadata"]["parents"] == [
        str(Path(f"output/{source.source}/assets/PUMP_p0.json")),
        str(Path(f"output/{source.source}/assets/VALVE_p0.json")),
    ]


def test_basic_auth_is_applied_and_never_written(reference, httpx_mock, client, tmp_path):
    source = reference
    httpx_mock.add_response(json={"data": [], "hasNext": False}, is_reusable=True)
    execute(
        source,
        client,
        Authenticator(source.auth, client, source.base_url),
        output_root=tmp_path,
        only=("tenant_info",),
    )
    sent = httpx_mock.get_requests()[0]
    assert sent.headers["Authorization"].startswith("Basic ")
    written = (tmp_path / f"output/{source.source}/tenant_info/all.json").read_text(encoding="utf-8")
    assert "hunter2" not in written and "***REDACTED***" in written


def test_running_measures_alone_uses_yesterdays_asset_files(
    reference, httpx_mock, client, tmp_path
):
    """`--endpoint measures` works without re-hitting /assets/search."""
    source = reference
    envelope.write(
        tmp_path / f"output/{source.source}/assets/PUMP_p0.json",
        {
            "metadata": {"endpoint": "assets", "source": source.source},
            "body": asset_page("PUMP", ["7"], False),
        },
    )
    httpx_mock.add_response(url=f"{TB}/assets/PUMP-7/measures?keys=temperature%2Chumidity", json={})

    result = execute(
        source,
        client,
        Authenticator(source.auth, client, source.base_url),
        output_root=tmp_path,
        only=("measures",),
    )
    assert result.written == 1
    assert [r.url.path for r in httpx_mock.get_requests()] == ["/api/assets/PUMP-7/measures"]
    written = envelope.read(tmp_path / f"output/{source.source}/measures/PUMP-7.json")
    assert written["metadata"]["parents"] == [str(Path(f"output/{source.source}/assets/PUMP_p0.json"))]


def test_the_manifest_records_lineage(reference, httpx_mock, client, tmp_path):
    source = reference
    envelope.write(
        tmp_path / f"output/{source.source}/assets/PUMP_p0.json",
        {
            "metadata": {"endpoint": "assets", "source": source.source},
            "body": asset_page("PUMP", ["7"], False),
        },
    )
    httpx_mock.add_response(json={}, is_reusable=True)
    result = execute(
        source,
        client,
        Authenticator(source.auth, client, source.base_url),
        output_root=tmp_path,
        only=("measures",),
    )
    (record,) = manifest.read(result.manifest_path)
    assert record["params"] == {"id": "PUMP-7"}
    assert record["parents"] == [str(Path(f"output/{source.source}/assets/PUMP_p0.json"))]
