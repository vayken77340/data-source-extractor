"""Tests for `tools/build_params.py`.

Loaded by path, because `tools/` is not a package and is not meant to be importable — the
script is run by hand, outside the application.

The merged-cell case is the reason this script exists at all: without forward-fill, every
row but the first of a merged range is blank and drops, so half the measure types vanish
and the run still looks healthy.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from tests.conftest import REPO_ROOT


def load_tool():
    path = REPO_ROOT / "tools" / "build_params.py"
    spec = importlib.util.spec_from_file_location("build_params_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_params = load_tool()


def make_xlsx(path: Path, sheet: str, rows: list[list], merges: list[str] = ()) -> Path:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    for row in rows:
        worksheet.append(row)
    for span in merges:
        worksheet.merge_cells(span)
    workbook.save(path)
    return path


@pytest.fixture
def merged_sheet(tmp_path) -> Path:
    """assetType merged across the two rows of measures it owns, as the business writes it."""
    return make_xlsx(
        tmp_path / "asset_types.xlsx",
        "Referentiel",
        [
            ["assetType", "measureType"],
            ["PUMP", "temperature"],
            [None, "humidity"],
            ["VALVE", "pressure"],
        ],
        merges=["A2:A3"],
    )


def extract(path: Path, columns: list[str], ffill: list[str], sheet: str = "Referentiel"):
    rows = build_params.read_sheet(path, sheet)
    return build_params.extract(rows, build_params.parse_columns(columns), frozenset(ffill))


def test_forward_fill_carries_a_merged_cell_down(merged_sheet):
    assert extract(merged_sheet, ["assetType", "measureType"], ["assetType"]) == [
        {"assetType": "PUMP", "measureType": "temperature"},
        {"assetType": "PUMP", "measureType": "humidity"},
        {"assetType": "VALVE", "measureType": "pressure"},
    ]


def test_without_forward_fill_the_merged_rows_are_lost(merged_sheet):
    """Documents the failure this script prevents: humidity silently disappears."""
    assert extract(merged_sheet, ["assetType", "measureType"], []) == [
        {"assetType": "PUMP", "measureType": "temperature"},
        {"assetType": "VALVE", "measureType": "pressure"},
    ]


def test_a_leading_blank_in_a_filled_column_drops_rather_than_crashing(tmp_path):
    path = make_xlsx(
        tmp_path / "a.xlsx",
        "S",
        [["assetType", "measureType"], [None, "orphan"], ["PUMP", "temperature"]],
    )
    assert extract(path, ["assetType", "measureType"], ["assetType"], sheet="S") == [
        {"assetType": "PUMP", "measureType": "temperature"},
    ]


def test_blanks_are_dropped_and_duplicates_collapse(tmp_path):
    path = make_xlsx(
        tmp_path / "a.xlsx",
        "S",
        [
            ["assetType", "measureType"],
            ["PUMP", "temperature"],
            ["  PUMP  ", "  temperature  "],
            ["VALVE", "   "],
            ["TANK", "level"],
        ],
    )
    assert extract(path, ["assetType", "measureType"], [], sheet="S") == [
        {"assetType": "PUMP", "measureType": "temperature"},
        {"assetType": "TANK", "measureType": "level"},
    ]


def test_missing_column_names_the_headers(merged_sheet):
    with pytest.raises(SystemExit, match="headers: assetType, measureType"):
        extract(merged_sheet, ["assetTypes"], [])


# ── Renaming ───────────────────────────────────────────────────────────────────────────
# The sheet's vocabulary is the business's; the parameter file's is yours. Renaming here
# means no marker, bind key or output template downstream ever sees the sheet's spelling.


@pytest.fixture
def business_sheet(tmp_path) -> Path:
    """Headers as the business writes them: spaces, accents, capitals, a merged type."""
    return make_xlsx(
        tmp_path / "referentiel.xlsx",
        "Referentiel",
        [
            ["Type d'actif", "Grandeur mesuree", "Commentaire"],
            ["PUMP", "temperature", "ignore me"],
            [None, "humidity", None],
            ["VALVE", "pressure", None],
        ],
        merges=["A2:A3"],
    )


def test_a_column_is_renamed_on_the_way_out(business_sheet):
    rows = extract(
        business_sheet,
        ["Type d'actif=assetType", "Grandeur mesuree=measureType"],
        ["assetType"],
    )
    assert rows == [
        {"assetType": "PUMP", "measureType": "temperature"},
        {"assetType": "PUMP", "measureType": "humidity"},
        {"assetType": "VALVE", "measureType": "pressure"},
    ]


def test_ffill_names_the_output_column_not_the_sheets(business_sheet):
    """Past the header row everything is in your vocabulary, so `--ffill` is too."""
    with pytest.raises(SystemExit, match=r"--ffill names .+ not in --columns \['assetType'\]"):
        build_params.main(
            [
                str(business_sheet),
                "-o",
                str(business_sheet.parent / "out.json"),
                "--sheet",
                "Referentiel",
                "--columns",
                "Type d'actif=assetType",
                "--ffill",
                "Type d'actif",
            ]
        )


def test_a_bare_column_is_not_renamed(merged_sheet):
    assert extract(merged_sheet, ["assetType=assetType", "measureType"], ["assetType"]) == extract(
        merged_sheet, ["assetType", "measureType"], ["assetType"]
    )


def test_two_columns_may_not_map_to_one_name():
    with pytest.raises(SystemExit, match="maps two sheet columns to 'assetType'"):
        build_params.parse_columns(["Type=assetType", "Kind=assetType"])


def test_the_same_sheet_column_may_not_be_named_twice():
    with pytest.raises(SystemExit, match="names the sheet column 'Type' twice"):
        build_params.parse_columns(["Type=assetType", "Type=kind"])


def test_the_flag_may_be_repeated_instead_of_comma_joined(business_sheet, tmp_path):
    out = tmp_path / "out.json"
    build_params.main(
        [
            str(business_sheet),
            "-o",
            str(out),
            "--sheet",
            "Referentiel",
            "--columns",
            "Type d'actif=assetType",
            "--columns",
            "Grandeur mesuree=measureType",
            "--ffill",
            "assetType",
        ]
    )
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["columns"] == ["assetType", "measureType"]
    assert document["rows"][1] == {"assetType": "PUMP", "measureType": "humidity"}


def test_missing_sheet_names_the_ones_present(merged_sheet):
    with pytest.raises(SystemExit, match="sheets: Referentiel"):
        build_params.read_sheet(merged_sheet, "Sheet1")


def test_missing_file(tmp_path):
    with pytest.raises(SystemExit, match="no such file"):
        build_params.read_sheet(tmp_path / "nope.xlsx", "S")


def test_an_empty_sheet_yields_no_rows(tmp_path):
    path = make_xlsx(tmp_path / "a.xlsx", "S", [])
    assert extract(path, ["assetType"], [], sheet="S") == []


def test_end_to_end_writes_a_parameter_file(merged_sheet, tmp_path):
    out = tmp_path / "params" / "asset_types.json"
    build_params.main(
        [
            str(merged_sheet),
            "-o",
            str(out),
            "--sheet",
            "Referentiel",
            "--columns",
            "assetType,measureType",
            "--ffill",
            "assetType",
        ]
    )
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["columns"] == ["assetType", "measureType"]
    assert document["sheet"] == "Referentiel"
    assert document["source_file"].endswith("asset_types.xlsx")
    assert document["generated_at"].endswith("Z")
    assert len(document["rows"]) == 3


def test_ffill_must_name_a_requested_column(merged_sheet, tmp_path):
    with pytest.raises(SystemExit, match="not in --columns"):
        build_params.main(
            [
                str(merged_sheet),
                "-o",
                str(tmp_path / "out.json"),
                "--sheet",
                "Referentiel",
                "--columns",
                "assetType",
                "--ffill",
                "measureType",
            ]
        )


def test_a_run_that_yields_nothing_is_an_error_not_an_empty_file(tmp_path):
    """An empty parameter file would plan zero requests and report success."""
    path = make_xlsx(tmp_path / "a.xlsx", "S", [["assetType"], [None]])
    out = tmp_path / "out.json"
    with pytest.raises(SystemExit, match="yielded no rows"):
        build_params.main(
            [str(path), "-o", str(out), "--sheet", "S", "--columns", "assetType"]
        )
    assert not out.exists()
