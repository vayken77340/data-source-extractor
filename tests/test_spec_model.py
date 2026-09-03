"""The intermediate model is the checkpoint: what the document says, testable without Word.

Built from the suite's own source and annotation, first with nothing on disk, then with
envelopes the real `envelope.build()` wrote. Every assertion here is about content the
external team will read; the renderers are projections and are tested separately.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from api_extractor.config.models import Source
from api_extractor.providers import registry
from api_extractor.providers.registry import SavedOutput
from specgen import evidence as ev, labels, model
from specgen.annotation import Annotation
from specgen.labels import TODO
from tests.conftest import REFERENCE_BASE_URL, build_envelope

GENERATED = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)


@pytest.fixture
def built(reference_source, reference_annotation):
    return model.build(reference_source, reference_annotation, generated_at=GENERATED)


def endpoint(built, name):
    return next(e for e in built.model["endpoints"] if e["name"] == name)


def rows(section, item):
    return [row["value"] for row in section if row["item"] == item]


def inline(endpoints: dict, providers: dict | None = None, **defaults) -> Source:
    return Source.model_validate(
        {
            "source": "demo",
            "base_url": "https://demo.example.com/v1",
            "providers": providers or {},
            "endpoints": endpoints,
            "defaults": defaults,
        }
    )


def annotation_for(source: Source, **endpoint_fields) -> Annotation:
    return Annotation.model_validate(
        {
            "spec": {"version": "1", "status": "draft", "date": "04/09/2026", "source_system": "Demo"},
            "secrets": "Coffre",
            "landing": {"key": "e={endpoint}/{slug}_p{page}.json"},
            "endpoints": {name: dict(endpoint_fields) for name in source.endpoints},
        }
    )


# --- ordering -----------------------------------------------------------------------


def test_drivers_come_before_leaves_then_alphabetical(reference_source):
    """The runner runs alarms first (alphabetical); a reader wants the driver first."""
    assert model.document_order(reference_source) == ["assets", "alarms", "tenant_info", "measures"]


def test_endpoints_are_numbered_in_that_order(built):
    assert [(e["number"], e["title"]) for e in built.model["endpoints"]] == [
        (1, "POST /assets/search — assets"),
        (2, "GET /alarms — alarms"),
        (3, "GET /tenant/info — tenant_info"),
        (4, "GET /assets/{id}/measures — measures"),
    ]


# --- one endpoint -------------------------------------------------------------------


def test_request_rows_name_location_type_and_origin(built):
    assert endpoint(built, "alarms")["params"] == [
        {"name": "pageSize", "location": "chaîne de requête", "type": "entier", "origin": "valeur fixe : 100"},
        {"name": "searchStatus", "location": "chaîne de requête", "type": "chaîne", "origin": "valeur fixe : ACTIVE"},
        {"name": "type", "location": "chaîne de requête", "type": "chaîne", "origin": "types d'actifs (2 valeurs)"},
    ]


def test_a_static_provider_without_a_name_is_described_by_its_fields():
    source = inline(
        {"a": {"method": "GET", "path": "/a", "query": {"t": {"from": "kinds"}}}},
        {"kinds": {"fn": "literal", "args": {"values": [{"kind": "x"}, {"kind": "y"}, {"kind": "z"}]}}},
    )
    built = model.build(source, annotation_for(source))
    assert endpoint(built, "a")["params"][0]["origin"] == "kind du référentiel (3 valeurs)"


def test_a_chained_origin_names_the_endpoint_it_reads_never_the_provider(built):
    measures = endpoint(built, "measures")
    assert measures["params"][0] == {
        "name": "id",
        "location": "chemin",
        "type": "chaîne",
        "origin": "enregistrement retourné par POST /assets/search",
    }
    assert rows(measures["summary"], "Dépend de") == ["assets"]
    assert rows(endpoint(built, "assets")["summary"], "Dépend de") == []


def test_labels_and_aliases_and_nesting_are_placed():
    source = inline(
        {
            "a": {
                "method": "POST",
                "path": "/a/{id}",
                "bind": {"id": {"from": "rows"}},
                "payload": {"filters": [{"key": "k", "value": {"from": "rows", "as": "kind"}}], "page": {"size": 50}},
                "label": {"name": {"from": "rows"}},
                "output": "out/{name}.json",
            }
        },
        {"rows": {"fn": "literal", "args": {"values": [{"id": "1", "kind": "x", "name": "n"}]}}},
    )
    params = endpoint(model.build(source, annotation_for(source)), "a")["params"]
    assert [(p["name"], p["location"]) for p in params] == [
        ("id", "chemin"),
        ("key", "corps de la requête (filters[0].key)"),
        ("kind", "corps de la requête (filters[0].value)"),
        ("size", "corps de la requête (page.size)"),
        ("name", "non transmis (sert au nommage du fichier)"),
    ]
    assert params[1]["origin"] == "valeur fixe : k"  # a literal next to a marker is still sent


def test_several_params_off_one_provider_get_the_stay_together_paragraph():
    source = inline(
        {"a": {"method": "GET", "path": "/a", "query": {"x": {"from": "rows"}, "y": {"from": "rows"}}}},
        {"rows": {"fn": "literal", "args": {"values": [{"x": 1, "y": 2}]}}},
    )
    (note,) = endpoint(model.build(source, annotation_for(source)), "a")["correlated_origins"]
    assert note.startswith("Les paramètres issus de « x / y du référentiel (1 valeur) »")


def test_only_a_paginated_endpoint_has_pagination_rows(built):
    assert endpoint(built, "alarms")["pagination"] is None
    assert rows(endpoint(built, "assets")["pagination"], "Emplacement du curseur") == ["payload.page"]


def test_the_stop_condition_states_both_halves(built):
    """The walk stops on an empty page whatever `has_more` says; the prototype forgot."""
    assert rows(endpoint(built, "assets")["pagination"], "La boucle s'arrête quand") == [
        "une page revient vide, ou le chemin JSON $.hasNext est faux"
    ]


def test_without_has_more_only_the_empty_page_stops():
    source = inline({"a": {"method": "GET", "path": "/a", "paginate": {"style": "page_number", "at": "query.p"}, "output": "o/{page}.json"}})
    assert model.stop_condition(source.endpoints["a"]) == "une page revient vide"


def test_iteration_pseudocode_loops_over_records_then_pages(built):
    assert endpoint(built, "assets")["iteration"][:4] == [
        "pour chaque types d'actifs :",
        "    page := 0",
        "    répéter",
        "        réponse := POST /assets/search",
    ]
    assert endpoint(built, "tenant_info")["iteration"] is None


def test_an_absent_optional_produces_no_row(built):
    tenant = endpoint(built, "tenant_info")
    assert rows(tenant["response"], "Structures imbriquées") == []
    assert rows(tenant["summary"], "Portée d'authentification requise") == []
    assert tenant["quirks"] == [] and tenant["sample"] is None
    assert rows(endpoint(built, "measures")["summary"], "Portée d'authentification requise") == ["lecture des télémesures"]


def test_a_structural_absence_renders_the_marker(built):
    assert endpoint(built, "alarms")["rationale"] == TODO
    assert endpoint(built, "alarms")["mode"] == TODO
    assert endpoint(built, "measures")["mode"] == "incrémental"


# --- volumes and calls --------------------------------------------------------------


def test_planned_volumes_ignore_every_sampling_cap():
    """The document describes the extraction, not this tool's sampling of it."""
    values = [{"k": str(i)} for i in range(7)]
    source = inline(
        {
            "a": {"method": "GET", "path": "/a", "query": {"k": {"from": "ks"}}, "limit": 2},
            "p": {"method": "GET", "path": "/p", "query": {"k": {"from": "ks"}}, "paginate": {"style": "page_number", "at": "query.page"}, "output": "o/{k}_{page}.json"},
            "one": {"method": "GET", "path": "/one"},
        },
        {"ks": {"fn": "literal", "args": {"values": values}}},
        limit=3,
    )
    built = model.build(source, annotation_for(source))
    assert endpoint(built, "a")["volume"] == {"planned": 7, "measured": None, "records_per_page": None, "text": "7", "measured_text": None}
    assert endpoint(built, "p")["volume"]["text"] == "7 × nombre de pages"
    assert endpoint(built, "one")["volume"]["text"] == "1"
    assert rows(endpoint(built, "a")["summary"], "Appelé") == ["7 fois par exécution — une par k du référentiel"]
    assert rows(endpoint(built, "one")["summary"], "Appelé") == ["Une fois par exécution"]


