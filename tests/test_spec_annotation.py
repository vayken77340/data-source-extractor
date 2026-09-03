"""The annotation file's shape: what it must carry, what it may, and what it may never.

The rule under test is "never restate the source YAML": a field the generator can read
elsewhere is an unknown key here, mechanically.
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from specgen.annotation import Annotation, EndpointAnnotation, load_annotation
from specgen.labels import TODO
from tests.conftest import REFERENCE_SPEC

MINIMAL = {
    "spec": {"version": "0.1", "status": "draft", "date": "04/09/2026", "source_system": "X"},
    "secrets": "Coffre",
    "landing": {"key": "source={source}/entity={endpoint}/{slug}_p{page}.json"},
}


def parse(**overrides) -> Annotation:
    return Annotation.model_validate({**MINIMAL, **overrides})


def errors(data) -> list[str]:
    with pytest.raises(ValidationError) as caught:
        Annotation.model_validate(data)
    return [".".join(str(p) for p in e["loc"]) + ": " + e["msg"] for e in caught.value.errors()]


def test_the_reference_annotation_parses():
    annotation = load_annotation(REFERENCE_SPEC)
    assert set(annotation.endpoints) == {"tenant_info", "alarms", "assets", "measures"}
    assert annotation.lists["asset_types"].name == "types d'actifs"


def test_the_minimum_is_document_control_a_vault_and_a_key():
    annotation = parse()
    assert annotation.spec.owner == TODO
    assert annotation.landing.bucket == TODO
    assert annotation.environments == {} and annotation.endpoints == {}


@pytest.mark.parametrize(
    "extra, loc",
    [
        ({"base_url": "https://x"}, "base_url"),
        ({"landing": {**MINIMAL["landing"], "retention": "90j"}}, "landing.retention"),
        ({"endpoints": {"a": {"method": "GET"}}}, "endpoints.a.method"),
        ({"endpoints": {"a": {"paginate": {}}}}, "endpoints.a.paginate"),
        ({"spec": {**MINIMAL["spec"], "api_version": "v2"}}, "spec.api_version"),
    ],
)
def test_anything_the_source_yaml_knows_is_an_unknown_key_here(extra, loc):
    found = errors({**MINIMAL, **extra})
    assert any(message.startswith(loc) for message in found), found


def test_the_marker_is_accepted_in_place_of_an_enum():
    annotation = parse(endpoints={"a": {"mode": TODO}})
    assert annotation.endpoints["a"].mode == TODO


def test_an_unknown_mode_is_not():
    assert any("endpoints.a.mode" in m for m in errors({**MINIMAL, "endpoints": {"a": {"mode": "delta"}}}))


def test_dates_are_jj_mm_aaaa():
    assert any("JJ/MM/AAAA" in m for m in errors({**MINIMAL, "spec": {**MINIMAL["spec"], "date": "2026-09-04"}}))


def test_a_missing_endpoint_annotation_is_all_markers_not_a_crash():
    """Coverage is a check, not an exception: the rest of the document still builds."""
    annotation = parse()
    assert annotation.endpoint("ghost") == EndpointAnnotation()
    assert annotation.endpoint("ghost").purpose == TODO


def test_a_per_endpoint_key_overrides_the_landing_key():
    annotation = parse(endpoints={"a": {"key": "a/{slug}.json"}, "b": {}})
    assert annotation.landing_key("a") == "a/{slug}.json"
    assert annotation.landing_key("b") == MINIMAL["landing"]["key"]


def test_optional_blocks_default_to_nothing():
    endpoint = parse(endpoints={"a": {}}).endpoints["a"]
    assert endpoint.quirks == [] and endpoint.response is None and endpoint.sample is None


def test_yaml_round_trip(tmp_path):
    path = tmp_path / "x.spec.yaml"
    path.write_text(yaml.safe_dump(MINIMAL, allow_unicode=True), encoding="utf-8")
    assert load_annotation(path).secrets == "Coffre"
