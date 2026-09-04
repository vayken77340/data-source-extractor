"""The Word template renders, and an edit that breaks it is caught with a location.

`config/specs/TEMPLATE.docx` is the one file under `config/` this suite reads, and the
coupling is deliberate: the template is an input a person edits in Word, and this is what
tells them, on every test run, that the tracked one still renders from the reference
fixture. Nothing here opens Word — the rendered document is unzipped and read as XML.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import pytest

from specgen import model, template
from specgen.labels import TODO_MARK
from tests.conftest import REPO_ROOT

pytest.importorskip("docxtpl")

from specgen import render_docx  # noqa: E402

TEMPLATE = REPO_ROOT / "config" / "specs" / "TEMPLATE.docx"
TEXT_RE = re.compile(r"<w:t[^>]*>([^<]*)</w:t>")
PARAGRAPH_RE = re.compile(r"<w:p[ >].*?</w:p>", re.S)


@pytest.fixture
def built(reference_source, reference_annotation):
    return model.build(reference_source, reference_annotation).model


def parts(rendered: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(rendered)) as archive:
        return {name: archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith(".xml")}


def paragraphs(xml: str) -> list[tuple[str, str]]:
    out = []
    for paragraph in PARAGRAPH_RE.findall(xml):
        style = re.search(r'<w:pStyle w:val="([^"]+)"', paragraph)
        text = "".join(TEXT_RE.findall(paragraph))
        if text.strip():
            out.append((style.group(1) if style else "", text))
    return out


def patched(tmp_path: Path, replacements: dict[str, str]) -> Path:
    """A copy of the tracked template with text replaced in its document part — the way
    an edit in Word would change it, minus Word."""
    out = tmp_path / "patched.docx"
    with zipfile.ZipFile(TEMPLATE) as source, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "word/document.xml":
                text = data.decode("utf-8")
                for old, new in replacements.items():
                    assert old in text, f"{old!r} is not in the template"
                    text = text.replace(old, new)
                data = text.encode("utf-8")
            target.writestr(item, data)
    return out


# --- the tracked template -------------------------------------------------------------


def test_the_tracked_template_passes_every_template_check(built):
    assert template.check(TEMPLATE, built) == []


def test_no_tag_survives_rendering(built):
    for name, xml in parts(render_docx.render_bytes(TEMPLATE, built)).items():
        text = "".join(TEXT_RE.findall(xml))
        assert "{{" not in text and "{%" not in text, name


def test_endpoint_sections_follow_the_model_in_order(built):
    body = paragraphs(parts(render_docx.render_bytes(TEMPLATE, built))["word/document.xml"])
    headings = [text for style, text in body if style == "Heading2" and text.startswith("3.")]
    assert headings == ["3.0 Conventions de pagination"] + [
        f"3.{e['number']} {e['title']}" for e in built["endpoints"]
    ]


def test_absent_subsections_leave_no_heading_behind(built):
    body = paragraphs(parts(render_docx.render_bytes(TEMPLATE, built))["word/document.xml"])
    texts = [text for _style, text in body]
    start = texts.index("3.3 GET /tenant/info — tenant_info")
    end = texts.index("3.4 GET /assets/{id}/measures — measures")
    section = texts[start:end]
    assert "Requête" not in section and "Itération" not in section
    assert "Particularités et points de vigilance" not in section
    assert "Forme de la réponse" in section


def test_the_toc_field_and_the_update_flag_are_present(built):
    rendered = parts(render_docx.render_bytes(TEMPLATE, built))
    assert "TOC" in rendered["word/document.xml"] and "fldChar" in rendered["word/document.xml"]
    assert "w:updateFields" in rendered["word/settings.xml"]


def test_the_header_reads_from_the_model(built):
    rendered = parts(render_docx.render_bytes(TEMPLATE, built))
    header = "".join(TEXT_RE.findall(rendered["word/header1.xml"]))
    assert header == "Spécification d'extraction API  |  Référence  |  v0.3 en revue"


def test_the_marker_count_in_the_document_equals_the_model_count(built):
    rendered = parts(render_docx.render_bytes(TEMPLATE, built))
    text = "".join(TEXT_RE.findall(rendered["word/document.xml"]))
    assert text.count(TODO_MARK) == built["completeness"]["todo"]


def test_code_blocks_carry_the_code_style_not_direct_formatting(built):
    """A block styled run by run cannot be restyled in Word, which the template is for."""
    rendered = parts(render_docx.render_bytes(TEMPLATE, built))
    assert 'w:styleId="Code"' in rendered["word/styles.xml"]
    document = rendered["word/document.xml"]
    assert document.count('w:val="Code"') >= len(built["landing"]["example_lines"])
    assert "Consolas" not in document


def test_the_code_style_keeps_a_block_together_and_indents_wraps(built):
    styles = parts(render_docx.render_bytes(TEMPLATE, built))["word/styles.xml"]
    block = styles[styles.index('w:styleId="Code"') :][:1200]
    assert "w:keepNext" in block and "w:keepLines" in block
    assert "w:hanging" in block  # a key too long for the column wraps to a deeper indent
    assert "w:shd" in block and "w:pBdr" in block


def test_nothing_test_specific_survived_into_the_template(built):
    """The template was derived from a document generated for one source; none of that
    source's words may be baked into it."""
    text = "".join(TEXT_RE.findall(parts(render_docx.render_bytes(TEMPLATE, built))["word/document.xml"]))
    for word in ("ThingsBoard", "OpenBao", "config/", "output/", "7 valeurs", "La pagination est portée par le corps"):
        assert word not in text, word


