"""Bring a specification template up to date with the generator, in place.

A template is yours: you reword its prose, restyle it, move things around. So the
generator's own template cannot simply be copied over yours — that would discard your
edits — and reproducing a dozen changes by hand in Word is the kind of work that goes
wrong quietly. This applies them one at a time to whatever template you point it at.

Every migration is idempotent and anchored on *text*, not on position, so your rewording
is tolerated as long as the block an edit attaches to still exists. One already applied is
reported and left alone; one whose anchor has gone is reported and skipped, and the run
continues with the rest.

    python tools/migrate_template.py config/specs/TEMPLATE.docx
    python tools/migrate_template.py config/specs/TEMPLATE.docx --dry-run

Two things it deliberately will not do. It never merges: if a tag is already present it
assumes you have your own version and leaves it. And it never guesses at an anchor it
cannot find — it tells you which edit it skipped, so you can add that one by hand.
"""

from __future__ import annotations

import argparse
import copy
import shutil
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docx import Document
from docx.oxml.ns import qn

sys.path.insert(0, str(Path(__file__).resolve().parent))

import add_code_style  # noqa: E402  a sibling script, not a package

P, TBL = qn("w:p"), qn("w:tbl")


class Missing(Exception):
    """An anchor this edit attaches to is not in the template any more."""


# --- reading the document -------------------------------------------------------------


def text_of(element: Any) -> str:
    return "".join(node.text or "" for node in element.iter(qn("w:t")))


def style_of(element: Any) -> str | None:
    properties = element.find(qn("w:pPr"))
    style = properties.find(qn("w:pStyle")) if properties is not None else None
    return style.get(qn("w:val")) if style is not None else None


def body(document: Document) -> list[Any]:
    return list(document.element.body)


def carries(document: Document, needle: str) -> bool:
    """Whether the template mentions something anywhere, tags and cells included."""
    return any(needle in text_of(element) for element in document.element.body.iter(P, TBL))


def find(document: Document, matches: Callable[[Any], bool], what: str) -> Any:
    for element in body(document):
        if matches(element):
            return element
    raise Missing(what)


def table_after(document: Document, anchor: Any, what: str) -> Any:
    elements = body(document)
    for element in elements[elements.index(anchor) + 1 :]:
        if element.tag == TBL:
            return element
    raise Missing(what)


# --- writing to it --------------------------------------------------------------------


def donor(document: Document, kind: str) -> Any:
    """A paragraph or table to copy, so inserted content inherits the template's look."""
    match kind:
        case "code":
            return find(document, lambda e: e.tag == P and style_of(e) == "Code", "a Code paragraph")
        case "heading":
            return find(document, lambda e: e.tag == P and style_of(e) == "Heading3", "a Heading 3")
        case "kv":
            return find(document, lambda e: e.tag == TBL and "{%tr for row in" in text_of(e), "a two-column table")
        case _:
            return find(
                document,
                lambda e: e.tag == P and style_of(e) is None and text_of(e).strip(),
                "an unstyled paragraph",
            )


def put(source: Any, value: str | None, after: Any) -> Any:
    """A copy of `source` carrying `value`, placed after `after`."""
    fresh = copy.deepcopy(source)
    if value is not None:
        runs = list(fresh.iter(qn("w:t")))
        runs[0].text = value
        for extra in runs[1:]:
            extra.text = ""
    after.addnext(fresh)
    return fresh


def put_all(document: Document, block: list[tuple[str, str | None]], after: Any) -> Any:
    """A run of paragraphs, each `(donor kind, text)`, in order after `after`."""
    last = after
    for kind, value in block:
        last = put(donor(document, kind), value, last)
    return last


def retarget(table: Any, loop: str) -> None:
    """Point a copied row loop at another collection."""
    for paragraph in table.iter(P):
        if "{%tr for" in text_of(paragraph):
            runs = list(paragraph.iter(qn("w:t")))
            runs[0].text = loop
            for extra in runs[1:]:
                extra.text = ""
            return


def swap(document: Document, replacements: dict[str, str]) -> int:
    """Rewrite tag text wherever it appears, cells included."""
    changed = 0
    for paragraph in document.element.body.iter(P):
        runs = list(paragraph.iter(qn("w:t")))
        joined = "".join(run.text or "" for run in runs)
        for old, new in replacements.items():
            if old in joined and new not in joined:
                runs[0].text = joined.replace(old, new)
                for extra in runs[1:]:
                    extra.text = ""
                changed += 1
                break
    return changed


def drop_through(document: Document, start: Any, stop: Callable[[Any], bool]) -> int:
    """Remove `start` and everything after it until `stop` says to halt."""
    elements = body(document)
    removed = 0
    for element in elements[elements.index(start) :]:
        if element is not start and stop(element):
            break
        document.element.body.remove(element)
        removed += 1
    return removed


