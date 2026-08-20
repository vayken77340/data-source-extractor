"""The endpoint dependency graph, derived from provider `depends_on` hooks.

This lives in the config layer, not the planner, because `depends_on` is a pure function
of config args: the whole graph is knowable from the YAML alone. The planner consumes the
ordering from here rather than rediscovering it, and nothing here knows the name
`from_output`.

There is no `depends_on` key in YAML. `measures` runs after `assets` because the provider
it names says so.
"""

from __future__ import annotations

from api_extractor.config.models import Source
from api_extractor.providers import registry


class CycleError(ValueError):
    def __init__(self, cycle: list[str]) -> None:
        self.cycle = cycle
        super().__init__("endpoint dependency cycle: " + " -> ".join(cycle))


def provider_dependencies(source: Source, provider_name: str) -> list[str]:
    """The endpoints one declared provider implies.

    Silent when the function is unregistered or its args do not fit: those are separate
    validation checks with better messages, and this must not raise before they run.
    """
    decl = source.providers[provider_name]
    if not registry.is_registered(decl.fn):
        return []
    provider = registry.get(decl.fn)
    if provider.signature_error(decl.args) is not None:
        return []
    return provider.endpoints_needed(decl.args)


def dependencies(source: Source) -> dict[str, set[str]]:
    """endpoint -> the endpoints it must run after. Names may not exist; see `known`."""
    graph: dict[str, set[str]] = {name: set() for name in source.endpoints}
    for name, endpoint in source.endpoints.items():
        for ref in endpoint.markers():
            if ref.marker.provider in source.providers:
                graph[name].update(provider_dependencies(source, ref.marker.provider))
    return graph


def known(graph: dict[str, set[str]]) -> dict[str, set[str]]:
    """Drop edges pointing at endpoints that were never declared."""
    return {name: {dep for dep in deps if dep in graph} for name, deps in graph.items()}


def find_cycle(graph: dict[str, set[str]]) -> list[str] | None:
    """A cycle as a path that starts and ends on the same endpoint, or None."""
    white, grey, black = 0, 1, 2
    colour = dict.fromkeys(graph, white)
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        colour[node] = grey
        stack.append(node)
        for dep in sorted(graph[node]):
            if dep not in graph:
                continue
            if colour[dep] == grey:
                return [*stack[stack.index(dep) :], dep]
            if colour[dep] == white:
                found = visit(dep)
                if found is not None:
                    return found
        stack.pop()
        colour[node] = black
        return None

    for node in sorted(graph):
        if colour[node] == white:
            found = visit(node)
            if found is not None:
                return found
    return None


def topological_order(source: Source) -> list[str]:
    """Endpoints in the order they must run, alphabetical within each level.

    Deterministic on purpose: `--dry-run` output should not shuffle between runs.
    """
    graph = known(dependencies(source))
    cycle = find_cycle(graph)
    if cycle is not None:
        raise CycleError(cycle)
    ordered: list[str] = []
    done: set[str] = set()
    while len(ordered) < len(graph):
        ready = sorted(name for name in graph if name not in done and graph[name] <= done)
        ordered.extend(ready)
        done.update(ready)
    return ordered
