"""CLI tests.

The CLI resolves sources relative to the working directory, so these run against a
throwaway project built in tmp_path rather than against whatever is in `config/sources/`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from api_extractor.cli import app
from api_extractor.providers import registry
from tests.conftest import REFERENCE_ENV, REFERENCE_NAME, REFERENCE_SOURCE, REPO_ROOT

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


def test_env_is_read_from_the_working_directory(project, monkeypatch):
    """Credentials and proxy settings come from .env, so nothing needs setting per shell.

    A bare load_dotenv() searches from the *package's* directory, which quietly finds the
    repo's own .env whichever project you are standing in.
    """
    for name in REFERENCE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    (project / ".env").write_text(
        "\n".join(f"{k}={v}" for k, v in REFERENCE_ENV.items())
        + "\nHTTPS_PROXY=http://proxy.example.com:8080\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["validate", REFERENCE_NAME])

    assert result.exit_code == 0, result.output
    assert os.environ["HTTPS_PROXY"] == "http://proxy.example.com:8080"


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


# --- the no-setup entry point --------------------------------------------------------


def test_main_py_puts_src_on_the_path():
    """`python main.py` must work with nothing installed and no PYTHONPATH."""
    import main

    assert str(main.SRC) in sys.path
    assert (main.SRC / "api_extractor" / "cli.py").is_file()


def test_main_py_runs_as_a_subprocess():
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "main.py", "list-providers"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "literal(values)" in result.stdout
