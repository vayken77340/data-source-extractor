"""Pydantic models for a YAML source definition.

Shape-level rules live here as validators: marker syntax, the pagination `at` path, secret
templates, enums. Cross-reference rules ("does this provider exist", "is this env var set")
live in `validate.py` instead, so every failure can be collected and reported in one pass.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

ENV_PREFIX = "env:"
PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
RESERVED_RE = re.compile(r"^__.+__$")

# A `{from: x}` marker is exactly these key sets and nothing else. A dict that merely
# contains a `from` key (a date range, say) stays a literal — see the
# config.markers.malformed check for how a typo'd marker is caught.
_MARKER_KEY_SETS = ({"from"}, {"from", "as"})

# `__name__` keys are reserved for out-of-band metadata providers attach to their rows
# (`__parents__`, later `__index__`, `__page__`). The runner strips every `__key__` before
# the row becomes request params, so config may never name one.


def placeholders(template: str) -> list[str]:
    """Return the `{name}` placeholders in a path or output template, in order."""
    return PLACEHOLDER_RE.findall(template)


def is_reserved(name: str) -> bool:
    return bool(RESERVED_RE.match(name))


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FromMarker(_Base):
    """`{from: provider}` — the only special marker in query, payload and bind.

    `as` overrides the parameter name, which is otherwise the nearest enclosing mapping
    key. Needed where that key is useless (`filters: [{key: assetType, value: {from: x}}]`
    would key on `value`) and to break a collision between two markers.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    provider: str = Field(alias="from")
    alias: str | None = Field(default=None, alias="as")


@dataclass(frozen=True)
class MarkerRef:
    """A marker plus where it was found, for error messages and param naming."""

    marker: FromMarker
    loc: str
    key: str | None

    @property
    def param(self) -> str | None:
        """The param name this marker's values land under; None when unnameable."""
        return self.marker.alias or self.key


def is_marker(node: Any) -> bool:
    return (
        isinstance(node, dict)
        and set(node) in _MARKER_KEY_SETS
        and all(isinstance(value, str) for value in node.values())
    )


def _substitute(node: Any, loc: str) -> Any:
    """Replace marker-shaped dicts with FromMarker instances at any nesting depth."""
    if is_marker(node):
        return FromMarker.model_validate(node)
    if isinstance(node, dict):
        return {key: _substitute(value, f"{loc}.{key}") for key, value in node.items()}
    if isinstance(node, list):
        return [_substitute(value, f"{loc}[{i}]") for i, value in enumerate(node)]
    return node


def _iter_markers(node: Any, loc: str, key: str | None) -> Iterator[MarkerRef]:
    if isinstance(node, FromMarker):
        yield MarkerRef(node, loc, key)
    elif isinstance(node, dict):
        for child_key, value in node.items():
            yield from _iter_markers(value, f"{loc}.{child_key}", child_key)
    elif isinstance(node, list):
        # A list does not rename anything: `values: [{from: x}]` keys on `values`.
        for i, value in enumerate(node):
            yield from _iter_markers(value, f"{loc}[{i}]", key)


