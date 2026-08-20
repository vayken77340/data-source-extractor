from __future__ import annotations

from pathlib import Path

import pytest

from api_extractor import providers
from api_extractor.providers import registry
from api_extractor.providers.builtin import from_output, literal
from api_extractor.providers.registry import ProviderContext

CTX = ProviderContext(run_id="test-run", output_root=Path("output"), source_name="demo")


@pytest.fixture
def isolated_registry(monkeypatch):
    """Registration is global and permanent; give mutating tests their own copy."""
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))


# --- registry -----------------------------------------------------------------------


def test_a_file_in_the_providers_directory_registers_itself(tmp_path, isolated_registry):
    """The extension point is outside src — a dev never opens the framework's code."""
    directory = tmp_path / "providers"
    directory.mkdir()
    (directory / "mine.py").write_text(
        "from api_extractor.providers import ProviderContext, provider\n"
        "\n"
        "@provider('my_column')\n"
        "def my_column(ctx: ProviderContext, *, colour: str):\n"
        "    return [{'colour': colour}]\n",
        encoding="utf-8",
    )
    (directory / "_ignored.py").write_text("raise AssertionError('should not load')\n", "utf-8")
    (directory / "notes.txt").write_text("not python\n", encoding="utf-8")

    assert providers.load_from(directory) == ["mine"]
    assert registry.is_registered("my_column")
    assert registry.get("my_column").arg_names() == ["colour"]


def test_loading_from_a_directory_that_does_not_exist(tmp_path):
    assert providers.load_from(tmp_path / "nope") == []


def test_a_user_provider_may_not_shadow_a_builtin(tmp_path, isolated_registry):
    directory = tmp_path / "providers"
    directory.mkdir()
    (directory / "clash.py").write_text(
        "from api_extractor.providers import provider\n"
        "\n"
        "@provider('literal')\n"
        "def literal(ctx, *, values):\n"
        "    return values\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="already registered"):
        providers.load_from(directory)


def test_the_builtins_are_only_what_is_generic():
    """A spreadsheet reader is somebody's use case, not the framework's business."""
    assert set(providers.BUILTINS) == {"literal", "from_output"}


def test_duplicate_registration_raises(isolated_registry):
    """Last-wins across two files is maddening to debug."""
    with pytest.raises(RuntimeError, match="already registered"):

        @registry.provider("literal")
        def _shadow(ctx, *, values):
            return values


def test_a_new_name_registers_fine(isolated_registry):
    @registry.provider("my_thing")
    def _mine(ctx, *, colour: str) -> list[dict]:
        return [{"colour": colour}]

    assert registry.is_registered("my_thing")
    assert registry.get("my_thing").arg_names() == ["colour"]


def test_from_output_declares_its_endpoint():
    entry = registry.get("from_output")
    assert entry.endpoints_needed({"endpoint": "assets", "path": "$.data[*].id"}) == ["assets"]


def test_a_provider_without_the_hook_declares_nothing():
    """The DAG builder asks everyone; only chaining providers answer."""
    assert registry.get("literal").depends_on is None
    assert registry.get("literal").endpoints_needed({"values": []}) == []


def test_signature_error_catches_a_typo_before_depends_on_runs():
    entry = registry.get("from_output")
    assert entry.signature_error({"endpoint": "assets", "path": "$.x"}) is None

    # `endpoints:` for `endpoint:` reads as the required one missing, which is the more
    # useful half of the message anyway — depends_on would have KeyError'd on it.
    typo = entry.signature_error({"endpoints": "assets", "path": "$.x"})
    assert typo is not None and "'endpoint'" in typo

    assert "'path'" in str(entry.signature_error({"endpoint": "assets"}))
    extra = entry.signature_error({"endpoint": "a", "path": "$.x", "sheet": "S"})
    assert extra is not None and "unexpected keyword argument 'sheet'" in extra


# --- literal ------------------------------------------------------------------------


def test_literal_returns_its_rows():
    rows = [{"region": "EU", "tier": "gold"}, {"region": "US", "tier": "silver"}]
    assert literal(CTX, values=rows) == rows


def test_literal_rejects_bare_scalars():
    with pytest.raises(ValueError, match=r"rows \[0, 2\] are not"):
        literal(CTX, values=["EU", {"region": "US"}, "APAC"])


# --- from_output --------------------------------------------------------------------


def test_from_output_with_nothing_on_disk_yields_nothing():
    """The default context has no outputs — see test_chaining.py for the real behaviour."""
    assert from_output(CTX, endpoint="assets", path="$.data[*].id.id") == []
