"""`--check`: every problem with a specification, reported in one pass.

Same shape as `config/validate.py`, for the same reason: a check that silently failed to
register would make validation pass vacuously, so the registry is asserted against the
declared set before anything runs, and no check short-circuits the others.

Messages are English, ASCII, and name the location in the annotation or the model. The
completion marker itself is counted, never printed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from api_extractor.config.loader import iter_env_refs
from api_extractor.config.models import ENV_PREFIX, Source, placeholders
from api_extractor.config.validate import INTRINSIC_OUTPUT_KEYS, Issue, endpoint_params
from api_extractor.logs import register_secret, scrub
from api_extractor.persist import envelope
from specgen import contract, evidence as ev, labels
from specgen.annotation import LANDING_KEYS, Annotation, iter_strings
from specgen.model import Built


@dataclass(frozen=True)
class Context:
    source: Source
    annotation: Annotation
    annotation_raw: Any
    built: Built
    evidence: ev.Evidence
    template: Path | None = None
    template_available: bool = False
    allow_partial: bool = False
    min_complete: float = 0.0


@dataclass(frozen=True)
class Report:
    checks_run: tuple[str, ...]
    issues: tuple[Issue, ...]
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues


Finding = tuple[str, str]
CheckFn = Callable[[Context], list[Finding]]

_CHECKS: dict[str, CheckFn] = {}
_NOTES: list[str] = []

EXPECTED_CHECK_IDS = frozenset(
    {
        "spec.endpoints.coverage",
        "spec.endpoints.stale",
        "spec.lists.stale",
        "spec.landing.key_resolvable",
        "spec.landing.key_page",
        "spec.landing.key_page_stray",
        "spec.landing.key_distinguishes",
        "spec.secrets.no_env_refs",
        "spec.secrets.no_leak",
        "spec.contract.matches_envelope",
        "spec.samples.current",
        "spec.completeness",
        "spec.template",
    }
)


def check(check_id: str) -> Callable[[CheckFn], CheckFn]:
    def register(fn: CheckFn) -> CheckFn:
        if check_id in _CHECKS:
            raise RuntimeError(f"duplicate check id {check_id!r}")
        _CHECKS[check_id] = fn
        return fn

    return register


def assert_registry() -> None:
    registered = frozenset(_CHECKS)
    if registered != EXPECTED_CHECK_IDS:
        raise RuntimeError(
            f"check registry does not match EXPECTED_CHECK_IDS "
            f"(missing: {sorted(EXPECTED_CHECK_IDS - registered)}, "
            f"unexpected: {sorted(registered - EXPECTED_CHECK_IDS)})"
        )


def note(text: str) -> None:
    """Something worth printing that is not a failure: a skipped scan, a count."""
    _NOTES.append(text)


# --- checks -------------------------------------------------------------------------


@check("spec.endpoints.coverage")
def _coverage(ctx: Context) -> list[Finding]:
    return [
        ("endpoints", f"endpoint {name!r} has no annotation — add `endpoints.{name}`")
        for name in ctx.source.endpoints
        if name not in ctx.annotation.endpoints
    ]


@check("spec.endpoints.stale")
def _stale(ctx: Context) -> list[Finding]:
    declared = ", ".join(sorted(ctx.source.endpoints))
    return [
        (f"endpoints.{name}", f"annotates an endpoint the source does not declare (declared: {declared})")
        for name in ctx.annotation.endpoints
        if name not in ctx.source.endpoints
    ]


@check("spec.lists.stale")
def _lists_stale(ctx: Context) -> list[Finding]:
    declared = ", ".join(sorted(ctx.source.providers)) or "none"
    return [
        (f"lists.{name}", f"names a provider the source does not declare (declared: {declared})")
        for name in ctx.annotation.lists
        if name not in ctx.source.providers
    ]


def _effective_keys(ctx: Context) -> list[tuple[str, str, str]]:
    """(loc, endpoint, key template) for every endpoint, whichever of the three applies.

    `loc` names the line a reader should go and edit, which is the endpoint's own key,
    the paginated default, or the base one.
    """
    out = []
    for name, endpoint in ctx.source.endpoints.items():
        paginated = endpoint.paginate is not None
        if ctx.annotation.endpoint(name).key:
            loc = f"endpoints.{name}.key"
        elif paginated and ctx.annotation.landing.key_paginated:
            loc = "landing.key_paginated"
        else:
            loc = "landing.key"
        out.append((loc, name, ctx.annotation.landing_key(name, paginated=paginated)))
    return out


@check("spec.landing.key_resolvable")
def _key_resolvable(ctx: Context) -> list[Finding]:
    """A placeholder the endpoint cannot fill renders empty — and every file of that
    endpoint then shares the collapsed key. Reuses the extractor's own placeholder rule."""
    findings = []
    for loc, name, key in _effective_keys(ctx):
        allowed = endpoint_params(ctx.source.endpoints[name]) | INTRINSIC_OUTPUT_KEYS | LANDING_KEYS
        for placeholder in placeholders(key):
            if placeholder not in allowed:
                findings.append(
                    (
                        loc,
                        f"{{{placeholder}}} does not resolve for endpoint {name!r} — "
                        f"available: {', '.join(sorted(allowed))}",
                    )
                )
    return findings


@check("spec.landing.key_page")
def _key_page(ctx: Context) -> list[Finding]:
    return [
        (loc, f"{key!r} has no {{page}}, so every page of paginated endpoint {name!r} would overwrite the last")
        for loc, name, key in _effective_keys(ctx)
        if ctx.source.endpoints[name].paginate is not None and "page" not in placeholders(key)
    ]


