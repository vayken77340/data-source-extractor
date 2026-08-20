from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from api_extractor import providers
from api_extractor.logs import forget_secrets

REPO_ROOT = Path(__file__).resolve().parents[1]

# The one complete source the suite runs end to end. Only this line names the file; the
# name *inside* it is read from the parsed config, so renaming either is a one-line change.
REFERENCE_SOURCE = REPO_ROOT / "config" / "sources" / "test.yaml"
REFERENCE_NAME = REFERENCE_SOURCE.stem

# The CLI does this at startup; tests that call the layers directly need it too, or
# `excel_column` would be unregistered and the reference source would not validate.
providers.load_from(REPO_ROOT / "providers")

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
def write_source(tmp_path: Path):
    """Write a source dict to a YAML file and return its path."""

    def _write(data: dict, name: str = "demo") -> Path:
        path = tmp_path / f"{name}.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    return _write
