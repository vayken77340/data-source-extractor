from __future__ import annotations

import pytest
from typer.testing import CliRunner

from api_extractor.cli import app
from tests.conftest import REFERENCE_NAME, REPO_ROOT

runner = CliRunner()


@pytest.fixture
def in_repo(monkeypatch):
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("TB_USER", "svc-account")
    monkeypatch.setenv("TB_PASSWORD", "hunter2-hunter2")


def test_validate_ok(in_repo):
    result = runner.invoke(app, ["validate", REFERENCE_NAME])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output


def test_validate_show_checks_lists_what_ran(in_repo):
    result = runner.invoke(app, ["validate", REFERENCE_NAME, "--show-checks"])
    assert "config.env.vars_set" in result.output
    assert "config.dag.acyclic" in result.output
    assert "deferred:" not in result.output


def test_list_providers(in_repo):
    result = runner.invoke(app, ["list-providers"])
    assert result.exit_code == 0
    assert "excel_column(path, sheet, columns)" in result.output
    assert "from_output(endpoint, path)  (chains off an endpoint)" in result.output
    assert "literal(values)" in result.output


def test_validate_missing_source_exits_nonzero(in_repo):
    result = runner.invoke(app, ["validate", "no-such-source"])
    assert result.exit_code == 1
    assert "cannot read file" in result.output


def test_list_sources(in_repo):
    result = runner.invoke(app, ["list-sources"])
    assert result.exit_code == 0
    assert REFERENCE_NAME in result.output