def test_a_chained_endpoint_without_evidence_reads_n(built):
    measures = endpoint(built, "measures")
    assert measures["volume"]["planned"] is None
    assert measures["volume"]["text"] == "N — un par enregistrement retourné par POST /assets/search, à mesurer sur une exécution réelle"
    assert rows(measures["summary"], "Appelé") == ["Une fois par enregistrement retourné par POST /assets/search"]


def test_the_pagination_sentence_counts_once_for_the_whole_source(built):
    assert built.model["flow"]["pagination_sentence"] == (
        "Sur les 4 endpoints de cette source, 1 pagine (assets) ; les autres répondent en une seule fois."
    )


def test_the_dependency_tree_and_sequence(built):
    assert built.model["flow"]["tree"] == ["assets   (pilote)", "  └── measures", "alarms", "tenant_info"]
    assert [s["action"] for s in built.model["flow"]["sequence"]] == [
        "POST /assets/search",
        "GET /alarms",
        "GET /tenant/info",
        "GET /assets/{id}/measures",
    ]
    assert built.model["flow"]["sequence"][3]["after"] == "assets"


def test_parameter_lists_are_named_and_described_generically(built):
    lists = {entry["name"]: entry for entry in built.model["flow"]["lists"]}
    assert lists["types d'actifs"]["used_by"] == "assets, alarms"
    assert rows(lists["types d'actifs"]["rows"], "Origine") == ["référentiel joint à ce document (2 valeurs)"]
    chained = lists["enregistrement retourné par POST /assets/search"]
    assert rows(chained["rows"], "Origine") == ["réponses de POST /assets/search déjà déposées"]
    assert rows(chained["rows"], "Chemin des enregistrements") == ["$.data[*].id.id"]


