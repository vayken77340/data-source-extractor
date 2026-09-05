"""The label file: formatting helpers, and one entry for every enum the models accept.

A missing label would render an English identifier into a French document — quietly.
Everything reader-facing comes from `config/specs/LABELS.yaml`, so these tests pin the
file to the code that reads it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import get_args

import pytest

from api_extractor.config.models import PAGINATE_ROOTS, Auth
from specgen import contract, labels
from specgen.annotation import MODES, EndpointAnnotation, SpecBlock
from specgen.labels import JSON_TYPES, L

THIN = " "


@pytest.mark.parametrize(
    "value, expected",
    [(7, "7"), (1234, f"1{THIN}234"), (1234567, f"1{THIN}234{THIN}567"), (0, "0")],
)
def test_integers_group_thousands_with_a_narrow_no_break_space(value, expected):
    assert labels.fr_int(value) == expected


def test_decimals_use_a_comma():
    assert labels.fr_decimal(3.5) == "3,5"
    assert labels.fr_decimal(1234.567, 2) == f"1{THIN}234,57"


@pytest.mark.parametrize(
    "value",
    ["2026-09-04", "2026-09-04T10:12:03Z", "04/09/2026", date(2026, 9, 4)],
)
def test_dates_render_as_jj_mm_aaaa(value):
    assert labels.fr_date(value) == "04/09/2026"


@pytest.mark.parametrize(
    "count, expected", [(0, "0 valeur"), (1, "1 valeur"), (2, "2 valeurs"), (7, "7 valeurs")]
)
def test_french_pluralises_from_two(count, expected):
    assert labels.plural(count, "valeur") == expected


def test_yes_no_come_from_the_file():
    assert (labels.yes_no(True), labels.yes_no(False)) == (L["enums.yes"], L["enums.no"])


@pytest.mark.parametrize(
    "value, expected",
    [
        ("[À COMPLÉTER]", True),
        ("[À COMPLÉTER — format des horodatages]", True),
        ("Volume négligeable", False),
        ("Les mentions « À COMPLÉTER » entre crochets", False),
        (7, False),
    ],
)
def test_a_marker_is_recognised_by_its_opening(value, expected):
    assert labels.is_todo(value) is expected


def test_json_values_get_iceberg_type_names():
    """One type vocabulary for the whole document, and it is the modelling team's."""
    assert [labels.iceberg(v) for v in (1, "a", True, 1.5, {}, [], None)] == [
        "long",
        "string",
        "boolean",
        "double",
        "struct",
        "list",
        "null",
    ]


# --- the file covers what the code asks of it ---------------------------------------


def literal_values(model, field: str) -> set[str]:
    return set(get_args(model.model_fields[field].annotation))


def test_every_status_has_a_label():
    assert set(L.section("enums.status")) == literal_values(SpecBlock, "status")


def test_every_mode_has_a_label():
    assert set(L.section("enums.mode")) == set(MODES) == literal_values(EndpointAnnotation, "mode") - {labels.TODO}


def test_every_auth_type_has_a_label():
    union, _metadata = get_args(Auth)
    names = {get_args(member.model_fields["type"].annotation)[0] for member in get_args(union)}
    assert set(L.section("enums.auth")) == names


def test_every_cursor_root_and_location_has_a_label():
    assert set(L.section("enums.cursor_root")) == set(PAGINATE_ROOTS)
    assert set(L.section("enums.location")) == set(PAGINATE_ROOTS) | {"path", "label"}


def test_every_json_type_maps_to_an_iceberg_type():
    assert set(L.section("types.json")) == set(JSON_TYPES.values())


def test_every_contract_type_and_mandatory_key_has_a_label():
    assert {a.type for a in contract.ATTRIBUTES} <= set(L.section("types.contract"))
    assert {a.mandatory for a in contract.ATTRIBUTES} <= set(L.section("enums.mandatory"))


def test_every_workbook_sheet_is_fully_described():
    sheets = L.section("workbook.sheets")
    assert set(sheets) == {"readme", "endpoints", "metadata", "list", "response"}
    for entry in sheets.values():
        assert set(entry) == {"name", "contents", "reader"}


def test_a_missing_key_names_the_file():
    with pytest.raises(KeyError, match="LABELS.yaml"):
        L["no.such.key"]


def test_lookup_and_formatting(tmp_path):
    path = tmp_path / "labels.yaml"
    path.write_text("a:\n  b: 'x {n}'\n", encoding="utf-8")
    loaded = labels.load(path)
    assert loaded["a.b"] == "x {n}" and loaded.fmt("a.b", n=1) == "x 1"
    assert loaded.get("a.zzz", "d") == "d"
    with pytest.raises(FileNotFoundError):
        labels.load(Path(tmp_path / "missing.yaml"))
