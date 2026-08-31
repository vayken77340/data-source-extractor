"""Join fetched assets against the measure types their asset type actually has.

The correlation is the business rule, so it lives in Python here rather than in YAML. It
cannot be expressed with `fan_out`: crossing asset ids with measure types would ask a valve
for its temperature, and the API answers that with an empty 200 rather than an error — a
wrong request that looks exactly like a right one.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from jsonpath_ng.ext import parse as parse_jsonpath

from api_extractor.providers import ProviderContext, provider

PARENTS = "__parents__"


@provider("measure_keys_for_assets", depends_on=lambda args: [args["endpoint"]])
def measure_keys_for_assets(
    ctx: ProviderContext,
    *,
    path: str,
    endpoint: str,
    type_column: str = "assetType",
    measure_column: str = "measureType",
    id_path: str = "$.data[*].id.id",
    separator: str = ",",
) -> list[dict[str, Any]]:
    """One row per asset: its id, and the measure keys its own asset type calls for.

    The asset type is the join key, and it survives the round trip through the API in
    `metadata.params` — the params the request was planned with, recorded on every envelope
    — so nothing has to be parsed back out of a filename. `type_column` therefore names two
    things that must agree: the column in the parameter file, and the param the assets
    endpoint fans out over.

    Both fields come off one row, so `bind: {id: {from: …}}` and `query: {keys: {from: …}}`
    pointing at this provider stay correlated instead of being crossed.

    An asset that surfaced under two types keeps the first type's keys and records both
    parents — the same first-wins rule `from_output` uses for a value seen twice.
    """
    measures = measures_by_type(Path(path), type_column, measure_column)

    expression = parse_jsonpath(id_path)
    rows: dict[str, dict[str, Any]] = {}
    for saved in ctx.outputs_for(endpoint):
        metadata = saved.envelope.get("metadata") or {}
        asset_type = (metadata.get("params") or {}).get(type_column)
        keys = measures.get(asset_type)
        if not keys:
            continue  # a type the sheet gives no measures: nothing worth requesting
        for match in expression.find(saved.body):
            if match.value is None:
                continue
            row = rows.setdefault(
                str(match.value),
                {
                    "id": match.value,
                    "keys": separator.join(str(key) for key in keys),
                    PARENTS: [],
                },
            )
            parent = str(saved.path)
            if parent not in row[PARENTS]:
                row[PARENTS].append(parent)
    return list(rows.values())


def measures_by_type(file: Path, type_column: str, measure_column: str) -> dict[Any, list[Any]]:
    """Group the parameter file's rows into type -> its measure types, order preserved.

    Grouping every run is the price of the parameter file staying generic, and for a few
    hundred rows it is not a price worth optimizing: keying the file by asset type instead
    would buy nothing and cost a bespoke file format per dataset.
    """
    document = load(file)
    for column in (type_column, measure_column):
        known = document.get("columns")
        if known and column not in known:
            raise ValueError(
                f"measure_keys_for_assets: {file} has no column {column!r} "
                f"(columns: {', '.join(map(str, known))})"
            )

    grouped: dict[Any, list[Any]] = defaultdict(list)
    for row in document["rows"]:
        asset_type, measure = row.get(type_column), row.get(measure_column)
        if asset_type is None or measure is None:
            continue
        if measure not in grouped[asset_type]:
            grouped[asset_type].append(measure)
    return dict(grouped)


def load(file: Path) -> dict[str, Any]:
    """See the note in `param_file.py`: files here cannot import each other, so this small
    reader is duplicated rather than shared."""
    if not file.is_file():
        raise FileNotFoundError(
            f"measure_keys_for_assets: no such parameter file: {file} — "
            f"generate it with tools/build_params.py"
        )
    text = file.read_text(encoding="utf-8")
    document = yaml.safe_load(text) if file.suffix in {".yaml", ".yml"} else json.loads(text)
    if not isinstance(document, dict) or not isinstance(document.get("rows"), list):
        raise ValueError(
            f"measure_keys_for_assets: {file} has no `rows` list — is it a parameter file?"
        )
    return document
