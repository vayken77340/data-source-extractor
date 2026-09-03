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
def reference_source(reference_env):
    from api_extractor.config.loader import load_source

    return load_source(REFERENCE_SOURCE)


# The suite's own annotation, for the specification generator under tools/specgen.
REFERENCE_SPEC = FIXTURES / "reference.spec.yaml"


@pytest.fixture
def reference_annotation():
    from specgen.annotation import load_annotation

    return load_annotation(REFERENCE_SPEC)


def build_envelope(
    source,
    endpoint: str,
    params: dict,
    body,
    *,
    status: int = 200,
    page: int = 0,
    parents: tuple[str, ...] = (),
    extracted_at: str = "2026-09-04T10:12:03Z",
) -> dict:
    """An envelope exactly as the runner would write it, without a runner.

    Built by `envelope.build()` so that anything asserting on envelope shape asserts on
    the real thing; the request goes through the planner's templating and the pagination
    cursor, the way a real one does.
    """
    from api_extractor.http import pagination
    from api_extractor.http.client import Request, Response
    from api_extractor.persist import envelope
    from api_extractor.plan import binding

    ep = source.endpoints[endpoint]
    spec = binding.RequestSpec(
        source=source.source,
        endpoint=endpoint,
        method=ep.method,
        path=binding.render(ep.path, params),
        query=binding.fill_markers(ep.query, None, params),
        payload=binding.fill_markers(ep.payload, None, params),
        params=dict(params),
        parents=parents,
        output_template=source.output_template(endpoint),
    )
    request = Request(
        method=spec.method,
        url=f"{source.base_url.rstrip('/')}/{spec.path.lstrip('/')}",
        query=spec.query,
        payload=spec.payload,
        headers={"Accept": "application/json", "Authorization": "Basic secret"},
    )
    if ep.paginate is not None:
        request = pagination.with_cursor(request, ep.paginate, ep.paginate.start + page)
    response = Response(status=status, headers={}, elapsed_ms=1, text="", body=body)
    return envelope.build(
        spec=spec,
        request=request,
        response=response,
        base_url=source.base_url,
        extracted_at=extracted_at,
    )


@pytest.fixture
def write_source(tmp_path: Path):
    """Write a source dict to a YAML file and return its path."""

    def _write(data: dict, name: str = "demo") -> Path:
        path = tmp_path / f"{name}.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    return _write
