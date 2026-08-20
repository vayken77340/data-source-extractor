from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from api_extractor.config.models import FromMarker, Source
from api_extractor.plan.binding import (
    Binding,
    build_plan,
    combine,
    effective_limit,
    render_output,
    slug_for,
    slugify,
)
from api_extractor.providers import registry
from api_extractor.providers.registry import ProviderContext

CTX = ProviderContext(run_id="test-run", output_root=Path("output"), source_name="demo")


@pytest.fixture
def fake(monkeypatch):
    """Register throwaway providers into a copy of the registry."""
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    calls: dict[str, int] = {}

    def add(name: str, rows: list[dict[str, Any]] | Exception, **kwargs) -> None:
        @registry.provider(name, **kwargs)
        def _fn(ctx, **args):
            calls[name] = calls.get(name, 0) + 1
            if isinstance(rows, Exception):
                raise rows
            return rows

    add.calls = calls  # type: ignore[attr-defined]
    return add


def source_with(providers: dict, endpoints: dict, defaults: dict | None = None) -> Source:
    body: dict[str, Any] = {
        "source": "demo",
        "base_url": "https://demo.example.com",
        "providers": providers,
        "endpoints": endpoints,
    }
    if defaults is not None:
        body["defaults"] = defaults
    return Source.model_validate(body)


def params_of(plan, endpoint: str = "e") -> list[dict[str, Any]]:
    (item,) = [p for p in plan.endpoints if p.endpoint == endpoint]
    assert item.error is None, item.error
    return [dict(request.params) for request in item.requests]


def requests_of(plan, endpoint: str = "e"):
    (item,) = [p for p in plan.endpoints if p.endpoint == endpoint]
    assert item.error is None, item.error
    return item.requests


def has_marker(node: Any) -> bool:
    if isinstance(node, FromMarker):
        return True
    if isinstance(node, dict):
        return any(has_marker(value) for value in node.values())
    if isinstance(node, list):
        return any(has_marker(value) for value in node)
    return False


# --- fan out ------------------------------------------------------------------------


def test_product_crosses_separate_providers(fake):
    fake("rows_a", [{"a": 1}, {"a": 2}])
    fake("rows_b", [{"b": "x"}, {"b": "y"}])
    plan = build_plan(
        source_with(
            {"pa": {"fn": "rows_a"}, "pb": {"fn": "rows_b"}},
            {
                "e": {
                    "method": "GET",
                    "path": "/e",
                    "query": {"a": {"from": "pa"}, "b": {"from": "pb"}},
                }
            },
        ),
        CTX,
        no_limit=True,
    )
    assert params_of(plan) == [
        {"a": 1, "b": "x"},
        {"a": 1, "b": "y"},
        {"a": 2, "b": "x"},
        {"a": 2, "b": "y"},
    ]


def test_zip_pairs_providers_positionally(fake):
    fake("rows_a", [{"a": 1}, {"a": 2}])
    fake("rows_b", [{"b": "x"}, {"b": "y"}])
    plan = build_plan(
        source_with(
            {"pa": {"fn": "rows_a"}, "pb": {"fn": "rows_b"}},
            {
                "e": {
                    "method": "GET",
                    "path": "/e",
                    "query": {"a": {"from": "pa"}, "b": {"from": "pb"}},
                    "fan_out": "zip",
                }
            },
        ),
        CTX,
        no_limit=True,
    )
    assert params_of(plan) == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]


def test_zip_of_unequal_lengths_raises_naming_the_providers():
    groups = [
        (
            "asset_types",
            [Binding(params={"a": 1}), Binding(params={"a": 2}), Binding(params={"a": 3})],
        ),
        ("regions", [Binding(params={"b": "x"})]),
    ]
    with pytest.raises(ValueError, match="asset_types: 3, regions: 1"):
        combine(groups, "zip")


def test_zip_mismatch_is_reported_not_truncated(fake):
    fake("rows_a", [{"a": 1}, {"a": 2}, {"a": 3}])
    fake("rows_b", [{"b": "x"}])
    plan = build_plan(
        source_with(
            {"pa": {"fn": "rows_a"}, "pb": {"fn": "rows_b"}},
            {
                "e": {
                    "method": "GET",
                    "path": "/e",
                    "query": {"a": {"from": "pa"}, "b": {"from": "pb"}},
                    "fan_out": "zip",
                }
            },
        ),
        CTX,
        no_limit=True,
    )
    assert "must yield the same number of rows" in plan.endpoints[0].error


