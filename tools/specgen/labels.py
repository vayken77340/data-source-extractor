"""The French label catalogue: every reader-facing string the generator *computes*.

Prose that never varies lives in the Word template, where a French speaker owns it. What
is here is the vocabulary for values the generator derives — enums, booleans, plurals,
dates, numbers — so that the document, the workbook and the validation messages agree on
one spelling. Keys are English identifiers; values are document content.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from types import NoneType
from typing import Any

# The one marker for "this document must carry it and nobody has written it yet". It is
# accepted as the value of any annotation field, counted by `--check`, and never printed
# on a console (Windows consoles default to a legacy code page).
TODO = "[À COMPLÉTER]"
# An annotation may qualify the marker — `[À COMPLÉTER — format des horodatages]` — so a
# marker is recognised by its opening, not by the whole literal.
TODO_MARK = "[À COMPLÉTER"

# French typography: a narrow no-break space groups thousands, a comma is the decimal mark.
THIN_SPACE = " "

STATUS = {"draft": "brouillon", "in_review": "en revue", "approved": "approuvé"}

MODE = {"full": "complet", "incremental": "incrémental"}

AUTH = {
    "basic": "HTTP Basic — Authorization: Basic base64(identifiant:secret)",
    "bearer": "Jeton porteur — Authorization: Bearer <jeton>",
    "header": "En-tête(s) statique(s) de créance",
    "oauth_client_credentials": "OAuth2 client_credentials — jeton obtenu auprès du serveur d'autorisation",
    "login_token": "Échange identifiant/mot de passe contre un jeton",
}

# Where a parameter lands in the request.
LOCATION = {
    "path": "chemin",
    "query": "chaîne de requête",
    "payload": "corps de la requête",
    "label": "non transmis (sert au nommage du fichier)",
}

# Where the page cursor lands; same words as LOCATION, kept apart because the two enums
# are checked independently against the models.
CURSOR_ROOT = {"query": "chaîne de requête", "payload": "corps de la requête"}

# JSON value types, for the request table and the field inventory.
TYPE = {
    int: "entier",
    str: "chaîne",
    bool: "booléen",
    float: "décimal",
    dict: "objet",
    list: "liste",
    NoneType: "nul",
}

# Contract attribute types and mandatory-ness (see contract.py).
CONTRACT_TYPE = {
    "string": "chaîne",
    "object": "objet",
    "integer": "entier",
    "list": "liste",
    "timestamp": "horodatage",
    "any": "JSON",
}
MANDATORY = {
    "yes": "Oui",
    "post_only": "POST seulement",
    "non_json": "Réponse non JSON seulement",
}

# Workbook tabs. The .docx names them in Annexe C, so both read this one place.
TABS = {
    "readme": "Lisez-moi",
    "endpoints": "Inventaire des endpoints",
    "fields": "Inventaire des champs",
    "metadata": "Métadonnées de dépôt",
    "volumes": "Volumétrie",
}
TAB_CONTENTS = {
    "readme": "Légende, conventions, avancement, vocabulaire du modèle",
    "endpoints": "Une ligne par endpoint : mécanique de requête, pagination, volumes",
    "fields": "Une ligne par champ et par endpoint : chemin JSON, types observés, présence, exemple",
    "metadata": "Spécification normative des attributs de métadonnées",
    "volumes": "Requêtes et fichiers par exécution, planifiés et mesurés",
}
TAB_READERS = {
    "readme": "Tous",
    "endpoints": "Ingénieurs d'ingestion",
    "fields": "Équipe de modélisation (référence, pas un mapping)",
    "metadata": "Ingénieurs d'ingestion",
    "volumes": "Ingénieurs d'ingestion, responsables de plateforme",
}

FILES_PER = {"page": "un fichier par page", "request": "un fichier par requête"}

# Sentences the generator assembles from parts. Kept here rather than inline so that a
# wording change is one edit and the tests can pin the pieces.
CALLED_ONCE = "Une fois par exécution"
CALLED_PER = "Une fois par {origins}"
CALLED_COUNT = "{count} par exécution — une par {origins}"
STATIC_LIST = "{fields} du référentiel"
STATIC_LIST_COUNT = "{fields} du référentiel ({count})"
NAMED_LIST_COUNT = "{name} ({count})"
CHAINED_LIST = "enregistrement retourné par {method} {path}"
FIXED_VALUE = "valeur fixe : {value}"
CORRELATED = (
    "Les paramètres issus de « {origin} » proviennent d'un même enregistrement et restent "
    "solidaires. Ils ne doivent jamais être recombinés entre eux : un produit cartésien "
    "produirait des requêtes dépourvues de sens."
)
PAGINATION_SUMMARY = (
    "Sur les {total} de cette source, {paged} pagine{plural} ({names}) ; les autres "
    "répondent en une seule fois."
)
PAGINATION_NONE = "Aucun des {total} de cette source ne pagine : chacun répond en une seule fois."
STOP_EMPTY = "une page revient vide"
STOP_FLAG = "le chemin JSON {path} est faux"
STOP_EITHER = "{empty}, ou {flag}"
VOLUME_UNKNOWN = "N — un par {origin}, à mesurer sur une exécution réelle"
VOLUME_PAGES = " × nombre de pages"
ROOT_OBJECT = "objet (clés : {keys})"
ROOT_LIST = "liste de {count}"
ROOT_SCALAR = "{type}"
SAMPLE_TRUNCATED = "extrait : listes tronquées à {count} par liste"
SAMPLE_FULL = "réponse complète"
ENDPOINT_NAMES = "Nom logique de l'endpoint : {names}."
INITIAL_VERSION = "Version initiale"
PRODUCTION = "Production"
PRODUCTION_PURPOSE = "Extraction en production"


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
    return "Oui" if value else "Non"


def type_of(value: Any) -> str:
    """The French name of a JSON value's type; `objet` for anything unexpected."""
    return TYPE.get(type(value), TYPE[dict])


def is_todo(value: Any) -> bool:
    return isinstance(value, str) and TODO_MARK in value
