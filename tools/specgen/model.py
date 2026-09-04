"""Build the intermediate model: everything the document says, as one JSON-able dict.

The .docx and the .xlsx are projections of this and compute nothing. Every reader-facing
string here is final text, taken from the label file; the code only decides which label
applies and what goes in its placeholders. The model is what a reviewer diffs and what
the tests assert on, which is how the document is testable without opening Word.

Derivation uses the config layer, the graph and the planner — never a provider's name.
A provider is *static* or *chained* by what its `depends_on` hook says, so a provider
written next year renders correctly without this file learning about it.

Nothing here counts anything. How many rows an environment holds is not a fact the
receiving team can use, so no volume — planned, measured or observed — reaches the
document.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api_extractor.config import graph
from api_extractor.config.models import Endpoint, FromMarker, Source, placeholders
from api_extractor.config.validate import endpoint_params
from api_extractor.http import pagination
from api_extractor.http.client import Request, Response
from api_extractor.persist import envelope
from api_extractor.plan import binding
from api_extractor.plan.binding import RequestSpec
from api_extractor.providers import registry
from api_extractor.providers.registry import ProviderContext
from specgen import contract, evidence as ev, labels
from specgen.annotation import Annotation
from specgen.labels import TODO, L

SCHEMA = "spec-model/2"
STATIC, CHAINED = "static", "chained"


@dataclass(frozen=True)
class Built:
    """The model, plus the sample files that go next to the document rather than in it."""

    model: dict[str, Any]
    samples: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    kind: str
    targets: tuple[str, ...]
    rows: list[dict[str, Any]] | None
    error: str | None
    fields: tuple[str, ...]
    phrase: str
    note: str | None


# --- ordering -----------------------------------------------------------------------


def document_order(source: Source) -> list[str]:
    """Reader order: by dependency level, drivers before leaves, alphabetical after that.

    The runner's order is alphabetical within a level, which puts `alarms` before the
    endpoint the whole chain hangs off. A reader wants the driver first. Endpoints of one
    level are independent, and §4.2 says so.
    """
    deps = graph.known(graph.dependencies(source))
    dependents = {name: {other for other in deps if name in deps[other]} for name in deps}
    ordered: list[str] = []
    done: set[str] = set()
    while len(ordered) < len(deps):
        ready = [name for name in deps if name not in done and deps[name] <= done]
        if not ready:  # pragma: no cover - validation rejects cycles before we get here
            raise graph.CycleError(sorted(set(deps) - done))
        ready.sort(key=lambda name: (not dependents[name], name))
        ordered.extend(ready)
        done.update(ready)
    return ordered


# --- providers ----------------------------------------------------------------------


def describe_providers(
    source: Source, annotation: Annotation, ctx: ProviderContext, cache: dict
) -> dict[str, ProviderInfo]:
    """Run every provider once and describe it without ever naming its function.

    Static providers read files, so running them costs nothing and yields the field names
    and value types the document wants. Chained ones read envelopes, and yield rows only
    when a run has left some on disk. Row *counts* are never used.
    """
    infos: dict[str, ProviderInfo] = {}
    for name, decl in source.providers.items():
        targets: tuple[str, ...] = ()
        if registry.is_registered(decl.fn) and registry.get(decl.fn).signature_error(decl.args) is None:
            targets = tuple(registry.get(decl.fn).endpoints_needed(decl.args))
        kind = CHAINED if targets else STATIC
        rows, error = None, None
        try:
            rows = binding.run_provider(source, name, ctx, cache)
        except Exception as exc:  # provider bodies are an I/O boundary
            error = f"{type(exc).__name__}: {exc}"
        fields = _fields_of(rows, decl.args)
        note = annotation.lists.get(name)
        infos[name] = ProviderInfo(
            name=name,
            kind=kind,
            targets=targets,
            rows=rows,
            error=error,
            fields=fields,
            phrase=(note.name if note and note.name else _phrase(source, kind, targets, fields)),
            note=note.note if note else None,
        )
    return infos


def _fields_of(rows: list[dict[str, Any]] | None, args: Mapping[str, Any]) -> tuple[str, ...]:
    if rows:
        return tuple(key for key in rows[0] if not binding.is_reserved(key))
    declared = args.get("fields")
    if isinstance(declared, Mapping):
        return tuple(declared)
    columns = args.get("columns")
    if isinstance(columns, list):
        return tuple(str(column) for column in columns)
    return ()


def _phrase(source: Source, kind: str, targets: tuple[str, ...], fields: tuple[str, ...]) -> str:
    if kind == CHAINED:
        target = source.endpoints.get(targets[0])
        if target is None:  # validation reports this; the phrase must still exist
            return L.fmt("lists.chained_unknown", endpoint=targets[0])
        return L.fmt("lists.chained", method=target.method, path=target.path)
    return L.fmt("lists.static", fields=" / ".join(fields) if fields else L["lists.default_field"])


def list_rows(source: Source, info: ProviderInfo) -> list[dict[str, str]]:
    """§4.3: how to obtain one parameter list, in generic terms.

    Args naming an endpoint render as that endpoint's method and path; args pointing at a
    file under `config/` or `input/` render as the attached referential — the document
    never mentions this repo's layout. Everything else is precise vocabulary the external
    team needs verbatim: a JSONPath, a join key.
    """
    decl = source.providers[info.name]
    arg_labels = L.section("lists.args")
    rows: list[dict[str, str]] = []
    if info.kind == CHAINED:
        target = source.endpoints.get(info.targets[0])
        if target is not None:
            rows.append(_row(L["lists.origin"], L.fmt("lists.origin_chained", method=target.method, path=target.path)))
    else:
        rows.append(_row(L["lists.origin"], L["lists.origin_static"]))
    for key, value in decl.args.items():
        if isinstance(value, str) and value in source.endpoints:
            continue
        if isinstance(value, str) and value.startswith(("config/", "input/")):
            rows.append(_row(L["lists.referential"], L["lists.attached"]))
        elif key == "fields" and isinstance(value, Mapping):
            for field_name, path in value.items():
                rows.append(_row(L.fmt("lists.value_of", field=field_name), L.fmt("lists.field_relative", path=path)))
        elif key == "values" and isinstance(value, list):
            rows.append(_row(arg_labels.get("values", key), _values_text(value)))
        elif isinstance(value, list):
            rows.append(_row(arg_labels.get(key, key), ", ".join(str(item) for item in value)))
        else:
            rows.append(_row(arg_labels.get(key, key), str(value)))
    if info.fields:
        rows.append(_row(L["lists.fields"], ", ".join(info.fields)))
    if info.note:
        rows.append(_row(L["lists.meaning"], info.note))
    return rows


def _values_text(values: list[Any]) -> str:
    items = []
    for value in values:
        if isinstance(value, Mapping) and len(value) == 1:
            items.append(str(next(iter(value.values()))))
        else:
            items.append(json.dumps(value, ensure_ascii=False))
    return ", ".join(items)


def _row(item: str, value: str) -> dict[str, str]:
    return {"item": item, "value": value}


# --- one endpoint -------------------------------------------------------------------


def param_rows(ep: Endpoint, infos: Mapping[str, ProviderInfo]) -> list[dict[str, str]]:
    rows = [_param(key, "path", "", marker, infos) for key, marker in ep.bind.items()]
    rows.extend(_walk_params(ep.query, "query", "", None, infos))
    rows.extend(_walk_params(ep.payload, "payload", "", None, infos))
    rows.extend(_param(key, "label", "", marker, infos) for key, marker in ep.label.items())
    return rows


def _walk_params(node: Any, root: str, prefix: str, key: str | None, infos) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if isinstance(node, FromMarker):
        rows.append(_param(node.alias or key or "?", root, prefix, node, infos))
    elif isinstance(node, Mapping):
        for child, value in node.items():
            loc = f"{prefix}.{child}" if prefix else child
            if isinstance(value, FromMarker | Mapping | list):
                rows.extend(_walk_params(value, root, loc, child, infos))
            else:
                rows.append(
                    {
                        "name": child,
                        "location": _location(root, loc, nested=bool(prefix)),
                        "type": labels.iceberg(value),
                        "origin": L.fmt("endpoint.fixed_value", value=value),
                    }
                )
    elif isinstance(node, list):
        if any(_has_marker(item) for item in node):
            for i, item in enumerate(node):
                rows.extend(_walk_params(item, root, f"{prefix}[{i}]", key, infos))
        elif key is not None:
            rows.append(
                {
                    "name": key,
                    "location": _location(root, prefix, nested="." in prefix),
                    "type": labels.iceberg(node),
                    "origin": L.fmt("endpoint.fixed_value", value=json.dumps(node, ensure_ascii=False)),
                }
            )
    return rows


def _has_marker(node: Any) -> bool:
    if isinstance(node, FromMarker):
        return True
    if isinstance(node, Mapping):
        return any(_has_marker(value) for value in node.values())
    if isinstance(node, list):
        return any(_has_marker(value) for value in node)
    return False


def _param(name: str, root: str, loc: str, marker: FromMarker, infos) -> dict[str, str]:
    info = infos.get(marker.provider)
    return {
        "name": name,
        "location": _location(root, loc, nested="." in loc or "[" in loc),
        "type": _marker_type(name, info),
        "origin": info.phrase if info else marker.provider,
    }


def _location(root: str, loc: str, *, nested: bool) -> str:
    text = str(L[f"enums.location.{root}"])
    return f"{text} ({loc})" if nested and loc else text


def _marker_type(name: str, info: ProviderInfo | None) -> str:
    """The type of the value a marker will carry, read off a real row when one exists."""
    if info is None or not info.rows:
        return str(L["types.json.string"])
    fields = {key: value for key, value in info.rows[0].items() if not binding.is_reserved(key)}
    try:
        return labels.iceberg(binding.select(info.name, fields, name))
    except ValueError:
        return str(L["types.json.string"])


def correlated_origins(ep: Endpoint, infos: Mapping[str, ProviderInfo]) -> list[str]:
    """Several params off one provider come from one record and must stay together."""
    used = [ref.marker.provider for ref in ep.markers()]
    shared = sorted({name for name in used if used.count(name) > 1})
    return [L.fmt("endpoint.correlated", origin=infos[name].phrase) for name in shared if name in infos]


def pagination_rows(ep: Endpoint) -> list[dict[str, str]]:
    """Empty when the endpoint does not paginate: an absence is never mentioned."""
    paginate = ep.paginate
    if paginate is None:
        return []
    return [
        _row(L["endpoint.pagination.carried_by"], L[f"enums.cursor_root.{paginate.at_root}"]),
        _row(L["endpoint.pagination.cursor"], paginate.at),
        _row(L["endpoint.pagination.first_page"], str(paginate.start)),
        _row(L["endpoint.pagination.signal"], paginate.has_more or L["endpoint.pagination.signal_none"]),
        _row(L["endpoint.pagination.stops_when"], stop_condition(ep)),
    ]


def stop_condition(ep: Endpoint) -> str:
    """Both halves. The walk stops on an empty page whatever `has_more` says."""
    paginate = ep.paginate
    assert paginate is not None
    if paginate.has_more is None:
        return str(L["endpoint.stop.empty"])
    return L.fmt(
        "endpoint.stop.either",
        empty=L["endpoint.stop.empty"],
        flag=L.fmt("endpoint.stop.flag", path=paginate.has_more),
    )


def _origins(ep: Endpoint, infos: Mapping[str, ProviderInfo]) -> str:
    providers = sorted({ref.marker.provider for ref in ep.markers()})
    return ", ".join(infos[name].phrase if name in infos else name for name in providers)


def iteration_lines(ep: Endpoint, infos: Mapping[str, ProviderInfo]) -> list[str]:
    """Pseudocode a reader can implement. The loop is over rows, not over one parameter:
    a provider emits rows, so several params off one row stay correlated."""
    origins = _origins(ep, infos)
    lines: list[str] = []
    if origins:
        lines.append(L.fmt("endpoint.pseudocode.for_each", origins=origins))
    indent = "    " if origins else ""
    paginate = ep.paginate
    if paginate is None:
        lines.append(L.fmt("endpoint.pseudocode.request", indent=indent, method=ep.method, path=ep.path))
        lines.append(L.fmt("endpoint.pseudocode.land", indent=indent))
        return lines
    lines.append(L.fmt("endpoint.pseudocode.page_init", indent=indent, start=paginate.start))
    lines.append(L.fmt("endpoint.pseudocode.repeat", indent=indent))
    lines.append(L.fmt("endpoint.pseudocode.response", indent=indent, method=ep.method, path=ep.path))
    lines.append(L.fmt("endpoint.pseudocode.cursor", indent=indent, at=paginate.at))
    lines.append(L.fmt("endpoint.pseudocode.land_in_loop", indent=indent))
    lines.append(L.fmt("endpoint.pseudocode.next_page", indent=indent))
    lines.append(L.fmt("endpoint.pseudocode.until", indent=indent, condition=stop_condition(ep)))
    return lines


def called_text(ep: Endpoint, infos: Mapping[str, ProviderInfo]) -> str:
    origins = _origins(ep, infos)
    return L.fmt("endpoint.called.per", origins=origins) if origins else str(L["endpoint.called.once"])


def render_key(template: str, source: str, endpoint: str, params: Mapping[str, Any], *, extract_date: str, page: int = 0) -> str:
    """Fill a landing key. An unresolvable placeholder renders as `<name>` rather than
    raising: `--check` reports it, and the model must still build for the report."""
    values: dict[str, Any] = {
        **params,
        "source": source,
        "endpoint": endpoint,
        "page": page,
        "slug": binding.slug_for(params),
        "extract_date": extract_date,
    }
    return binding.PLACEHOLDER_RE.sub(
        lambda match: str(values.get(match.group(1), f"<{match.group(1)}>")), template
    )


def full_key(annotation: Annotation, key: str) -> str:
    """The bucket is one slot, counted once on the layout table — not once per key."""
    bucket = "<bucket>" if labels.is_todo(annotation.landing.bucket) else annotation.landing.bucket
    prefix = f"/{annotation.landing.prefix.strip('/')}" if annotation.landing.prefix else ""
    return f"s3://{bucket}{prefix}/{key}"


# --- the model ----------------------------------------------------------------------


def build(
    source: Source,
    annotation: Annotation,
    evidence: ev.Evidence | None = None,
    *,
    sample_records: int = 3,
    generated_at: datetime | None = None,
) -> Built:
    evidence = evidence or ev.Evidence()
    now = generated_at or datetime.now(UTC)
    today = now.strftime("%Y-%m-%d")
    ctx = ProviderContext(
        run_id="spec",
        output_root=Path("output"),
        source_name=source.source,
        outputs_for=evidence.for_endpoint,
    )
    cache: dict[str, list[dict[str, Any]]] = {}
    infos = describe_providers(source, annotation, ctx, cache)
    order = document_order(source)
    deps = graph.known(graph.dependencies(source))
    dependents = {name: sorted(other for other in order if name in deps[other]) for name in order}
    names = sorted(source.endpoints)

    # Planned only to borrow one real request's parameters for the key examples and the
    # landing example; the request counts a plan yields are never read.
    plans = {name: binding.plan_one(source, name, ctx, cache, no_limit=True) for name in order}

    samples: dict[str, dict[str, Any]] = {}
    endpoints: list[dict[str, Any]] = []
    for number, name in enumerate(order, start=1):
        ep = source.endpoints[name]
        ann = annotation.endpoint(name)
        on_disk = evidence.for_endpoint(name)

        sample = ev.sample_for(evidence, name)
        sample_entry = None
        if sample is not None and not (ann.sample and ann.sample.exclude):
            document, cut = ev.sample_document(
                sample, keep=sample_records, json_paths=ann.sample.redact if ann.sample else ()
            )
            file_name = L.fmt("document.sample_file", endpoint=name)
            samples[file_name] = document
            sample_entry = {
                "endpoint": name,
                "file": file_name,
                "captured_at": labels.fr_date(document["metadata"]["extracted_at"]),
                "note": L.fmt("samples.truncated", count=sample_records) if cut else str(L["samples.full"]),
            }

        root = (ann.response.root if ann.response else None) or (
            ev.root_shape(sample.body) if sample is not None else TODO
        )
        response_rows = [_row(L["endpoint.response.root"], root), _row(L["endpoint.response.grain"], ann.record_grain)]
        if ann.response and ann.response.nested:
            response_rows.append(_row(L["endpoint.response.nested"], ann.response.nested))
        if sample_entry:
            response_rows.append(_row(L["endpoint.response.sample"], L.fmt("endpoint.response.sample_value", file=sample_entry["file"])))

        summary = [
            _row(L["endpoint.summary.method"], f"{ep.method} {ep.path}"),
            _row(L["endpoint.summary.purpose"], ann.purpose),
            _row(L["endpoint.summary.called"], called_text(ep, infos)),
        ]
        if deps[name]:
            summary.append(_row(L["endpoint.summary.depends_on"], ", ".join(sorted(deps[name]))))
        summary.append(_row(L["endpoint.summary.grain"], ann.record_grain))
        if ann.auth_scope:
            summary.append(_row(L["endpoint.summary.auth_scope"], ann.auth_scope))
        if ann.vendor_ref:
            summary.append(_row(L["endpoint.summary.vendor_ref"], ann.vendor_ref))

        example_params = _example_params(ep, plans[name].requests, sample)
        key_template = annotation.landing_key(name, paginated=ep.paginate is not None)
        rendered = render_key(key_template, source.source, name, example_params, extract_date=today)

        endpoints.append(
            {
                "name": name,
                "number": number,
                "method": ep.method,
                "path": ep.path,
                "title": L.fmt("endpoint.title", method=ep.method, path=ep.path, name=name),
                # Purpose and grain live in `summary` (and grain again in `response`),
                # exactly as many times as the document shows them: completeness counts
                # markers on the model, so the model carries no copy the document does not.
                "depends_on": sorted(deps[name]),
                "dependents": dependents[name],
                "summary": summary,
                "params": param_rows(ep, infos),
                "correlated_origins": correlated_origins(ep, infos),
                "pagination": pagination_rows(ep) or None,
                "iteration_title": str(L["endpoint.iteration_title.paginated" if ep.paginate else "endpoint.iteration_title.plain"]),
                "iteration": iteration_lines(ep, infos) if (ep.paginate or ep.markers()) else None,
                "response": response_rows,
                "response_fields": ev.field_inventory(on_disk),
                "quirks": list(ann.quirks),
                "mode": str(L.get(f"enums.mode.{ann.mode}", ann.mode)),
                "rationale": ann.rationale,
                "files": str(L["endpoint.files.page" if ep.paginate else "endpoint.files.request"]),
                "landing_key": key_template,
                # An exception is one this endpoint declares, not merely a key that
                # differs from the base — every paginated endpoint differs from the base.
                "key_overridden": ann.key is not None,
                "rendered_key": full_key(annotation, rendered),
                "sample": sample_entry,
                "provider_error": next((infos[n].error for n in _providers_of(ep) if infos.get(n) and infos[n].error), None),
            }
        )

    paged = [name for name in order if source.endpoints[name].paginate]
    total = labels.plural(len(order), str(L["flow.endpoint_word"]))
    pagination_sentence = (
        L.fmt("flow.pagination_some", total=total, paged=len(paged), plural="nt" if len(paged) > 1 else "", names=", ".join(paged))
        if paged
        else L.fmt("flow.pagination_none", total=total)
    )

    status = str(L[f"enums.status.{annotation.spec.status}"])
    version_text = L.fmt("document.version_text", version=annotation.spec.version, status=status)
    history = [
        {"version": e.version, "date": e.date, "author": e.author, "summary": e.summary}
        for e in annotation.spec.history
    ] or [
        {
            "version": annotation.spec.version,
            "date": annotation.spec.date,
            "author": annotation.spec.author,
            "summary": str(L["document.history_initial"]),
        }
    ]
    workbook_file = L.fmt("document.workbook_file", source=source.source)
    cover = [
        _row(L["document.cover.owner"], annotation.spec.owner),
        _row(L["document.cover.author"], annotation.spec.author),
        _row(L["document.cover.version"], version_text),
        _row(L["document.cover.date"], annotation.spec.date),
    ]
    if annotation.spec.reviewers:
        cover.append(_row(L["document.cover.reviewers"], annotation.spec.reviewers))
    cover.append(_row(L["document.cover.team"], annotation.spec.implementation_team))
    cover.append(_row(L["document.cover.workbook"], workbook_file))

    example, example_endpoint = _landing_example(source, annotation, order, plans, evidence, today)

    model: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {"name": source.source, "base_url": source.base_url, "endpoints": names},
        "document": {
            "source_system": annotation.spec.source_system,
            "version": annotation.spec.version,
            "status": status,
            "version_text": version_text,
            "date": annotation.spec.date,
            "title": str(L["document.title"]),
            "subtitle": L.fmt("document.subtitle", source_system=annotation.spec.source_system),
            "header": L.fmt("document.header", source_system=annotation.spec.source_system, version=annotation.spec.version, status=status),
            "cover": cover,
            "history": history,
            "file": L.fmt("document.file", source=source.source),
        },
        "definitions": [{"term": term, "definition": text} for term, text in annotation.definitions.items()],
        "related": _related(annotation, infos, bool(samples)),
        "environments": _environments(source, annotation),
        "auth": _auth(source, annotation),
        "endpoints": endpoints,
        "flow": {
            "pagination_sentence": pagination_sentence,
            "tree": _tree(order, deps, dependents),
            "sequence": [
                {
                    "step": str(i),
                    "action": f"{source.endpoints[name].method} {source.endpoints[name].path}",
                    "after": ", ".join(sorted(deps[name])),
                    "files": str(L["endpoint.files.page" if source.endpoints[name].paginate else "endpoint.files.request"]),
                }
                for i, name in enumerate(order, start=1)
            ],
            "lists": [
                {
                    "name": infos[name].phrase,
                    "used_by": L.fmt(
                        "lists.used_by",
                        names=", ".join(n for n in order if name in _providers_of(source.endpoints[n])),
                    ),
                    "rows": list_rows(source, infos[name]),
                }
                for name in source.providers
            ],
        },
        "landing": {
            "contract": contract.rows(names),
            "example": example,
            "example_endpoint": example_endpoint,
            "example_lines": json.dumps(example, ensure_ascii=False, indent=2).splitlines(),
            "layout": _layout(annotation),
            "key_template": annotation.landing.key,
            "rendered_keys": [e["rendered_key"] for e in endpoints],
            "key_template_paginated": annotation.landing.key_paginated,
            "overrides": [
                {"endpoint": e["name"], "key": e["landing_key"]}
                for e in endpoints
                if e["key_overridden"]
            ],
        },
        "appendix": {
            "samples": [e["sample"] for e in endpoints if e["sample"]],
            "workbook": {"file": workbook_file, "tabs": _tabs(order)},
        },
    }
    model["completeness"] = completeness(model, annotation, order)
    return Built(model=model, samples=samples)


def _providers_of(ep: Endpoint) -> set[str]:
    return {ref.marker.provider for ref in ep.markers()}


def _tabs(order: list[str]) -> list[dict[str, str]]:
    """The workbook's sheets, in order: the fixed ones, then one per endpoint."""
    sheets = L.section("workbook.sheets")
    tabs = [
        {"key": key, "name": str(sheets[key]["name"]), "contents": str(sheets[key]["contents"]), "reader": str(sheets[key]["reader"])}
        for key in ("readme", "endpoints", "metadata")
    ]
    for name in order:
        tabs.append(
            {
                "key": f"response:{name}",
                "name": str(sheets["response"]["name"]).format(endpoint=name),
                "contents": str(sheets["response"]["contents"]).format(endpoint=name),
                "reader": str(sheets["response"]["reader"]),
            }
        )
    return tabs