@check("spec.landing.key_page_stray")
def _key_page_stray(ctx: Context) -> list[Finding]:
    """The mirror of `key_page`. An endpoint that answers once renders `_p0` forever, and
    the key then claims a pagination the API does not have — which a Bronze loader may
    well believe."""
    return [
        (
            loc,
            f"{key!r} carries {{page}} but endpoint {name!r} does not paginate — every file "
            f"would land as page 0 and the key would claim a pagination the API does not have",
        )
        for loc, name, key in _effective_keys(ctx)
        if ctx.source.endpoints[name].paginate is None and "page" in placeholders(key)
    ]


@check("spec.landing.key_distinguishes")
def _key_distinguishes(ctx: Context) -> list[Finding]:
    findings = []
    for loc, name, key in _effective_keys(ctx):
        params = endpoint_params(ctx.source.endpoints[name])
        used = set(placeholders(key))
        if params and "slug" not in used and not (used & params):
            findings.append(
                (
                    loc,
                    f"{key!r} contains neither {{slug}} nor a parameter of {name!r} "
                    f"({', '.join(sorted(params))}), so every request's file would land on one key",
                )
            )
    return findings


@check("spec.secrets.no_env_refs")
def _no_env_refs(ctx: Context) -> list[Finding]:
    """The annotation carries a vault name and nothing else about credentials."""
    return [
        (loc, f"looks like an env reference ({text[:20]!r}) — the annotation names the vault only")
        for loc, text in iter_strings(ctx.annotation_raw)
        if text.startswith(ENV_PREFIX)
    ]


@check("spec.secrets.no_leak")
def _no_leak(ctx: Context) -> list[Finding]:
    """Second line of defence. The first is construction: the model is built from the
    config layer, which never resolves `env:`. This scans anyway, the way the log
    scrubber does, because the places you check are never the places that leak."""
    unset = []
    for _loc, var in iter_env_refs(ctx.source):
        value = os.environ.get(var)
        if value is None:
            unset.append(var)
        else:
            register_secret(value, name=var)
    if unset:
        note(f"secrets scan: {', '.join(sorted(set(unset)))} not set in the environment, so their values could not be scanned for")
    findings = []
    text = json.dumps(ctx.built.model, ensure_ascii=False)
    if scrub(text) != text:
        findings.append(("model", "a registered secret value appears in the model"))
    for file_name, document in ctx.built.samples.items():
        text = json.dumps(document, ensure_ascii=False)
        if scrub(text) != text:
            findings.append((f"samples/{file_name}", "a registered secret value appears in this sample"))
    return findings


@check("spec.contract.matches_envelope")
def _contract(ctx: Context) -> list[Finding]:
    """The same comparison as tests/test_spec_contract.py, so a CI job without pytest
    still catches a description that drifted from `envelope.build()`."""
    findings = []
    for built in contract.probe():
        seen = contract.paths(built)
        for missing in sorted(seen - contract.DECLARED):
            findings.append(("contract", f"the envelope writes {missing!r}, which the contract does not describe"))
        for extra in sorted(contract.DECLARED - seen):
            if extra == "body_raw" and "body_raw" not in built:
                continue  # conditional: only the non-JSON probe carries it
            findings.append(("contract", f"the contract describes {extra!r}, which the envelope no longer writes"))
        for attribute in contract.ATTRIBUTES:
            if attribute.path in seen and not contract.type_matches(attribute, contract.value_at(built, attribute.path)):
                findings.append(("contract", f"{attribute.path} is not of the declared type {attribute.type!r}"))
    return sorted(set(findings))


@check("spec.samples.current")
def _samples_current(ctx: Context) -> list[Finding]:
    """Envelopes from before the contract change are evidence, not samples."""
    findings = []
    for name in ctx.source.endpoints:
        on_disk = ctx.evidence.for_endpoint(name)
        if on_disk and not any(ev.is_current(saved) for saved in on_disk):
            findings.append(
                (
                    f"output/{name}",
                    f"{len(on_disk)} envelope(s) on disk, none written by the current contract — "
                    f"re-run `run {ctx.source.source} --endpoint {name} --force`",
                )
            )
    return findings


@check("spec.completeness")
def _completeness(ctx: Context) -> list[Finding]:
    for endpoint in ctx.built.model["endpoints"]:
        if endpoint["provider_error"]:
            note(f"providers: {endpoint['name']} could not resolve its parameters ({endpoint['provider_error']}); its example key uses placeholders")
    done = ctx.built.model["completeness"]
    note(f"completion: {done['filled']} slot(s) answered, {done['todo']} marker(s) left ({done['percent']}% complete)")
    for loc in done["locations"]:
        note(f"  marker at {loc}")
    if done["percent"] < ctx.min_complete:
        return [("completeness", f"{done['percent']}% complete, below the required {ctx.min_complete:g}%")]
    return []


@check("spec.template")
def _template(ctx: Context) -> list[Finding]:
    if ctx.template is None:
        note("template: none found, so no .docx will be rendered")
        return []
    if not ctx.template_available:
        note(f"template: {ctx.template} not checked (docxtpl is not installed; see requirements-docs.txt)")
        return []
    from specgen import template

    note(f"template: {ctx.template}")
    return template.check(ctx.template, ctx.built.model, allow_partial=ctx.allow_partial)


# --- runner -------------------------------------------------------------------------


def run(ctx: Context) -> Report:
    assert_registry()
    _NOTES.clear()
    issues: list[Issue] = []
    for check_id in sorted(_CHECKS):
        issues.extend(Issue(check=check_id, loc=loc, message=message) for loc, message in _CHECKS[check_id](ctx))
    return Report(checks_run=tuple(sorted(_CHECKS)), issues=tuple(issues), notes=tuple(_NOTES))
