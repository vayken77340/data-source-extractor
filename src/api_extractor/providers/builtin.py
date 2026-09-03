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
def from_output(
    ctx: ProviderContext,
    *,
    endpoint: str,
    path: str,
    fields: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Read a JSONPath out of a prior endpoint's saved envelopes.

    This is what makes chaining work, and it is deliberately just another provider — the
    runner has no special case for it. It reads whatever is already on disk, so
    `run <source> --endpoint measures` works against yesterday's asset files without
    re-hitting the endpoint that produced them.

    `path` selects nodes. `fields` says how to turn each node into a row, mapping a field
    name to a JSONPath *relative to that node*; left out, the node itself is the value and
    is named after the last identifier in `path`.

        path: "$.data[*].id"
        fields: { id: "$.id", entityType: "$.entityType" }

    `fields` is how several values off one object stay correlated. Two providers reading
    the same object separately would be crossed or zipped instead, and a node missing any
    requested field is dropped rather than emitted half-filled.

    A row that surfaced in two envelopes is one row with two parents, not two rows: an
    asset can appear under two asset types, and both paths are worth keeping.
    """
    expression = parse_jsonpath(path)
    selectors = {name: parse_jsonpath(sub) for name, sub in (fields or {}).items()}
    names = list(selectors) or [field_name(path)]

    rows: dict[tuple[str, ...], dict[str, Any]] = {}
    for saved in ctx.outputs_for(endpoint):
        for match in expression.find(saved.body):
            extracted = extract(match.value, selectors, names)
            if extracted is None:
                continue
            # Type-tagged, so the integer 1 and the string "1" stay two rows.
            identity = tuple(f"{type(value).__name__}:{value}" for value in extracted.values())
            row = rows.setdefault(identity, {**extracted, PARENTS: []})
            parent = str(saved.path)
            if parent not in row[PARENTS]:
                row[PARENTS].append(parent)
    return list(rows.values())


def extract(node: Any, selectors: dict[str, Any], names: list[str]) -> dict[str, Any] | None:
    """One node's fields, or None when the node — or any requested field — is missing.

    A half-filled row would break the correlation `fields` exists to keep, and a blank
    parameter is not worth a request.
    """
    if not selectors:
        return None if node is None else {names[0]: node}

    out: dict[str, Any] = {}
    for name, selector in selectors.items():
        found = selector.find(node)
        if not found or found[0].value is None:
            return None
        out[name] = found[0].value
    return out


def field_name(json_path: str) -> str:
    """Name the row's field after the last identifier in the path.

    `$.data[*].id.id` yields rows of `{"id": ...}`, so `bind: {id: {from: ...}}` lines up
    by name as well as by the single-field rule.
    """
    identifiers = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", json_path)
    return identifiers[-1] if identifiers else "value"
