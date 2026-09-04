"""The workbook is a projection of the model: sheets from the model, labels from the file,
numbers as numbers, and one sheet per endpoint holding its response's structure."""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from api_extractor.providers.registry import SavedOutput
from specgen import evidence as ev, model, render_xlsx
from specgen.labels import L
from tests.conftest import build_envelope


@pytest.fixture
def workbook(reference_source, reference_annotation, tmp_path):
    built = model.build(reference_source, reference_annotation)
    path = render_xlsx.render(built.model, tmp_path / "annexes.xlsx")
    return built.model, load_workbook(path)


def column(sheet, index: int, start: int = 2) -> list:
    return [sheet.cell(row=r, column=index).value for r in range(start, sheet.max_row + 1)]


def header(sheet) -> list:
    return [sheet.cell(row=1, column=c).value for c in range(1, sheet.max_column + 1)]


def test_sheets_are_the_fixed_three_then_one_per_endpoint(workbook):
    built, book = workbook
    assert book.sheetnames == [tab["name"] for tab in built["appendix"]["workbook"]["tabs"]]
    assert book.sheetnames == ["Readme", "Endpoints", "Metadata", "assets", "alarms", "tenant_info", "measures"]


def test_the_endpoints_sheet_has_the_configured_columns_and_no_more(workbook):
    built, book = workbook
    sheet = book["Endpoints"]
    assert header(sheet) == list(L.section("workbook.endpoints.columns").values())
    assert column(sheet, 1) == [e["name"] for e in built["endpoints"]]
    for gone in ("Appelé", "N°"):
        assert gone not in header(sheet)
    assert sheet.freeze_panes == "A2"


def test_the_metadata_sheet_uses_iceberg_types(workbook):
    built, book = workbook
    sheet = book["Metadata"]
    assert column(sheet, 1) == [row["attribute"] for row in built["landing"]["contract"]]
    assert set(column(sheet, 2)) <= set(L.section("types.contract").values())
    assert "timestamp" in column(sheet, 2) and "int" in column(sheet, 2)


def test_the_readme_has_no_model_vocabulary(workbook):
    _built, book = workbook
    values = [str(cell.value) for row in book["Readme"].iter_rows() for cell in row if cell.value]
    assert not any("[]" in v or v.startswith("Vocabulaire") for v in values)
    assert L["workbook.readme.tabs_title"] in values


def test_a_response_sheet_without_evidence_says_so(workbook):
    _built, book = workbook
    sheet = book["tenant_info"]
    assert header(sheet) == list(L.section("workbook.response.columns").values())
    assert sheet.cell(row=2, column=1).value == L["workbook.response.empty"]


def test_a_response_sheet_describes_the_body_as_observed(reference_source, reference_annotation, tmp_path):
    saved = [
        SavedOutput(
            Path("output/reference/assets/PUMP_p0.json"),
            build_envelope(reference_source, "assets", {"assetType": "PUMP"}, {"data": [{"id": {"id": "PUMP-1"}, "label": None}], "hasNext": True}),
        ),
        SavedOutput(
            Path("output/reference/assets/VALVE_p0.json"),
            build_envelope(reference_source, "assets", {"assetType": "VALVE"}, {"data": [], "hasNext": False}),
        ),
    ]
    built = model.build(reference_source, reference_annotation, ev.Evidence(envelopes={"assets": saved}))
    book = load_workbook(render_xlsx.render(built.model, tmp_path / "a.xlsx"))
    sheet = book["assets"]
    rows = {sheet.cell(row=r, column=1).value: [sheet.cell(row=r, column=c).value for c in range(2, 6)] for r in range(2, sheet.max_row + 1)}
    assert rows["$.hasNext"] == ["boolean", "Non", 1.0, "True"]
    assert rows["$.data[].id.id"][:2] == ["string", "Oui"]  # present in one response of two
    assert rows["$.data[].label"][:2] == ["null", "Oui"]
    assert sheet.cell(row=2, column=4).number_format == "0 %"


def test_no_sheet_and_no_cell_mentions_volumes(workbook):
    _built, book = workbook
    for sheet in book.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                assert "volum" not in str(cell.value or "").lower(), (sheet.title, cell.coordinate)


@pytest.mark.parametrize("name, expected", [("assets", "assets"), ("a/b:c?", "a_b_c_"), ("x" * 40, "x" * 31)])
def test_sheet_titles_fit_excel(name, expected):
    assert render_xlsx._sheet_title(name) == expected
