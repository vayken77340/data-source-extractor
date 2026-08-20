"""The built-in providers.

Only what is generic to the framework lives here: values written inline, and reading back
what a previous endpoint wrote. Anything that knows about a particular file format or a
particular business belongs in `providers/` at the repo root.

YAML supplies args, never code: no eval, no dotted import paths, no expression strings.
"""

from __future__ import annotations

import re
from typing import Any

from jsonpath_ng.ext import parse as parse_jsonpath

from api_extractor.providers.registry import ProviderContext, provider

# The runner strips every `__key__` before a row becomes request params.
PARENTS = "__parents__"


@provider("literal")
def literal(ctx: ProviderContext, *, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows written inline in YAML."""
    scalars = [i for i, row in enumerate(values) if not isinstance(row, dict)]
    if scalars:
        raise ValueError(
            f"literal.values must be a list of mappings, but rows {scalars} are not — "
            f"write `- {{name: value}}`, not a bare scalar. A provider yields rows so that "
            f"several fields off one row stay correlated."
        )
    return [dict(row) for row in values]


@provider("from_output", depends_on=lambda args: [args["endpoint"]])
def from_output(ctx: ProviderContext, *, endpoint: str, path: str) -> list[dict[str, Any]]:
    """Read a JSONPath out of a prior endpoint's saved envelopes.

    This is what makes chaining work, and it is deliberately just another provider — the
    runner has no special case for it. It reads whatever is already on disk, so
    `run <source> --endpoint measures` works against yesterday's asset files without
    re-hitting the endpoint that produced them.

    A value that surfaced in two envelopes is one row with two parents, not two rows: an
    asset can appear under two asset types, and both paths are worth keeping.
    """
    expression = parse_jsonpath(path)
    field = field_name(path)
    rows: dict[str, dict[str, Any]] = {}
    for saved in ctx.outputs_for(endpoint):
        for match in expression.find(saved.body):
            if match.value is None:
                continue
            key = f"{type(match.value).__name__}:{match.value}"
            row = rows.setdefault(key, {field: match.value, PARENTS: []})
            parent = str(saved.path)
            if parent not in row[PARENTS]:
                row[PARENTS].append(parent)
    return list(rows.values())


def field_name(json_path: str) -> str:
    """Name the row's field after the last identifier in the path.

    `$.data[*].id.id` yields rows of `{"id": ...}`, so `bind: {id: {from: ...}}` lines up
    by name as well as by the single-field rule.
    """
    identifiers = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", json_path)
    return identifiers[-1] if identifiers else "value"