def test_fields_from_one_provider_stay_row_wise(fake):
    """EU/gold and US/silver — never the cross product."""
    fake("rows", [{"region": "EU", "tier": "gold"}, {"region": "US", "tier": "silver"}])
    plan = build_plan(
        source_with(
            {"p": {"fn": "rows"}},
            {
                "e": {
                    "method": "GET",
                    "path": "/e",
                    "query": {"region": {"from": "p"}, "tier": {"from": "p"}},
                }
            },
        ),
        CTX,
        no_limit=True,
    )
    assert params_of(plan) == [
        {"region": "EU", "tier": "gold"},
        {"region": "US", "tier": "silver"},
    ]


def test_no_markers_means_exactly_one_request(fake):
    plan = build_plan(source_with({}, {"e": {"method": "GET", "path": "/e"}}), CTX)
    assert params_of(plan) == [{}]


def test_a_provider_yielding_nothing_means_no_requests(fake):
    fake("rows", [])
    plan = build_plan(
        source_with(
            {"p": {"fn": "rows"}},
            {"e": {"method": "GET", "path": "/e", "query": {"a": {"from": "p"}}}},
        ),
        CTX,
    )
    assert params_of(plan) == []


# --- picking a value off a row ------------------------------------------------------


def test_a_single_field_row_is_unwrapped_whatever_it_is_called(fake):
    """`type: {from: asset_types}` against rows of `{assetType: ...}`."""
    fake("rows", [{"assetType": "PUMP"}])
    plan = build_plan(
        source_with(
            {"p": {"fn": "rows"}},
            {"e": {"method": "GET", "path": "/e", "query": {"type": {"from": "p"}}}},
        ),
        CTX,
    )
    assert params_of(plan) == [{"type": "PUMP"}]


def test_a_multi_field_row_selects_by_param_name(fake):
    fake("rows", [{"region": "EU", "tier": "gold"}])
    plan = build_plan(
        source_with(
            {"p": {"fn": "rows"}},
            {"e": {"method": "GET", "path": "/e", "query": {"tier": {"from": "p"}}}},
        ),
        CTX,
    )
    assert params_of(plan) == [{"tier": "gold"}]


def test_a_multi_field_row_with_no_match_says_what_it_does_have(fake):
    fake("rows", [{"region": "EU", "tier": "gold"}])
    plan = build_plan(
        source_with(
            {"p": {"fn": "rows"}},
            {"e": {"method": "GET", "path": "/e", "query": {"colour": {"from": "p"}}}},
        ),
        CTX,
    )
    error = plan.endpoints[0].error
    assert "has no field 'colour'" in error
    assert "['region', 'tier']" in error


# --- lineage ------------------------------------------------------------------------


def test_reserved_keys_become_parents_and_never_params(fake):
    fake("rows", [{"id": "a1", "__parents__": ["output/demo/assets/pump_p0.json"]}])
    plan = build_plan(
        source_with(
            {"p": {"fn": "rows"}},
            {"e": {"method": "GET", "path": "/e", "query": {"id": {"from": "p"}}}},
        ),
        CTX,
    )
    (request,) = requests_of(plan)
    assert dict(request.params) == {"id": "a1"}
    assert request.parents == ("output/demo/assets/pump_p0.json",)


def test_a_product_of_two_chained_providers_has_two_parents(fake):
    fake("rows_a", [{"a": 1, "__parents__": ["one.json"]}])
    fake("rows_b", [{"b": 2, "__parents__": ["two.json"]}])
    plan = build_plan(
        source_with(
            {"pa": {"fn": "rows_a"}, "pb": {"fn": "rows_b"}},
            {
                "e": {
                    "method": "GET",
                    "path": "/e",
                    "query": {"a": {"from": "pa"}, "b": {"from": "pb"}},
                }
            },
        ),
        CTX,
    )
    assert requests_of(plan)[0].parents == ("one.json", "two.json")


