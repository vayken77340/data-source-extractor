from __future__ import annotations

import copy

import pytest

from api_extractor.config import validate as validate_module
from api_extractor.config.validate import (
    EXPECTED_CHECK_IDS,
    PARSE_CHECK_ID,
    assert_registry,
    validate_source,
)
from tests.conftest import REFERENCE_SOURCE

# One file that trips every Phase 1 check, so the aggregation test is not a fiction.
BROKEN = {
    "source": "broken",
    "base_url": "https://x.example.com",
    "auth": {"type": "bearer", "token": "env:DEFINITELY_NOT_SET"},
    "providers": {
        "types": {"fn": "literal", "args": {}},  # args_match: `values` missing
        "unknown_fn": {"fn": "nope", "args": {}},  # fn_registered
        "orphan": {"fn": "from_output", "args": {"endpoint": "ghost", "path": "$.x"}},
        "loop_a": {"fn": "from_output", "args": {"endpoint": "g", "path": "$.x"}},
        "loop_b": {"fn": "from_output", "args": {"endpoint": "h", "path": "$.x"}},
    },
    "endpoints": {
        "a": {
            "method": "GET",
            "path": "/a/{id}",
            "query": {"t": {"from": "nope"}},
            "output": "output/{missing}.json",
        },
        "b": {
            "method": "POST",
            "path": "/b",
            "bind": {"x": {"from": "types"}},
            "payload": {
                "assetType": {"from": "types"},
                "nested": {"assetType": {"from": "types"}},
            },
        },
        "c": {
            "method": "GET",
            "path": "/c",
            "query": {"t": {"from": "types", "az": "typo"}},
        },
        "d": {
            "method": "GET",
            "path": "/d",
            "query": {"v": {"from": "types", "as": "__parents__"}},
            "output": "output/{__parents__}.json",
        },
        "e": {
            "method": "GET",
            "path": "/e",
            "query": {"cursor": {"from": "types"}},
            "paginate": {"style": "page_number", "at": "query.cursor"},
        },
        "f": {
            "method": "POST",
            "path": "/f",
            "paginate": {"style": "page_number", "at": "payload.pageLink.page"},
        },
        # g and h chain off each other: dag.acyclic
        "g": {"method": "GET", "path": "/g", "query": {"v": {"from": "loop_b"}}},
        "h": {"method": "GET", "path": "/h", "query": {"v": {"from": "loop_a"}}},
        # a label absent from `output`, so it shapes nothing: label.shapes_output
        "i": {"method": "GET", "path": "/i", "label": {"region": {"from": "types"}}},
    },
}


def checks_that_fired(report) -> set[str]:
    return {issue.check for issue in report.issues}


def messages(report) -> str:
    return "\n".join(str(issue) for issue in report.issues)


@pytest.fixture
def env_set(monkeypatch, reference_env):
    monkeypatch.delenv("DEFINITELY_NOT_SET", raising=False)


def test_registry_matches_the_declared_set():
    assert_registry()
    assert set(validate_module._CHECKS) == EXPECTED_CHECK_IDS


def test_a_check_that_fails_to_register_is_caught(monkeypatch):
    """A vacuous pass is worse than a missing check, so the mismatch must raise."""
    shrunk = dict(validate_module._CHECKS)
    shrunk.pop("config.env.vars_set")
    monkeypatch.setattr(validate_module, "_CHECKS", shrunk)
    with pytest.raises(RuntimeError, match="config.env.vars_set"):
        assert_registry()


def test_reference_source_is_valid(env_set):
    report = validate_source(REFERENCE_SOURCE)
    assert report.ok, messages(report)
    assert set(report.checks_run) == EXPECTED_CHECK_IDS | {PARSE_CHECK_ID}


def test_nothing_is_deferred_since_phase_2(env_set):
    """`depends_on` made the dependency graph a config-layer artifact, so config
    validation is now complete — but the mechanism stays for later phases."""
    report = validate_source(REFERENCE_SOURCE)
    assert report.checks_deferred == ()
    assert not set(report.checks_deferred) & set(report.checks_run)


def test_every_check_reports_in_one_pass(env_set, write_source):
    report = validate_source(write_source(BROKEN, "broken"))
    assert checks_that_fired(report) == EXPECTED_CHECK_IDS, messages(report)


def test_undeclared_provider(env_set, write_source):
    report = validate_source(write_source(BROKEN, "broken"))
    text = messages(report)
    assert "endpoints.a.query.t: {from: nope} is not a declared provider" in text
    assert "types" in text


def test_unregistered_provider_function(env_set, write_source):
    report = validate_source(write_source(BROKEN, "broken"))
    text = messages(report)
    assert "providers.unknown_fn.fn: 'nope' is not a registered provider function" in text
    assert "from_output" in text and "literal" in text


def test_provider_args_must_fit_the_function(env_set, write_source):
    report = validate_source(write_source(BROKEN, "broken"))
    text = messages(report)
    assert "providers.types.args: do not fit literal(values)" in text


