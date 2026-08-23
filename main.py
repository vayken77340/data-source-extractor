"""Run the extractor with no setup at all: `python main.py run <source>`.

The package lives in `src/`, which Python does not search on its own. This puts it on the
path itself, so nothing needs installing and `PYTHONPATH` never has to be set. Credentials
and proxy settings come from `.env` in the working directory.

`python -m api_extractor ...` remains equivalent on a venv that has the `.pth` file — see
the README. This file is the version that works everywhere, immediately.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


if __name__ == "__main__":
    from api_extractor.cli import app

    app()
