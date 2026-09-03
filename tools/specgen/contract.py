"""The landing contract, attribute by attribute — what §6.3 of the document says.

This catalogue is the *only* prose description of the envelope. It is verified against
`envelope.build()` by `tests/test_spec_contract.py`: a key the envelope writes without an
entry here fails, and an entry here that the envelope no longer writes fails. The Word
template renders this catalogue and nothing else, so the document cannot describe a
contract the code does not write — which is the class of drift this project exists to
prevent.

`type` and `mandatory` are keys into `labels.CONTRACT_TYPE` and `labels.MANDATORY`;
`description` is document content.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from specgen import labels

TIMESTAMP_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


@dataclass(frozen=True)
class Attribute:
    path: str
    type: str
    mandatory: str
    description: str


ATTRIBUTES: tuple[Attribute, ...] = (
    Attribute("metadata.source", "string", "yes", "Identifie l'API source."),
    Attribute("metadata.endpoint", "string", "yes", "Nom logique de l'endpoint."),
    Attribute(
        "metadata.extracted_at",
        "timestamp",
        "yes",
        "Instant de réception de la réponse, en UTC (AAAA-MM-JJThh:mm:ssZ). Devient la "
        "colonne _extracted_at en Bronze.",
    ),
    Attribute(
        "metadata.params",
        "object",
        "yes",
        "Paramètres résolus ayant produit cette requête. Seul lien entre l'enregistrement "
        "et ce qui l'a demandé — y compris les valeurs qui ne sont pas transmises à l'API.",
    ),
    Attribute("metadata.request.method", "string", "yes", "GET, POST, …"),
    Attribute(
        "metadata.request.base_url",
        "string",
        "yes",
        "URL de base, telle qu'appelée, sans barre oblique finale. Distingue les "
        "environnements.",
    ),
    Attribute(
        "metadata.request.path",
        "string",
        "yes",
        "Chemin résolu, paramètres de chemin remplacés. base_url + path est l'URL appelée, "
        "hors chaîne de requête.",
    ),
    Attribute(
        "metadata.request.query",
        "object",
        "yes",
        "Paramètres de requête tels qu'envoyés, curseur de pagination compris. Objet vide "
        "s'il n'y en a pas.",
    ),
    Attribute(
        "metadata.request.payload",
        "any",
        "post_only",
        "Corps tel qu'envoyé, curseur de pagination compris. null pour une requête sans "
        "corps.",
    ),
    Attribute(
        "metadata.request.headers",
        "object",
        "yes",
        "En-têtes envoyés. Toute créance est remplacée par ***REDACTED*** à l'écriture.",
    ),
    Attribute(
        "metadata.response.status",
        "integer",
        "yes",
        "Code HTTP. Une réponse en erreur est déposée, pas escamotée : c'est une "
        "information sur l'API.",
    ),
    Attribute(
        "metadata.parents",
        "list",
        "yes",
        "Identifiants, dans la zone de dépôt, des fichiers dont les paramètres de cette "
        "requête proviennent. Liste vide pour un endpoint pilote.",
    ),
    Attribute(
        "body",
        "any",
        "yes",
        "La réponse de l'API, verbatim : aucun renommage, aucune conversion, aucun "
        "aplatissement. null si la réponse n'est pas du JSON.",
    ),
    Attribute(
        "body_raw",
        "string",
        "non_json",
        "Texte brut de la réponse quand elle n'est pas du JSON. Absent sinon.",
    ),
)

DECLARED = frozenset(attribute.path for attribute in ATTRIBUTES)


def paths(envelope: Mapping[str, Any]) -> set[str]:
    """The dotted paths an envelope carries, stopping where the catalogue declares a leaf.

    `metadata.request.query` is one attribute whatever the query contains, so the walk
    stops there; an undeclared mapping is walked into, so that a key added to the envelope
    shows up as a path the catalogue does not know.
    """
    found: set[str] = set()

    def walk(node: Any, prefix: str) -> None:
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if path in DECLARED or not isinstance(value, Mapping):
                found.add(path)
            else:
                walk(value, path)

    walk(envelope, "")
    return found


def value_at(envelope: Mapping[str, Any], path: str) -> Any:
    node: Any = envelope
    for key in path.split("."):
        node = node[key]
    return node


def type_matches(attribute: Attribute, value: Any) -> bool:
    """Whether a built value fits the type the catalogue promises for it."""
    match attribute.type:
        case "string":
            return isinstance(value, str)
        case "object":
            return isinstance(value, Mapping)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "list":
            return isinstance(value, list)
        case "timestamp":
            return isinstance(value, str) and TIMESTAMP_RE.match(value) is not None
        case "any":
            return True
    return False  # pragma: no cover - every type key above is enumerated


def probe() -> list[dict[str, Any]]:
    """Two envelopes built by `envelope.build()` itself — one JSON, one not — for the
    comparison. Synthetic inputs, real builder: what it writes is what is compared."""
    from api_extractor.http.client import Request, Response
    from api_extractor.persist import envelope
    from api_extractor.plan.binding import RequestSpec

    spec = RequestSpec(
        source="probe",
        endpoint="things",
        method="GET",
        path="/things/1",
        query={"page": 0},
        payload=None,
        params={"id": "1"},
        parents=("output/probe/parents/all.json",),
        output_template="output/{source}/{endpoint}/{slug}.json",
    )
    request = Request(
        method="GET",
        url="https://probe.example.com/api/things/1",
        query={"page": 0},
        headers={"Accept": "application/json", "Authorization": "Basic x"},
    )
    built = []
    for parsed, body, text in ((True, {"ok": True}, '{"ok": true}'), (False, None, "<html>")):
        response = Response(status=200, headers={}, elapsed_ms=1, text=text, body=body, parsed=parsed)
        built.append(
            envelope.build(
                spec=spec,
                request=request,
                response=response,
                base_url="https://probe.example.com/api",
                extracted_at="2026-01-01T00:00:00Z",
            )
        )
    return built


def rows(endpoint_names: list[str]) -> list[dict[str, str]]:
    """The catalogue as document rows, with the endpoint names folded into `endpoint`."""
    out = []
    for attribute in ATTRIBUTES:
        description = attribute.description
        if attribute.path == "metadata.endpoint":
            description += " " + labels.ENDPOINT_NAMES.format(names=", ".join(endpoint_names))
        out.append(
            {
                "attribute": attribute.path,
                "type": labels.CONTRACT_TYPE[attribute.type],
                "mandatory": labels.MANDATORY[attribute.mandatory],
                "description": description,
            }
        )
    return out