def _example_params(ep: Endpoint, requests: Sequence[RequestSpec], sample) -> dict[str, Any]:
    if sample is not None:
        return dict((sample.envelope.get("metadata") or {}).get("params") or {})
    if requests:
        return dict(requests[0].params)
    return _placeholder_params(ep)


def _landing_example(source, annotation, order, plans, evidence, today) -> tuple[dict[str, Any], str]:
    """§6.4, produced by `envelope.build()` so it cannot disagree with the contract.

    A real envelope is preferred when the run left one; otherwise a synthetic request is
    built from the plan. Parents are shown as landing keys, since that is what the
    external pipeline will write there.
    """
    chosen = max(order, key=lambda name: (len(endpoint_params(source.endpoints[name])), order.index(name)))
    sample = ev.sample_for(evidence, chosen)
    body = {"…": str(L["landing.example_body"])}
    if sample is not None:
        example = {"metadata": json.loads(json.dumps(sample.envelope["metadata"])), "body": body}
    else:
        ep = source.endpoints[chosen]
        specs = plans[chosen].requests
        spec = specs[0] if specs else RequestSpec(
            source=source.source,
            endpoint=chosen,
            method=ep.method,
            path=binding.render(ep.path, {name: f"<{name}>" for name in placeholders(ep.path)}),
            query=binding.fill_markers(ep.query, None, _placeholder_params(ep)),
            payload=binding.fill_markers(ep.payload, None, _placeholder_params(ep)),
            params=_placeholder_params(ep),
            parents=(),
            output_template=source.output_template(chosen),
        )
        auth_names = _auth_header_names(source)
        headers = {**dict(source.defaults.headers), **{name: "secret" for name in auth_names}}
        request = Request(
            method=spec.method,
            url=f"{source.base_url.rstrip('/')}/{spec.path.lstrip('/')}",
            query=spec.query,
            payload=spec.payload,
            headers=headers,
        )
        if ep.paginate is not None:
            request = pagination.with_cursor(request, ep.paginate, ep.paginate.start)
        response = Response(status=200, headers={}, elapsed_ms=0, text="{}", body=body)
        example = envelope.build(
            spec=spec,
            request=request,
            response=response,
            base_url=source.base_url,
            extracted_at=f"{today}T02:04:19Z",
            auth_headers=auth_names,
        )
    example["metadata"]["parents"] = [
        _parent_key(parent, source, annotation, evidence, today) for parent in example["metadata"]["parents"]
    ]
    return example, chosen


