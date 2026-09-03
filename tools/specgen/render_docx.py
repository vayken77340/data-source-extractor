"""Project the model into the Word template with docxtpl.

Nothing is computed here. The template decides what to show and the model supplies the
words; this file only runs Jinja inside the .docx and asks Word to refresh its fields on
open, so the table of contents reflects the headings that were actually rendered.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def render_bytes(template: Path, model: Mapping[str, Any], jinja_env: Any = None) -> bytes:
    """The rendered document as bytes, so that a check can render without writing."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docxtpl import DocxTemplate
    from jinja2 import StrictUndefined
    from jinja2.sandbox import SandboxedEnvironment

    document = DocxTemplate(str(template))
    # StrictUndefined: a tag naming a field the model lacks is an error, never an empty
    # cell that reads as "nothing to say".
    document.render(dict(model), jinja_env or SandboxedEnvironment(undefined=StrictUndefined))

    # `w:updateFields` makes Word refresh the TOC field on open (after one prompt). The
    # alternative — rendering the TOC ourselves — would duplicate Word's job badly.
    settings = document.docx.settings.element
    if settings.find(qn("w:updateFields")) is None:
        flag = OxmlElement("w:updateFields")
        flag.set(qn("w:val"), "true")
        settings.append(flag)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def render(template: Path, model: Mapping[str, Any], out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(render_bytes(template, model))
    return out
