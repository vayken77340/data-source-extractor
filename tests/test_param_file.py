"""Tests for `providers/param_file.py` — a local provider, not part of the framework.

Reached through the registry rather than imported, because that is how the runner reaches
it: the file is loaded by path at startup, not imported by name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from api_extractor import providers
from api_extractor.providers import registry
from api_extractor.providers.registry import ProviderContext
from tests.conftest import REPO_ROOT

CTX = ProviderContext(run_id="test-run", output_root=Path("output"), source_name="demo")


@pytest.fixture(scope="module")
def param_file():
    providers.load_from(REPO_ROOT / "providers")
    return registry.get("param_file").fn


def write_json(path: Path, rows: list[dict], columns: list[str] | None = None) -> Path:
    document: dict = {"schema": "param-file/1", "rows": rows}
    if columns is not None:
        document["columns"] = columns
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_it_is_registered_but_is_not_a_builtin():
    assert registry.is_registered("param_file")
    assert "param_file" not in providers.BUILTINS


def test_reads_a_column(tmp_path, param_file):
    path = write_json(
        tmp_path / "p.json",
        [{"assetType": "PUMP"}, {"assetType": "VALVE"}],
        columns=["assetType"],
    )
    assert param_file(CTX, path=str(path), columns=["assetType"]) == [
        {"assetType": "PUMP"},
        {"assetType": "VALVE"},
    ]


def test_keeps_several_columns_row_wise(tmp_path, param_file):
    """assetType and measureType off one row stay correlated — never a cross product."""
    path = write_json(
        tmp_path / "p.json",
        [
            {"assetType": "PUMP", "measureType": "temperature", "note": "x"},
            {"assetType": "VALVE", "measureType": "pressure", "note": "y"},
        ],
        columns=["assetType", "measureType", "note"],
    )
    assert param_file(CTX, path=str(path), columns=["assetType", "measureType"]) == [
        {"assetType": "PUMP", "measureType": "temperature"},
        {"assetType": "VALVE", "measureType": "pressure"},
    ]


def test_projecting_one_column_dedupes(tmp_path, param_file):
    """The parameter file is denormalized, so a type repeats once per measure."""
    path = write_json(
        tmp_path / "p.json",
        [
            {"assetType": "PUMP", "measureType": "temperature"},
            {"assetType": "PUMP", "measureType": "humidity"},
            {"assetType": "VALVE", "measureType": "pressure"},
        ],
        columns=["assetType", "measureType"],
    )
    assert param_file(CTX, path=str(path), columns=["assetType"]) == [
        {"assetType": "PUMP"},
        {"assetType": "VALVE"},
    ]


def test_drops_a_row_missing_any_requested_cell(tmp_path, param_file):
    path = write_json(
        tmp_path / "p.json",
        [
            {"assetType": "PUMP", "measureType": "temperature"},
            {"assetType": "VALVE", "measureType": None},
            {"assetType": "TANK"},
            {"assetType": "GRID", "measureType": "voltage"},
        ],
        columns=["assetType", "measureType"],
    )
    assert param_file(CTX, path=str(path), columns=["assetType", "measureType"]) == [
        {"assetType": "PUMP", "measureType": "temperature"},
        {"assetType": "GRID", "measureType": "voltage"},
    ]


def test_preserves_non_string_values(tmp_path, param_file):
    path = write_json(tmp_path / "p.json", [{"code": 100}, {"code": 200}], columns=["code"])
    assert param_file(CTX, path=str(path), columns=["code"]) == [{"code": 100}, {"code": 200}]


def test_reads_yaml_as_the_same_shape(tmp_path, param_file):
    """A second encoding for hand-maintained tables, never a second structure."""
    path = tmp_path / "p.yaml"
    path.write_text(
        yaml.safe_dump({"columns": ["region"], "rows": [{"region": "EU"}, {"region": "US"}]}),
        encoding="utf-8",
    )
    assert param_file(CTX, path=str(path), columns=["region"]) == [
        {"region": "EU"},
        {"region": "US"},
    ]


def test_columns_may_be_inferred_when_the_file_does_not_declare_them(tmp_path, param_file):
    path = write_json(tmp_path / "p.json", [{"region": "EU"}])
    assert param_file(CTX, path=str(path), columns=["region"]) == [{"region": "EU"}]


def test_missing_file_says_how_to_make_one(tmp_path, param_file):
    with pytest.raises(FileNotFoundError, match="tools/build_params.py"):
        param_file(CTX, path=str(tmp_path / "nope.json"), columns=["a"])


def test_missing_column_names_the_ones_present(tmp_path, param_file):
    path = write_json(tmp_path / "p.json", [{"assetType": "PUMP"}], columns=["assetType"])
    with pytest.raises(ValueError, match="columns: assetType"):
        param_file(CTX, path=str(path), columns=["assetTypes"])


def test_a_file_that_is_not_a_parameter_file(tmp_path, param_file):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"asset_types": ["PUMP"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="no `rows` list"):
        param_file(CTX, path=str(path), columns=["assetType"])


def test_on_an_empty_parameter_file(tmp_path, param_file):
    path = write_json(tmp_path / "p.json", [], columns=["assetType"])
    assert param_file(CTX, path=str(path), columns=["assetType"]) == []
