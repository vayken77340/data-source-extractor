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


def link_filter(document: Any) -> Any:
    """`{{ text | link(url) }}` — a real Word hyperlink, or the plain text without a URL.

    A hyperlink needs a relationship in the document part, which only the template
    instance can create, so the filter is built per render rather than registered once.
    An absent URL is the ordinary case until the files are published somewhere, and it
    must read exactly as it did before links existed.
    """
    from docxtpl import RichText

    def link(text: Any, url: str | None = None) -> Any:
        if not url:
            return text
        rich = RichText()
        rich.add(str(text), url_id=document.build_url_id(url), color="0563C1", underline=True)
        return rich

    return link


def render_bytes(template: Path, model: Mapping[str, Any], jinja_env: Any = None) -> bytes:
    """The rendered document as bytes, so that a check can render without writing."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docxtpl import DocxTemplate

    document = DocxTemplate(str(template))
    environment = jinja_env or jinja_environment()
    environment.filters["link"] = link_filter(document)
    # docxtpl uses a supplied environment as-is, so this must be the one above.
    document.render(dict(model), environment)

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