def _placeholder_params(ep: Endpoint) -> dict[str, str]:
    return {name: f"<{name}>" for name in sorted(endpoint_params(ep))}


def _parent_key(parent: str, source, annotation, evidence, today) -> str:
    """A parent's local path, shown as the landing key that file would have.

    The page is read back off the parent's request, where the cursor sits verbatim: that
    is the whole argument for not carrying a separate page number.
    """
    for name in source.endpoints:
        ep = source.endpoints[name]
        for saved in evidence.for_endpoint(name):
            if Path(saved.path) == Path(parent):
                metadata = saved.envelope.get("metadata") or {}
                page = _page_of(metadata.get("request") or {}, ep) if ep.paginate else 0
                return render_key(
                    annotation.landing_key(name, paginated=ep.paginate is not None),
                    source.source,
                    name,
                    metadata.get("params") or {},
                    extract_date=today,
                    page=page,
                )
    return parent


def _page_of(request: Mapping[str, Any], ep: Endpoint) -> int:
    assert ep.paginate is not None
    node: Any = request.get(ep.paginate.at_root)
    for key in ep.paginate.at_keys:
        if not isinstance(node, Mapping):
            return 0
        node = node.get(key)
    return node - ep.paginate.start if isinstance(node, int) else 0


def _auth_header_names(source: Source) -> list[str]:
    """Header *names* the auth layer sets, by construction; values are never read."""
    return [name for name, _shape in _auth_header_shapes(source)]


