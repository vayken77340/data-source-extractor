"""Put `src` on the path so the tests import the package without an install step.

pytest imports this before collecting anything. Running the CLI needs the same thing done
by hand — see the README: `PYTHONPATH=src python -m api_extractor ...`.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# `tools/specgen` is a consumer of the extractor, one layer above it, and is tested like
# the rest. It sits on the path *after* `src` so nothing under `src/` could import it
# by accident — the layering is one-way, and this keeps it that way even for the tests.
TOOLS = Path(__file__).parent / "tools"
if str(TOOLS) not in sys.path:
    sys.path.append(str(TOOLS))
