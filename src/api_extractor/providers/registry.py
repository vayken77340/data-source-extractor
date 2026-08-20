"""Provider registry: name -> function, plus the optional `depends_on` hook.

Knows nothing about HTTP, YAML or endpoints. A provider is a source of parameter values,
and it always yields rows (dicts) so that several fields off one source row stay
correlated.

`depends_on` is how a provider declares that its args name an endpoint. It is the only way
endpoint dependencies are discovered, so a future chaining provider works without the
planner or the validator learning its name.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ProviderFn = Callable[..., list[dict[str, Any]]]
DependsOnFn = Callable[[dict[str, Any]], list[str]]

# Stands in for `ctx` when checking args against a signature, so a provider's first
# positional parameter does not have to be named or built to validate config.
_CONTEXT_PLACEHOLDER = object()


@dataclass(frozen=True)
class SavedOutput:
    """An envelope already on disk, and where it lives.

    Deliberately a plain mapping rather than a persist-layer type: the registry sits below
    persist and must not import it.
    """

    path: Path
    envelope: Mapping[str, Any]

    @property
    def body(self) -> Any:
        return self.envelope.get("body")


def no_outputs(endpoint: str) -> list[SavedOutput]:
    """The default: nothing has been written yet, or nobody wired a reader in."""
    return []


@dataclass(frozen=True)
class ProviderContext:
    """Injected by the runner.

    `outputs_for` is what makes chaining work, and it is just a callable the runner
    supplies — the registry never learns where output lives or how it is stored.
    """

    run_id: str
    output_root: Path
    source_name: str
    outputs_for: Callable[[str], list[SavedOutput]] = no_outputs


@dataclass(frozen=True)
class Provider:
    name: str
    fn: ProviderFn
    depends_on: DependsOnFn | None = None

    def endpoints_needed(self, args: dict[str, Any]) -> list[str]:
        """The endpoints these args imply. Empty for a provider that never chains."""
        if self.depends_on is None:
            return []
        return list(self.depends_on(args))

    def arg_names(self) -> list[str]:
        """The keyword-only params this provider takes, i.e. what YAML `args` may set."""
        return [
            param.name
            for param in inspect.signature(self.fn).parameters.values()
            if param.kind is inspect.Parameter.KEYWORD_ONLY
        ]

    def signature_error(self, args: dict[str, Any]) -> str | None:
        """None when `args` satisfies the signature, else why it does not.

        Checked before `depends_on` is ever called, so a typo'd arg is a validation
        message rather than a KeyError out of somebody's lambda.
        """
        try:
            inspect.signature(self.fn).bind(_CONTEXT_PLACEHOLDER, **args)
        except TypeError as exc:
            return str(exc)
        return None


_REGISTRY: dict[str, Provider] = {}


def provider(
    name: str, *, depends_on: DependsOnFn | None = None
) -> Callable[[ProviderFn], ProviderFn]:
    """Register a provider under `name`. Duplicate names raise."""

    def register(fn: ProviderFn) -> ProviderFn:
        if name in _REGISTRY:
            raise RuntimeError(
                f"provider {name!r} is already registered by "
                f"{_REGISTRY[name].fn.__module__}, now also by {fn.__module__} — "
                f"last-wins across two files is maddening to debug, so names must be unique"
            )
        _REGISTRY[name] = Provider(name=name, fn=fn, depends_on=depends_on)
        return fn

    return register


def get(name: str) -> Provider:
    return _REGISTRY[name]


def is_registered(name: str) -> bool:
    return name in _REGISTRY


def registered() -> list[Provider]:
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]


def forget_all() -> None:
    """Test hook. Registration is otherwise permanent for the life of the process."""
    _REGISTRY.clear()
