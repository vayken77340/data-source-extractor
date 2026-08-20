from __future__ import annotations

import pytest

from api_extractor.config import graph
from api_extractor.config.loader import load_source
from api_extractor.config.models import Source
from tests.conftest import REFERENCE_SOURCE


def build(providers: dict, endpoints: dict) -> Source:
    return Source.model_validate(
        {
            "source": "demo",
            "base_url": "https://demo.example.com",
            "providers": providers,
            "endpoints": endpoints,
        }
    )


def chain(endpoint: str) -> dict:
    return {"fn": "from_output", "args": {"endpoint": endpoint, "path": "$.x"}}


def uses(provider: str) -> dict:
    return {"method": "GET", "path": "/x", "query": {"v": {"from": provider}}}


def test_dependency_comes_from_the_provider_not_a_yaml_key():
    source = load_source(REFERENCE_SOURCE)
    assert graph.dependencies(source)["measures"] == {"assets"}
    assert graph.dependencies(source)["assets"] == set()


def test_the_reference_source_puts_measures_after_assets():
    order = graph.topological_order(load_source(REFERENCE_SOURCE))
    assert order.index("assets") < order.index("measures")


def test_order_is_alphabetical_within_a_level():
    """Deterministic on purpose — --dry-run output should not shuffle between runs."""
    source = build({}, {"c": uses_none(), "a": uses_none(), "b": uses_none()})
    assert graph.topological_order(source) == ["a", "b", "c"]


def uses_none() -> dict:
    return {"method": "GET", "path": "/x"}


def test_a_chain_of_three_orders_correctly():
    source = build(
        {"from_a": chain("a"), "from_b": chain("b")},
        {"a": uses_none(), "b": uses("from_a"), "c": uses("from_b")},
    )
    assert graph.topological_order(source) == ["a", "b", "c"]


def test_cycle_is_detected_and_named():
    source = build(
        {"from_g": chain("g"), "from_h": chain("h")},
        {"g": uses("from_h"), "h": uses("from_g")},
    )
    with pytest.raises(graph.CycleError, match="g -> h -> g") as caught:
        graph.topological_order(source)
    assert caught.value.cycle == ["g", "h", "g"]


def test_self_cycle_is_detected():
    source = build({"from_a": chain("a")}, {"a": uses("from_a")})
    with pytest.raises(graph.CycleError, match="a -> a"):
        graph.topological_order(source)


def test_edges_to_undeclared_endpoints_are_dropped_for_ordering():
    """`depends_on_targets` reports those; ordering must not trip over them first."""
    source = build({"from_ghost": chain("ghost")}, {"a": uses("from_ghost")})
    assert graph.dependencies(source)["a"] == {"ghost"}
    assert graph.known(graph.dependencies(source))["a"] == set()
    assert graph.topological_order(source) == ["a"]


def test_an_unregistered_function_contributes_no_edges():
    """Silent here so `fn_registered` can report it with a better message."""
    source = build({"mystery": {"fn": "nope", "args": {"endpoint": "a"}}}, {"a": uses("mystery")})
    assert graph.provider_dependencies(source, "mystery") == []


def test_bad_args_contribute_no_edges():
    """depends_on would KeyError on these; `args_match` reports them instead."""
    source = build({"typo": {"fn": "from_output", "args": {"endpoints": "a"}}}, {"a": uses("typo")})
    assert graph.provider_dependencies(source, "typo") == []