def test_render_writes_a_docx(built, tmp_path):
    out = render_docx.render(TEMPLATE, built, tmp_path / "spec.docx")
    assert zipfile.is_zipfile(out)


# --- structure: a rendered value must never become markup ----------------------------

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def body_of(rendered: bytes):
    from xml.etree import ElementTree as ET

    return ET.fromstring(parts(rendered)["word/document.xml"]).find(f"{W}body")


def test_sections_stay_at_body_level(built):
    """The regression that made a 25-page document open as 3 crushed pages.

    A model value holding `<créance>` was rendered into the XML unescaped, opened an
    element, and swallowed every later section into one table cell. docxtpl's `fix_tables`
    then repaired that into valid XML, so nothing raised and no tag was left behind — only
    the shape of the body gave it away.
    """
    body = body_of(render_docx.render_bytes(TEMPLATE, built))
    children = list(body)
    assert len(children) > 100, "the document collapsed into a handful of body children"
    assert len(body.findall(f"{W}tbl")) == len(list(body.iter(f"{W}tbl"))), "a table contains a table"
    assert len(body.findall(f"{W}p")) == len(list(body.iter(f"{W}p"))) - _cell_paragraphs(body)


def _cell_paragraphs(body) -> int:
    return sum(len(list(cell.iter(f"{W}p"))) for table in body.findall(f"{W}tbl") for cell in table.iter(f"{W}tc"))


def test_no_heading_is_swallowed_into_a_table(built):
    body = body_of(render_docx.render_bytes(TEMPLATE, built))
    in_cells = [
        style.get(f"{W}val")
        for table in body.findall(f"{W}tbl")
        for style in table.iter(f"{W}pStyle")
    ]
    assert not [style for style in in_cells if style and style.startswith(("Heading", "Title"))]


@pytest.mark.parametrize(
    "value",
    [
        "<créance> (secret)",
        "clé & valeur",
        "a < b et b > a",
        "<w:tbl>",
        "</w:p></w:tc></w:tr></w:tbl>",
        'guillemets "doubles" et \'simples\'',
    ],
)
def test_markup_in_a_model_value_is_text_not_structure(built, value):
    """Placeholders like `<bucket>` and `<id>` make such values ordinary, and an
    annotation is free prose — so escaping is the contract, not a special case."""
    built["endpoints"][0]["summary"][1]["value"] = value
    rendered = render_docx.render_bytes(TEMPLATE, built)
    body = body_of(rendered)
    assert len(body.findall(f"{W}tbl")) == len(list(body.iter(f"{W}tbl")))
    text = "".join(TEXT_RE.findall(parts(rendered)["word/document.xml"]))
    import html

    assert value in html.unescape(text)


def test_nesting_depth_counts_tables():
    assert template.nesting_depth("<w:tbl></w:tbl><w:tbl></w:tbl>") == 1
    assert template.nesting_depth("<w:tbl><w:tbl></w:tbl></w:tbl>") == 2
    assert template.nesting_depth("") == 0


def test_the_structural_guard_would_have_caught_the_original_bug(built, monkeypatch):
    """Autoescape makes this unreachable through a model value, which is the point. The
    guard stays because corruption of the body's shape is the only symptom this class of
    bug has, whatever future change causes it."""
    from jinja2 import StrictUndefined
    from jinja2.sandbox import SandboxedEnvironment

    monkeypatch.setattr(
        render_docx, "jinja_environment", lambda: SandboxedEnvironment(undefined=StrictUndefined)
    )
    built["auth"]["headers"][0]["value"] = "<créance>"
    findings = template.check(TEMPLATE, built)
    assert any("nested tables" in message for _loc, message in findings), findings


# --- edits that break it ------------------------------------------------------------


def test_a_mistyped_variable_is_named_with_a_hint(built, tmp_path):
    broken = patched(tmp_path, {"document.source_system": "document.sourcesystem"})
    (finding,) = template.check(broken, built)
    assert "sourcesystem" in finding[1]
    assert "did you mean" in finding[1] and "source_system" in finding[1]
    assert "paragraph 'Le présent document" in finding[1]
    assert "--variables" in finding[1]


def test_an_unbalanced_tag_is_located_by_its_paragraph(built, tmp_path):
    broken = patched(tmp_path, {"{{ document.subtitle }}": "{{ document.subtitle"})
    findings = template.check(broken, built)
    assert any("unbalanced tag in paragraph '{{ document.subtitle'" in message for _loc, message in findings)


def test_dropping_a_required_section_needs_allow_partial(built, tmp_path):
    broken = patched(tmp_path, {"landing.example_lines": "landing.rendered_keys"})
    (finding,) = template.check(broken, built)
    assert "no longer carries the landing example" in finding[1]
    assert template.check(broken, built, allow_partial=True) == []


def test_a_tag_that_survives_rendering_is_reported(built, tmp_path):
    """A model value that looks like a tag is rendered as text, and then looks like a tag
    nobody rendered. Rare, but it is the one leftover the other checks cannot see."""
    built["endpoints"][0]["summary"][1]["value"] = "un objet qui contient {{ ceci }}"
    (finding,) = template.check(TEMPLATE, built)
    assert "a tag survived rendering" in finding[1]


def test_resolution_order(tmp_path):
    root = tmp_path
    assert template.resolve("x", None, root) is None
    (root / "TEMPLATE.docx").write_bytes(b"")
    assert template.resolve("x", None, root) == root / "TEMPLATE.docx"
    (root / "x.template.docx").write_bytes(b"")
    assert template.resolve("x", None, root) == root / "x.template.docx"
    assert template.resolve("x", Path("elsewhere.docx"), root) == Path("elsewhere.docx")
