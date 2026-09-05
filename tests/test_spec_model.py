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
        {"name": "pageSize", "location": "chaîne de requête", "type": "long", "origin": "valeur fixe : 100"},
        {"name": "searchStatus", "location": "chaîne de requête", "type": "string", "origin": "valeur fixe : ACTIVE"},
        {"name": "type", "location": "chaîne de requête", "type": "string", "origin": "types d'actifs"},
    ]


def test_a_static_provider_without_a_name_is_described_by_its_fields():
    source = inline(
        {"a": {"method": "GET", "path": "/a", "query": {"t": {"from": "kinds"}}}},
        {"kinds": {"fn": "literal", "args": {"values": [{"kind": "x"}, {"kind": "y"}, {"kind": "z"}]}}},
    )
    built = model.build(source, annotation_for(source))
    assert endpoint(built, "a")["params"][0]["origin"] == "kind du référentiel"


def test_a_chained_origin_names_the_endpoint_it_reads_never_the_provider(built):
    measures = endpoint(built, "measures")
    assert measures["params"][0] == {
        "name": "id",
        "location": "chemin",
        "type": "string",
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
    assert note.startswith("Les paramètres issus de « x / y du référentiel »")


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


def test_called_names_the_pattern_never_a_count():
    """A count is a fact about one environment; the pattern is a fact about the API."""
    values = [{"k": str(i)} for i in range(7)]
    source = inline(
        {
            "a": {"method": "GET", "path": "/a", "query": {"k": {"from": "ks"}}, "limit": 2},
            "one": {"method": "GET", "path": "/one"},
        },
        {"ks": {"fn": "literal", "args": {"values": values}}},
        limit=3,
    )
    built = model.build(source, annotation_for(source))
    assert rows(endpoint(built, "a")["summary"], "Appelé") == ["Une fois par k du référentiel"]
    assert rows(endpoint(built, "one")["summary"], "Appelé") == ["Une fois par exécution"]
    assert "volume" not in endpoint(built, "a")
    assert "7" not in json.dumps(endpoint(built, "a"), ensure_ascii=False)


def test_a_chained_endpoint_is_called_once_per_parent_record(built):
    measures = endpoint(built, "measures")
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


def test_a_list_backed_by_values_points_at_its_sheet(built):
    """Its values are a table, so they are in the workbook and not described in prose."""
    (entry,) = endpoint(built, "alarms")["lists"]
    assert entry["name"] == "types d'actifs"
    assert rows(entry["rows"], "Valeurs") == ["onglet « types d'actifs » du classeur d'accompagnement"]
    assert rows(entry["rows"], "Origine") == []  # nothing left to say about where it lives


def test_a_chained_list_keeps_its_recipe_beside_the_endpoint_it_drives(built):
    """It has no values to tabulate, so the recipe is the whole of it."""
    (entry,) = endpoint(built, "measures")["lists"]
    assert entry["name"] == "enregistrement retourné par POST /assets/search"
    assert rows(entry["rows"], "Origine") == ["réponses de POST /assets/search déjà déposées"]
    assert rows(entry["rows"], "Chemin des enregistrements") == ["$.data[*].id.id"]


def test_a_list_is_described_under_every_endpoint_it_drives(built):
    """assets and alarms share one list; each section stands on its own."""
    for name in ("assets", "alarms"):
        assert [e["name"] for e in endpoint(built, name)["lists"]] == ["types d'actifs"]
    assert endpoint(built, "tenant_info")["lists"] == []


def test_the_lists_no_longer_have_a_section_of_their_own(built):
    assert "lists" not in built.model["flow"]


# --- the request as it goes on the wire ----------------------------------------------


def test_a_nested_payload_is_shown_as_a_body_not_as_dotted_paths():
    """The whole point: `pageLink.page` in a table hides the shape a reader needs."""
    source = inline(
        {
            "search": {
                "method": "POST",
                "path": "/search",
                "payload": {"pageLink": {"pageSize": 100, "sortOrder": {"property": "name"}}, "assetType": {"from": "kinds"}},
                "paginate": {"style": "page_number", "at": "payload.pageLink.page", "has_more": "$.hasNext"},
                "output": "o/{assetType}_{page}.json",
            }
        },
        {"kinds": {"fn": "literal", "args": {"values": [{"assetType": "PUMP"}]}}},
    )
    shape = endpoint(model.build(source, annotation_for(source)), "search")["payload_shape"]
    assert [line.split("   ←")[0].rstrip() for line in shape] == [
        "{",
        '  "pageLink": {',
        '    "pageSize": 100,',
        '    "sortOrder": {',
        '      "property": "name"',
        "    },",
        '    "page": 0',
        "  },",
        '  "assetType": "<assetType>"',
        "}",
    ]


def test_every_value_in_the_shape_says_where_it_comes_from():
    source = inline(
        {
            "search": {
                "method": "POST",
                "path": "/search",
                "payload": {"pageSize": 100, "assetType": {"from": "kinds"}},
                "paginate": {"style": "page_number", "at": "payload.page"},
                "output": "o/{assetType}_{page}.json",
            }
        },
        {"kinds": {"fn": "literal", "args": {"values": [{"assetType": "PUMP"}]}}},
    )
    notes = [line.split("←")[1].strip() for line in endpoint(model.build(source, annotation_for(source)), "search")["payload_shape"] if "←" in line]
    assert notes == ["valeur fixe", "assetType du référentiel", "curseur de pagination, incrémenté à chaque page"]


def test_a_query_shape_is_flat_and_carries_the_cursor(built):
    shape = endpoint(built, "alarms")["query_shape"]
    assert [line.split("←")[0].rstrip() for line in shape] == [
        "pageSize = 100",
        'searchStatus = "ACTIVE"',
        'type = "<type>"',
    ]
    assert [line.split("←")[1].strip() for line in shape] == ["valeur fixe", "valeur fixe", "types d'actifs"]
    # One column for the notes, so the origins read as a column and not as clutter.
    assert len({line.index("←") for line in shape}) == 1
    assert endpoint(built, "alarms")["payload_shape"] is None


def test_an_endpoint_that_sends_nothing_has_no_shape(built):
    tenant = endpoint(built, "tenant_info")
    assert tenant["payload_shape"] is None and tenant["query_shape"] is None


def test_the_shape_never_replaces_the_exhaustive_list(built):
    """The workbook still renders every parameter; the document shows the shape."""
    assert [p["name"] for p in endpoint(built, "assets")["params"]] == ["assetType", "pageSize", "page"]


def test_the_page_cursor_is_a_parameter_like_any_other(built):
    """Declared under `paginate`, not in the body — so walking the body alone missed it,
    and a reader working from the list would have built a request with no cursor."""
    (cursor,) = [p for p in endpoint(built, "assets")["params"] if p["name"] == "page"]
    assert cursor == {
        "name": "page",
        "location": "corps de la requête",
        "type": "long",
        "origin": "curseur de pagination, incrémenté à chaque page",
    }
    assert not [p for p in endpoint(built, "alarms")["params"] if p["name"] == "page"]


def test_a_nested_cursor_carries_its_path():
    source = inline(
        {
            "search": {
                "method": "POST",
                "path": "/search",
                "payload": {"pageLink": {"pageSize": 100}},
                "paginate": {"style": "page_number", "at": "payload.pageLink.page"},
                "output": "o/p{page}.json",
            }
        }
    )
    (cursor,) = [p for p in endpoint(model.build(source, annotation_for(source)), "search")["params"] if p["name"] == "page"]
    assert cursor["location"] == "corps de la requête (pageLink.page)"


# --- links ---------------------------------------------------------------------------


def test_a_filled_link_reaches_the_pointers_that_can_use_it(built):
    workbook = built.model["links"]["workbook"]
    assert workbook == "https://exemple.sharepoint.com/sites/data/Spec_Annexes.xlsx"
    assert built.model["appendix"]["workbook"]["url"] == workbook
    assert built.model["flow"]["workbook_pointer"]["url"] == workbook
    assert built.model["landing"]["contract_pointer"]["url"] == workbook


def test_the_workbook_is_pointed_at_once_for_the_whole_catalogue(built):
    """Repeating one file's link under every endpoint says nothing new each time."""
    assert "un onglet par endpoint" in built.model["flow"]["workbook_pointer"]["text"]
    assert not any("detail" in e for e in built.model["endpoints"])


def test_the_contract_pointer_names_the_sheet_that_holds_it(built):
    pointer = built.model["landing"]["contract_pointer"]["text"]
    assert "onglet « Metadata »" in pointer and "Les noms font foi." in pointer
    assert built.model["landing"]["contract"], "the catalogue itself still feeds the workbook"


def test_a_link_nobody_has_filled_in_is_no_link(built):
    """A marker is not a URL, so the pointer stays the plain text it already was."""
    assert built.model["links"]["samples"] is None and built.model["links"]["vendor"] is None
    vendor = next(r for r in built.model["related"] if r["item"] == "Documentation API de l'éditeur")
    assert vendor["url"] is None


def test_related_lists_the_workbook_with_its_url(built):
    workbook = next(r for r in built.model["related"] if r["item"] == "Classeur d'accompagnement")
    assert workbook["value"] == built.model["appendix"]["workbook"]["file"]
    assert workbook["url"] == built.model["links"]["workbook"]


# --- auth ---------------------------------------------------------------------------


def auth_headers(auth: dict) -> list[tuple[str, str]]:
    source = Source.model_validate(
        {
            "source": "demo",
            "base_url": "https://demo.example.com/v1",
            "auth": auth,
            "defaults": {"headers": {"Accept": "application/json"}},
            "endpoints": {"e": {"method": "GET", "path": "/e"}},
        }
    )
    built = model.build(source, annotation_for(source))
    return [(h["name"], h["value"]) for h in built.model["auth"]["headers"]]


@pytest.mark.parametrize(
    "auth, expected",
    [
        (
            {"type": "header", "headers": {"X-Authorization": {"value": "env:K", "template": "ApiKey {value}"}}},
            [("X-Authorization", "ApiKey <secret>")],
        ),
        ({"type": "header", "headers": {"X-API-Key": "env:K"}}, [("X-API-Key", "<secret>")]),
        ({"type": "bearer", "token": "env:T"}, [("Authorization", "Bearer <secret>")]),
        (
            {"type": "bearer", "token": "env:T", "apply": {"header": "X-Auth", "template": "Token {token}"}},
            [("X-Auth", "Token <secret>")],
        ),
        ({"type": "basic", "username": "env:U", "password": "env:P"}, [("Authorization", "Basic <base64(identifiant:secret)>")]),
    ],
)
def test_a_credential_header_shows_its_wrapper_and_hides_only_the_secret(auth, expected, monkeypatch):
    """`ApiKey ` is structure the implementing team must reproduce; only the key is secret.

    A template may contain nothing but `{value}` or `{token}` — the models reject anything
    else — so printing it can never print a credential.
    """
    for name in ("K", "T", "U", "P"):
        monkeypatch.setenv(name, "hunter2-hunter2")
    assert auth_headers(auth)[-len(expected) :] == expected


def test_default_headers_come_before_the_credential_ones(monkeypatch):
    monkeypatch.setenv("K", "hunter2-hunter2")
    headers = auth_headers({"type": "header", "headers": {"X-API-Key": "env:K"}})
    assert headers[0] == ("Accept", "application/json")


def test_no_secret_value_reaches_the_headers_table(monkeypatch):
    monkeypatch.setenv("K", "hunter2-hunter2")
    assert not any("hunter2" in value for _name, value in auth_headers({"type": "header", "headers": {"X-API-Key": "env:K"}}))


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
    assert keys[3].endswith("entity=measures/extract_date=2026-09-04/id.json")


def test_only_a_paginated_endpoint_gets_the_paginated_key(built):
    """`_p0` on an endpoint that answers once would claim a pagination it does not have."""
    by_name = {e["name"]: e for e in built.model["endpoints"]}
    assert by_name["assets"]["landing_key"].endswith("_p{page}.json")
    for name in ("alarms", "tenant_info", "measures"):
        assert by_name[name]["landing_key"].endswith("{slug}.json"), name
        assert "_p0" not in by_name[name]["rendered_key"], name


def test_an_endpoint_key_wins_over_both_defaults_and_is_listed_as_an_exception(
    reference_source, reference_annotation
):
    reference_annotation.endpoints["measures"].key = "m/{assetType}/{assetName}.json"
    model_ = model.build(reference_source, reference_annotation, generated_at=GENERATED).model
    assert model_["landing"]["overrides"] == [{"endpoint": "measures", "key": "m/{assetType}/{assetName}.json"}]


def test_a_paginated_default_is_not_an_exception(built):
    """Every paginated endpoint differs from the base key; that is the rule, not a deviation."""
    assert built.model["landing"]["overrides"] == []
    assert built.model["landing"]["key_template_paginated"].endswith("_p{page}.json")


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
    return ev.Evidence(envelopes={"assets": assets, "measures": measures})


def test_evidence_never_turns_into_a_count(reference_source, reference_annotation, evidence):
    built = model.build(reference_source, reference_annotation, evidence, generated_at=GENERATED)
    text = json.dumps(built.model, ensure_ascii=False).lower()
    assert "volum" not in text and " valeurs)" not in text
    assert rows(endpoint(built, "assets")["summary"], "Appelé") == ["Une fois par types d'actifs"]


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


def test_response_fields_record_what_came_back_in_iceberg_types(reference_source, reference_annotation, evidence):
    built = model.build(reference_source, reference_annotation, evidence)
    fields = {row["path"]: row for row in endpoint(built, "assets")["response_fields"]}
    assert fields["$.data[].id.id"]["type"] == "string" and fields["$.data[].id.id"]["example"] == "PUMP-1"
    assert fields["$.hasNext"]["type"] == "boolean" and fields["$.hasNext"]["nullable"] is False
    assert fields["$.data[].id"]["presence"] == pytest.approx(2 / 3)
    assert fields["$.data[].id"]["nullable"] is True  # absent from the empty VALVE page
    assert endpoint(built, "tenant_info")["response_fields"] == []


def test_a_captured_response_is_shown_as_json_with_its_lists_cut(reference_source, reference_annotation, evidence):
    """Structure is the point, so a list needs enough items to show that it repeats."""
    built = model.build(reference_source, reference_annotation, evidence)
    shape = endpoint(built, "assets")["response_shape"]
    assert shape[0] == "{" and shape[-1] == "}"
    assert '  "data": [' in shape
    assert sum(1 for line in shape if line.strip() == "{") == 3  # the body plus two items
    assert not any("secret-owner" in line for line in shape)  # annotation: sample.redact


def test_an_endpoint_with_no_captured_response_has_no_shape(built):
    assert all(e["response_shape"] is None for e in built.model["endpoints"])


def test_a_long_response_is_cut_off_and_says_so(reference_source, reference_annotation):
    saved = SavedOutput(
        Path("output/reference/assets/PUMP_p0.json"),
        build_envelope(reference_source, "assets", {"assetType": "PUMP"}, {f"k{i}": i for i in range(80)}),
    )
    built = model.build(reference_source, reference_annotation, ev.Evidence(envelopes={"assets": saved and [saved]}))
    shape = endpoint(built, "assets")["response_shape"]
    assert len(shape) == int(labels.L["limits.response_shape_lines"]) + 1
    assert shape[-1].startswith("…") and "fichier joint" in shape[-1]


def test_the_workbook_has_a_sheet_per_list_then_one_per_endpoint(built):
    tabs = built.model["appendix"]["workbook"]["tabs"]
    assert [t["key"] for t in tabs] == [
        "readme", "endpoints", "metadata",
        "list:0",
        "response:assets", "response:alarms", "response:tenant_info", "response:measures",
    ]
    assert [t["name"] for t in tabs] == [
        "Readme", "Endpoints", "Metadata", "types d'actifs",
        "assets", "alarms", "tenant_info", "measures",
    ]


def test_a_value_list_carries_its_columns_and_rows(built):
    (entry,) = built.model["appendix"]["workbook"]["lists"]
    assert (entry["name"], entry["sheet"]) == ("types d'actifs", "types d'actifs")
    assert entry["columns"] == ["assetType"] and entry["rows"] == [["PUMP"], ["VALVE"]]


def test_a_chained_list_gets_no_sheet(built):
    """There is nothing to tabulate: its values only exist once a run has happened."""
    assert len(built.model["appendix"]["workbook"]["lists"]) == 1


# --- the document never mentions the tool ------------------------------------------


def test_the_model_never_mentions_this_repository(reference_source, reference_annotation, evidence, reference_env):
    """Decision 6 as a test: the reader builds their own pipeline and has never seen this repo."""
    built = model.build(reference_source, reference_annotation, evidence)
    text = json.dumps(built.model, ensure_ascii=False)
    forbidden = ["config/", "input/", "output/{", "defaults.", "env:", "run_id", "max_pages", "rate_limit", "volum"]
    forbidden += [entry.name for entry in registry.registered()]
    forbidden += list(reference_source.providers)
    forbidden += list(reference_env.values())
    for word in forbidden:
        assert word.lower() not in text.lower(), word


def test_variables_lists_what_a_template_may_reference(built):
    paths = dict(model.variables(built.model))
    assert paths["document.source_system"] == "Référence"
    assert "endpoints[].params[].origin" in paths
    assert "landing.contract[].attribute" in paths
    assert paths["appendix.workbook.tabs[].name"] == "Readme"