def iter_from_dicts(node: Any, loc: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield literal dicts that still carry a `from` key after marker substitution."""
    if isinstance(node, FromMarker):
        return
    if isinstance(node, dict):
        if "from" in node:
            yield loc, node
        for key, value in node.items():
            yield from iter_from_dicts(value, f"{loc}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from iter_from_dicts(value, f"{loc}[{i}]")


PAGINATE_ROOTS = ("query", "payload")


class Paginate(_Base):
    """A pagination walk.

    Only the page cursor is described here. Page *size* is not a pagination concept — it
    is an ordinary literal in `query` or `payload`, sitting wherever the API wants it.

    `at` is a dotted path rooted at `query.` or `payload.`, so the cursor lands either in a
    query param (`query.page`) or at any depth inside a JSON body
    (`payload.pageLink.page`). `defaults.max_pages` caps the walk whatever the stop
    condition says.
    """

    style: Literal["page_number"]
    at: str
    start: int = 0
    has_more: str | None = None  # JSONPath; falsy stops. Absent: stop on an empty page.

    @model_validator(mode="after")
    def _check_at(self) -> Paginate:
        parts = self.at.split(".")
        if len(parts) < 2 or parts[0] not in PAGINATE_ROOTS or not all(parts):
            raise ValueError(
                f"`at` must be a dotted path rooted at 'query.' or 'payload.' — got {self.at!r}"
            )
        return self

    @property
    def at_root(self) -> str:
        """Which half of the request the cursor lands in: `query` or `payload`."""
        return self.at.split(".")[0]

    @property
    def at_keys(self) -> tuple[str, ...]:
        """The keys under that root, outermost first."""
        return tuple(self.at.split(".")[1:])


class Endpoint(_Base):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    query: dict[str, Any] = Field(default_factory=dict)
    payload: Any = None
    bind: dict[str, FromMarker] = Field(default_factory=dict)
    label: dict[str, FromMarker] = Field(default_factory=dict)
    fan_out: Literal["product", "zip"] = "product"
    paginate: Paginate | None = None
    limit: int | None = None
    output: str | None = None

    @model_validator(mode="after")
    def _resolve_markers(self) -> Endpoint:
        self.query = _substitute(self.query, "query")
        self.payload = _substitute(self.payload, "payload")
        for key, marker in self.bind.items():
            if marker.alias is not None:
                raise ValueError(
                    f"bind.{key}: `as` is not allowed in bind — the key already names the "
                    f"parameter and must match the {{{key}}} placeholder in `path`"
                )
        for key, marker in self.label.items():
            if marker.alias is not None:
                raise ValueError(
                    f"label.{key}: `as` is not allowed in label — the key already names the "
                    f"parameter and must match the {{{key}}} placeholder in `output`"
                )
        return self

    def markers(self) -> list[MarkerRef]:
        """Every `{from:}` marker in this endpoint, wherever it sits.

        `label` markers resolve like any other, but nothing ever renders them into the
        request — they exist to shape the output path and to be recorded as provenance.
        """
        refs = [MarkerRef(marker, f"bind.{key}", key) for key, marker in self.bind.items()]
        refs.extend(MarkerRef(marker, f"label.{key}", key) for key, marker in self.label.items())
        refs.extend(_iter_markers(self.query, "query", None))
        refs.extend(_iter_markers(self.payload, "payload", None))
        return refs

    def inherits_limit(self) -> bool:
        """True when `limit` was absent, as opposed to an explicit `null` (= unlimited)."""
        return "limit" not in self.model_fields_set


class ProviderDecl(_Base):
    fn: str
    args: dict[str, Any] = Field(default_factory=dict)


def check_secret_template(template: str, key: str) -> None:
    """A template that drops its secret would send a constant header and look fine."""
    found = set(placeholders(template))
    unknown = found - {key}
    if unknown:
        raise ValueError(
            f"template {template!r} uses unknown placeholder(s) {sorted(unknown)} — "
            f"the secret is {{{key}}}"
        )
    if not found:
        raise ValueError(
            f"template {template!r} does not contain {{{key}}}, so the secret would be dropped"
        )


class AuthApply(_Base):
    header: str = "Authorization"
    template: str = "Bearer {token}"

    @model_validator(mode="after")
    def _check_template(self) -> AuthApply:
        check_secret_template(self.template, "token")
        return self


class HeaderValue(_Base):
    """One auth header's value. `template` wraps the secret, which is `{value}`."""

    value: str
    template: str = "{value}"

    @model_validator(mode="after")
    def _check_template(self) -> HeaderValue:
        check_secret_template(self.template, "value")
        return self


def _coerce_header_value(node: Any) -> Any:
    """`X-API-Key: env:K` is shorthand for `X-API-Key: {value: env:K}`."""
    if isinstance(node, str):
        return {"value": node}
    return node


HeaderValueSpec = Annotated[HeaderValue, BeforeValidator(_coerce_header_value)]


class AuthRequest(_Base):
    """A credential-acquiring request, used by the token-exchange auth types."""

    method: Literal["GET", "POST"] = "POST"
    path: str
    query: dict[str, Any] = Field(default_factory=dict)
    payload: Any = None
    headers: dict[str, str] = Field(default_factory=dict)


class BearerAuth(_Base):
    type: Literal["bearer"]
    token: str
    apply: AuthApply = Field(default_factory=AuthApply)


class BasicAuth(_Base):
    type: Literal["basic"]
    username: str
    password: str


class HeaderAuth(_Base):
    """One or more static credential headers.

    Some APIs want two at once — a templated `X-Authorization: ApiKey <key>` alongside a
    raw `X-EDF-APIKey: <key>` — so this is a mapping rather than a single pair.
    """

    type: Literal["header"]
    headers: dict[str, HeaderValueSpec] = Field(min_length=1)


class OAuthClientCredentialsAuth(_Base):
    type: Literal["oauth_client_credentials"]
    token_url: str
    client_id: str
    client_secret: str
    scope: str | None = None
    token_path: str = "$.access_token"
    apply: AuthApply = Field(default_factory=AuthApply)


class LoginTokenAuth(_Base):
    type: Literal["login_token"]
    request: AuthRequest
    token_path: str
    apply: AuthApply = Field(default_factory=AuthApply)


Auth = Annotated[
    BearerAuth | BasicAuth | HeaderAuth | OAuthClientCredentialsAuth | LoginTokenAuth,
    Field(discriminator="type"),
]


class Defaults(_Base):
    headers: dict[str, str] = Field(default_factory=dict)
    timeout: float = 30
    retries: int = 2
    rate_limit: float | None = None
    limit: int | None = None
    max_pages: int = 20
    output: str = "output/{source}/{endpoint}/{slug}.json"


class Source(_Base):
    source: str
    base_url: str
    auth: Auth | None = None
    defaults: Defaults = Field(default_factory=Defaults)
    providers: dict[str, ProviderDecl] = Field(default_factory=dict)
    endpoints: dict[str, Endpoint] = Field(min_length=1)

    def output_template(self, endpoint: str) -> str:
        return self.endpoints[endpoint].output or self.defaults.output
