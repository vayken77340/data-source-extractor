"""Where an envelope lands.

Templating happens in `plan`, which knows the params. This is only the filesystem end of
it: same params and same page mean the same path, so a rerun is a no-op without --force.
"""

from __future__ import annotations

from pathlib import Path

from api_extractor.plan.binding import RequestSpec


def for_request(spec: RequestSpec, page: int = 0) -> Path:
    return Path(spec.output(page))


def already_written(path: Path) -> bool:
    return path.is_file()
