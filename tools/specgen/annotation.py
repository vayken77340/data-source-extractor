"""The annotation file: what the source YAML cannot know.

`config/specs/<name>.spec.yaml` holds the human knowledge — what an endpoint is *for*, what
one record means, what was observed that the vendor never wrote down, where the files
land. Its one design rule is that it never restates the source YAML: any field the
generator can read elsewhere is an unknown key here, and `extra="forbid"` makes that
mechanical rather than a matter of discipline.

Keys are English identifiers matched against endpoint and provider names; values that a
reader will see are French. `[À COMPLÉTER]` is accepted anywhere a string is, including in
place of an enum, and `--check` counts every one that survives into the document.

Two kinds of absence, stated per field:

- *structural* — the document has a slot for it, so an absence renders `[À COMPLÉTER]` and
  counts against completeness. These default to `TODO`.
- *optional* — an absence produces no row, no section, no sentence. These default to
  `None` or an empty collection.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from specgen.labels import TODO

SPECS_ROOT = Path("config/specs")

# Only the mode values are enumerated here; their French labels live in LABELS.yaml.
MODES = ("full", "incremental")

DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")

# Placeholders a landing key may use that are neither intrinsic to the extractor nor an
# endpoint parameter: the external pipeline generates them at run start.
LANDING_KEYS = frozenset({"extract_date"})


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HistoryEntry(_Base):
    version: str
    date: str
    author: str = TODO
    summary: str = TODO

    @field_validator("date")
    @classmethod
    def _date(cls, value: str) -> str:
        return check_date(value)


class SpecBlock(_Base):
    """Document control. `source_system` is a display name; `source:` in the YAML is a
    partition value and is never shown as a name."""

    version: str
    status: Literal["draft", "in_review", "approved"]
    date: str
    source_system: str
    owner: str = TODO  # structural
    author: str = TODO  # structural
    implementation_team: str = TODO  # structural
    reviewers: str | None = None  # optional
    vendor_docs: str = TODO  # structural: a URL the document must carry
    history: list[HistoryEntry] = Field(default_factory=list)  # optional; derived when empty

    @field_validator("date")
    @classmethod
    def _date(cls, value: str) -> str:
        return check_date(value)


class Landing(_Base):
    """Where files land. Belongs to the target platform, which is why it is not in the
    source YAML: the extractor writes under `output/` and knows nothing of S3.

    Two defaults rather than one, because whether an endpoint paginates is the only thing
    that reliably changes the shape of its file name. A single key carrying `{page}`
    renders `_p0` on every endpoint that answers once, which tells the receiving team the
    API pages when it does not; a single key without it silently overwrites every page but
    the last. `key_paginated` applies to the endpoints that walk, `key` to the rest.
    """

    key: str
    key_paginated: str | None = None  # optional; `key` covers everything when absent
    bucket: str = TODO  # structural
    prefix: str | None = None  # optional
    encryption: str | None = None  # optional


class Environment(_Base):
    """Only the environments the source YAML does not describe: production is `base_url`."""

    base_url: str = TODO
    notes: str | None = None


class ListNote(_Base):
    """A human name and meaning for a parameter list. The mechanics come from the YAML;
    the derived phrase is correct but clumsy as a heading, so a name may replace it."""

    name: str | None = None
    note: str | None = None


class ResponseNote(_Base):
    root: str | None = None  # derived from a sample envelope when absent
    nested: str | None = None  # optional prose


class Sample(_Base):
    redact: list[str] = Field(default_factory=list)  # JSONPaths into body, masked
    exclude: bool = False  # keep this endpoint out of Annexe A


class EndpointAnnotation(_Base):
    purpose: str = TODO  # structural
    record_grain: str = TODO  # structural: the single most misread fact downstream
    mode: Literal["full", "incremental", "[À COMPLÉTER]"] = TODO  # structural
    rationale: str = TODO  # structural
    quirks: list[str] = Field(default_factory=list)  # optional
    auth_scope: str | None = None  # optional
    vendor_ref: str | None = None  # optional
    response: ResponseNote | None = None  # optional, partly derivable
    key: str | None = None  # optional per-endpoint landing key override
    sample: Sample | None = None  # optional


class Annotation(_Base):
    spec: SpecBlock
    secrets: str  # the vault's name and nothing else — the document circulates widely
    landing: Landing
    environments: dict[str, Environment] = Field(default_factory=dict)
    definitions: dict[str, str] = Field(default_factory=dict)
    lists: dict[str, ListNote] = Field(default_factory=dict)
    endpoints: dict[str, EndpointAnnotation] = Field(default_factory=dict)

    def endpoint(self, name: str) -> EndpointAnnotation:
        """The annotation for an endpoint, or an all-`TODO` one when it has none.

        Coverage is a `--check` failure, not a crash: the model still builds so that the
        rest of the document can be reviewed while that entry is being written.
        """
        return self.endpoints.get(name) or EndpointAnnotation()

    def landing_key(self, name: str, *, paginated: bool = False) -> str:
        """The key template for one endpoint. Most specific first: its own `key`, then
        `key_paginated` when it walks pages, then `key`."""
        override = self.endpoint(name).key
        if override:
            return override
        if paginated and self.landing.key_paginated:
            return self.landing.key_paginated
        return self.landing.key


def check_date(value: str) -> str:
    if value != TODO and not DATE_RE.match(value):
        raise ValueError(f"expected JJ/MM/AAAA, got {value!r}")
    return value


def spec_path(name: str, root: Path = SPECS_ROOT) -> Path:
    return root / f"{name}.spec.yaml"


def read_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_annotation(path: Path) -> Annotation:
    """Read and parse an annotation file. Raises pydantic.ValidationError on a bad shape."""
    return Annotation.model_validate(read_yaml(path))


def iter_strings(node: Any, loc: str = "") -> list[tuple[str, str]]:
    """Every string leaf with its dotted location, for the checks that scan text."""
    found: list[tuple[str, str]] = []
    if isinstance(node, str):
        found.append((loc, node))
    elif isinstance(node, dict):
        for key, value in node.items():
            found.extend(iter_strings(value, f"{loc}.{key}" if loc else str(key)))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found.extend(iter_strings(value, f"{loc}[{i}]"))
    return found
