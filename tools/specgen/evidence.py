"""What a run left on disk: envelopes, read as evidence about response shapes.

The extractor deliberately does no schema inference or profiling — bodies are written
verbatim and what happens to them next is somebody else's layer. This is that layer. It
reads output, never writes it, and everything it derives is *observed*: types seen, keys
present. Nothing here is a promise about the API, and nothing here counts volumes — how
many rows an environment happens to hold is not a fact the receiving team can use.

All of it is optional input to the model. With nothing on disk the document still
builds; response shapes read `[À COMPLÉTER]`, the response sheets say so, and Annexe A is
empty.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from jsonpath_ng.ext import parse as parse_jsonpath

from api_extractor.config.models import Source
from api_extractor.logs import REDACTED
from api_extractor.providers.registry import SavedOutput
from api_extractor.runner import read_outputs
from specgen import labels
from specgen.labels import L

INVENTORY_MAX_DEPTH = 12


@dataclass(frozen=True)
class Evidence:
    envelopes: Mapping[str, list[SavedOutput]] = field(default_factory=dict)

    def for_endpoint(self, name: str) -> list[SavedOutput]:
        return list(self.envelopes.get(name, ()))

    @property
    def empty(self) -> bool:
        return not any(self.envelopes.values())


def gather(source: Source) -> Evidence:
    """Every envelope on disk for this source, per endpoint.

    Found the way the runner finds them — by output template and by their own metadata —
    so a stray file is never mistaken for evidence.
    """
    return Evidence(envelopes={name: read_outputs(source, name) for name in source.endpoints})


# --- shapes ---------------------------------------------------------------------------


def root_shape(body: Any) -> str:
    """One line about the top of a body: `objet (clés : data, hasNext)` or `liste de 12 éléments`."""
    if isinstance(body, Mapping):
        if not body:
            return str(L["endpoint.root_shape.object_empty"])
        return L.fmt("endpoint.root_shape.object", keys=", ".join(body))
    if isinstance(body, list):
        return L.fmt("endpoint.root_shape.list", count=labels.plural(len(body), L["endpoint.root_shape.item"]))
    return L.fmt("endpoint.root_shape.scalar", type=labels.iceberg(body))


def field_inventory(envelopes: Iterable[SavedOutput]) -> list[dict[str, Any]]:
    """One row per JSON path seen in the bodies: Iceberg types observed, whether it can
    be absent or null, the share of responses carrying it, and an example.

    List items are written `[]`, so `$.data[].id.id` covers every element rather than
    one. `nullable` is true when the path was ever null, or ever missing from a response
    that had a body. This records what came back; it prescribes nothing.
    """
    seen: dict[str, dict[str, Any]] = {}
    total = 0
    for saved in envelopes:
        if saved.body is None:
            continue  # a non-JSON response has no structure to record
        total += 1
        present: set[str] = set()
        _walk_body(saved.body, "$", seen, present, 0)
        for path in present:
            seen[path]["envelopes"] += 1
    rows = []
    for path in sorted(seen):
        entry = seen[path]
        types = sorted(entry["types"] - {"null"})
        rows.append(
            {
                "path": path,
                "type": ", ".join(str(L[f"types.json.{t}"]) for t in types) or str(L["types.json.null"]),
                "nullable": "null" in entry["types"] or entry["envelopes"] < total,
                "presence": entry["envelopes"] / total if total else 0.0,
                "example": entry["example"],
            }
        )
    return rows


def _walk_body(node: Any, path: str, seen: dict, present: set[str], depth: int) -> None:
    if depth > INVENTORY_MAX_DEPTH:
        return
    entry = seen.setdefault(path, {"types": set(), "envelopes": 0, "example": None})
    entry["types"].add(labels.json_type(node))
    present.add(path)
    if entry["example"] is None and not isinstance(node, Mapping | list) and node is not None:
        entry["example"] = _short(node)
    if isinstance(node, Mapping):
        for key, value in node.items():
            _walk_body(value, f"{path}.{key}", seen, present, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _walk_body(value, f"{path}[]", seen, present, depth + 1)


def _short(value: Any, limit: int = 60) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- samples --------------------------------------------------------------------------


def is_current(saved: SavedOutput) -> bool:
    """Written by the current envelope contract, as opposed to before the migration."""
    return "extracted_at" in (saved.envelope.get("metadata") or {})


def sample_for(evidence: Evidence, endpoint: str) -> SavedOutput | None:
    """The first successful, current-shape envelope — the one to attach as a sample."""
    for saved in evidence.for_endpoint(endpoint):
        status = ((saved.envelope.get("metadata") or {}).get("response") or {}).get("status")
        if is_current(saved) and isinstance(status, int) and status < 400:
            return saved
    return None


def truncate_lists(body: Any, keep: int) -> tuple[Any, bool]:
    """A copy with every list cut to its first `keep` items, and whether anything was cut.

    Truncation is a fact about the sample, not the response, so it is reported in the
    Annexe A table and never written inside `body`.
    """
    cut = False

    def walk(node: Any) -> Any:
        nonlocal cut
        if isinstance(node, list):
            if len(node) > keep:
                cut = True
            return [walk(item) for item in node[:keep]]
        if isinstance(node, Mapping):
            return {key: walk(value) for key, value in node.items()}
        return node

    return walk(copy.deepcopy(body)), cut


def redact(body: Any, json_paths: Iterable[str]) -> Any:
    """Mask every value a JSONPath selects. Headers are already redacted by the envelope;
    this is for what the annotation says is sensitive in the body itself."""
    masked = copy.deepcopy(body)
    for json_path in json_paths:
        masked = parse_jsonpath(json_path).update(masked, REDACTED)
    return masked


def sample_document(saved: SavedOutput, *, keep: int, json_paths: Iterable[str]) -> tuple[dict, bool]:
    """The whole envelope, body truncated and masked, so the metadata contract is shown
    on a real file rather than described."""
    document = copy.deepcopy(dict(saved.envelope))
    body, cut = truncate_lists(document.get("body"), keep)
    document["body"] = redact(body, json_paths) if body is not None else None
    if "body_raw" in document:
        document["body_raw"] = _short(document["body_raw"], 2000)
    return document, cut
