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
    fields: dict[str, str] | None = None,
    separator: str = ",",
) -> list[dict[str, Any]]:
    """`from_output`, plus what a parameter file associates with each value's origin.

    `path` is a JSONPath into the envelopes of `endpoint`, exactly as `from_output` takes
    one — the API's shape is not yours to control, so a selector is the only option there.
    `file`, `join_on` and `select` name a lookup instead of describing one: `join_on` is a
    column in the file *and* the param recorded on the envelope, which is the join key;
    `select` is the column whose values are attached, joined with `separator`.

    `path` selects nodes; `fields` says how to turn each node into a row. Left out, the node
    *is* the value and is named after the last identifier in `path`, the way `from_output`
    names its own. Given, it maps a field name to a JSONPath *relative to that node*, so
    several values off one record stay together:

        path: "$.data[*]"
        fields: { id: "$.id.id", assetName: "$.latest.ENTITY_FIELD.name.value" }

    Two independent paths over the whole body would be the obvious shortcut and it is wrong
    for the same reason two providers are: one record missing a name shifts every later
    pairing, silently. A record missing any requested field is dropped instead.

    Every row also carries the join key under `join_on`'s own name, since that value is what
    the whole join turned on and is usually worth putting in an output path.

    A value that surfaced under two different keys keeps the first and records both parents,
    matching what `from_output` does with a value seen twice.
    """
    selectors = {name: parse_jsonpath(sub) for name, sub in (fields or {}).items()}
    names = list(selectors) if selectors else [field_name(path)]
    check_names(names, join_on, select)

    lookup = group(Path(file), join_on, select)
    expression = parse_jsonpath(path)
    rows: dict[tuple[Any, ...], dict[str, Any]] = {}
    for saved in ctx.outputs_for(endpoint):
        metadata = saved.envelope.get("metadata") or {}
        key = (metadata.get("params") or {}).get(join_on)
        selected = lookup.get(key)
        if not selected:
            continue  # nothing associated with this key: no request worth making
        for match in expression.find(saved.body):
            extracted = extract(match.value, selectors, names)
            if extracted is None:
                continue
            identity = tuple(extracted[name] for name in names)
            row = rows.setdefault(
                identity,
                {
                    **extracted,
                    join_on: key,
                    select: separator.join(str(item) for item in selected),
                    PARENTS: [],
                },
            )
            parent = str(saved.path)
            if parent not in row[PARENTS]:
                row[PARENTS].append(parent)
    return list(rows.values())


def extract(node: Any, selectors: dict[str, Any], names: list[str]) -> dict[str, Any] | None:
    """One record's fields, or None when any of them is missing.

    A partial row is worse than no row: it breaks the correlation the whole provider exists
    to keep, and a blank parameter is not worth a request.
    """
    if not selectors:
        return None if node is None else {names[0]: node}

    out: dict[str, Any] = {}
    for name, selector in selectors.items():
        found = selector.find(node)
        value = found[0].value if found else None
        if value is None:
            return None
        out[name] = value
    return out


def check_names(names: list[str], join_on: str, select: str) -> None:
    """Every field lands on one row, so two of a name would silently lose one."""
    everything = [*names, join_on, select]
    clashing = sorted({name for name in everything if everything.count(name) > 1})
    if clashing:
        raise ValueError(
            f"from_output_joined: {clashing} would appear twice on one row — `fields` "
            f"(or the name derived from `path`), `join_on` and `select` must all differ"
        )


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