# --- the migrations -------------------------------------------------------------------


@dataclass(frozen=True)
class Migration:
    id: str
    what: str
    done: Callable[[Document], bool]
    apply: Callable[[Document], None]


def _code_style(document: Document) -> None:
    """The style itself, plus every monospace body paragraph put under it."""
    add_code_style.build_style(document.styles, add_code_style.DEFAULT_FONT, add_code_style.DEFAULT_SIZE)
    for paragraph in document.paragraphs:
        if add_code_style.is_monospaced(paragraph, add_code_style.DEFAULT_FONT):
            add_code_style.apply(paragraph)


def _paginated_key(document: Document) -> None:
    anchor = find(document, lambda e: text_of(e).strip() == "{{ landing.key_template }}", "the key pattern in §6.5")
    put_all(
        document,
        [
            ("plain", "{%p if landing.key_template_paginated %}"),
            ("plain", "Endpoints paginés :"),
            ("code", "{{ landing.key_template_paginated }}"),
            ("plain", "{%p endif %}"),
        ],
        anchor,
    )


def _request_shape(document: Document) -> None:
    heading = find(document, lambda e: e.tag == P and text_of(e).strip() == "Requête", "the Requête heading")
    table = table_after(document, heading, "the request parameter table")
    if "endpoint.params" not in text_of(table):
        raise Missing("the request parameter table")
    document.element.body.remove(table)
    put_all(
        document,
        [
            ("plain", "{%p if endpoint.payload_shape %}"),
            ("plain", "Corps de la requête, tel qu'il part :"),
            ("code", "{%p for line in endpoint.payload_shape %}"),
            ("code", "{{ line }}"),
            ("code", "{%p endfor %}"),
            ("plain", "{%p endif %}"),
            ("plain", "{%p if endpoint.query_shape %}"),
            ("plain", "Paramètres de requête, tels qu'ils partent :"),
            ("code", "{%p for line in endpoint.query_shape %}"),
            ("code", "{{ line }}"),
            ("code", "{%p endfor %}"),
            ("plain", "{%p endif %}"),
        ],
        heading,
    )


def _endpoint_lists(document: Document) -> None:
    anchor = find(
        document,
        lambda e: "{%p for note in endpoint.correlated_origins %}" in text_of(e),
        "the correlated-origins loop",
    )
    table = copy.deepcopy(donor(document, "kv"))
    retarget(table, "{%tr for row in lst.rows %}")
    last = put_all(
        document,
        [
            ("plain", "{%p if endpoint.lists %}"),
            ("heading", "Listes de valeurs"),
            ("plain", "{%p for lst in endpoint.lists %}"),
            ("plain", "{{ lst.name }}"),
        ],
        anchor,
    )
    last.addnext(table)
    last = put_all(document, [("plain", "{%p endfor %}"), ("plain", "{%p endif %}")], table)
    # The block was built after the loop tag; move it in front, or it renders once per note.
    elements = body(document)
    block = elements[elements.index(anchor) + 1 : elements.index(last) + 1]
    for element in block:
        document.element.body.remove(element)
    for element in block:
        anchor.addprevious(element)


def _list_skeleton(document: Document) -> None:
    """Separate from the block above it: a template can carry one and not the other, and
    reporting that as done would leave a silent half-migration."""
    put_all(
        document,
        [
            ("plain", "{%p if lst.skeleton %}"),
            ("plain", "Emplacement des valeurs dans la réponse déjà déposée :"),
            ("code", "{%p for line in lst.skeleton %}"),
            ("code", "{{ line }}"),
            ("code", "{%p endfor %}"),
            ("plain", "{%p endif %}"),
        ],
        find(
            document,
            lambda e: e.tag == TBL and "{%tr for row in lst.rows %}" in text_of(e),
            "the parameter-list table under an endpoint",
        ),
    )


def _response_shape(document: Document) -> None:
    heading = find(
        document, lambda e: e.tag == P and text_of(e).strip() == "Forme de la réponse", "the response heading"
    )
    put_all(
        document,
        [
            ("plain", "{%p if endpoint.response_shape %}"),
            ("plain", "Structure observée, sur une réponse réelle :"),
            ("code", "{%p for line in endpoint.response_shape %}"),
            ("code", "{{ line }}"),
            ("code", "{%p endfor %}"),
            ("plain", "{%p endif %}"),
        ],
        table_after(document, heading, "the response table"),
    )


def _workbook_pointer(document: Document) -> None:
    anchor = find(
        document,
        lambda e: text_of(e).strip().startswith("Toutes les requêtes d'une même séquence"),
        "the page-size bullet in §3.0",
    )
    put_all(
        document,
        [
            ("plain", "{{ flow.workbook_pointer.text | link(flow.workbook_pointer.url) }}"),
            (
                "plain",
                "Filtrer ces listes est le levier le plus efficace sur la durée totale d'une "
                "exécution, et l'endroit le plus facile pour exclure définitivement des données "
                "par inadvertance.",
            ),
        ],
        anchor,
    )