def test_duplicate_parents_collapse(fake):
    fake("rows_a", [{"a": 1, "__parents__": ["same.json"]}])
    fake("rows_b", [{"b": 2, "__parents__": ["same.json"]}])
    plan = build_plan(
        source_with(
            {"pa": {"fn": "rows_a"}, "pb": {"fn": "rows_b"}},
            {
                "e": {
                    "method": "GET",
                    "path": "/e",
                    "query": {"a": {"from": "pa"}, "b": {"from": "pb"}},
                }
            },
        ),
        CTX,
    )
    assert requests_of(plan)[0].parents == ("same.json",)


# --- substitution -------------------------------------------------------------------


def test_markers_are_filled_at_any_depth(fake):
    fake("types", [{"assetType": "PUMP"}])
    fake("regions", [{"region": "EU"}])
    plan = build_plan(
        source_with(
            {"t": {"fn": "types"}, "r": {"fn": "regions"}},
            {
                "e": {
                    "method": "POST",
                    "path": "/e",
                    "payload": {
                        "pageSize": 100,
                        "filters": [
                            {"key": "assetType", "value": {"from": "t", "as": "assetType"}}
                        ],
                        "scope": {"region": {"from": "r"}},
                    },
                }
            },
        ),
        CTX,
    )
    (request,) = requests_of(plan)
    assert request.payload == {
        "pageSize": 100,
        "filters": [{"key": "assetType", "value": "PUMP"}],
        "scope": {"region": "EU"},
    }
    assert not has_marker(request.payload)


def test_path_placeholders_are_filled_from_bind(fake):
    fake("ids", [{"id": "9f3c"}])
    plan = build_plan(
        source_with(
            {"p": {"fn": "ids"}},
            {
                "e": {
                    "method": "GET",
                    "path": "/assets/{id}/measures",
                    "bind": {"id": {"from": "p"}},
                }
            },
        ),
        CTX,
    )
    assert requests_of(plan)[0].path == "/assets/9f3c/measures"


# --- limits -------------------------------------------------------------------------


def test_limit_cascades_default_then_endpoint_then_cli():
    source = source_with(
        {},
        {
            "inherits": {"method": "GET", "path": "/a"},
            "override": {"method": "GET", "path": "/b", "limit": 20},
            "unlimited": {"method": "GET", "path": "/c", "limit": None},
        },
        defaults={"limit": 5},
    )
    assert effective_limit(source, "inherits", None, False) == 5
    assert effective_limit(source, "override", None, False) == 20
    assert effective_limit(source, "unlimited", None, False) is None
    assert effective_limit(source, "override", 3, False) == 3
    assert effective_limit(source, "override", None, True) is None


def test_limit_caps_values_fanned_out(fake):
    fake("rows", [{"a": n} for n in range(10)])
    source = source_with(
        {"p": {"fn": "rows"}},
        {"e": {"method": "GET", "path": "/e", "query": {"a": {"from": "p"}}}},
        defaults={"limit": 3},
    )
    assert len(params_of(build_plan(source, CTX))) == 3
    assert len(params_of(build_plan(source, CTX, limit=2))) == 2
    assert len(params_of(build_plan(source, CTX, no_limit=True))) == 10


def test_limit_is_per_provider_so_a_product_still_multiplies(fake):
    fake("rows_a", [{"a": n} for n in range(10)])
    fake("rows_b", [{"b": n} for n in range(10)])
    source = source_with(
        {"pa": {"fn": "rows_a"}, "pb": {"fn": "rows_b"}},
        {"e": {"method": "GET", "path": "/e", "query": {"a": {"from": "pa"}, "b": {"from": "pb"}}}},
        defaults={"limit": 3},
    )
    assert len(params_of(build_plan(source, CTX))) == 9


# --- output paths -------------------------------------------------------------------


def test_slugify_lowercases_and_collapses():
    assert slugify("PUMP/EU") == "pump-eu"
    assert slugify("a  b__c") == "a-b-c"
    assert slugify("--PUMP--") == "pump"


def test_slugify_truncates_with_a_hash_so_paths_stay_unique():
    long_a = "x" * 150 + "-one"
    long_b = "x" * 150 + "-two"
    assert len(slugify(long_a)) == 100
    assert slugify(long_a) != slugify(long_b)


def test_empty_params_get_a_readable_slug():
    assert slug_for({}) == "all"


def test_slug_is_stable_regardless_of_param_order():
    assert slug_for({"b": 2, "a": 1}) == slug_for({"a": 1, "b": 2})


