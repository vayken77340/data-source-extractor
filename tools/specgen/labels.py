"""The label catalogue: every reader-facing string the generator computes, loaded from
`config/specs/LABELS.yaml`.

Nothing a reader sees is written in code. The YAML holds the French phrases, the enum
labels, the Iceberg type names and the workbook layout; this module only reads it, checks
that what the code needs is there, and formats numbers and dates the French way. Prose
that never varies lives in the Word template, and facts about one API in its annotation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from types import NoneType
from typing import Any

import yaml

LABELS_PATH = Path("config/specs/LABELS.yaml")

# French typography: a narrow no-break space groups thousands, a comma is the decimal mark.
THIN_SPACE = " "

# The JSON type of a Python value, as the `types.json` mapping names them.
JSON_TYPES = {
    bool: "boolean",
    int: "integer",
    float: "number",
    str: "string",
    dict: "object",
    list: "array",
    NoneType: "null",
}


class Labels:
    """Dotted lookup into the YAML, failing loudly with the file that is missing a key."""

    def __init__(self, data: Mapping[str, Any], path: Path) -> None:
        self._data = data
        self.path = path

    def __getitem__(self, key: str) -> Any:
        node: Any = self._data
        for part in key.split("."):
            if not isinstance(node, Mapping) or part not in node:
                raise KeyError(f"{key!r} is not defined in {self.path}")
            node = node[part]
        return node

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def fmt(self, key: str, **values: Any) -> str:
        return str(self[key]).format(**values)

    def section(self, key: str) -> dict[str, Any]:
        node = self[key]
        if not isinstance(node, Mapping):
            raise KeyError(f"{key!r} in {self.path} is not a mapping")
        return dict(node)


def load(path: Path = LABELS_PATH) -> Labels:
    if not path.is_file():
        raise FileNotFoundError(f"{path} is missing — the generator's labels live there")
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, Mapping):
        raise ValueError(f"{path} must hold a mapping at the top level")
    return Labels(data, path)


L = load()

# The one marker for "this document must carry it and nobody has written it yet". An
# annotation may qualify it — `[À COMPLÉTER — format des horodatages]` — so a marker is
# recognised by its opening, not by the whole literal.
TODO: str = L["marker"]
TODO_MARK: str = TODO.rstrip("]")


def fr_int(value: int) -> str:
    """`1234567` -> `1 234 567`, with the narrow no-break space French typography wants."""
    return f"{value:,}".replace(",", THIN_SPACE)


def fr_decimal(value: float, places: int = 1) -> str:
    whole, _, fraction = f"{value:,.{places}f}".partition(".")
    return f"{whole.replace(',', THIN_SPACE)},{fraction}"


def fr_date(value: str | date | datetime) -> str:
    """ISO (`2026-09-04` or a full timestamp) -> `04/09/2026`. `JJ/MM/AAAA` passes through."""
    if isinstance(value, datetime | date):
        return value.strftime("%d/%m/%Y")
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", value):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d/%m/%Y")


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    """`7, "valeur"` -> `7 valeurs`; French pluralises from two, so 0 and 1 stay singular."""
    word = singular if count < 2 else (plural_form or f"{singular}s")
    return f"{fr_int(count)} {word}"


def yes_no(value: bool) -> str:
    return L["enums.yes"] if value else L["enums.no"]


def json_type(value: Any) -> str:
    """The JSON type of a value: `boolean`, `integer`, `number`, `string`, `object`,
    `array` or `null`. Anything unexpected reads as an object."""
    return JSON_TYPES.get(type(value), "object")


def iceberg(value: Any) -> str:
    """The Iceberg type name for a JSON value, as `types.json` maps it."""
    return str(L[f"types.json.{json_type(value)}"])


def is_todo(value: Any) -> bool:
    return isinstance(value, str) and TODO_MARK in value
