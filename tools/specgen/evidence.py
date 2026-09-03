"""What a run left on disk: envelopes and the manifest, read as evidence.

The extractor deliberately does no schema inference or profiling — bodies are written
verbatim and what happens to them next is somebody else's layer. This is that layer. It
reads output, never writes it, and everything it derives is *observed*: types seen, keys
present, records counted. Nothing here is a promise about the API.

All of it is optional input to the model. With nothing on disk the document still
builds; volumes read *N*, response shapes read `[À COMPLÉTER]`, and Annexe A is empty.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonpath_ng.ext import parse as parse_jsonpath

from api_extractor.config.models import Source
from api_extractor.logs import REDACTED
from api_extractor.persist import manifest
from api_extractor.providers.registry import SavedOutput
from api_extractor.runner import read_outputs, search_root
from specgen import labels

INVENTORY_MAX_DEPTH = 12


@dataclass(frozen=True)
class Evidence:
    envelopes: Mapping[str, list[SavedOutput]] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)  # one manifest's lines
    run_id: str | None = None

    def for_endpoint(self, name: str) -> list[SavedOutput]:
        return list(self.envelopes.get(name, ()))

    @property
    def empty(self) -> bool:
        return not any(self.envelopes.values()) and not self.records


def gather(source: Source, output_root: Path = Path("output"), run_id: str | None = None) -> Evidence:
    """Everything on disk for this source: envelopes per endpoint, plus one manifest.

    Envelopes are found the way the runner finds them — by output template and by their
    own metadata — so a stray file is never mistaken for evidence. The manifest is the one
    named, else the most recent one whose records belong to this source.
    """
    envelopes = {name: read_outputs(source, name) for name in source.endpoints}
    chosen = run_id or latest_run(source, output_root)
    records = manifest.read(manifest.path_for(output_root, chosen)) if chosen else []
    return Evidence(envelopes=envelopes, records=records, run_id=chosen)


def latest_run(source: Source, output_root: Path) -> str | None:
    """The newest manifest that belongs to this source, by name (ids sort by time).

    Manifest records carry no source name, so membership is judged by what they do carry:
    every endpoint is one of ours and every output path sits under one of our roots.
    """
    runs_dir = output_root / manifest.RUNS_DIR
    if not runs_dir.is_dir():
        return None
    roots = {str(search_root(source, name)) for name in source.endpoints}
    for path in sorted(runs_dir.glob("*.jsonl"), reverse=True):
        records = manifest.read(path)
        if records and all(_belongs(record, source, roots) for record in records):
            return path.stem
    return None


def _belongs(record: Mapping[str, Any], source: Source, roots: set[str]) -> bool:
    if record.get("endpoint") not in source.endpoints:
        return False
    output = record.get("output")
    if output is None:
        return True  # an unplanned endpoint has no path, and is still ours
    return any(str(Path(output)).startswith(root) for root in roots)


# --- volumes --------------------------------------------------------------------------


def measured(evidence: Evidence, endpoint: str) -> dict[str, Any] | None:
    """Counts off the manifest for one endpoint, or None when it recorded nothing."""
    records = [r for r in evidence.records if r.get("endpoint") == endpoint and "params" in r]
    if not records:
        return None
    statuses = [r.get("status") for r in records]
    written = sum(1 for s in statuses if isinstance(s, int) and s < 400)
    pages = [int(r.get("page", 0)) for r in records]
    return {
        "requests": len(records),
        "written": written,
        "failed": len(records) - written - sum(1 for s in statuses if s == "skipped"),
        "skipped": sum(1 for s in statuses if s == "skipped"),
        "pages_max": max(pages) + 1 if pages else 0,
        "distinct_params": len({_params_key(r) for r in records}),
    }


def _params_key(record: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((k, str(v)) for k, v in (record.get("params") or {}).items()))


def records_per_page(envelopes: Iterable[SavedOutput]) -> int | None:
    """The largest list a page carried, counted rather than inferred."""
    counts = [_record_count(saved.body) for saved in envelopes]
    counts = [c for c in counts if c is not None]
    return max(counts) if counts else None


def _record_count(body: Any) -> int | None:
    if isinstance(body, list):
        return len(body)
    if isinstance(body, Mapping):
        lists = [len(v) for v in body.values() if isinstance(v, list)]
        return max(lists) if lists else None
    return None


# --- shapes ---------------------------------------------------------------------------


def root_shape(body: Any) -> str:
    """One line about the top of a body: `objet (clés : data, hasNext)` or `liste de 12`."""
    if isinstance(body, Mapping):
        return labels.ROOT_OBJECT.format(keys=", ".join(body) or "—")
    if isinstance(body, list):
        return labels.ROOT_LIST.format(count=labels.plural(len(body), "élément"))
    return labels.ROOT_SCALAR.format(type=labels.type_of(body))


def field_inventory(envelopes: Iterable[SavedOutput]) -> list[dict[str, Any]]:
    """One row per JSON path seen in the bodies: observed types, presence, an example.

    Presence is the share of envelopes in which the path appeared at least once. List
    items are written `[]`, so `$.data[].id.id` covers every element rather than one. This
    records what came back; it prescribes nothing.
    """
    seen: dict[str, dict[str, Any]] = {}
    total = 0
    for saved in envelopes:
        total += 1
        present: set[str] = set()
        _walk_body(saved.body, "$", seen, present, 0)
        for path in present:
            seen[path]["envelopes"] += 1
    rows = []
    for path in sorted(seen):
        entry = seen[path]
        rows.append(
            {
                "path": path,
                "types": ", ".join(sorted(entry["types"])),
                "presence": entry["envelopes"] / total if total else 0.0,
                "example": entry["example"],
            }
        )
    return rows


def _walk_body(node: Any, path: str, seen: dict, present: set[str], depth: int) -> None:
    if depth > INVENTORY_MAX_DEPTH:
        return
    entry = seen.setdefault(path, {"types": set(), "envelopes": 0, "example": None})
    entry["types"].add(labels.type_of(node))
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