def test_a_typo_in_provider_args_does_not_crash_depends_on(env_set, write_source):
    """`endpoints:` instead of `endpoint:` would KeyError inside from_output's lambda."""
    data = copy.deepcopy(BROKEN)
    data["providers"]["orphan"] = {"fn": "from_output", "args": {"endpoints": "a", "path": "$.x"}}
    report = validate_source(write_source(data, "typo"))
    text = messages(report)
    assert "providers.orphan.args: do not fit from_output(endpoint, path)" in text
    assert "config.providers.depends_on_targets" not in text


def test_chaining_off_an_endpoint_that_does_not_exist(env_set, write_source):
    report = validate_source(write_source(BROKEN, "broken"))
    assert "providers.orphan.args: chains off endpoint 'ghost', which is not declared" in messages(
        report
    )


def test_dependency_cycle_is_reported_with_its_path(env_set, write_source):
    report = validate_source(write_source(BROKEN, "broken"))
    assert "dependency cycle: g -> h -> g" in messages(report)


def test_malformed_marker_is_not_silently_literal(env_set, write_source):
    report = validate_source(write_source(BROKEN, "broken"))
    assert "endpoints.c.query.t: looks like a {from: types} marker" in messages(report)
    assert "['az']" in messages(report)


def test_param_collision(env_set, write_source):
    report = validate_source(write_source(BROKEN, "broken"))
    text = messages(report)
    assert "two markers both resolve to param 'assetType'" in text
    assert "payload.assetType" in text and "payload.nested.assetType" in text


def test_collision_is_fixed_by_as(env_set, write_source):
    data = copy.deepcopy(BROKEN)
    data["endpoints"]["b"]["payload"]["nested"]["assetType"] = {"from": "types", "as": "inner"}
    report = validate_source(write_source(data, "fixed"))
    assert "config.params.no_collision" not in checks_that_fired(report)


def test_path_and_bind_must_match_both_ways(env_set, write_source):
    report = validate_source(write_source(BROKEN, "broken"))
    text = messages(report)
    assert "endpoints.a.path: {id} in `path` has no matching entry in `bind`" in text
    assert "endpoints.b.bind.x: bound but `path` has no {x} placeholder" in text


def test_output_placeholder_must_resolve(env_set, write_source):
    report = validate_source(write_source(BROKEN, "broken"))
    assert "endpoints.a.output: {missing} does not resolve" in messages(report)


def test_reserved_namespace_is_refused(env_set, write_source):
    report = validate_source(write_source(BROKEN, "broken"))
    text = messages(report)
    assert "`as: __parents__` is reserved" in text
    assert "endpoints.d.output: {__parents__} is in the reserved" in text


def test_page_cursor_must_not_overwrite_a_marker(env_set, write_source):
    report = validate_source(write_source(BROKEN, "broken"))
    assert "'query.cursor' already holds a {from: types} marker" in messages(report)


def test_page_cursor_into_a_payload_that_does_not_exist(env_set, write_source):
    report = validate_source(write_source(BROKEN, "broken"))
    assert "'payload.pageLink.page' targets the payload, but this endpoint has none" in messages(
        report
    )


def test_a_paginated_endpoint_needs_page_in_its_output_path(env_set, write_source):
    """Otherwise every page overwrites the last, and 'one file per page' is a lie."""
    report = validate_source(write_source(BROKEN, "broken"))
    assert "has no {page}, so every page of this paginated endpoint" in messages(report)


def test_every_missing_env_var_is_reported(env_set, write_source):
    data = copy.deepcopy(BROKEN)
    data["auth"] = {
        "type": "basic",
        "username": "env:MISSING_ONE",
        "password": "env:MISSING_TWO",
    }
    report = validate_source(write_source(data, "envs"))
    env_issues = [i for i in report.issues if i.check == "config.env.vars_set"]
    assert {i.message for i in env_issues} == {
        "environment variable MISSING_ONE is not set",
        "environment variable MISSING_TWO is not set",
    }


def test_marker_without_a_name_asks_for_as(env_set, write_source):
    data = {
        "source": "demo",
        "base_url": "https://demo.example.com",
        "providers": {"types": {"fn": "literal", "args": {}}},
        "endpoints": {"e": {"method": "POST", "path": "/e", "payload": {"from": "types"}}},
    }
    report = validate_source(write_source(data, "unnamed"))
    assert "add `as: <name>`" in messages(report)


def test_parse_failure_reports_all_shape_errors(env_set, write_source):
    data = {
        "source": "demo",
        "base_url": "https://demo.example.com",
        "endpoints": {
            "a": {"method": "FETCH", "path": "/a"},
            "b": {"method": "GET", "path": "/b", "fan_out": "cartesian"},
        },
    }
    report = validate_source(write_source(data, "shape"))
    assert not report.ok
    assert {i.check for i in report.issues} == {PARSE_CHECK_ID}
    assert len(report.issues) >= 2
    assert report.checks_run == (PARSE_CHECK_ID,)


def test_missing_file_is_an_issue_not_a_crash(tmp_path):
    report = validate_source(tmp_path / "absent.yaml")
    assert not report.ok
    assert "cannot read file" in messages(report)


def test_malformed_yaml_is_an_issue_not_a_crash(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("source: demo\n  bad: [indent\n", encoding="utf-8")
    report = validate_source(path)
    assert not report.ok
    assert "invalid YAML" in messages(report)