def _auth_header_shapes(source: Source) -> list[tuple[str, str]]:
    """(name, the value's shape) for every header the auth layer sets.

    The shape is the credential's *template* with the secret standing in — `ApiKey
    <secret>` rather than a bare placeholder. That wrapper is structure the implementing
    team has to reproduce byte for byte, and a document that hides it hides the wrong
    half. It is safe to print by construction: a template may contain only `{value}` or
    `{token}` (`models.check_secret_template` rejects anything else), so it never carries
    a secret, and the secret itself is never read here at all.
    """
    auth = source.auth
    if auth is None:
        return []
    secret = str(L["auth.secret_placeholder"])
    if auth.type == "basic":
        return [("Authorization", str(L["auth.basic_value"]))]
    if auth.type == "header":
        return [(name, value.template.format(value=secret)) for name, value in auth.headers.items()]
    return [(auth.apply.header, auth.apply.template.format(token=secret))]


def _auth(source: Source, annotation: Annotation) -> dict[str, Any]:
    auth = source.auth
    rows = [
        _row(L["auth.mechanism"], str(L[f"enums.auth.{auth.type}"]) if auth is not None else str(L["auth.none"])),
        _row(L["auth.secrets"], annotation.secrets),
    ]
    headers = [
        {"name": name, "value": value, "required": labels.yes_no(True), "scope": str(L["auth.scope_all"])}
        for name, value in source.defaults.headers.items()
    ]
    for name, shape in _auth_header_shapes(source):
        headers.append(
            {
                "name": name,
                "value": shape,
                "required": labels.yes_no(True),
                "scope": str(L["auth.scope_auth"]),
            }
        )
    return {"rows": rows, "headers": headers}


