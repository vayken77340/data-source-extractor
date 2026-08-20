from __future__ import annotations

import pytest

from api_extractor.config.loader import (
    MissingEnvVarError,
    iter_env_refs,
    list_sources,
    load_source,
    resolve_env_string,
    resolve_env_tree,
    source_path,
)
from api_extractor.logs import REDACTED, register_secret, scrub
from tests.conftest import REFERENCE_SOURCE


def test_source_path_and_list_sources(tmp_path):
    (tmp_path / "a.yaml").write_text("", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")
    assert list_sources(tmp_path) == ["a", "b"]
    assert list_sources(tmp_path / "nope") == []
    assert source_path("demo", tmp_path).name == "demo.yaml"


def test_resolve_env_string(monkeypatch):
    monkeypatch.setenv("TB_USER", "svc-account")
    assert resolve_env_string("env:TB_USER") == "svc-account"
    assert resolve_env_string("plain") == "plain"


def test_missing_env_var_raises_at_point_of_use(monkeypatch):
    monkeypatch.delenv("TB_USER", raising=False)
    with pytest.raises(MissingEnvVarError, match="TB_USER"):
        resolve_env_string("env:TB_USER")


def test_resolve_env_tree(monkeypatch):
    monkeypatch.setenv("TB_USER", "svc-account")
    monkeypatch.setenv("TB_PASSWORD", "hunter2-hunter2")
    tree = {"payload": {"username": "env:TB_USER", "password": "env:TB_PASSWORD"}, "n": [1, "x"]}
    assert resolve_env_tree(tree) == {
        "payload": {"username": "svc-account", "password": "hunter2-hunter2"},
        "n": [1, "x"],
    }


def test_resolution_registers_the_value_as_a_secret(monkeypatch):
    monkeypatch.setenv("TB_PASSWORD", "hunter2-hunter2")
    resolve_env_string("env:TB_PASSWORD")
    assert scrub("logging in with hunter2-hunter2") == f"logging in with {REDACTED}"


def test_short_values_are_not_scrubbed(monkeypatch):
    """Redacting a page size would make logs unreadable for no security gain."""
    monkeypatch.setenv("TB_PAGE", "100")
    resolve_env_string("env:TB_PAGE")
    assert scrub("pageSize=100") == "pageSize=100"


@pytest.mark.parametrize("var", ["TB_PASSWORD", "TB_PASS", "TB_TOKEN", "TB_SECRET", "TB_API_KEY"])
def test_credential_named_vars_ignore_the_length_floor(monkeypatch, var):
    """A short dev password is exactly the one that gets reused somewhere that matters."""
    monkeypatch.setenv(var, "hunter")
    resolve_env_string(f"env:{var}")
    assert scrub("login=hunter") == f"login={REDACTED}"


def test_a_secret_registered_without_a_var_name_is_unconditional():
    """The login_token bearer never passes through the environment."""
    register_secret("abc")
    assert scrub("Bearer abc") == f"Bearer {REDACTED}"


def test_iter_env_refs_finds_every_reference():
    source = load_source(REFERENCE_SOURCE)
    refs = dict(iter_env_refs(source))
    assert refs == {"auth.username": "TB_USER", "auth.password": "TB_PASSWORD"}


def test_parsed_config_holds_no_secrets(monkeypatch):
    """`env:` refs stay unresolved in the model, so the config is safe to print."""
    monkeypatch.setenv("TB_PASSWORD", "hunter2-hunter2")
    source = load_source(REFERENCE_SOURCE)
    assert source.auth is not None
    assert source.auth.password == "env:TB_PASSWORD"
