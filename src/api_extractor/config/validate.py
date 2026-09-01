"""Cross-reference validation, reported all at once.

Failing on request 400 of 900 because a provider name was misspelled is the exact outcome
this prevents, so no check short-circuits the others.

Every check registers under an id, and `EXPECTED_CHECK_IDS` declares the set that must be
present. A check that silently fails to register would make validation pass vacuously,
which is worse than not having the check — so the registry is asserted against the
declared set before anything runs.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from api_extractor.config import graph
from api_extractor.config.loader import iter_env_refs, load_source
from api_extractor.config.models import Endpoint, Source, is_reserved, iter_from_dicts, placeholders
from api_extractor.providers import registry

PARSE_CHECK_ID = "config.parse"

# Placeholders an output template may use beyond the endpoint's own params.
INTRINSIC_OUTPUT_KEYS = frozenset({"source", "endpoint", "page", "slug"})


@dataclass(frozen=True)
class Issue:
    check: str
    loc: str
    message: str

    def __str__(self) -> str:
        return f"{self.loc}: {self.message}  [{self.check}]"


@dataclass(frozen=True)
class Report:
    path: Path
    checks_run: tuple[str, ...]
    checks_deferred: tuple[str, ...]
    issues: tuple[Issue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues


# (loc, message) pairs; the runner attaches the check id.
Finding = tuple[str, str]
CheckFn = Callable[[Source], list[Finding]]

_CHECKS: dict[str, CheckFn] = {}

EXPECTED_CHECK_IDS = frozenset(
    {
        "config.providers.declared",
        "config.providers.fn_registered",
        "config.providers.args_match",
        "config.providers.depends_on_targets",
        "config.dag.acyclic",
        "config.label.shapes_output",
        "config.markers.malformed",
        "config.params.no_collision",
        "config.path.bind_match",
        "config.output.template_resolvable",
        "config.reserved.namespace",
        "config.paginate.target",
        "config.paginate.output_page",
        "config.env.vars_set",
    }
)

# Checks a later phase will add. Reported by `--show-checks` so that partial coverage
# reads as planned rather than as full coverage. Empty since phase 2: config validation
# is complete, because `depends_on` made the dependency graph a config-layer artifact.
DEFERRED_CHECKS: dict[str, str] = {}


def check(check_id: str) -> Callable[[CheckFn], CheckFn]:
    def register(fn: CheckFn) -> CheckFn:
        if check_id in _CHECKS:
            raise RuntimeError(f"duplicate check id {check_id!r}")
        _CHECKS[check_id] = fn
        return fn

    return register


def assert_registry() -> None:
    """Guard against a check that silently failed to register."""
    registered = frozenset(_CHECKS)
    if registered != EXPECTED_CHECK_IDS:
        missing = sorted(EXPECTED_CHECK_IDS - registered)
        unexpected = sorted(registered - EXPECTED_CHECK_IDS)
        raise RuntimeError(
            f"check registry does not match EXPECTED_CHECK_IDS "
            f"(missing: {missing}, unexpected: {unexpected})"
        )


def endpoint_params(endpoint: Endpoint) -> set[str]:
    return {ref.param for ref in endpoint.markers() if ref.param is not None}


# --- checks -------------------------------------------------------------------------


@check("config.providers.declared")
def _providers_declared(source: Source) -> list[Finding]:
    known = sorted(source.providers)
    findings: list[Finding] = []
    for name, endpoint in source.endpoints.items():
        for ref in endpoint.markers():
            if ref.marker.provider not in source.providers:
                findings.append(
                    (
                        f"endpoints.{name}.{ref.loc}",
                        f"{{from: {ref.marker.provider}}} is not a declared provider "
                        f"(declared: {', '.join(known) or 'none'})",
                    )
                )
    return findings


@check("config.providers.fn_registered")
def _providers_fn_registered(source: Source) -> list[Finding]:
    available = ", ".join(entry.name for entry in registry.registered())
    return [
        (
            f"providers.{name}.fn",
            f"{decl.fn!r} is not a registered provider function (registered: {available})",
        )
        for name, decl in source.providers.items()
        if not registry.is_registered(decl.fn)
    ]


@check("config.providers.args_match")
def _providers_args_match(source: Source) -> list[Finding]:
    """YAML args must fit the function they are passed to, before anything calls it."""
    findings: list[Finding] = []
    for name, decl in source.providers.items():
        if not registry.is_registered(decl.fn):
            continue
        entry = registry.get(decl.fn)
        error = entry.signature_error(decl.args)
        if error is not None:
            findings.append(
                (
                    f"providers.{name}.args",
                    f"do not fit {decl.fn}({', '.join(entry.arg_names())}): {error}",
                )
            )
    return findings


@check("config.providers.depends_on_targets")
def _depends_on_targets(source: Source) -> list[Finding]:
    """Whatever endpoint a provider says it chains off has to exist."""
    declared = ", ".join(sorted(source.endpoints))
    return [
        (
            f"providers.{name}.args",
            f"chains off endpoint {target!r}, which is not declared (declared: {declared})",
        )
        for name in source.providers
        for target in graph.provider_dependencies(source, name)
        if target not in source.endpoints
    ]


@check("config.dag.acyclic")
def _dag_acyclic(source: Source) -> list[Finding]:
    cycle = graph.find_cycle(graph.known(graph.dependencies(source)))
    if cycle is None:
        return []
    return [("endpoints", f"dependency cycle: {' -> '.join(cycle)}")]


@check("config.markers.malformed")
def _markers_malformed(source: Source) -> list[Finding]:
    """Catch a typo'd marker (`{from: x, az: y}`) that fell through to a literal dict.

    A literal `from` value that happens to name a declared provider is not a coincidence.
    """
    findings: list[Finding] = []
    for name, endpoint in source.endpoints.items():
        trees = (("query", endpoint.query), ("payload", endpoint.payload))
        for root, tree in trees:
            for loc, node in iter_from_dicts(tree, root):
                value = node.get("from")
                if isinstance(value, str) and value in source.providers:
                    extra = sorted(set(node) - {"from", "as"})
                    findings.append(
                        (
                            f"endpoints.{name}.{loc}",
                            f"looks like a {{from: {value}}} marker but has unexpected "
                            f"keys {extra} — a marker takes only `from` and `as`",
                        )
                    )
    return findings


@check("config.params.no_collision")
def _params_no_collision(source: Source) -> list[Finding]:
    findings: list[Finding] = []
    for name, endpoint in source.endpoints.items():
        by_param: dict[str, list[str]] = {}
        for ref in endpoint.markers():
            if ref.param is None:
                findings.append(
                    (
                        f"endpoints.{name}.{ref.loc}",
                        "marker has no enclosing key to name it — add `as: <name>`",
                    )
                )
                continue
            by_param.setdefault(ref.param, []).append(ref.loc)
        for param, locs in by_param.items():
            if len(locs) > 1:
                findings.append(
                    (
                        f"endpoints.{name}",
                        f"two markers both resolve to param {param!r} ({', '.join(locs)}) — "
                        f"disambiguate one with `as`",
                    )
                )
    return findings


@check("config.path.bind_match")
def _path_bind_match(source: Source) -> list[Finding]:
    findings: list[Finding] = []
    for name, endpoint in source.endpoints.items():
        in_path = set(placeholders(endpoint.path))
        bound = set(endpoint.bind)
        for missing in sorted(in_path - bound):
            findings.append(
                (
                    f"endpoints.{name}.path",
                    f"{{{missing}}} in `path` has no matching entry in `bind`",
                )
            )
        for unused in sorted(bound - in_path):
            findings.append(
                (
                    f"endpoints.{name}.bind.{unused}",
                    f"bound but `path` has no {{{unused}}} placeholder",
                )
            )
    return findings


@check("config.label.shapes_output")
def _label_shapes_output(source: Source) -> list[Finding]:
    """A label is never sent, so one absent from `output` does nothing at all.

    Everything else in an endpoint has a visible effect if you get it wrong. A label that
    shapes no path is the one piece of config that can look meaningful and do nothing, so
    it is an error rather than something to notice months later.
    """
    findings: list[Finding] = []
    for name, endpoint in source.endpoints.items():
        template = source.output_template(name)
        used = set(placeholders(template))
        for key in endpoint.label:
            if key not in used:
                findings.append(
                    (
                        f"endpoints.{name}.label.{key}",
                        f"does not appear in `output` ({template!r}) — a label is never "
                        f"sent, so one that shapes no path has no effect",
                    )
                )
    return findings


@check("config.output.template_resolvable")
def _output_template_resolvable(source: Source) -> list[Finding]:
    findings: list[Finding] = []
    for name, endpoint in source.endpoints.items():
        template = source.output_template(name)
        allowed = endpoint_params(endpoint) | INTRINSIC_OUTPUT_KEYS
        for key in placeholders(template):
            if key not in allowed:
                findings.append(
                    (
                        f"endpoints.{name}.output",
                        f"{{{key}}} does not resolve — available: {', '.join(sorted(allowed))}",
                    )
                )
    return findings


@check("config.reserved.namespace")
def _reserved_namespace(source: Source) -> list[Finding]:
    findings: list[Finding] = []
    for name, endpoint in source.endpoints.items():
        for ref in endpoint.markers():
            if ref.marker.alias is not None and is_reserved(ref.marker.alias):
                findings.append(
                    (
                        f"endpoints.{name}.{ref.loc}",
                        f"`as: {ref.marker.alias}` is reserved — the __name__ namespace "
                        f"belongs to runner-injected provenance",
                    )
                )
        for block, keys in (("bind", endpoint.bind), ("label", endpoint.label)):
            for key in keys:
                if is_reserved(key):
                    findings.append(
                        (
                            f"endpoints.{name}.{block}.{key}",
                            f"{block} key is in the reserved __name__ namespace",
                        )
                    )
        for key in placeholders(source.output_template(name)):
            if is_reserved(key):
                findings.append(
                    (
                        f"endpoints.{name}.output",
                        f"{{{key}}} is in the reserved __name__ namespace",
                    )
                )
    return findings


@check("config.paginate.target")
def _paginate_target(source: Source) -> list[Finding]:
    """The cursor has to land somewhere real, and not on top of something else."""
    findings: list[Finding] = []
    for name, endpoint in source.endpoints.items():
        paginate = endpoint.paginate
        if paginate is None:
            continue
        loc = f"endpoints.{name}.paginate.at"
        if paginate.at_root == "payload" and endpoint.payload is None:
            findings.append(
                (loc, f"{paginate.at!r} targets the payload, but this endpoint has none")
            )
        if paginate.at_root == "query" and len(paginate.at_keys) > 1:
            findings.append(
                (loc, f"{paginate.at!r} nests a query param — only a payload can be nested")
            )
        for ref in endpoint.markers():
            if ref.loc == paginate.at:
                findings.append(
                    (
                        loc,
                        f"{paginate.at!r} already holds a {{from: {ref.marker.provider}}} "
                        f"marker — the page cursor would overwrite it",
                    )
                )
    return findings


@check("config.paginate.output_page")
def _paginate_output_page(source: Source) -> list[Finding]:
    """One file per page means the path has to vary by page."""
    return [
        (
            f"endpoints.{name}.output",
            f"{source.output_template(name)!r} has no {{page}}, so every page of this "
            f"paginated endpoint would overwrite the last",
        )
        for name, endpoint in source.endpoints.items()
        if endpoint.paginate is not None
        and "page" not in placeholders(source.output_template(name))
    ]


@check("config.env.vars_set")
def _env_vars_set(source: Source) -> list[Finding]:
    return [
        (loc, f"environment variable {var} is not set")
        for loc, var in iter_env_refs(source)
        if var not in os.environ
    ]


# --- runner -------------------------------------------------------------------------


def run_checks(source: Source) -> tuple[tuple[str, ...], tuple[Issue, ...]]:
    assert_registry()
    issues: list[Issue] = []
    for check_id in sorted(_CHECKS):
        issues.extend(
            Issue(check=check_id, loc=loc, message=message)
            for loc, message in _CHECKS[check_id](source)
        )
    return tuple(sorted(_CHECKS)), tuple(issues)


def validate_source(path: Path) -> Report:
    """Parse and check a source file, collecting every problem in one pass.

    A parse failure stops the cross-reference checks — they have nothing to run against —
    but pydantic still reports every shape error it found, not just the first.
    """
    deferred = tuple(sorted(DEFERRED_CHECKS))
    try:
        source = load_source(path)
    except OSError as exc:
        issue = Issue(PARSE_CHECK_ID, str(path), f"cannot read file: {exc}")
        return Report(path, (PARSE_CHECK_ID,), deferred, (issue,))
    except yaml.YAMLError as exc:
        issue = Issue(PARSE_CHECK_ID, str(path), f"invalid YAML: {exc}")
        return Report(path, (PARSE_CHECK_ID,), deferred, (issue,))
    except ValidationError as exc:
        issues = tuple(
            Issue(
                check=PARSE_CHECK_ID,
                loc=".".join(str(part) for part in error["loc"]) or str(path),
                message=error["msg"],
            )
            for error in exc.errors()
        )
        return Report(path, (PARSE_CHECK_ID,), deferred, issues)
    checks_run, issues = run_checks(source)
    return Report(path, (PARSE_CHECK_ID, *checks_run), deferred, issues)
