"""Project the model into the companion workbook with openpyxl.

Tab names come from the model (`appendix.workbook.tabs`), which the .docx also renders in
Annexe C — so the two cannot disagree. Every number that is a number in the model is a
number in the cell; text stays text. Nothing is computed here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from specgen import labels
from specgen.model import variables

HEADER_FILL = PatternFill("solid", fgColor="EDEDED")
WIDTH_MAX = 70


def render(model: Mapping[str, Any], out: Path) -> Path:
    workbook = Workbook()
    workbook.remove(workbook.active)
    builders = {
        "readme": _readme,
        "endpoints": _endpoints,
        "fields": _fields,
        "metadata": _metadata,
        "volumes": _volumes,
    }
    for tab in model["appendix"]["workbook"]["tabs"]:
        sheet = workbook.create_sheet(tab["name"])
        builders[tab["key"]](sheet, model)
    out.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(out)
    return out


def _table(sheet, headers: Sequence[str], rows: Sequence[Sequence[Any]], start_row: int = 1) -> None:
    sheet.append([])  # openpyxl appends after the last used row; keep our own cursor instead
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
        sheet.column_dimensions[get_column_letter(column)].width = min(max(12, longest + 2), WIDTH_MAX)


def _readme(sheet, model: Mapping[str, Any]) -> None:
    document = model["document"]
    done = model["completeness"]
    intro = [
        ("Document", document["title"]),
        ("Système source", document["source_system"]),
        ("Version", document["version_text"]),
        ("Date", document["date"]),
        ("Généré le", labels.fr_date(model["generated_at"])),
        ("Avancement", f"{labels.fr_decimal(done['percent'])} % — {labels.plural(done['todo'], 'mention')} « {labels.TODO} » restante(s)"),
        ("Convention", "Une ligne vide ou absente signifie « sans objet », jamais « inconnu »."),
    ]
    _table(sheet, ["Élément", "Valeur"], intro)
    start = len(intro) + 3
    sheet.cell(row=start, column=1, value="Onglets").font = Font(bold=True)
    tabs = [(tab["name"], tab["contents"], tab["reader"]) for tab in model["appendix"]["workbook"]["tabs"]]
    _table(sheet, ["Onglet", "Contenu", "Lecteur principal"], tabs, start_row=start + 1)
    start += len(tabs) + 3
    sheet.cell(row=start, column=1, value="Vocabulaire du modèle (pour l'édition du gabarit Word)").font = Font(bold=True)
    _table(sheet, ["Chemin", "Exemple"], variables(model), start_row=start + 1)


def _endpoints(sheet, model: Mapping[str, Any]) -> None:
    rows = []
    for endpoint in model["endpoints"]:
        pagination = "; ".join(f"{row['item']} : {row['value']}" for row in (endpoint["pagination"] or []))
        rows.append(
            (
                endpoint["number"],
                endpoint["name"],
                endpoint["method"],
                endpoint["path"],
                _summary_value(endpoint, "Objet"),
                _summary_value(endpoint, "Appelé"),
                ", ".join(endpoint["depends_on"]),
                "; ".join(f"{p['name']} ({p['location']}) ← {p['origin']}" for p in endpoint["params"]),
                pagination,
                endpoint["files"],
                endpoint["mode"],
                _summary_value(endpoint, "Grain d'enregistrement"),
                endpoint["rendered_key"],
            )
        )
    _table(
        sheet,
        ["N°", "Endpoint", "Méthode", "Chemin", "Objet", "Appelé", "Dépend de", "Paramètres", "Pagination", "Fichiers", "Mode", "Grain", "Clé de dépôt (exemple)"],
        rows,
    )


def _summary_value(endpoint: Mapping[str, Any], item: str) -> str:
    return next((row["value"] for row in endpoint["summary"] if row["item"] == item), "")


def _fields(sheet, model: Mapping[str, Any]) -> None:
    rows = [
        (name, row["path"], row["types"], row["presence"], row["example"])
        for name, inventory in model["evidence"]["fields"].items()
        for row in inventory
    ]
    _table(sheet, ["Endpoint", "Chemin JSON", "Types observés", "Présence", "Exemple"], rows)
    for r in range(2, len(rows) + 2):
        sheet.cell(row=r, column=4).number_format = "0 %"


def _metadata(sheet, model: Mapping[str, Any]) -> None:
    rows = [(a["attribute"], a["type"], a["mandatory"], a["description"]) for a in model["landing"]["contract"]]
    _table(sheet, ["Attribut", "Type", "Obligatoire", "Description"], rows)


def _volumes(sheet, model: Mapping[str, Any]) -> None:
    rows = []
    for endpoint in model["endpoints"]:
        volume = endpoint["volume"]
        measured = volume["measured"] or {}
        rows.append(
            (
                endpoint["name"],
                volume["text"],
                volume["planned"],
                measured.get("requests"),
                measured.get("written"),
                measured.get("pages_max"),
                volume["records_per_page"],
            )
        )
    _table(
        sheet,
        ["Endpoint", "Requêtes par exécution", "Planifié", "Mesuré : requêtes", "Mesuré : fichiers", "Mesuré : pages max", "Enregistrements par page (observé)"],
        rows,
    )
