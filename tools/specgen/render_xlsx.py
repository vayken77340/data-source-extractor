"""Project the model into the companion workbook with openpyxl.

Sheet names, column headers and every cell label come from the label file; the sheet list
comes from the model (`appendix.workbook.tabs`), which the .docx also renders in its
annex — so the two cannot disagree. One sheet per endpoint, named after the endpoint,
holds the structure of its response as observed. Nothing is computed here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from specgen import labels
from specgen.labels import L

HEADER_FILL = PatternFill("solid", fgColor="EDEDED")
WIDTH_MAX = 70


def render(model: Mapping[str, Any], out: Path) -> Path:
    workbook = Workbook()
    workbook.remove(workbook.active)
    by_name = {endpoint["name"]: endpoint for endpoint in model["endpoints"]}
    for tab in model["appendix"]["workbook"]["tabs"]:
        sheet = workbook.create_sheet(_sheet_title(tab["name"]))
        key = tab["key"]
        if key == "readme":
            _readme(sheet, model)
        elif key == "endpoints":
            _endpoints(sheet, model)
        elif key == "metadata":
            _metadata(sheet, model)
        elif key.startswith("response:"):
            _response(sheet, by_name[key.split(":", 1)[1]])
    out.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(out)
    return out


def _sheet_title(name: str) -> str:
    """Excel caps a sheet name at 31 characters and forbids a few of them."""
    cleaned = "".join("_" if c in '[]:*?/\\' else c for c in name)
    return cleaned[:31]


def _table(sheet, headers: Sequence[str], rows: Sequence[Sequence[Any]], start_row: int = 1) -> None:
    for column, header in enumerate(headers, start=1):
        cell = sheet.cell(row=start_row, column=column, value=header)
        cell.font = Font(bold=True)
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="top", wrap_text=True)
    for offset, row in enumerate(rows, start=1):
        for column, value in enumerate(row, start=1):
            cell = sheet.cell(row=start_row + offset, column=column, value=value)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    sheet.freeze_panes = sheet.cell(row=start_row + 1, column=1)
    for column in range(1, len(headers) + 1):
        longest = max(
            (len(str(sheet.cell(row=r, column=column).value or "")) for r in range(start_row, start_row + len(rows) + 1)),
            default=10,
        )
        current = sheet.column_dimensions[get_column_letter(column)].width or 0
        sheet.column_dimensions[get_column_letter(column)].width = max(current, min(max(12, longest + 2), WIDTH_MAX))


def _readme(sheet, model: Mapping[str, Any]) -> None:
    document = model["document"]
    done = model["completeness"]
    rows = L.section("workbook.readme.rows")
    intro = [
        (rows["document"], document["title"]),
        (rows["source_system"], document["source_system"]),
        (rows["version"], document["version_text"]),
        (rows["date"], document["date"]),
        (rows["generated"], labels.fr_date(model["generated_at"])),
        (
            rows["progress"],
            str(rows["progress_value"]).format(
                percent=labels.fr_decimal(done["percent"]),
                count=labels.plural(done["todo"], str(rows["progress_unit"])),
            ),
        ),
        (rows["convention"], rows["convention_value"]),
    ]
    _table(sheet, list(L["workbook.readme.columns"]), intro)
    start = len(intro) + 3
    sheet.cell(row=start, column=1, value=str(L["workbook.readme.tabs_title"])).font = Font(bold=True)
    tabs = [(tab["name"], tab["contents"], tab["reader"]) for tab in model["appendix"]["workbook"]["tabs"]]
    _table(sheet, list(L["workbook.readme.tabs_columns"]), tabs, start_row=start + 1)


def _endpoints(sheet, model: Mapping[str, Any]) -> None:
    columns = L.section("workbook.endpoints.columns")
    rows = []
    for endpoint in model["endpoints"]:
        pagination = "; ".join(
            L.fmt("workbook.endpoints.pagination_cell", item=row["item"], value=row["value"])
            for row in (endpoint["pagination"] or [])
        )
        params = "; ".join(
            L.fmt("workbook.endpoints.param_cell", name=p["name"], location=p["location"], origin=p["origin"])
            for p in endpoint["params"]
        )
        rows.append(
            {
                "name": endpoint["name"],
                "method": endpoint["method"],
                "path": endpoint["path"],
                "purpose": _summary_value(endpoint, L["endpoint.summary.purpose"]),
                "depends_on": ", ".join(endpoint["depends_on"]),
                "params": params,
                "pagination": pagination,
                "files": endpoint["files"],
                "mode": endpoint["mode"],
                "grain": _summary_value(endpoint, L["endpoint.summary.grain"]),
                "key": endpoint["rendered_key"],
            }
        )
    # The label file decides which columns exist and in what order.
    _table(sheet, list(columns.values()), [[row[key] for key in columns] for row in rows])


def _summary_value(endpoint: Mapping[str, Any], item: str) -> str:
    return next((row["value"] for row in endpoint["summary"] if row["item"] == item), "")


def _metadata(sheet, model: Mapping[str, Any]) -> None:
    rows = [(a["attribute"], a["type"], a["mandatory"], a["description"]) for a in model["landing"]["contract"]]
    _table(sheet, list(L["workbook.metadata.columns"]), rows)


def _response(sheet, endpoint: Mapping[str, Any]) -> None:
    columns = L.section("workbook.response.columns")
    fields = endpoint["response_fields"]
    if not fields:
        _table(sheet, list(columns.values()), [])
        sheet.cell(row=2, column=1, value=str(L["workbook.response.empty"])).font = Font(italic=True)
        return
    rows = [
        {
            "path": row["path"],
            "type": row["type"],
            "nullable": labels.yes_no(row["nullable"]),
            "presence": row["presence"],
            "example": row["example"],
        }
        for row in fields
    ]
    _table(sheet, list(columns.values()), [[row[key] for key in columns] for row in rows])
    if "presence" in columns:
        column = list(columns).index("presence") + 1
        for r in range(2, len(rows) + 2):
            sheet.cell(row=r, column=column).number_format = "0 %"
