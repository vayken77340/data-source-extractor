"""Tests for `providers/measures_for_assets.py` — a local provider, not the framework's.

The point of the provider is that a valve is never asked for its temperature, so most of
these are about which pairs do *not* come out.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api_extractor import providers
from api_extractor.config.loader import load_source
from api_extractor.plan import binding
from api_extractor.providers import registry
from api_extractor.providers.registry import ProviderContext, SavedOutput
from tests.conftest import REPO_ROOT

REFERENCE = [
    {"assetType": "PUMP", "measureType": "temperature"},
    {"assetType": "PUMP", "measureType": "humidity"},
    {"assetType": "VALVE", "measureType": "pressure"},
]


@pytest.fixture(scope="module")
def measure_keys_for_assets():
    providers.load_from(REPO_ROOT / "providers")
    return registry.get("measure_keys_for_assets").fn


@pytest.fixture
def params(tmp_path) -> Path:
    path = tmp_path / "asset_types.json"
    path.write_text(
        json.dumps({"columns": ["assetType", "measureType"], "rows": REFERENCE}), encoding="utf-8"
    )
    return path


def envelope(asset_type: str, ids: list[str]) -> dict:
    """What the runner writes for one page of `POST /assets/search`."""
    return {
        "metadata": {"endpoint": "assets", "params": {"assetType": asset_type}},
        "body": {"data": [{"id": {"id": value}} for value in ids]},
    }


def context(*saved: SavedOutput) -> ProviderContext:
    return ProviderContext(
        run_id="test-run",
        output_root=Path("output"),
        source_name="demo",
        outputs_for=lambda endpoint: list(saved),
    )


def envelope_path(name: str) -> Path:
    return Path(f"output/demo/assets/{name}.json")


def saved_output(name: str, asset_type: str, ids: list[str]) -> SavedOutput:
    return SavedOutput(path=envelope_path(name), envelope=envelope(asset_type, ids))


def test_it_is_registered_and_declares_its_dependency():
    entry = registry.get("measure_keys_for_assets")
    assert "measure_keys_for_assets" not in providers.BUILTINS
    assert entry.endpoints_needed({"endpoint": "assets", "path": "p.json"}) == ["assets"]


def test_each_asset_gets_only_its_own_types_measures(params, measure_keys_for_assets):
    ctx = context(
        saved_output("pump_p0", "PUMP", ["a1", "a2"]),
        saved_output("valve_p0", "VALVE", ["b1"]),
    )
    rows = measure_keys_for_assets(ctx, path=str(params), endpoint="assets")
    assert [(row["id"], row["keys"]) for row in rows] == [
        ("a1", "temperature,humidity"),
        ("a2", "temperature,humidity"),
        ("b1", "pressure"),
    ]


def test_a_valve_is_never_asked_for_its_temperature(params, measure_keys_for_assets):
    """The whole reason this provider exists rather than a fan_out: product."""
    ctx = context(saved_output("valve_p0", "VALVE", ["b1"]))
    rows = measure_keys_for_assets(ctx, path=str(params), endpoint="assets")
    assert [row["keys"] for row in rows] == ["pressure"]


def test_records_the_envelope_each_id_came_from(params, measure_keys_for_assets):
    ctx = context(saved_output("pump_p0", "PUMP", ["a1"]))
    (row,) = measure_keys_for_assets(ctx, path=str(params), endpoint="assets")
    assert row["__parents__"] == [str(envelope_path("pump_p0"))]


def test_an_id_seen_twice_is_one_row_with_two_parents(params, measure_keys_for_assets):
    ctx = context(
        saved_output("pump_p0", "PUMP", ["a1"]),
        saved_output("pump_p1", "PUMP", ["a1"]),
    )
    (row,) = measure_keys_for_assets(ctx, path=str(params), endpoint="assets")
    assert row["__parents__"] == [str(envelope_path("pump_p0")), str(envelope_path("pump_p1"))]


def test_a_type_absent_from_the_sheet_yields_nothing(params, measure_keys_for_assets):
    """No measures on file means no request worth making, not a request with empty keys."""
    ctx = context(saved_output("tank_p0", "TANK", ["c1"]))
    assert measure_keys_for_assets(ctx, path=str(params), endpoint="assets") == []


def test_an_envelope_without_the_join_key_yields_nothing(params, measure_keys_for_assets):
    ctx = context(SavedOutput(path=envelope_path("x"), envelope={"body": {"data": []}}))
    assert measure_keys_for_assets(ctx, path=str(params), endpoint="assets") == []


def test_nothing_on_disk_yet(params, measure_keys_for_assets):
    ctx = ProviderContext(run_id="r", output_root=Path("output"), source_name="demo")
    assert measure_keys_for_assets(ctx, path=str(params), endpoint="assets") == []


def test_the_columns_and_separator_are_arguments(tmp_path, measure_keys_for_assets):
    path = tmp_path / "p.json"
    path.write_text(
        json.dumps(
            {
                "columns": ["kind", "metric"],
                "rows": [{"kind": "PUMP", "metric": "t"}, {"kind": "PUMP", "metric": "h"}],
            }
        ),
        encoding="utf-8",
    )
    ctx = context(
        SavedOutput(
            path=Path("output/demo/assets/p0.json"),
            envelope={
                "metadata": {"params": {"kind": "PUMP"}},
                "body": {"data": [{"id": {"id": "a1"}}]},
            },
        )
    )
    (row,) = measure_keys_for_assets(
        ctx, path=str(path), endpoint="assets", type_column="kind", measure_column="metric",
        separator="|",
    )
    assert row["keys"] == "t|h"


def test_missing_column_names_the_ones_present(tmp_path, measure_keys_for_assets):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"columns": ["assetType"], "rows": []}), encoding="utf-8")
    ctx = context(saved_output("p0", "PUMP", ["a1"]))
    with pytest.raises(ValueError, match="columns: assetType"):
        measure_keys_for_assets(ctx, path=str(path), endpoint="assets")


def test_missing_parameter_file_says_how_to_make_one(tmp_path, measure_keys_for_assets):
    ctx = context(saved_output("p0", "PUMP", ["a1"]))
    with pytest.raises(FileNotFoundError, match="tools/build_params.py"):
        measure_keys_for_assets(ctx, path=str(tmp_path / "nope.json"), endpoint="assets")


def test_the_whole_two_hop_chain_plans(params, write_source, measure_keys_for_assets):
    """Through the real planner: order is derived, and both params come off one row.

    The assertion that matters is the one that isn't here — no request pairs b1 with
    temperature, which `fan_out: product` across two providers would have produced.
    """
    path = write_source(
        {
            "source": "demo",
            "base_url": "https://demo.example.com",
            "providers": {
                "types": {
                    "fn": "param_file",
                    "args": {"path": str(params), "columns": ["assetType"]},
                },
                "asset_measures": {
                    "fn": "measure_keys_for_assets",
                    "args": {"path": str(params), "endpoint": "assets"},
                },
            },
            "endpoints": {
                # Declared measures-first, so a passing order proves it was derived.
                "measures": {
                    "method": "GET",
                    "path": "/assets/{id}/measures",
                    "bind": {"id": {"from": "asset_measures"}},
                    "query": {"keys": {"from": "asset_measures"}},
                    "output": "output/{source}/measures/{id}.json",
                },
                "assets": {
                    "method": "POST",
                    "path": "/assets/search",
                    "payload": {"assetType": {"from": "types"}},
                    "output": "output/{source}/assets/{assetType}.json",
                },
            },
        }
    )
    source = load_source(path)
    ctx = context(
        saved_output("pump", "PUMP", ["a1", "a2"]),
        saved_output("valve", "VALVE", ["b1"]),
    )
    plan = binding.build_plan(source, ctx)

    assert plan.order == ("assets", "measures")
    measures = next(entry for entry in plan.endpoints if entry.endpoint == "measures")
    assert measures.error is None
    assert [(spec.path, spec.query["keys"]) for spec in measures.requests] == [
        ("/assets/a1/measures", "temperature,humidity"),
        ("/assets/a2/measures", "temperature,humidity"),
        ("/assets/b1/measures", "pressure"),
    ]