def _environments(source: Source, annotation: Annotation) -> list[dict[str, str]]:
    rows = [{"name": str(L["environments.production"]), "base_url": source.base_url, "notes": str(L["environments.production_purpose"])}]
    for name, env in annotation.environments.items():
        rows.append({"name": name.upper(), "base_url": env.base_url, "notes": env.notes or ""})
    return rows


def _related(annotation: Annotation, infos: Mapping[str, ProviderInfo], has_samples: bool) -> list[dict[str, str]]:
    rows = [_row(L["related.vendor_docs"], annotation.spec.vendor_docs)]
    if any(info.kind == STATIC for info in infos.values()):
        rows.append(_row(L["related.referentials"], L["related.referentials_value"]))
    if has_samples:
        rows.append(_row(L["related.samples"], L["related.samples_value"]))
    return rows


def _tree(order: list[str], deps, dependents) -> list[str]:
    roots = [name for name in order if not deps[name]]
    roots.sort(key=lambda name: (-len(dependents[name]), name))
    lines = []
    for root in roots:
        lines.append(f"{root}{L['flow.driver_suffix'] if dependents[root] else ''}")
        for j, child in enumerate(dependents[root]):
            lines.append(f"  {'└──' if j == len(dependents[root]) - 1 else '├──'} {child}")
    return lines


