"""The Word template as an input: which one applies, and whether an edit broke it.

The template is edited in Word by whoever owns the document. Anything that leaves its
tags pointing at model fields renders; anything else fails here with the tag and the
paragraph it sits in, before a single file is written. `docxtpl` is imported lazily so
that `--check` and `--model-only` run on `requirements.txt` alone.
"""

from __future__ import annotations

import difflib
import io
import re
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from specgen.annotation import SPECS_ROOT
from specgen.model import variables

DEFAULT_TEMPLATE = "TEMPLATE.docx"
TAG_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}")
PARAGRAPH_RE = re.compile(r"<w:p[ >].*?</w:p>", re.S)
TEXT_RE = re.compile(r"<w:t[^>]*>([^<]*)</w:t>")
XML_PARTS = re.compile(r"^word/(document|header\d*|footer\d*)\.xml$")

# What a document is not a specification without. Dropping one is allowed, but only on
# purpose (`--allow-partial`), never by an edit that did not mean to.
REQUIRED = {
    "the endpoint loop (`{%p for endpoint in endpoints %}`)": re.compile(r"for\s+endpoint\s+in\s+endpoints"),
    "the metadata contract table (`landing.contract`)": re.compile(r"landing\.contract"),
    "the landing example (`landing.example_lines`)": re.compile(r"landing\.example_lines"),
}


def available() -> bool:
    try:
        import docxtpl  # noqa: F401
    except ImportError:
        return False
    return True


def resolve(source_name: str, override: Path | None = None, root: Path = SPECS_ROOT) -> Path | None:
    """`--template`, else the per-source `<name>.template.docx`, else `TEMPLATE.docx`."""
    if override is not None:
        return override
    for candidate in (root / f"{source_name}.template.docx", root / DEFAULT_TEMPLATE):
        if candidate.is_file():
            return candidate
    return None


def paragraphs(path: Path) -> list[tuple[str, str]]:
    """(part, paragraph text) for every paragraph in the body, headers and footers.

    Read off the zip directly, so that a template Word has mangled still yields the text
    a person can search for.
    """
    out = []
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            if not XML_PARTS.match(name):
                continue
            xml = archive.read(name).decode("utf-8")
            for paragraph in PARAGRAPH_RE.findall(xml):
                text = "".join(TEXT_RE.findall(paragraph))
                if text.strip():
                    out.append((name, text))
    return out


def tags(path: Path) -> list[tuple[str, str]]:
    """(tag, paragraph text) for every Jinja tag in the template."""
    return [(tag, text) for _part, text in paragraphs(path) for tag in TAG_RE.findall(text)]


def check(path: Path, model: Mapping[str, Any], *, allow_partial: bool = False) -> list[tuple[str, str]]:
    """Every way an edit can break generation, in one pass. See the module docstring."""
    from jinja2 import StrictUndefined, TemplateSyntaxError, UndefinedError
    from jinja2.sandbox import SandboxedEnvironment

    from specgen.render_docx import render_bytes

    loc = str(path)
    findings: list[tuple[str, str]] = []

    # A tag Word split across runs, or one left unclosed, shows up as unbalanced braces
    # in a paragraph's text before Jinja ever sees it.
    for _part, text in paragraphs(path):
        if text.count("{{") != text.count("}}") or text.count("{%") != text.count("%}"):
            findings.append((loc, f"unbalanced tag in paragraph {_excerpt(text)!r} — retype the tag in one go"))

    env = SandboxedEnvironment(undefined=StrictUndefined)
    try:
        rendered = render_bytes(path, model, env)
    except TemplateSyntaxError as exc:
        findings.append((loc, f"template syntax: {exc.message} {_where(path, str(exc.message))}"))
        return findings
    except UndefinedError as exc:
        missing = _missing_name(str(exc))
        known = [name for name, _ in variables(model)]
        close = difflib.get_close_matches(missing, [n.rsplit(".", 1)[-1] for n in known], n=3)
        hint = f" — did you mean {', '.join(sorted(set(close)))}?" if close else ""
        findings.append((loc, f"{exc} {_where(path, missing)}{hint}; `--variables` lists what the model exposes"))
        return findings
    except Exception as exc:  # docxtpl raises its own for a {%tr%} outside a table row, etc.
        findings.append((loc, f"template failed to render: {type(exc).__name__}: {exc}"))
        return findings

    if not allow_partial:
        joined = " ".join(tag for tag, _ in tags(path))
        for what, pattern in REQUIRED.items():
            if not pattern.search(joined):
                findings.append((loc, f"the template no longer carries {what}; pass --allow-partial if that is intended"))

    with zipfile.ZipFile(io.BytesIO(rendered)) as archive:
        for name in archive.namelist():
            if XML_PARTS.match(name):
                xml = archive.read(name).decode("utf-8")
                text = "".join(TEXT_RE.findall(xml))
                if "{{" in text or "{%" in text:
                    findings.append((loc, f"a tag survived rendering in {name}: {_excerpt(text[text.find('{'):])!r}"))
    return findings


def _missing_name(message: str) -> str:
    found = re.findall(r"'([^']+)'", message)
    return found[-1] if found else message


def _where(path: Path, needle: str) -> str:
    hits = [text for _part, text in paragraphs(path) if needle and needle in text]
    return f"(paragraph {_excerpt(hits[0])!r})" if hits else ""


def _excerpt(text: str, limit: int = 60) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
