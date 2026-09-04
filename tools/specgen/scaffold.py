"""Write the first annotation file for a source, so nobody starts from a blank page.

Everything a scaffold can know, it reads off the source: the endpoint names, the order a
reader wants them in, which one drives the chain, which paginate. Everything it cannot
know is a `[À COMPLÉTER]` marker, because that is exactly the list `--check` then counts
down. What comes out passes every check on the first run and produces a complete document
with nothing in it yet — which is the honest starting point.

Optional blocks are written commented out. An absent optional produces no row in the
document (an absence is never announced), so a stub that silently became an empty section
would be worse than no stub at all — but a reader of this file still needs to know the
block exists.
"""

from __future__ import annotations

from datetime import date

import yaml

from api_extractor.config import graph
from api_extractor.config.models import Source
from specgen.annotation import Annotation
from specgen.labels import TODO

# Valid for any source, whatever its endpoints: `slug` and `page` are intrinsic, so these
# resolve everywhere and vary per request. `key` covers the endpoints that answer once,
# `key_paginated` those that walk pages — which is the only distinction that reliably
# changes a file name. Narrow them once you know what the platform wants.
_STEM = "source={source}/entity={endpoint}/extract_date={extract_date}/{slug}"
DEFAULT_KEY = f"{_STEM}.json"
DEFAULT_KEY_PAGINATED = f"{_STEM}_p{{page}}.json"


def build(source: Source, name: str, today: date | None = None) -> str:
    """A complete annotation file for `source`, as YAML text with its comments."""
    order = _order(source)
    lines = [
        f"# What config/sources/{name}.yaml cannot know, for the specification generator.",
        "#",
        f"#   python tools/build_spec.py {name} --check   # what is still missing",
        f"#   python tools/build_spec.py {name}           # write the document",
        "#",
        "# Anything the source YAML already says is an unknown key here and fails to parse.",
        f"# {TODO} is accepted wherever a string is, and --check counts what is left.",
        "# Prose is French: it lands in the generated document verbatim.",
        "",
        "spec:",
        '  version: "0.1"',
        "  status: draft",
        f'  date: "{(today or date.today()).strftime("%d/%m/%Y")}"',
        f"  source_system: {_scalar(TODO)}   # the vendor's name for the system, not {name!r}",
        f"  owner: {_scalar(TODO)}",
        f"  author: {_scalar(TODO)}",
        f"  implementation_team: {_scalar(TODO)}",
        f"  vendor_docs: {_scalar(TODO)}",
        "",
        f"secrets: {_scalar(TODO)}   # the vault's name and nothing else — this document circulates",
        "",
        "landing:",
        f"  bucket: {_scalar(TODO)}",
        "  prefix: raw",
        f"  key: {_scalar(DEFAULT_KEY)}",
        f"  key_paginated: {_scalar(DEFAULT_KEY_PAGINATED)}   # endpoints that walk pages",
        "",]
    lines += [
        "# A single endpoint may override both:",
        "#   endpoints:",
        "#     <name>:",
        f"#       key: {DEFAULT_KEY}",
        "",
        "links:                    # filled in once the files are published somewhere.",
        "  # Until then a pointer renders as the plain text it already is, and nothing breaks.",
        f"  workbook: {_scalar(TODO)}",
        f"  document: {_scalar(TODO)}",
        f"  samples: {_scalar(TODO)}",
        f"  vendor: {_scalar(TODO)}",
        "",
        "# definitions:            # domain terms as the vendor means them, not as you use them",
        '#   Actif: "…"',
        "",
        "# environments:           # only the ones the source YAML does not describe",
        "#   qa:",
        '#     base_url: "…"',
        '#     notes: "…"',
        "",
    ]
    lines.extend(_lists(source))
    lines.append("endpoints:")
    for endpoint in order:
        lines.append("")
        lines.extend(_endpoint(source, endpoint, indent="  "))
    return "\n".join(lines) + "\n"


def missing(source: Source, annotation: Annotation) -> str | None:
    """The fragment to paste when a source has grown past its annotation, or None.

    The recurring case: an endpoint is added to the YAML months later and `--check` fails
    on coverage. Rewriting the file would throw away the prose in it, so this prints only
    what is absent.
    """
    absent = [name for name in _order(source) if name not in annotation.endpoints]
    strays = sorted(name for name in source.providers if name not in annotation.lists)
    if not absent:
        return None
    lines = [f"# Add to config/specs/<source>.spec.yaml under `endpoints:` ({len(absent)} missing)"]
    for endpoint in absent:
        lines.append("")
        lines.extend(_endpoint(source, endpoint, indent="  "))
    if strays:
        lines.append("")
        lines.append(f"# Optional: a readable name for {', '.join(strays)} under `lists:`.")
    return "\n".join(lines) + "\n"


def _order(source: Source) -> list[str]:
    from specgen.model import document_order

    return document_order(source)


def _lists(source: Source) -> list[str]:
    if not source.providers:
        return []
    lines = [
        "# lists:                  # a readable name for a parameter list, where the derived",
        "#                         # phrase reads badly as a heading",
    ]
    lines.extend(f'#   {_key(name)}: {{ name: "…" }}' for name in source.providers)
    lines.append("")
    return lines


def _endpoint(source: Source, name: str, indent: str) -> list[str]:
    lines = [f"{indent}# {_hint(source, name)}", f"{indent}{_key(name)}:"]
    for field in ("purpose", "record_grain", "mode", "rationale"):
        lines.append(f"{indent}  {field}: {_scalar(TODO)}")
    lines.append(f"{indent}  # quirks:              # observed behaviour the vendor never documented")
    lines.append(f'{indent}  #   - "…"')
    return lines


def _hint(source: Source, name: str) -> str:
    """What the source already tells a reader about this endpoint, in one line."""
    endpoint = source.endpoints[name]
    deps = graph.known(graph.dependencies(source))
    dependents = sorted(other for other in deps if name in deps[other])
    notes = []
    if dependents:
        notes.append(f"drives {', '.join(dependents)}")
    if deps[name]:
        notes.append(f"runs after {', '.join(sorted(deps[name]))}")
    if endpoint.paginate is not None:
        notes.append("paginated")
    if not endpoint.markers():
        notes.append("no parameters")
    return f"{endpoint.method} {endpoint.path}" + (f" — {'; '.join(notes)}" if notes else "")


def _key(name: str) -> str:
    """A mapping key, quoted only when YAML would misread it."""
    dumped = yaml.safe_dump(name, default_flow_style=True).strip().rstrip("...").strip()
    return dumped


def _scalar(value: str) -> str:
    return yaml.safe_dump(value, allow_unicode=True, default_flow_style=True).strip().rstrip("...").strip()
