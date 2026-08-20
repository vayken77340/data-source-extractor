"""Find, read and parse source definitions; resolve `env:` references.

`env:` values are deliberately *not* substituted into the parsed `Source`. The config
object stays secret-free so it can be logged, printed by `--dry-run` and passed around
freely; resolution happens at the point of use via `resolve_env_tree`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from api_extractor.config.models import ENV_PREFIX, Source
from api_extractor.logs import register_secret

SOURCES_ROOT = Path("config/sources")


class MissingEnvVarError(RuntimeError):
    """Raised at the point of use. Validation reports all missing vars ahead of this."""


def source_path(name: str, root: Path = SOURCES_ROOT) -> Path:
    return root / f"{name}.yaml"


def list_sources(root: Path = SOURCES_ROOT) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(path.stem for path in root.glob("*.yaml"))


def read_yaml(path: Path) -> Any:
    """Parse YAML. Raises yaml.YAMLError on malformed input, OSError if unreadable."""
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_source(path: Path) -> Source:
    """Read and parse a source file. Raises pydantic.ValidationError on a bad shape."""
    return Source.model_validate(read_yaml(path))


def is_env_ref(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(ENV_PREFIX)


def env_var_name(value: str) -> str:
    return value[len(ENV_PREFIX) :]


def resolve_env_string(value: str) -> str:
    """Resolve one `env:NAME` reference. Non-references are returned unchanged."""
    if not is_env_ref(value):
        return value
    name = env_var_name(value)
    resolved = os.environ.get(name)
    if resolved is None:
        raise MissingEnvVarError(f"environment variable {name} is not set")
    register_secret(resolved, name=name)
    return resolved


def resolve_env_tree(node: Any) -> Any:
    """Resolve every `env:NAME` string in a nested structure."""
    if isinstance(node, str):
        return resolve_env_string(node)
    if isinstance(node, dict):
        return {key: resolve_env_tree(value) for key, value in node.items()}
    if isinstance(node, list):
        return [resolve_env_tree(value) for value in node]
    return node


def iter_env_refs(node: Any, loc: str = "") -> Iterator[tuple[str, str]]:
    """Yield (loc, var name) for every `env:` reference anywhere in a model or tree."""
    if isinstance(node, str):
        if is_env_ref(node):
            yield loc, env_var_name(node)
    elif isinstance(node, BaseModel):
        for name, value in node:
            yield from iter_env_refs(value, f"{loc}.{name}" if loc else name)
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from iter_env_refs(value, f"{loc}.{key}" if loc else str(key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from iter_env_refs(value, f"{loc}[{i}]")
