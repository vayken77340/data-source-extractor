"""Expand a validated source into concrete request specs.

Given config plus provider output, this emits a list of requests and nothing else — no
sockets, no files written. That is what makes the whole fan-out testable: `--dry-run` runs
exactly this code and then stops.

Ordering comes from `config/graph.py` rather than being rediscovered here.
"""

from __future__ import annotations

import hashlib
import itertools
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from api_extractor.config import graph
from api_extractor.config.models import PLACEHOLDER_RE, FromMarker, Source, is_reserved
from api_extractor.providers import registry
from api_extractor.providers.registry import ProviderContext

PARENTS_KEY = "__parents__"
SLUG_MAX = 100
SLUG_HASH = 8
EMPTY_SLUG = "all"


@dataclass(frozen=True)
class Binding:
    """One provider row's contribution: the params it fills, and its lineage."""

    params: Mapping[str, Any]
    parents: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequestSpec:
    source: str
    endpoint: str
    method: str
    path: str
    query: Mapping[str, Any]
    payload: Any
    params: Mapping[str, Any]
    parents: tuple[str, ...]
    output_template: str

    def output(self, page: int = 0) -> str:
        return render_output(self.output_template, self.source, self.endpoint, self.params, page)


@dataclass(frozen=True)
class EndpointPlan:
    endpoint: str
    requests: tuple[RequestSpec, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class Plan:
    source: str
    order: tuple[str, ...]
    provider_rows: Mapping[str, int] = field(default_factory=dict)
    endpoints: tuple[EndpointPlan, ...] = ()

    @property
    def request_count(self) -> int:
        return sum(len(plan.requests) for plan in self.endpoints)

    @property
    def failures(self) -> tuple[EndpointPlan, ...]:
        return tuple(plan for plan in self.endpoints if plan.error is not None)


# --- templating ---------------------------------------------------------------------


def slugify(value: str) -> str:
    """Lowercase, non-alphanumerics to `-`, collapsed, truncated with a hash if cut."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not cleaned:
        return EMPTY_SLUG
    if len(cleaned) <= SLUG_MAX:
        return cleaned
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:SLUG_HASH]
    return f"{cleaned[: SLUG_MAX - SLUG_HASH - 1].rstrip('-')}-{digest}"


def slug_for(params: Mapping[str, Any]) -> str:
    """Resolved params joined and slugified. Sorted, so the same params give one path."""
    if not params:
        return EMPTY_SLUG
    return slugify("-".join(str(params[key]) for key in sorted(params)))


def render(template: str, values: Mapping[str, Any]) -> str:
    """Fill `{name}` placeholders. Validation has already proved every name resolves."""
    return PLACEHOLDER_RE.sub(lambda match: str(values[match.group(1)]), template)


def render_output(
    template: str, source: str, endpoint: str, params: Mapping[str, Any], page: int = 0
) -> str:
    # Intrinsics last: `output/{source}/...` must always be the source name, or the tree
    # shape depends on whether some API happens to have a parameter called `source`.
    values = {
        **params,
        "source": source,
        "endpoint": endpoint,
        "page": page,
        "slug": slug_for(params),
    }
    return render(template, values)


# --- provider rows to bindings ------------------------------------------------------


def strip_reserved(row: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Split a provider row into params and the `__key__` metadata the runner reads."""
    fields = {key: value for key, value in row.items() if not is_reserved(key)}
    parents = tuple(str(parent) for parent in row.get(PARENTS_KEY, ()))
    return fields, parents


def select(provider_name: str, fields: Mapping[str, Any], param: str) -> Any:
    """The value a marker takes off one row.

    Named field first. A single-field row is unwrapped whatever the field is called, which
    is what lets `type: {from: asset_types}` read rows of `{assetType: ...}`.
    """
    if param in fields:
        return fields[param]
    if len(fields) == 1:
        return next(iter(fields.values()))
    raise ValueError(
        f"provider {provider_name!r} has no field {param!r} to fill — its rows carry "
        f"{sorted(fields)}. Name the marker after the field, or rename it with `as`."
    )


def bindings_for(
    provider_name: str, rows: Sequence[Mapping[str, Any]], params: Sequence[str]
) -> list[Binding]:
    out: list[Binding] = []
    for row in rows:
        fields, parents = strip_reserved(row)
        out.append(
            Binding(
                params={param: select(provider_name, fields, param) for param in params},
                parents=parents,
            )
        )
    return out


def combine(groups: Sequence[tuple[str, Sequence[Binding]]], fan_out: str) -> list[Binding]:
    """Cross or pair separate providers. Fields from one provider are already row-wise."""
    if not groups:
        return [Binding(params={})]

    if fan_out == "zip":
        lengths = {len(bindings) for _name, bindings in groups}
        if len(lengths) > 1:
            counts = ", ".join(f"{name}: {len(bindings)}" for name, bindings in groups)
            raise ValueError(
                f"fan_out: zip pairs providers positionally, so they must yield the same "
                f"number of rows — got {counts}"
            )
        combos: Any = zip(*(bindings for _name, bindings in groups), strict=True)
    else:
        combos = itertools.product(*(bindings for _name, bindings in groups))

    merged: list[Binding] = []
    for combo in combos:
        params: dict[str, Any] = {}
        parents: list[str] = []
        for binding in combo:
            params.update(binding.params)
            parents.extend(binding.parents)
        merged.append(Binding(params=params, parents=tuple(dict.fromkeys(parents))))
    return merged


def fill_markers(node: Any, key: str | None, params: Mapping[str, Any]) -> Any:
    """Replace every marker with its value, at any depth. Mirrors `Endpoint.markers()`."""
    if isinstance(node, FromMarker):
        name = node.alias or key
        if name is None:  # config.params.no_collision rejects this before we get here
            raise ValueError("marker has no name")
        return params[name]
    if isinstance(node, dict):
        return {child: fill_markers(value, child, params) for child, value in node.items()}
    if isinstance(node, list):
        return [fill_markers(value, key, params) for value in node]
    return node


# --- planning -----------------------------------------------------------------------


def effective_limit(
    source: Source, endpoint_name: str, override: int | None, no_limit: bool
) -> int | None:
    """default -> endpoint -> CLI. `None` means unlimited."""
    if no_limit:
        return None
    if override is not None:
        return override
    endpoint = source.endpoints[endpoint_name]
    if not endpoint.inherits_limit():
        return endpoint.limit
    return source.defaults.limit


def run_provider(
    source: Source, name: str, ctx: ProviderContext, cache: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Run a declared provider once per plan, however many endpoints name it."""
    if name not in cache:
        decl = source.providers[name]
        cache[name] = registry.get(decl.fn).fn(ctx, **decl.args)
    return cache[name]


def params_by_provider(source: Source, endpoint_name: str) -> dict[str, list[str]]:
    """Which params each provider has to fill, in the order the markers appear."""
    wanted: dict[str, list[str]] = {}
    for ref in source.endpoints[endpoint_name].markers():
        params = wanted.setdefault(ref.marker.provider, [])
        if ref.param is not None and ref.param not in params:
            params.append(ref.param)
    return wanted


def plan_endpoint(
    source: Source,
    endpoint_name: str,
    ctx: ProviderContext,
    cache: dict[str, list[dict[str, Any]]],
    limit: int | None,
) -> list[RequestSpec]:
    endpoint = source.endpoints[endpoint_name]
    groups: list[tuple[str, Sequence[Binding]]] = []
    for provider_name, params in params_by_provider(source, endpoint_name).items():
        rows = run_provider(source, provider_name, ctx, cache)
        capped = rows if limit is None else rows[:limit]
        groups.append((provider_name, bindings_for(provider_name, capped, params)))

    template = source.output_template(endpoint_name)
    specs: list[RequestSpec] = []
    for binding in combine(groups, endpoint.fan_out):
        specs.append(
            RequestSpec(
                source=source.source,
                endpoint=endpoint_name,
                method=endpoint.method,
                path=render(endpoint.path, binding.params),
                query=fill_markers(endpoint.query, None, binding.params),
                payload=fill_markers(endpoint.payload, None, binding.params),
                params=dict(binding.params),
                parents=binding.parents,
                output_template=template,
            )
        )
    return specs


def plan_order(source: Source, only: Sequence[str] = ()) -> tuple[str, ...]:
    return tuple(name for name in graph.topological_order(source) if not only or name in only)


def plan_one(
    source: Source,
    endpoint_name: str,
    ctx: ProviderContext,
    cache: dict[str, list[dict[str, Any]]],
    *,
    limit: int | None = None,
    no_limit: bool = False,
) -> EndpointPlan:
    """Plan a single endpoint, recording rather than raising if it cannot be planned.

    One endpoint that will not resolve — a missing spreadsheet, a parent that has not run
    — should not cost you the other nineteen.
    """
    try:
        requests = plan_endpoint(
            source,
            endpoint_name,
            ctx,
            cache,
            effective_limit(source, endpoint_name, limit, no_limit),
        )
    except Exception as exc:  # provider bodies are an I/O boundary
        return EndpointPlan(endpoint=endpoint_name, error=f"{type(exc).__name__}: {exc}")
    return EndpointPlan(endpoint=endpoint_name, requests=tuple(requests))


def build_plan(
    source: Source,
    ctx: ProviderContext,
    *,
    only: Sequence[str] = (),
    limit: int | None = None,
    no_limit: bool = False,
) -> Plan:
    """Expand every endpoint into requests, in dependency order.

    This plans everything up front, which is right for `--dry-run` and wrong for a real
    run: a chained endpoint can only be planned once its parent's envelopes exist. The
    runner therefore plans one endpoint at a time via `plan_one`. Here, a chained endpoint
    resolves against whatever is already on disk — which is exactly what a dry run should
    show you.
    """
    order = plan_order(source, only)
    cache: dict[str, list[dict[str, Any]]] = {}
    plans = [plan_one(source, name, ctx, cache, limit=limit, no_limit=no_limit) for name in order]
    return Plan(
        source=source.source,
        order=order,
        provider_rows={name: len(rows) for name, rows in sorted(cache.items())},
        endpoints=tuple(plans),
    )