# --- landing ------------------------------------------------------------------------


def test_the_landing_example_is_built_by_the_envelope_builder(built):
    example = built.model["landing"]["example"]
    assert list(example["metadata"]) == ["source", "endpoint", "extracted_at", "params", "request", "response", "parents"]
    assert example["metadata"]["request"]["base_url"] == REFERENCE_BASE_URL
    assert example["metadata"]["request"]["headers"]["Authorization"] == "***REDACTED***"
    assert built.model["landing"]["example_endpoint"] == "measures"


def test_rendered_keys_use_real_parameters_and_the_run_date(built):
    keys = built.model["landing"]["rendered_keys"]
    assert keys[0] == "s3://bucket-de-test/raw/source=reference/entity=assets/extract_date=2026-09-04/pump_p0.json"
    assert keys[3].endswith("entity=measures/extract_date=2026-09-04/id_p0.json")


def test_an_unresolvable_placeholder_renders_visibly_rather_than_empty():
    assert model.render_key("k/{ghost}/{slug}", "s", "e", {"a": 1}, extract_date="d") == "k/<ghost>/1"


def test_the_contract_rows_fold_in_the_endpoint_names(built):
    endpoint_row = next(r for r in built.model["landing"]["contract"] if r["attribute"] == "metadata.endpoint")
    assert endpoint_row["description"].endswith("Nom logique de l'endpoint : alarms, assets, measures, tenant_info.")


# --- completeness -------------------------------------------------------------------


def test_completeness_counts_what_the_document_shows(built):
    done = built.model["completeness"]
    assert done["todo"] == len(done["locations"]) > 0
    assert "endpoints[1].mode" in done["locations"]
    assert "document.cover[4].value" in done["locations"]  # implementation_team
    assert 0 < done["percent"] < 100


def test_answering_a_slot_moves_the_percentage(reference_source, reference_annotation):
    before = model.build(reference_source, reference_annotation).model["completeness"]
    reference_annotation.spec.implementation_team = "Équipe plateforme"
    after = model.build(reference_source, reference_annotation).model["completeness"]
    assert after["filled"] == before["filled"] + 1 and after["todo"] == before["todo"] - 1


# --- with evidence ------------------------------------------------------------------


def page(ids: list[str], has_next: bool) -> dict:
    return {"data": [{"id": {"id": i, "entityType": "ASSET"}, "owner": "secret-owner"} for i in ids], "hasNext": has_next}


@pytest.fixture
def evidence(reference_source):
    source = reference_source
    assets = [
        SavedOutput(Path("output/reference/assets/PUMP_p0.json"), build_envelope(source, "assets", {"assetType": "PUMP"}, page(["PUMP-1", "PUMP-2"], True))),
        SavedOutput(Path("output/reference/assets/PUMP_p1.json"), build_envelope(source, "assets", {"assetType": "PUMP"}, page(["PUMP-3"], False), page=1)),
        SavedOutput(Path("output/reference/assets/VALVE_p0.json"), build_envelope(source, "assets", {"assetType": "VALVE"}, page([], False))),
    ]
    measures = [
        SavedOutput(
            Path("output/reference/measures/PUMP-1.json"),
            build_envelope(source, "measures", {"id": "PUMP-1"}, {"temperature": [1, 2, 3, 4]}, parents=("output/reference/assets/PUMP_p0.json",)),
        )
    ]
    records = [
        {"run_id": "r", "endpoint": "assets", "params": {"assetType": "PUMP"}, "page": 0, "status": 200},
        {"run_id": "r", "endpoint": "assets", "params": {"assetType": "PUMP"}, "page": 1, "status": 200},
        {"run_id": "r", "endpoint": "assets", "params": {"assetType": "VALVE"}, "page": 0, "status": 200},
        {"run_id": "r", "endpoint": "measures", "params": {"id": "PUMP-1"}, "page": 0, "status": "error", "error": "boom"},
    ]
    return ev.Evidence(envelopes={"assets": assets, "measures": measures}, records=records, run_id="r")


