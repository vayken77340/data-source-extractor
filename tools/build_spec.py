"""Generate the extraction specification for a source: a French .docx, its companion
workbook, the sample files, and the model they were rendered from.

    python tools/build_spec.py test                  # output/_docs/test/…
    python tools/build_spec.py test --check          # every problem in one pass, writes nothing
    python tools/build_spec.py test --model-only     # spec.json only; no Word library needed
    python tools/build_spec.py test --variables      # what a template tag may reference

Four inputs: `config/sources/<name>.yaml`, `config/specs/<name>.spec.yaml`, the Word
template (`config/specs/TEMPLATE.docx`, or `<name>.template.docx`, or `--template`), and
whatever a run left under `output/`. Nothing is rendered until the model is built and
checked; a failing check writes nothing and exits 1.

Deliberately a script under `tools/` rather than a CLI subcommand: the extractor must
never need a Word library to fetch JSON. `docxtpl` lives in `requirements-docs.txt`.

Console output is English and ASCII — the completion marker is counted, not printed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT / "src", ROOT / "tools"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from dotenv import find_dotenv, load_dotenv  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from api_extractor import providers  # noqa: E402
from api_extractor.config.loader import load_source, source_path  # noqa: E402
from api_extractor.config.validate import Issue, validate_source  # noqa: E402
from specgen import check, evidence, model, template  # noqa: E402
from specgen.annotation import load_annotation, read_yaml, spec_path  # noqa: E402

MODEL_FILE = "spec.json"
SAMPLES_DIR = "samples"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", help="source name, e.g. test")
    parser.add_argument("--check", action="store_true", help="run every check, write nothing")
    parser.add_argument("--model-only", action="store_true", help="write spec.json and the samples only")
    parser.add_argument("--variables", action="store_true", help="print the paths a template tag may use")
    parser.add_argument("--template", type=Path, help="Word template to use instead of the default")
    parser.add_argument("--allow-partial", action="store_true", help="accept a template missing a required section")
    parser.add_argument("--run-id", help="manifest to read volumes from (default: the latest of this source)")
    parser.add_argument("--min-complete", type=float, default=0.0, help="fail below this completion percentage")
    parser.add_argument("--sample-records", type=int, default=3, help="list items kept per list in a sample")
    parser.add_argument("-o", "--out", type=Path, help="output directory (default: output/_docs/<source>)")
    args = parser.parse_args(argv)

    dotenv = find_dotenv(usecwd=True)
    if dotenv:
        load_dotenv(dotenv)
    providers.load_from()

    report = validate_source(source_path(args.source))
    if not report.ok:
        _print_issues(f"{report.path}: the source itself does not validate", report.issues)
        return 1
    source = load_source(source_path(args.source))

    annotation_path = spec_path(args.source)
    try:
        raw = read_yaml(annotation_path)
        annotation = load_annotation(annotation_path)
    except OSError as exc:
        print(f"{annotation_path}: cannot read: {exc}")
        return 1
    except ValidationError as exc:
        issues = [
            Issue("spec.annotation.parse", ".".join(str(p) for p in error["loc"]) or str(annotation_path), error["msg"])
            for error in exc.errors()
        ]
        _print_issues(f"{annotation_path}: {len(issues)} problem(s)", issues)
        return 1

    found = evidence.gather(source, run_id=args.run_id)
    built = model.build(source, annotation, found, sample_records=args.sample_records)

    if args.variables:
        for path, example in model.variables(built.model):
            print(f"{path:<60} {example}")
        return 0

    chosen = template.resolve(args.source, args.template)
    ctx = check.Context(
        source=source,
        annotation=annotation,
        annotation_raw=raw,
        built=built,
        evidence=found,
        template=chosen,
        template_available=template.available(),
        allow_partial=args.allow_partial,
        min_complete=args.min_complete,
    )
    result = check.run(ctx)
    for line in result.notes:
        print(line)
    if not result.ok:
        _print_issues(f"{annotation_path}: {len(result.issues)} problem(s)", result.issues)
        return 1
    print(f"checks run ({len(result.checks_run)}): ok")
    if args.check:
        return 0

    out = args.out or Path("output") / "_docs" / args.source
    out.mkdir(parents=True, exist_ok=True)
    (out / MODEL_FILE).write_text(json.dumps(built.model, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"model    -> {out / MODEL_FILE}")
    for file_name, document in built.samples.items():
        path = out / SAMPLES_DIR / file_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"sample   -> {path}")
    if args.model_only:
        return 0

    from specgen import render_xlsx

    workbook = render_xlsx.render(built.model, out / built.model["appendix"]["workbook"]["file"])
    print(f"workbook -> {workbook}")

    if chosen is None:
        print("document -> skipped: no template (config/specs/TEMPLATE.docx or --template)")
        return 0
    if not template.available():
        print("document -> skipped: docxtpl is not installed (pip install -r requirements-docs.txt)")
        return 0
    from specgen import render_docx

    document = render_docx.render(chosen, built.model, out / built.model["document"]["file"])
    print(f"document -> {document}")
    return 0


def _print_issues(title: str, issues) -> None:
    print(title)
    for issue in issues:
        print(f"  {issue}")


if __name__ == "__main__":
    raise SystemExit(main())
