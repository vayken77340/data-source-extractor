"""Turn a business spreadsheet into a parameter file.

Run by hand when the business sends a new sheet, then commit the result. This is the only
thing in the repo that knows what a merged cell is — everything downstream reads the one
shape written here, so a second spreadsheet with different columns needs a different
invocation of this script, not a different provider.

    python tools/build_params.py input/asset_types.xlsx \
        --sheet Referentiel --columns assetType,measureType --ffill assetType \
        -o config/params/asset_types.json

Deliberately not in `providers/`: every `.py` there is imported at startup, and a build
script has no business running during `validate` or `list-providers`.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

SCHEMA = "param-file/1"


def clean(cell: Any) -> Any:
    """Blank means blank: whitespace-only strings are nothing."""
    if isinstance(cell, str):
        return cell.strip() or None
    return cell


def read_sheet(workbook_path: Path, sheet: str) -> list[tuple[Any, ...]]:
    if not workbook_path.is_file():
        raise SystemExit(f"no such file: {workbook_path}")
    workbook = load_workbook(workbook_path, read_only=True, data_only=True)
    try:
        if sheet not in workbook.sheetnames:
            raise SystemExit(
                f"{workbook_path} has no sheet {sheet!r} "
                f"(sheets: {', '.join(workbook.sheetnames)})"
            )
        return list(workbook[sheet].iter_rows(values_only=True))
    finally:
        workbook.close()


def extract(
    rows: list[tuple[Any, ...]], columns: list[str], ffill: frozenset[str]
) -> list[dict[str, Any]]:
    """Project `columns`, forward-filling `ffill`, dropping blanks, deduping.

    Forward-fill is how a merged cell survives. In read-only mode openpyxl returns None for
    every cell of a merged range but the top-left, and does not populate `merged_cells` at
    all — so a blank in a forward-filled column means "the same value as the row above",
    which is exactly what the merge means on screen.

    Declared per column rather than inferred, because a blank meaning "missing" and a blank
    meaning "same as above" are indistinguishable from here. Only you know which sheet is
    which.
    """
    if not rows:
        return []

    header = [clean(cell) for cell in rows[0]]
    missing = [column for column in columns if column not in header]
    if missing:
        present = ", ".join(str(cell) for cell in header if cell is not None)
        raise SystemExit(f"no column(s) {missing} in the sheet (headers: {present})")
    indexes = {column: header.index(column) for column in columns}

    carried: dict[str, Any] = {}
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for row in rows[1:]:
        record: dict[str, Any] = {}
        for column, index in indexes.items():
            value = clean(row[index]) if index < len(row) else None
            if column in ffill:
                # `.get`, not `[]`: a sheet whose very first data row is blank here has
                # nothing to carry down, and that row should drop rather than crash.
                value = carried.get(column) if value is None else value
                carried[column] = value
            record[column] = value

        cells = tuple(record[column] for column in columns)
        if any(cell is None for cell in cells) or cells in seen:
            continue
        seen.add(cells)
        out.append(record)
    return out


def build(workbook_path: Path, sheet: str, columns: list[str], rows: list[dict[str, Any]]) -> dict:
    """Provenance plus rows. The provenance is half the point: it is what tells you, six
    weeks later, which spreadsheet a given output tree came from."""
    return {
        "schema": SCHEMA,
        "source_file": workbook_path.as_posix(),
        "sheet": sheet,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "columns": columns,
        "rows": rows,
    }


def write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def summarize(rows: list[dict[str, Any]], columns: list[str]) -> str:
    """Counts, so a sheet that quietly halved is visible without opening the file."""
    counts = ", ".join(
        f"{column} {len({row[column] for row in rows})} distinct" for column in columns
    )
    return f"{len(rows)} rows ({counts})"


def split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workbook", type=Path, help="the spreadsheet the business supplied")
    parser.add_argument("-o", "--out", type=Path, required=True, help="parameter file to write")
    parser.add_argument("--sheet", required=True)
    parser.add_argument("--columns", required=True, help="comma-separated, in output order")
    parser.add_argument(
        "--ffill",
        default="",
        help="comma-separated subset of --columns whose blanks mean 'same as the row "
        "above' — i.e. the merged ones",
    )
    args = parser.parse_args(argv)

    columns = split(args.columns)
    ffill = frozenset(split(args.ffill))
    unknown = sorted(ffill - set(columns))
    if unknown:
        raise SystemExit(f"--ffill names {unknown}, which is not in --columns {columns}")

    rows = extract(read_sheet(args.workbook, args.sheet), columns, ffill)
    if not rows:
        raise SystemExit(
            f"{args.workbook}[{args.sheet}] yielded no rows — wrong sheet or columns?"
        )

    write(args.out, build(args.workbook, args.sheet, columns, rows))
    print(f"{summarize(rows, columns)} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
