"""Join saved output against a lookup table, on the param that produced it.

Nothing here knows about assets or measures. The shape it captures is "what I need for this
request depends on which request produced the last one": read values out of an endpoint's
envelopes, and attach whatever a parameter file associates with the param that endpoint was
fetched with.

It is one provider rather than two because two would be crossed or zipped. A second
provider supplying the looked-up half would have to walk the same envelopes anyway, just to
know how many rows to emit and in what order — so it would cost the same work and add a
positional coupling that breaks silently the first time the two disagree.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from jsonpath_ng.ext import parse as parse_jsonpath

from api_extractor.providers import ProviderContext, provider

PARENTS = "__parents__"


@provider("from_output_joined", depends_on=lambda args: [args["endpoint"]])
def from_output_joined(
    ctx: ProviderContext,
    *,
    endpoint: str,
    path: str,
    file: str,
    join_on: str,
    select: str,
    separator: str = ",",
) -> list[dict[str, Any]]:
    """`from_output`, plus what a parameter file associates with each value's origin.

    `path` is a JSONPath into the envelopes of `endpoint`, exactly as `from_output` takes
    one — the API's shape is not yours to control, so a selector is the only option there.
    `file`, `join_on` and `select` name a lookup instead of describing one: `join_on` is a
    column in the file *and* the param recorded on the envelope, which is the join key;
    `select` is the column whose values are attached, joined with `separator`.

    Rows carry two fields — the extracted value, named after the last identifier in `path`
    the way `from_output` names its own, and the selected column under its own name. Two
    markers naming this provider are therefore filled from one row and stay correlated.

    A value that surfaced under two different keys keeps the first and records both
    parents, matching what `from_output` does with a value seen twice.
    """
    value_field = field_name(path)
    if value_field == select:
        raise ValueError(
            f"from_output_joined: `path` is named after {value_field!r} and `select` is "
            f"{select!r} — one row cannot carry two fields of the same name"
        )

    lookup = group(Path(file), join_on, select)
    expression = parse_jsonpath(path)
    rows: dict[str, dict[str, Any]] = {}
    for saved in ctx.outputs_for(endpoint):
        metadata = saved.envelope.get("metadata") or {}
        key = (metadata.get("params") or {}).get(join_on)
        selected = lookup.get(key)
        if not selected:
            continue  # nothing associated with this key: no request worth making
        for match in expression.find(saved.body):
            if match.value is None:
                continue
            row = rows.setdefault(
                str(match.value),
                {
                    value_field: match.value,
                    select: separator.join(str(item) for item in selected),
                    PARENTS: [],
                },
            )
            parent = str(saved.path)
            if parent not in row[PARENTS]:
                row[PARENTS].append(parent)
    return list(rows.values())


def field_name(json_path: str) -> str:
    """Name the row's field after the last identifier in the path.

    The same rule `from_output` uses, so `$.data[*].id.id` yields `id` and lines up with
    `bind: {id: ...}` by name. Duplicated rather than imported to keep this file to the
    public provider API.
    """
    identifiers = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", json_path)
    return identifiers[-1] if identifiers else "value"


def group(file: Path, join_on: str, select: str) -> dict[Any, list[Any]]:
    """Build `join_on -> [select]` out of a parameter file's rows, order preserved.

    Grouping every run is what lets the file stay flat rows that any `param_file` provider
    can also read. Keying it on disk would buy a lookup that costs nothing and cost a
    bespoke file format per dataset.
    """
    document = load(file)
    known = document.get("columns")
    missing = [column for column in (join_on, select) if known and column not in known]
    if missing:
        raise ValueError(
            f"from_output_joined: {file} has no column(s) {missing} "
            f"(columns: {', '.join(map(str, known))})"
        )

    grouped: dict[Any, list[Any]] = defaultdict(list)
    for row in document["rows"]:
        key, value = row.get(join_on), row.get(select)
        if key is None or value is None:
            continue
        if value not in grouped[key]:
            grouped[key].append(value)
    return dict(grouped)


def load(file: Path) -> dict[str, Any]:
    """See the note in `param_file.py`: files here cannot import each other, so this small
    reader is duplicated rather than shared."""
    if not file.is_file():
        raise FileNotFoundError(
            f"from_output_joined: no such parameter file: {file} — "
            f"generate it with tools/build_params.py"
        )
    text = file.read_text(encoding="utf-8")
    document = yaml.safe_load(text) if file.suffix in {".yaml", ".yml"} else json.loads(text)
    if not isinstance(document, dict) or not isinstance(document.get("rows"), list):
        raise ValueError(
            f"from_output_joined: {file} has no `rows` list — is it a parameter file?"
        )
    return document