def _contract_pointer(document: Document) -> None:
    heading = find(document, lambda e: text_of(e).strip().startswith("6.3 "), "the §6.3 heading")
    table = table_after(document, heading, "the metadata attribute table")
    if "landing.contract" not in text_of(table):
        raise Missing("the metadata attribute table")
    put(
        donor(document, "plain"),
        "{{ landing.contract_pointer.text | link(landing.contract_pointer.url) }}",
        table,
    )
    document.element.body.remove(table)


def _drop_section_43(document: Document) -> None:
    heading = find(document, lambda e: text_of(e).strip().startswith("4.3 "), "the §4.3 heading")
    drop_through(document, heading, lambda e: style_of(e) in ("Heading1", "Heading2"))


def _link_filters(document: Document) -> None:
    if not swap(
        document,
        {
            "{{ r.value }}": "{{ r.value | link(r.url) }}",
            "{{ appendix.workbook.file }}": "{{ appendix.workbook.file | link(appendix.workbook.url) }}",
            "{{ s.file }}": "{{ s.file | link(links.samples) }}",
        },
    ):
        raise Missing("the pointers in §1.4, Annexe A and Annexe B")


MIGRATIONS = (
    Migration(
        "code-style",
        "a Code paragraph style for the JSON, pseudocode and key blocks",
        lambda d: any(s.style_id == "Code" for s in d.styles),
        _code_style,
    ),
    Migration(
        "paginated-key",
        "the paginated key pattern in §6.5",
        lambda d: carries(d, "landing.key_template_paginated"),
        _paginated_key,
    ),
    Migration(
        "request-shape",
        "the request shown as a body, replacing its parameter table",
        lambda d: carries(d, "endpoint.payload_shape"),
        _request_shape,
    ),
    Migration(
        "endpoint-lists",
        "a Listes de valeurs block under each endpoint",
        lambda d: carries(d, "endpoint.lists"),
        _endpoint_lists,
    ),
    Migration(
        "list-skeleton",
        "where a chained list's values sit in the parent response",
        lambda d: carries(d, "lst.skeleton"),
        _list_skeleton,
    ),
    Migration(
        "response-shape",
        "a JSON excerpt of a captured response",
        lambda d: carries(d, "endpoint.response_shape"),
        _response_shape,
    ),
    Migration(
        "workbook-pointer",
        "the workbook pointer and the list-filtering warning in §3.0",
        lambda d: carries(d, "flow.workbook_pointer"),
        _workbook_pointer,
    ),
    Migration(
        "contract-pointer",
        "§6.3 pointing at the workbook instead of holding the table",
        lambda d: carries(d, "landing.contract_pointer"),
        _contract_pointer,
    ),
    Migration(
        "drop-section-4.3",
        "§4.3 removed, its lists now living under each endpoint",
        lambda d: not any(text_of(e).strip().startswith("4.3 ") for e in body(d)),
        _drop_section_43,
    ),
    Migration(
        "link-filters",
        "pointers that become hyperlinks once a URL is filled in",
        lambda d: carries(d, "| link(r.url)"),
        _link_filters,
    ),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("template", type=Path, help="the .docx to bring up to date, in place")
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    parser.add_argument("--no-backup", action="store_true", help="do not write a .bak copy first")
    args = parser.parse_args(argv)

    if not args.template.is_file():
        raise SystemExit(f"no such file: {args.template}")

    document = Document(str(args.template))
    applied, skipped = [], []
    for migration in MIGRATIONS:
        if migration.done(document):
            print(f"  already   {migration.id:18} {migration.what}")
            continue
        try:
            migration.apply(document)
        except Missing as missing:
            print(f"  SKIPPED   {migration.id:18} cannot find {missing}")
            skipped.append(migration)
        else:
            print(f"  applied   {migration.id:18} {migration.what}")
            applied.append(migration)

    if not applied:
        print("\nnothing to do." if not skipped else "\nnothing applied.")
    elif args.dry_run:
        print(f"\n--dry-run: {len(applied)} change(s) not written.")
    else:
        if not args.no_backup:
            backup = args.template.with_suffix(args.template.suffix + ".bak")
            shutil.copy2(args.template, backup)
            print(f"\nbackup    {backup}")
        document.save(str(args.template))
        print(f"written   {args.template}  ({len(applied)} change(s))")

    if skipped:
        print(f"\n{len(skipped)} edit(s) need doing by hand — the anchor each one attaches to is gone:")
        for migration in skipped:
            print(f"  {migration.id}: {migration.what}")
        print("Compare against config/specs/TEMPLATE.docx in this repo for what they add.")
    print("\nThen check it: python tools/build_spec.py <source> --check")
    return 1 if skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
