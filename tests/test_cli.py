"""CLI tests.

The CLI resolves sources relative to the working directory, so these run against a
throwaway project built in tmp_path rather than against whatever is in `config/sources/`.
"""

from __future__ import annotations

import shutil

import pytest
from typer.testing import CliRunner

from api_extractor.cli import app
from api_extractor.providers import registry
from tests.conftest import REFERENCE_ENV, REFERENCE_NAME, REFERENCE_SOURCE

runner = CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A minimal project tree: config/sources/<reference>.yaml and nothing else.

    The registry is given its own copy, because every CLI invocation loads providers from
    the working directory and would otherwise leak them into the rest of the session.
    """
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    sources = tmp_path / "config" / "sources"
    sources.mkdir(parents=True)
    shutil.copy(REFERENCE_SOURCE, sources / REFERENCE_SOURCE.name)
    monkeypatch.chdir(tmp_path)
    for name, value in REFERENCE_ENV.items():
        monkeypatch.setenv(name, value)
    return tmp_path


def test_validate_ok(project):
    result = runner.invoke(app, ["validate", REFERENCE_NAME])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output


def test_validate_show_checks_lists_what_ran(project):
    result = runner.invoke(app, ["validate", REFERENCE_NAME, "--show-checks"])
    assert "config.env.vars_set" in result.output
    assert "config.dag.acyclic" in result.output
    assert "deferred:" not in result.output


def test_validate_reports_a_missing_env_var(project, monkeypatch):
    monkeypatch.delenv("REF_PASSWORD", raising=False)
    result = runner.invoke(app, ["validate", REFERENCE_NAME])
    assert result.exit_code == 1
    assert "REF_PASSWORD is not set" in result.output


def test_validate_missing_source_exits_nonzero(project):
    result = runner.invoke(app, ["validate", "no-such-source"])
    assert result.exit_code == 1
    assert "cannot read file" in result.output


def test_list_sources(project):
    result = runner.invoke(app, ["list-sources"])
    assert result.exit_code == 0
    assert REFERENCE_NAME in result.output


def test_list_sources_with_none_defined(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["list-sources"])
    assert result.exit_code == 0
    assert "no source definitions" in result.output


def test_list_providers(project):
    """The built-ins are always there; local providers come from ./providers."""
    result = runner.invoke(app, ["list-providers"])
    assert result.exit_code == 0
    assert "literal(values)" in result.output
    assert "from_output(endpoint, path)  (chains off an endpoint)" in result.output


def test_list_providers_picks_up_a_local_provider(project):
    (project / "providers").mkdir()
    (project / "providers" / "mine.py").write_text(
        "from api_extractor.providers import provider\n"
        "\n"
        "@provider('cli_probe')\n"
        "def cli_probe(ctx, *, colour: str):\n"
        "    return [{'colour': colour}]\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["list-providers"])
    assert "cli_probe(colour)" in result.output


def test_dry_run_prints_the_plan_and_issues_nothing(project):
    result = runner.invoke(app, ["run", REFERENCE_NAME, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dag order" in result.output
    assert "total" in result.output
    assert not (project / "output").exists()


def test_run_rejects_an_unknown_endpoint(project):
    result = runner.invoke(app, ["run", REFERENCE_NAME, "--endpoint", "nope", "--dry-run"])
    assert result.exit_code == 1
    assert "no such endpoint(s): nope" in result.output
