"""Providers: anything that supplies values for a request parameter.

The three built-ins ship with the framework. Everything else belongs to whoever is using
it, and lives in `providers/` at the repo root — outside `src/`, because writing one
should never mean reading the framework's code.

Those files register themselves by existing: every `.py` in that directory is loaded at
startup, so there is no import list to maintain.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

from api_extractor.providers import builtin  # noqa: F401  registers the built-ins
from api_extractor.providers.registry import (
    Provider,
    ProviderContext,
    ProviderFn,
    SavedOutput,
    forget_all,
    get,
    is_registered,
    provider,
    registered,
)

__all__ = [
    "BUILTINS",
    "PROVIDERS_DIR",
    "Provider",
    "ProviderContext",
    "ProviderFn",
    "SavedOutput",
    "forget_all",
    "get",
    "is_registered",
    "load_from",
    "provider",
    "registered",
]

# Whatever was registered by the time `builtin` finished importing, and nothing else.
BUILTINS = tuple(entry.name for entry in registered())

# Relative to the working directory, like `config/` and `output/`.
PROVIDERS_DIR = Path("providers")

# Loaded modules go under a private prefix so they cannot collide with anything installed.
_MODULE_PREFIX = "api_extractor_user_providers"


def load_from(directory: Path = PROVIDERS_DIR) -> list[str]:
    """Import every `.py` in `directory`, registering whatever it declares.

    Loaded by file path rather than by import name, so the directory does not have to be a
    package, be on `sys.path`, or know anything about how the CLI was invoked.

    Calling this twice is a no-op the second time. Re-executing a module would re-run its
    decorators and trip the duplicate-name rule, which would make the perfectly reasonable
    act of loading providers again into an error.

    An error raised while importing one of these is the author's to see, so it propagates.
    """
    if not directory.is_dir():
        return []

    # Scoped by directory, so two directories may hold a same-named file without one
    # silently shadowing the other.
    scope = hashlib.sha256(str(directory.resolve()).encode("utf-8")).hexdigest()[:8]

    loaded: list[str] = []
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = f"{_MODULE_PREFIX}.{scope}.{path.stem}"
        if module_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:  # pragma: no cover - unreadable file
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        loaded.append(path.stem)
    return loaded
