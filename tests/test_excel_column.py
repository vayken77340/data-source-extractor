"""Tests for `providers/excel_column.py` — a local provider, not part of the framework.

It is reached through the registry rather than imported, because that is how the runner
reaches it: the file is loaded by path at startup, not imported by name.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from api_extractor.providers import registry
from api_extractor.providers.registry import ProviderContext

CTX = ProviderContext(run_id="test-run", output_root=Path("output"), source_name="demo")


@pytest.fixture(scope="module")
def excel_column():
    """Loaded by tests/conftest.py, exactly as the CLI loads it."""
    return registry.get("excel_column").fn


def make_xlsx(path: Path, sheet: str, rows: list[list]) -> Path:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    for row in rows:
        worksheet.append(row)
    workbook.save(path)
    return path


def test_it_is_registered_but_is_not_a_builtin():
    from api_extractor import providers

    assert registry.is_registered("excel_column")
    assert "excel_column" not in providers.BUILTINS


def test_reads_a_column(tmp_path, excel_column):
    path = make_xlsx(tmp_path / "a.xlsx", "Referentiel", [["assetType"], ["PUMP"], ["VALVE"]])
    assert excel_column(CTX, path=str(path), sheet="Referentiel", columns=["assetType"]) == [
        {"assetType": "PUMP"},
        {"assetType": "VALVE"},
    ]


def test_keeps_several_columns_row_wise(tmp_path, excel_column):
    """assetType and region off one row stay correlated — never a cross product."""
    path = make_xlsx(
        tmp_path / "a.xlsx",
        "S",
        [["assetType", "region", "note"], ["PUMP", "EU", "x"], ["VALVE", "US", "y"]],
    )
    assert excel_column(CTX, path=str(path), sheet="S", columns=["assetType", "region"]) == [
        {"assetType": "PUMP", "region": "EU"},
        {"assetType": "VALVE", "region": "US"},
    ]


def test_drops_blanks_and_dedupes_preserving_order(tmp_path, excel_column):
    path = make_xlsx(
        tmp_path / "a.xlsx",
        "S",
        [
            ["assetType"],
            ["VALVE"],
            [None],
            ["PUMP"],
            ["   "],
            ["VALVE"],
            ["  PUMP  "],
            ["TANK"],
        ],
    )
    assert excel_column(CTX, path=str(path), sheet="S", columns=["assetType"]) == [
        {"assetType": "VALVE"},
        {"assetType": "PUMP"},
        {"assetType": "TANK"},
    ]


def test_drops_a_row_missing_any_requested_cell(tmp_path, excel_column):
    """A blank parameter is not worth a request, and a partial row breaks correlation."""
    path = make_xlsx(
        tmp_path / "a.xlsx",
        "S",
        [["assetType", "region"], ["PUMP", "EU"], ["VALVE", None], ["TANK", "US"]],
    )
    assert excel_column(CTX, path=str(path), sheet="S", columns=["assetType", "region"]) == [
        {"assetType": "PUMP", "region": "EU"},
        {"assetType": "TANK", "region": "US"},
    ]


def test_preserves_non_string_values(tmp_path, excel_column):
    path = make_xlsx(tmp_path / "a.xlsx", "S", [["code"], [100], [200]])
    assert excel_column(CTX, path=str(path), sheet="S", columns=["code"]) == [
        {"code": 100},
        {"code": 200},
    ]


def test_missing_file(tmp_path, excel_column):
    with pytest.raises(FileNotFoundError, match="no such file"):
        excel_column(CTX, path=str(tmp_path / "nope.xlsx"), sheet="S", columns=["a"])


def test_missing_sheet_names_the_ones_present(tmp_path, excel_column):
    path = make_xlsx(tmp_path / "a.xlsx", "Referentiel", [["assetType"]])
    with pytest.raises(ValueError, match="sheets: Referentiel"):
        excel_column(CTX, path=str(path), sheet="Sheet1", columns=["assetType"])


def test_missing_column_names_the_headers(tmp_path, excel_column):
    path = make_xlsx(tmp_path / "a.xlsx", "S", [["assetType", "region"], ["PUMP", "EU"]])
    with pytest.raises(ValueError, match="headers: assetType, region"):
        excel_column(CTX, path=str(path), sheet="S", columns=["assetTypes"])


def test_on_an_empty_sheet(tmp_path, excel_column):
    path = make_xlsx(tmp_path / "a.xlsx", "S", [])
    assert excel_column(CTX, path=str(path), sheet="S", columns=["assetType"]) == []
