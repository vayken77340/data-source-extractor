"""Tests for `providers/from_output_joined.py` — a local provider, not the framework's.

The provider exists so that a value and whatever the parameter file associates with its
origin arrive on one row. Most of these are therefore about which pairs do *not* come out.

Nothing here is about assets or measures beyond the fixture names: the last few tests join
an entirely different domain through the same arguments, which is the point of it being
generic rather than a `measure_keys_for_assets`.
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

ASSET_ARGS = {"path": "$.data[*].id.id", "join_on": "assetType", "select": "measureType"}


@pytest.fixture(scope="module")
def from_output_joined():
    providers.load_from(REPO_ROOT / "providers")
    return registry.get("from_output_joined").fn


@pytest.fixture
def params(tmp_path) -> Path:
    return write_params(tmp_path / "asset_types.json", ["assetType", "measureType"], REFERENCE)


def write_params(path: Path, columns: list[str], rows: list[dict]) -> Path:
    path.write_text(json.dumps({"columns": columns, "rows": rows}), encoding="utf-8")
    return path


def envelope_path(name: str) -> Path:
    return Path(f"output/demo/assets/{name}.json")


def saved_output(name: str, params: dict, body: dict) -> SavedOutput:
    envelope = {"metadata": {"params": params}, "body": body}
    return SavedOutput(path=envelope_path(name), envelope=envelope)


def assets(name: str, asset_type: str, ids: list[str]) -> SavedOutput:
    """What the runner writes for one page of `POST /assets/search`."""
    return saved_output(
        name, {"assetType": asset_type}, {"data": [{"id": {"id": value}} for value in ids]}
    )


def context(*saved: SavedOutput) -> ProviderContext:
    return ProviderContext(
        run_id="test-run",
        output_root=Path("output"),
        source_name="demo",
        outputs_for=lambda endpoint: list(saved),
    )


def test_it_is_registered_and_declares_its_dependency():
    entry = registry.get("from_output_joined")
    assert "from_output_joined" not in providers.BUILTINS
    assert entry.endpoints_needed({"endpoint": "assets"}) == ["assets"]


def test_each_value_gets_only_what_its_own_key_selects(params, from_output_joined):
    ctx = context(assets("pump", "PUMP", ["a1", "a2"]), assets("valve", "VALVE", ["b1"]))
    rows = from_output_joined(ctx, endpoint="assets", file=str(params), **ASSET_ARGS)
    assert [(row["id"], row["measureType"]) for row in rows] == [
        ("a1", "temperature,humidity"),
        ("a2", "temperature,humidity"),
        ("b1", "pressure"),
    ]


def test_a_valve_is_never_paired_with_a_pumps_measures(params, from_output_joined):
    """The whole reason this is one provider rather than two crossed ones."""
    ctx = context(assets("valve", "VALVE", ["b1"]))
    rows = from_output_joined(ctx, endpoint="assets", file=str(params), **ASSET_ARGS)
    assert [row["measureType"] for row in rows] == ["pressure"]


def test_the_value_field_is_named_after_the_json_path(params, from_output_joined):
    """The same rule `from_output` uses, so `bind: {id: ...}` lines up by name."""
    ctx = context(assets("pump", "PUMP", ["a1"]))
    (row,) = from_output_joined(ctx, endpoint="assets", file=str(params), **ASSET_ARGS)
    assert set(row) == {"id", "assetType", "measureType", "__parents__"}


def test_records_the_envelope_each_value_came_from(params, from_output_joined):
    ctx = context(assets("pump", "PUMP", ["a1"]))
    (row,) = from_output_joined(ctx, endpoint="assets", file=str(params), **ASSET_ARGS)
    assert row["__parents__"] == [str(envelope_path("pump"))]


def test_a_value_seen_twice_is_one_row_with_two_parents(params, from_output_joined):
    ctx = context(assets("pump_p0", "PUMP", ["a1"]), assets("pump_p1", "PUMP", ["a1"]))
    (row,) = from_output_joined(ctx, endpoint="assets", file=str(params), **ASSET_ARGS)
    assert row["__parents__"] == [str(envelope_path("pump_p0")), str(envelope_path("pump_p1"))]


def test_a_key_absent_from_the_file_yields_nothing(params, from_output_joined):
    """Nothing associated means no request worth making, not a request with empty values."""
    ctx = context(assets("tank", "TANK", ["c1"]))
    assert from_output_joined(ctx, endpoint="assets", file=str(params), **ASSET_ARGS) == []


def test_an_envelope_without_the_join_key_yields_nothing(params, from_output_joined):
    ctx = context(saved_output("x", {}, {"data": [{"id": {"id": "a1"}}]}))
    assert from_output_joined(ctx, endpoint="assets", file=str(params), **ASSET_ARGS) == []


def test_nothing_on_disk_yet(params, from_output_joined):
    ctx = ProviderContext(run_id="r", output_root=Path("output"), source_name="demo")
    assert from_output_joined(ctx, endpoint="assets", file=str(params), **ASSET_ARGS) == []


def test_the_separator_is_an_argument(params, from_output_joined):
    ctx = context(assets("pump", "PUMP", ["a1"]))
    (row,) = from_output_joined(
        ctx, endpoint="assets", file=str(params), separator="|", **ASSET_ARGS
    )
    assert row["measureType"] == "temperature|humidity"


def test_missing_column_names_the_ones_present(tmp_path, from_output_joined):
    params = write_params(tmp_path / "p.json", ["assetType"], [])
    ctx = context(assets("pump", "PUMP", ["a1"]))
    with pytest.raises(ValueError, match="columns: assetType"):
        from_output_joined(ctx, endpoint="assets", file=str(params), **ASSET_ARGS)


def test_missing_parameter_file_says_how_to_make_one(tmp_path, from_output_joined):
    ctx = context(assets("pump", "PUMP", ["a1"]))
    with pytest.raises(FileNotFoundError, match="tools/build_params.py"):
        from_output_joined(ctx, endpoint="assets", file=str(tmp_path / "nope.json"), **ASSET_ARGS)


def test_a_path_and_select_that_collide_are_refused(tmp_path, from_output_joined):
    """Both land on one row, so two fields of one name would silently lose one."""
    params = write_params(tmp_path / "p.json", ["region", "tier"], [{"region": "EU", "tier": "a"}])
    ctx = context(saved_output("x", {"region": "EU"}, {"data": [{"tier": "z"}]}))
    with pytest.raises(ValueError, match=r"\['tier'\] would appear twice on one row"):
        from_output_joined(
            ctx, endpoint="things", file=str(params), path="$.data[*].tier", join_on="region",
            select="tier",
        )


# ── Nothing about assets ───────────────────────────────────────────────────────────────
# The same arguments over an unrelated domain. If these need the provider changed, it was
# not generic.


def test_it_joins_a_domain_it_knows_nothing_about(tmp_path, from_output_joined):
    params = write_params(
        tmp_path / "regions.json",
        ["region", "currency"],
        [
            {"region": "EU", "currency": "EUR"},
            {"region": "EU", "currency": "CHF"},
            {"region": "US", "currency": "USD"},
        ],
    )
    ctx = context(
        saved_output("eu", {"region": "EU"}, {"items": [{"sku": "s-1"}, {"sku": "s-2"}]}),
        saved_output("us", {"region": "US"}, {"items": [{"sku": "s-9"}]}),
    )
    rows = from_output_joined(
        ctx,
        endpoint="catalogue",
        file=str(params),
        path="$.items[*].sku",
        join_on="region",
        select="currency",
    )
    assert [(row["sku"], row["currency"]) for row in rows] == [
        ("s-1", "EUR,CHF"),
        ("s-2", "EUR,CHF"),
        ("s-9", "USD"),
    ]


def test_the_whole_two_hop_chain_plans(params, write_source, from_output_joined):
    """Through the real planner: order is derived, and both params come off one row.

    The assertion that matters is the one that is not here — nothing pairs b1 with
    temperature, which two providers crossed by `fan_out: product` would have produced.
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
                    "fn": "from_output_joined",
                    "args": {"endpoint": "assets", "file": str(params), **ASSET_ARGS},
                },
            },
            "endpoints": {
                # Declared measures-first, so a passing order proves it was derived.
                "measures": {
                    "method": "GET",
                    "path": "/assets/{id}/measures",
                    "bind": {"id": {"from": "asset_measures"}},
                    "query": {"keys": {"from": "asset_measures", "as": "measureType"}},
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
    ctx = context(assets("pump", "PUMP", ["a1", "a2"]), assets("valve", "VALVE", ["b1"]))
    plan = binding.build_plan(source, ctx)

    assert plan.order == ("assets", "measures")
    measures = next(entry for entry in plan.endpoints if entry.endpoint == "measures")
    assert measures.error is None
    assert [(spec.path, spec.query["keys"]) for spec in measures.requests] == [
        ("/assets/a1/measures", "temperature,humidity"),
        ("/assets/a2/measures", "temperature,humidity"),
        ("/assets/b1/measures", "pressure"),
    ]
