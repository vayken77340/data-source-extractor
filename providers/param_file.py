"""Read rows out of a parameter file — a spreadsheet with the spreadsheet taken out.

Generic on purpose. `tools/build_params.py` normalizes every business file into one shape,
so this one provider serves every reference dataset you will ever have, whatever columns it
happens to carry. A new spreadsheet means a new invocation of the build script, not a new
provider.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from api_extractor.providers import ProviderContext, provider


@provider("param_file")
def param_file(ctx: ProviderContext, *, path: str, columns: list[str]) -> list[dict[str, Any]]:
    """One or more columns off a parameter file, keyed by column name.

    The same semantics as `excel_column`: several columns stay row-wise so fields off one
    row remain correlated, a row is dropped when any requested cell is blank, duplicates
    collapse first-wins, and order is otherwise preserved.
    """
    document = load(Path(path))
    rows = document["rows"]
    known = document.get("columns") or sorted({key for row in rows for key in row})
    missing = [column for column in columns if column not in known]
    if missing:
        raise ValueError(
            f"param_file: {path} has no column(s) {missing} (columns: {', '.join(map(str, known))})"
        )

    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        cells = tuple(row.get(column) for column in columns)
        if any(cell is None for cell in cells) or cells in seen:
            continue
        seen.add(cells)
        out.append(dict(zip(columns, cells, strict=True)))
    return out


def load(file: Path) -> dict[str, Any]:
    """A parameter file is provenance plus `rows`. JSON or YAML — the same shape either way.

    YAML is here for the small tables a human maintains by hand rather than exports; it is a
    second *encoding*, never a second structure. Anything that does not come out row-shaped
    belongs in the build step, not in an argument to this function.

    Duplicated rather than shared with `measures_for_assets.py` because files in this
    directory are loaded by path and cannot import each other — `providers/` is never on
    `sys.path`. Eight lines is a cheaper price than a catch-all module.
    """
    if not file.is_file():
        raise FileNotFoundError(
            f"param_file: no such parameter file: {file} — generate it with tools/build_params.py"
        )
    text = file.read_text(encoding="utf-8")
    document = yaml.safe_load(text) if file.suffix in {".yaml", ".yml"} else json.loads(text)
    if not isinstance(document, dict) or not isinstance(document.get("rows"), list):
        raise ValueError(f"param_file: {file} has no `rows` list — is it a parameter file?")
    return document