def test_output_template_fills_params_and_intrinsics():
    template = "output/{source}/assets/{assetType}_p{page}.json"
    rendered = render_output(template, "thingsboard", "assets", {"assetType": "PUMP"}, page=2)
    assert rendered == "output/thingsboard/assets/PUMP_p2.json"


def test_default_output_template_uses_the_slug(fake):
    fake("rows", [{"assetType": "PUMP"}])
    plan = build_plan(
        source_with(
            {"p": {"fn": "rows"}},
            {"e": {"method": "GET", "path": "/e", "query": {"type": {"from": "p"}}}},
        ),
        CTX,
    )
    assert requests_of(plan)[0].output() == "output/demo/e/pump.json"


def test_same_params_and_page_give_the_same_path(fake):
    """Reruns are idempotent: identical params land on identical paths."""
    fake("rows", [{"a": "PUMP"}, {"a": "PUMP"}])
    plan = build_plan(
        source_with(
            {"p": {"fn": "rows"}},
            {
                "e": {
                    "method": "GET",
                    "path": "/e",
                    "query": {"a": {"from": "p"}},
                    "paginate": {"style": "page_number", "at": "query.page"},
                    "output": "output/{source}/{endpoint}/{slug}_p{page}.json",
                }
            },
        ),
        CTX,
    )
    first, second = requests_of(plan)
    assert first.output(0) == second.output(0) == "output/demo/e/pump_p0.json"
    assert first.output(1) == "output/demo/e/pump_p1.json"


# --- whole plans --------------------------------------------------------------------


def test_order_comes_from_the_graph(fake):
    fake("rows", [{"id": 1}])
    fake("chained", [{"id": 2}], depends_on=lambda args: [args["endpoint"]])
    source = source_with(
        {"p": {"fn": "rows"}, "c": {"fn": "chained", "args": {"endpoint": "b"}}},
        {
            "a": {"method": "GET", "path": "/a", "query": {"id": {"from": "c"}}},
            "b": {"method": "GET", "path": "/b", "query": {"id": {"from": "p"}}},
        },
    )
    assert build_plan(source, CTX).order == ("b", "a")


def test_endpoint_subset(fake):
    fake("rows", [{"a": 1}])
    source = source_with(
        {"p": {"fn": "rows"}},
        {
            "e": {"method": "GET", "path": "/e", "query": {"a": {"from": "p"}}},
            "other": {"method": "GET", "path": "/other"},
        },
    )
    plan = build_plan(source, CTX, only=("e",))
    assert plan.order == ("e",)
    assert [item.endpoint for item in plan.endpoints] == ["e"]


def test_a_provider_runs_once_however_many_endpoints_name_it(fake):
    fake("rows", [{"a": 1}])
    source = source_with(
        {"p": {"fn": "rows"}},
        {
            "one": {"method": "GET", "path": "/one", "query": {"a": {"from": "p"}}},
            "two": {"method": "GET", "path": "/two", "query": {"a": {"from": "p"}}},
        },
    )
    build_plan(source, CTX)
    assert fake.calls["rows"] == 1


def test_one_unplannable_endpoint_does_not_hide_the_others(fake):
    """Seeing the other nineteen is worth more than failing on the twentieth."""
    fake("good", [{"a": 1}])
    fake("bad", FileNotFoundError("no such file: input/missing.xlsx"))
    source = source_with(
        {"g": {"fn": "good"}, "b": {"fn": "bad"}},
        {
            "works": {"method": "GET", "path": "/works", "query": {"a": {"from": "g"}}},
            "broken": {"method": "GET", "path": "/broken", "query": {"a": {"from": "b"}}},
        },
    )
    plan = build_plan(source, CTX)
    by_name = {item.endpoint: item for item in plan.endpoints}
    assert by_name["works"].error is None and len(by_name["works"].requests) == 1
    assert "input/missing.xlsx" in by_name["broken"].error
    assert plan.request_count == 1
    assert [item.endpoint for item in plan.failures] == ["broken"]


def test_plan_reports_rows_per_provider(fake):
    fake("rows", [{"a": n} for n in range(7)])
    source = source_with(
        {"p": {"fn": "rows"}},
        {"e": {"method": "GET", "path": "/e", "query": {"a": {"from": "p"}}}},
        defaults={"limit": 3},
    )
    plan = build_plan(source, CTX)
    assert plan.provider_rows == {"p": 7}
    assert plan.request_count == 3
