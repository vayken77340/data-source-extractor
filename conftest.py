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
