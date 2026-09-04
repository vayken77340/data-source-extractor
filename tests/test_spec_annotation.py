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


# --- starting one from a source ------------------------------------------------------


@pytest.fixture
def scaffolded(reference_source, tmp_path):
    from specgen import scaffold

    path = tmp_path / "reference.spec.yaml"
    path.write_text(scaffold.build(reference_source, "reference"), encoding="utf-8")
    return path


def test_a_scaffold_covers_every_endpoint_in_reading_order(scaffolded):
    annotation = load_annotation(scaffolded)
    assert list(annotation.endpoints) == ["assets", "alarms", "tenant_info", "measures"]


def test_a_scaffold_passes_every_check(reference_source, scaffolded, reference_env):
    """A starting point that fails its own checks would be worse than a blank page. In
    particular its landing key must resolve, vary per request and vary per page, for any
    source whatever its endpoints."""
    from specgen import check, evidence, model

    annotation = load_annotation(scaffolded)
    report = check.run(
        check.Context(
            source=reference_source,
            annotation=annotation,
            annotation_raw=yaml.safe_load(scaffolded.read_text(encoding="utf-8")),
            built=model.build(reference_source, annotation),
            evidence=evidence.Evidence(),
        )
    )
    assert report.ok, [str(issue) for issue in report.issues]


def test_a_scaffold_answers_nothing_and_says_so(reference_source, scaffolded):
    from specgen import model

    annotation = load_annotation(scaffolded)
    done = model.build(reference_source, annotation).model["completeness"]
    assert (done["filled"], done["percent"]) == (0, 0.0)
    assert done["todo"] > 20


def test_a_scaffold_carries_what_the_source_already_knows(reference_source, scaffolded):
    """Derived hints, so the person filling it in is not re-reading the YAML."""
    text = scaffolded.read_text(encoding="utf-8")
    assert "# POST /assets/search — drives measures; paginated" in text
    assert "# GET /assets/{id}/measures — runs after assets" in text
    assert "# GET /tenant/info — no parameters" in text
    assert "asset_types" in text and "asset_ids" in text  # both providers offered under `lists`


def test_optional_blocks_are_offered_commented_out(scaffolded):
    """An absent optional produces no row, so a stub that became an empty section would
    be worse than none — but the block still has to be discoverable."""
    text = scaffolded.read_text(encoding="utf-8")
    for block in ("definitions:", "environments:", "lists:", "quirks:"):
        assert f"# {block}" in text or f"# {block}"[:14] in text
    assert load_annotation(scaffolded).definitions == {}


def test_missing_names_only_what_a_grown_source_lacks(reference_source, reference_annotation):
    from specgen import scaffold

    assert scaffold.missing(reference_source, reference_annotation) is None
    del reference_annotation.endpoints["measures"]
    fragment = scaffold.missing(reference_source, reference_annotation)
    assert "measures:" in fragment and "assets:" not in fragment
    assert "runs after assets" in fragment


def test_init_never_overwrites_an_existing_annotation(reference_source, tmp_path, capsys):
    """The prose in an existing file is the whole value of it."""
    import importlib.util

    from tests.conftest import REPO_ROOT

    spec = importlib.util.spec_from_file_location("build_spec_under_test", REPO_ROOT / "tools" / "build_spec.py")
    build_spec = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_spec)

    path = tmp_path / "reference.spec.yaml"
    assert build_spec._init(reference_source, path) == 0
    written = path.read_text(encoding="utf-8")

    assert build_spec._init(reference_source, path) == 0
    assert path.read_text(encoding="utf-8") == written
    assert "will not be overwritten" in capsys.readouterr().out
