"""`--check`: one broken annotation per check id, and a clean one that passes them all."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from api_extractor.providers.registry import SavedOutput
from specgen import check, contract, evidence as ev, model
from specgen.annotation import Annotation
from tests.conftest import REFERENCE_SPEC, build_envelope

RAW = yaml.safe_load(REFERENCE_SPEC.read_text(encoding="utf-8"))


def context(source, raw=None, *, evidence=None, **overrides) -> check.Context:
    raw = copy.deepcopy(RAW if raw is None else raw)
    annotation = Annotation.model_validate(raw)
    found = evidence or ev.Evidence()
    return check.Context(
        source=source,
        annotation=annotation,
        annotation_raw=raw,
        built=model.build(source, annotation, found),
        evidence=found,
        **overrides,
    )


def failing(report: check.Report) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for issue in report.issues:
        out.setdefault(issue.check, []).append(f"{issue.loc}: {issue.message}")
    return out


def test_the_registry_matches_the_declared_set():
    check.assert_registry()
    assert set(check._CHECKS) == check.EXPECTED_CHECK_IDS


def test_the_reference_annotation_passes_every_check(reference_source):
    report = check.run(context(reference_source))
    assert report.ok, failing(report)
    assert len(report.checks_run) == len(check.EXPECTED_CHECK_IDS)
    assert any(line.startswith("completion:") for line in report.notes)


def test_coverage(reference_source):
    raw = copy.deepcopy(RAW)
    del raw["endpoints"]["alarms"]
    found = failing(check.run(context(reference_source, raw)))
    assert found["spec.endpoints.coverage"] == ["endpoints: endpoint 'alarms' has no annotation — add `endpoints.alarms`"]


def test_stale_endpoint_and_list(reference_source):
    raw = copy.deepcopy(RAW)
    raw["endpoints"]["ghost"] = {"purpose": "x"}
    raw["lists"]["nobody"] = {"name": "x"}
    found = failing(check.run(context(reference_source, raw)))
    assert found["spec.endpoints.stale"][0].startswith("endpoints.ghost: annotates an endpoint the source does not declare")
    assert found["spec.lists.stale"][0].startswith("lists.nobody: names a provider the source does not declare")


def test_a_key_placeholder_the_endpoint_cannot_fill(reference_source):
    raw = copy.deepcopy(RAW)
    raw["landing"]["key"] = "e={endpoint}/{assetType}/{slug}_p{page}.json"
    found = failing(check.run(context(reference_source, raw)))["spec.landing.key_resolvable"]
    assert any("{assetType} does not resolve for endpoint 'tenant_info'" in m for m in found)
    assert not any("'assets'" in m for m in found)  # assets has assetType


def test_a_per_endpoint_override_is_checked_under_its_own_location(reference_source):
    raw = copy.deepcopy(RAW)
    raw["endpoints"]["measures"]["key"] = "m/{ghost}.json"
    (message,) = failing(check.run(context(reference_source, raw)))["spec.landing.key_resolvable"]
    assert message.startswith("endpoints.measures.key: {ghost} does not resolve")


def test_a_paginated_endpoint_needs_page_in_its_key(reference_source):
    raw = copy.deepcopy(RAW)
    del raw["landing"]["key_paginated"]
    (message,) = failing(check.run(context(reference_source, raw)))["spec.landing.key_page"]
    assert "paginated endpoint 'assets'" in message
    assert message.startswith("landing.key:")


def test_a_stray_page_on_an_endpoint_that_answers_once(reference_source):
    """The mirror: `_p0` forever, and a key claiming a pagination the API lacks."""
    raw = copy.deepcopy(RAW)
    raw["landing"]["key"] = raw["landing"]["key_paginated"]
    found = failing(check.run(context(reference_source, raw)))["spec.landing.key_page_stray"]
    assert {m.split(":")[0] for m in found} == {"landing.key"}
    assert len(found) == 3  # alarms, tenant_info, measures — not assets
    assert any("'tenant_info' does not paginate" in m for m in found)


def test_the_paginated_default_is_reported_under_its_own_location(reference_source):
    raw = copy.deepcopy(RAW)
    raw["landing"]["key_paginated"] = "e={endpoint}/{ghost}_p{page}.json"
    (message,) = failing(check.run(context(reference_source, raw)))["spec.landing.key_resolvable"]
    assert message.startswith("landing.key_paginated: {ghost} does not resolve for endpoint 'assets'")


def test_a_fanned_out_endpoint_needs_something_that_varies_in_its_key(reference_source):
    raw = copy.deepcopy(RAW)
    raw["landing"]["key"] = "e={endpoint}/nothing.json"
    raw["landing"]["key_paginated"] = "e={endpoint}/p{page}.json"
    found = failing(check.run(context(reference_source, raw)))["spec.landing.key_distinguishes"]
    assert {m.split(":")[0] for m in found} == {"landing.key", "landing.key_paginated"}
    assert any("'alarms' (type)" in m for m in found)
    assert not any("tenant_info" in m for m in found)  # nothing fans out there


def test_the_annotation_may_not_reference_env_vars(reference_source):
    raw = copy.deepcopy(RAW)
    raw["secrets"] = "env:REF_PASSWORD"
    (message,) = failing(check.run(context(reference_source, raw)))["spec.secrets.no_env_refs"]
    assert message.startswith("secrets: looks like an env reference")


def test_a_secret_value_in_the_model_is_caught(reference_source, reference_env):
    """Second line of defence: the model is built without secrets by construction, but if
    one is pasted into the annotation it must not reach the document either."""
    raw = copy.deepcopy(RAW)
    raw["endpoints"]["assets"]["purpose"] = f"mot de passe {reference_env['REF_PASSWORD']}"
    found = failing(check.run(context(reference_source, raw)))
    assert found["spec.secrets.no_leak"] == ["model: a registered secret value appears in the model"]


def test_an_unset_env_var_is_reported_as_unscannable(reference_source, monkeypatch, reference_env):
    monkeypatch.delenv("REF_USER")
    report = check.run(context(reference_source))
    assert any("REF_USER not set" in line for line in report.notes)


def test_a_contract_entry_the_envelope_no_longer_writes(reference_source, monkeypatch):
    ctx = context(reference_source)  # built before the catalogue is tampered with
    extra = contract.Attribute("metadata.batch_id", "string", "yes")
    monkeypatch.setattr(contract, "ATTRIBUTES", (*contract.ATTRIBUTES, extra))
    monkeypatch.setattr(contract, "DECLARED", contract.DECLARED | {extra.path})
    found = failing(check.run(ctx))["spec.contract.matches_envelope"]
    assert "contract: the contract describes 'metadata.batch_id', which the envelope no longer writes" in found


def test_pre_migration_envelopes_are_evidence_not_samples(reference_source):
    old = SavedOutput(
        Path("output/reference/assets/PUMP_p0.json"),
        {"metadata": {"source": "reference", "endpoint": "assets", "run_id": "x", "params": {}}, "body": {}},
    )
    found = failing(check.run(context(reference_source, evidence=ev.Evidence(envelopes={"assets": [old]}))))
    (message,) = found["spec.samples.current"]
    assert message.startswith("output/assets: 1 envelope(s) on disk, none written by the current contract")


def test_a_current_envelope_alongside_old_ones_is_enough(reference_source):
    old = SavedOutput(Path("o/a.json"), {"metadata": {"endpoint": "assets", "params": {}}, "body": {}})
    new = SavedOutput(Path("o/b.json"), build_envelope(reference_source, "assets", {"assetType": "PUMP"}, {"data": []}))
    report = check.run(context(reference_source, evidence=ev.Evidence(envelopes={"assets": [old, new]})))
    assert "spec.samples.current" not in failing(report)


def test_completeness_fails_only_below_the_threshold(reference_source):
    assert "spec.completeness" not in failing(check.run(context(reference_source)))
    (message,) = failing(check.run(context(reference_source, min_complete=100)))["spec.completeness"]
    assert message.endswith("below the required 100%")


def test_an_unplannable_endpoint_is_noted_not_failed(reference_source, reference_env):
    raw = copy.deepcopy(RAW)
    source = reference_source.model_copy(deep=True)
    source.providers["asset_ids"].args["path"] = "$.data[*].id.id"  # unchanged, still fine
    source.providers["asset_types"].args["values"] = "not-a-list"  # `literal` will raise
    report = check.run(context(source, raw))
    assert any("could not resolve its parameters" in line for line in report.notes)


def test_without_a_template_nothing_is_rendered_and_nothing_fails(reference_source):
    report = check.run(context(reference_source, template=None))
    assert "spec.template" not in failing(report)
    assert any(line.startswith("template: none found") for line in report.notes)


def test_a_template_without_docxtpl_is_skipped_with_a_notice(reference_source, tmp_path):
    report = check.run(context(reference_source, template=tmp_path / "x.docx", template_available=False))
    assert "spec.template" not in failing(report)
    assert any("docxtpl is not installed" in line for line in report.notes)
