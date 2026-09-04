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


def jinja_environment() -> Any:
    """The one environment every render and every check uses.

    `autoescape` is not a nicety here: a rendered value goes straight into the document's
    XML, so a model value containing `<`, `>` or `&` is markup unless it is escaped. A
    header value of `<créance>` opened an element that swallowed the rest of the document,
    and docxtpl's `fix_tables` then repaired the wreckage into *valid* XML with every
    later section buried inside one table cell — a document that opens without complaint
    and is three crushed pages long. Placeholders like `<bucket>` and `<id>` make such
    values ordinary, so escaping is the default and there is no unescaped path.

    `StrictUndefined`: a tag naming a field the model lacks is an error, never an empty
    cell that reads as "nothing to say".
    """
    from jinja2 import StrictUndefined
    from jinja2.sandbox import SandboxedEnvironment

    return SandboxedEnvironment(undefined=StrictUndefined, autoescape=True)


def render_bytes(template: Path, model: Mapping[str, Any], jinja_env: Any = None) -> bytes:
    """The rendered document as bytes, so that a check can render without writing."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docxtpl import DocxTemplate

    document = DocxTemplate(str(template))
    # docxtpl uses a supplied environment as-is, so this must be the one above.
    document.render(dict(model), jinja_env or jinja_environment())

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