def test_evidence_plans_the_chained_endpoint_and_measures_the_rest(reference_source, reference_annotation, evidence):
    built = model.build(reference_source, reference_annotation, evidence, generated_at=GENERATED)
    measures = endpoint(built, "measures")
    assert measures["volume"]["planned"] == 3  # PUMP-1, PUMP-2, PUMP-3 read off the envelopes
    assert rows(measures["summary"], "Appelé") == ["3 fois par exécution — une par enregistrement retourné par POST /assets/search"]
    assets = endpoint(built, "assets")
    assert assets["volume"]["measured"] == {"requests": 3, "written": 3, "failed": 0, "skipped": 0, "pages_max": 2, "distinct_params": 2}
    assert assets["volume"]["measured_text"] == "3 requêtes, 3 fichiers, 2 pages au plus par séquence"
    assert assets["volume"]["records_per_page"] == 2


def test_evidence_describes_the_response_shape_and_attaches_a_sample(reference_source, reference_annotation, evidence):
    built = model.build(reference_source, reference_annotation, evidence, sample_records=1)
    assets = endpoint(built, "assets")
    assert rows(assets["response"], "Élément racine") == ["objet (clés : data, hasNext)"]
    assert rows(assets["response"], "Structures imbriquées") == ["`id` est un objet { id, entityType }."]
    assert assets["sample"] == {"endpoint": "assets", "file": "assets_exemple.json", "captured_at": "04/09/2026", "note": "extrait : listes tronquées à 1 par liste"}
    sample = built.samples["assets_exemple.json"]
    assert len(sample["body"]["data"]) == 1
    assert sample["body"]["data"][0]["owner"] == "***REDACTED***"  # annotation: sample.redact
    assert sample["metadata"]["request"]["payload"] == {"assetType": "PUMP", "pageSize": 100, "page": 0}


def test_the_landing_example_prefers_a_real_envelope_and_shows_parents_as_keys(reference_source, reference_annotation, evidence):
    built = model.build(reference_source, reference_annotation, evidence, generated_at=GENERATED)
    example = built.model["landing"]["example"]
    assert example["metadata"]["params"] == {"id": "PUMP-1"}
    assert example["metadata"]["parents"] == ["source=reference/entity=assets/extract_date=2026-09-04/pump_p0.json"]
    assert example["body"] == {"…": "la réponse de l'API pour cette page, verbatim"}


def test_the_field_inventory_records_what_came_back(reference_source, reference_annotation, evidence):
    built = model.build(reference_source, reference_annotation, evidence)
    inventory = {row["path"]: row for row in built.model["evidence"]["fields"]["assets"]}
    assert inventory["$.data[].id.id"]["types"] == "chaîne"
    assert inventory["$.data[].id.id"]["example"] == "PUMP-1"
    assert inventory["$.hasNext"]["types"] == "booléen"
    assert inventory["$.data[].id"]["presence"] == pytest.approx(2 / 3)
    assert [t["key"] for t in built.model["appendix"]["workbook"]["tabs"]] == ["readme", "endpoints", "fields", "metadata", "volumes"]


def test_without_evidence_the_workbook_has_no_field_inventory(built):
    assert [t["key"] for t in built.model["appendix"]["workbook"]["tabs"]] == ["readme", "endpoints", "metadata", "volumes"]


# --- the document never mentions the tool ------------------------------------------


def test_the_model_never_mentions_this_repository(reference_source, reference_annotation, evidence, reference_env):
    """Decision 6 as a test: the reader builds their own pipeline and has never seen this repo."""
    built = model.build(reference_source, reference_annotation, evidence)
    text = json.dumps(built.model, ensure_ascii=False)
    forbidden = ["config/", "input/", "output/{", "defaults.", "env:", "run_id", "max_pages", "rate_limit"]
    forbidden += [entry.name for entry in registry.registered()]
    forbidden += list(reference_source.providers)
    forbidden += list(reference_env.values())
    for word in forbidden:
        assert word not in text, word


def test_variables_lists_what_a_template_may_reference(built):
    paths = dict(model.variables(built.model))
    assert paths["document.source_system"] == "Référence"
    assert "endpoints[].params[].origin" in paths
    assert "landing.contract[].attribute" in paths
    assert paths["appendix.workbook.tabs[].name"] == "Lisez-moi"
