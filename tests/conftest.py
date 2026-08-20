"""Shared fixtures.

Nothing here reads `config/sources/`, `input/` or `providers/`. Those belong to whoever is
using the tool, and a suite that depends on them breaks the moment a source is renamed or
replaced with a real one. The tests own their own source, at `tests/fixtures/reference.yaml`.

The two tests that *are* about project-owned files — `config/TEMPLATE.yaml` and
`providers/excel_column.py` — load what they need themselves, so the reason is visible at
the point of coupling.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from api_extractor.logs import forget_secrets

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"

# The suite's own complete source. Owned here, so it changes only when a test needs it to.
REFERENCE_SOURCE = FIXTURES / "reference.yaml"
REFERENCE_NAME = REFERENCE_SOURCE.stem
REFERENCE_BASE_URL = "https://ref.example.com/api"
REFERENCE_ENV = {"REF_USER": "svc-account", "REF_PASSWORD": "hunter2-hunter2"}

MINIMAL: dict = {
    "source": "demo",
    "base_url": "https://demo.example.com",
    "endpoints": {"things": {"method": "GET", "path": "/things"}},
}


@pytest.fixture(autouse=True)
def _clear_secrets():
    forget_secrets()
    yield
    forget_secrets()


@pytest.fixture
def reference_env(monkeypatch):
    """The env vars `tests/fixtures/reference.yaml` refers to."""
    for name, value in REFERENCE_ENV.items():
        monkeypatch.setenv(name, value)
    return REFERENCE_ENV


@pytest.fixture
def write_source(tmp_path: Path):
    """Write a source dict to a YAML file and return its path."""

    def _write(data: dict, name: str = "demo") -> Path:
        path = tmp_path / f"{name}.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    return _write