def _layout(annotation: Annotation) -> list[dict[str, str]]:
    rows = [_row(L["landing.layout.bucket"], annotation.landing.bucket)]
    if annotation.landing.prefix:
        rows.append(_row(L["landing.layout.prefix"], annotation.landing.prefix))
    rows.append(_row(L["landing.layout.format"], L["landing.layout.format_value"]))
    if annotation.landing.encryption:
        rows.append(_row(L["landing.layout.encryption"], annotation.landing.encryption))
    return rows


# --- completeness -------------------------------------------------------------------


STRUCTURAL_SPEC = ("owner", "author", "implementation_team", "vendor_docs")
STRUCTURAL_ENDPOINT = ("purpose", "record_grain", "mode", "rationale")


def completeness(model: Mapping[str, Any], annotation: Annotation, order: list[str]) -> dict[str, Any]:
    """How much of the document is still `[À COMPLÉTER]`, counted on the model.

    `todo` is every string in the model carrying the marker, so a derived placeholder (a
    response shape with no sample to read it from) counts exactly as the document shows
    it. `filled` is every structural slot that holds real text. The percentage is the
    share of slots with an answer.
    """
    locations = sorted(loc for loc, text in _strings(model) if labels.is_todo(text))
    filled = 0
    for name in STRUCTURAL_SPEC:
        filled += not labels.is_todo(getattr(annotation.spec, name))
    filled += not labels.is_todo(annotation.landing.bucket)
    for name in order:
        ann = annotation.endpoint(name)
        for field_name in STRUCTURAL_ENDPOINT:
            filled += not labels.is_todo(getattr(ann, field_name))
    root_label = str(L["endpoint.response.root"])
    root_rows = [
        row["value"]
        for endpoint in model["endpoints"]
        for row in endpoint["response"]
        if row["item"] == root_label
    ]
    filled += sum(not labels.is_todo(value) for value in root_rows)
    todo = len(locations)
    percent = round(100 * filled / (filled + todo), 1) if filled + todo else 100.0
    return {"todo": todo, "filled": filled, "percent": percent, "locations": locations}


def _strings(node: Any, loc: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(node, str):
        found.append((loc, node))
    elif isinstance(node, Mapping):
        for key, value in node.items():
            found.extend(_strings(value, f"{loc}.{key}" if loc else str(key)))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found.extend(_strings(value, f"{loc}[{i}]"))
    return found


# --- what a template may reference --------------------------------------------------


def variables(model: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Every path a template tag may use, with an example value. Lists are shown once,
    through their first item, as `path[]`."""
    out: list[tuple[str, str]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            if node and isinstance(node[0], Mapping | list):
                walk(node[0], f"{path}[]")
            else:
                out.append((path, _example_text(node)))
        else:
            out.append((path, _example_text(node)))

    walk(model, "")
    return out


def _example_text(value: Any, limit: int = 70) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"
