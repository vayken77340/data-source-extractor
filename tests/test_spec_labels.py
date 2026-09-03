"""The French label catalogue: formatting, and one label for every enum the models accept.

A missing label would render an English identifier into a French document — quietly.
"""

from __future__ import annotations

from datetime import date
from typing import get_args

import pytest

from api_extractor.config.models import PAGINATE_ROOTS, Auth
from specgen import contract, labels
from specgen.annotation import EndpointAnnotation, SpecBlock

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


def test_an_invariable_plural_is_explicit():
    assert labels.plural(7, "fois", "fois") == "7 fois"


def test_yes_no():
    assert (labels.yes_no(True), labels.yes_no(False)) == ("Oui", "Non")


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


def test_json_types_have_names():
    assert [labels.type_of(v) for v in (1, "a", True, 1.5, {}, [], None)] == [
        "entier",
        "chaîne",
        "booléen",
        "décimal",
        "objet",
        "liste",
        "nul",
    ]


# --- every enum has a label --------------------------------------------------------


def literal_values(model, field: str) -> set[str]:
    return set(get_args(model.model_fields[field].annotation))


def test_every_status_has_a_label():
    assert set(labels.STATUS) == literal_values(SpecBlock, "status")


def test_every_mode_has_a_label():
    assert set(labels.MODE) == literal_values(EndpointAnnotation, "mode") - {labels.TODO}


def test_every_auth_type_has_a_label():
    union, _metadata = get_args(Auth)
    names = {get_args(member.model_fields["type"].annotation)[0] for member in get_args(union)}
    assert set(labels.AUTH) == names


def test_every_cursor_root_has_a_label():
    assert set(labels.CURSOR_ROOT) == set(PAGINATE_ROOTS)
    assert set(PAGINATE_ROOTS) <= set(labels.LOCATION)


def test_every_contract_type_and_mandatory_key_has_a_label():
    assert {a.type for a in contract.ATTRIBUTES} <= set(labels.CONTRACT_TYPE)
    assert {a.mandatory for a in contract.ATTRIBUTES} <= set(labels.MANDATORY)


def test_workbook_tab_tables_agree():
    assert set(labels.TABS) == set(labels.TAB_CONTENTS) == set(labels.TAB_READERS)
