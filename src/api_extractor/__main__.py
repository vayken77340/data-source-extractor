"""Entry point for `python -m api_extractor`.

No install step: run it from the repo root and the package is found on the path.
"""

from __future__ import annotations

from api_extractor.cli import app

app()
