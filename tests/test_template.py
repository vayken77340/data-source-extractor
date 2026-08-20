"""The template is documentation, so it is tested like code.

Anything shown uncommented in config/TEMPLATE.yaml must actually validate, and every
auth type and pagination style the models accept must appear somewhere in the file.
"""

from __future__ import annotations

from typing import get_args

import pytest

from api_extractor.config.loader import load_source
from api_extractor.config.models import Auth, Paginate
from api_extractor.config.validate import validate_source
from tests.conftest import REPO_ROOT

TEMPLATE = REPO_ROOT / "config" / "TEMPLATE.yaml"


def auth_type_names() -> set[str]:
    """Pull the `type` literal off each member of the Auth union, so this can't drift."""
    union, _metadata = get_args(Auth)
    return {get_args(member.model_fields["type"].annotation)[0] for member in get_args(union)}


@pytest.fixture
def template_env(monkeypatch):
    monkeypatch.setenv("MY_USER", "svc-account")
    monkeypatch.setenv("MY_PASSWORD", "hunter2-hunter2")


def test_template_validates(template_env):
    report = validate_source(TEMPLATE)
    assert report.ok, "\n".join(str(issue) for issue in report.issues)


def test_template_shows_every_auth_type():
    text = TEMPLATE.read_text(encoding="utf-8")
    missing = [name for name in auth_type_names() if f"type: {name}" not in text]
    assert not missing, f"auth types absent from the template: {missing}"


def test_template_shows_every_pagination_style(template_env):
    """Derived from the model, so shipping a new style forces documenting it."""
    source = load_source(TEMPLATE)
    shown = {
        endpoint.paginate.style
        for endpoint in source.endpoints.values()
        if endpoint.paginate is not None
    }
    assert shown == set(get_args(Paginate.model_fields["style"].annotation))


def test_template_shows_the_cursor_landing_in_both_halves_of_a_request(template_env):
    """A query param and a nested payload key are the two shapes in the wild."""
    source = load_source(TEMPLATE)
    roots = {
        endpoint.paginate.at_root
        for endpoint in source.endpoints.values()
        if endpoint.paginate is not None
    }
    assert roots == {"query", "payload"}


def test_template_shows_both_fan_out_modes(template_env):
    source = load_source(TEMPLATE)
    assert {endpoint.fan_out for endpoint in source.endpoints.values()} == {"product", "zip"}


def test_template_shows_the_limit_cascade(template_env):
    """default, per-endpoint override, and explicit null for unlimited."""
    source = load_source(TEMPLATE)
    assert source.defaults.limit == 5
    assert source.endpoints["measures"].limit == 20
    paired = source.endpoints["paired"]
    assert paired.limit is None and not paired.inherits_limit()
    assert source.endpoints["ping"].inherits_limit()


def test_template_shows_every_builtin_provider(template_env):
    source = load_source(TEMPLATE)
    assert {decl.fn for decl in source.providers.values()} == {
        "literal",
        "excel_column",
        "from_output",
    }
