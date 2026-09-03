"""The workbook is a projection of the model: same tabs, same rows, numbers as numbers."""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from api_extractor.providers.registry import SavedOutput
from specgen import evidence as ev, model, render_xlsx
from tests.conftest import build_envelope


@pytest.fixture
def workbook(reference_source, reference_annotation, tmp_path):
    built = model.build(reference_source, reference_annotation)
    path = render_xlsx.render(built.model, tmp_path / "annexes.xlsx")
    return built.model, load_workbook(path)


def test_tab_names_come_from_the_model(workbook):
    built, book = workbook
    assert book.sheetnames == [tab["name"] for tab in built["appendix"]["workbook"]["tabs"]]
    assert book.sheetnames == ["Lisez-moi", "Inventaire des endpoints", "Métadonnées de dépôt", "Volumétrie"]


def test_one_row_per_endpoint_in_document_order(workbook):
    built, book = workbook
    sheet = book["Inventaire des endpoints"]
    names = [sheet.cell(row=r, column=2).value for r in range(2, sheet.max_row + 1)]
    assert names == [e["name"] for e in built["endpoints"]]
    assert sheet.cell(row=1, column=2).value == "Endpoint"
    assert sheet.freeze_panes == "A2"


def test_the_contract_tab_has_every_attribute(workbook):
    built, book = workbook
    sheet = book["Métadonnées de dépôt"]
    attributes = [sheet.cell(row=r, column=1).value for r in range(2, sheet.max_row + 1)]
    assert attributes == [row["attribute"] for row in built["landing"]["contract"]]


def test_planned_counts_are_numbers(workbook):
    _built, book = workbook
    sheet = book["Volumétrie"]
    by_name = {sheet.cell(row=r, column=1).value: sheet.cell(row=r, column=3).value for r in range(2, sheet.max_row + 1)}
    assert by_name["alarms"] == 2 and isinstance(by_name["alarms"], int)
    assert by_name["measures"] is None


def test_the_readme_carries_the_template_vocabulary(workbook):
    _built, book = workbook
    values = [cell.value for row in book["Lisez-moi"].iter_rows() for cell in row if cell.value]
    assert "endpoints[].params[].origin" in values
    assert any(str(v).startswith("Vocabulaire du modèle") for v in values)


def test_the_field_inventory_tab_appears_with_evidence(reference_source, reference_annotation, tmp_path):
    saved = SavedOutput(
        Path("output/reference/assets/PUMP_p0.json"),
        build_envelope(reference_source, "assets", {"assetType": "PUMP"}, {"data": [{"id": {"id": "PUMP-1"}}], "hasNext": False}),
    )
    built = model.build(reference_source, reference_annotation, ev.Evidence(envelopes={"assets": [saved]}))
    book = load_workbook(render_xlsx.render(built.model, tmp_path / "a.xlsx"))
    sheet = book["Inventaire des champs"]
    paths = [sheet.cell(row=r, column=2).value for r in range(2, sheet.max_row + 1)]
    assert "$.data[].id.id" in paths
    presence = sheet.cell(row=2, column=4)
    assert presence.value == 1.0 and presence.number_format == "0 %"
