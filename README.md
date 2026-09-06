# api-extractor

Pulls sample responses from third-party HTTP APIs down to local JSON files, so you can
read their actual structure before designing anything on top of them.

The awkward part of sampling an API is that requests are chained and parameterised: one
endpoint needs an `assetType` that lives in a spreadsheet, another needs an `id` that only
exists in the previous endpoint's response. This tool treats those as the same thing — a
**provider** is anything that supplies values for a parameter, whether that is an Excel
column, a literal list, or a JSONPath into responses already on disk. One mechanism, not
two, which is why chaining needs no special syntax.

A source is one YAML file. No code is required to add one.

## Setup

Nothing is installed and nothing needs to be set in your shell.

```bash
python -m venv .venv
source .venv/bin/activate                # .venv\Scripts\activate on Windows

pip install -r requirements.txt          # or requirements-dev.txt to run the tests
cp .env.example .env                     # then fill in the values

python main.py list-sources
```

That's the whole setup. [`main.py`](main.py) puts `src` on the path itself, so there is no
`PYTHONPATH` to remember, no install step and no packaging metadata.

**Everything else comes from `.env`** — credentials *and* proxy settings, read from the
working directory:

```
TB_USER=svc-account
TB_PASSWORD=...
HTTPS_PROXY=http://proxy.example.com:8080
```

Run from the repo root: `.env`, `config/`, `providers/`, `input/` and `output/` all resolve
relative to the working directory.

`python -m pytest` works on a bare clone before any of the above —
[`conftest.py`](conftest.py) puts `src` on the path itself.

<details>
<summary><code>python -m api_extractor</code> instead of <code>python main.py</code></summary>

Both work. `python -m` needs `src` on the path first, which one line per venv arranges:

```bash
python -c "import sysconfig, pathlib; pathlib.Path(sysconfig.get_paths()['purelib'], 'api_extractor.pth').write_text(str(pathlib.Path('src').resolve()))"
```

That writes a `.pth` file into the venv's `site-packages` holding the absolute path to
`src`; Python reads `.pth` files at startup. **Redo it if you recreate the venv** — `.venv/`
is gitignored and the path inside is absolute. A `.pth` pointing at a directory that does
not exist is silently ignored, which is why a stale one fails as a bare `No module named
api_extractor`.

</details>

### Behind a corporate proxy

Nothing to configure for certificates: TLS is verified against the **operating system's
trust store** rather than certifi's public-CA bundle, so the internal root your IT
department installed is already trusted. Without that, a TLS-inspecting proxy — which
re-signs every certificate — fails with `unable to get local issuer certificate`.

Proxy settings go in `.env` alongside your credentials; httpx reads them itself and `.env`
is loaded before any request is built:

```
HTTPS_PROXY=http://proxy.example.com:8080
NO_PROXY=localhost,127.0.0.1
```

If you hit a certificate error anyway, do **not** reach for `verify=False`. That does not
fix trust, it removes it — on a network that is already inspecting your traffic.

## Running

```bash
python main.py list-sources             # what is defined
python main.py list-providers           # what is registered, and the args each takes
python main.py validate <source>        # every problem in one pass, no network
python main.py run <source> --dry-run   # the resolved plan, still no network
python main.py run <source>             # issue the requests
```

`run` flags:

| flag | effect |
|---|---|
| `--endpoint NAME` | run a subset; repeatable |
| `--limit N` | cap the values fanned out per endpoint |
| `--no-limit` | remove every limit, including the config's |
| `--dry-run` | print the plan and issue nothing |
| `--force` | rewrite outputs that already exist |
| `--run-id ID` | reuse a run id instead of minting one |
| `-v` | trace every request and response (before the subcommand) |

**`-v` when a source misbehaves.** It prints what actually went out and what came back —
the body you sent is usually the thing you got wrong:

```
DEBUG api_extractor.runner -> POST https://api.example.com/v1/things/search
     query   -
     payload {"status": "ACTIVE", "pageSize": 100}
     headers {'Accept': 'application/json', 'X-Authorization': '***REDACTED***'}
DEBUG api_extractor.runner <- 200 in 143ms
     headers {'content-type': 'application/json'}
     body    {"data":[1,2]}
```

Credentials are redacted before the log record is built, not filtered on the way out, so
`-v` is safe to paste into a ticket. Response bodies are truncated at 500 characters.

**Always dry-run first.** It runs the exact same planning code, then stops:

```
test — dag order: alarms, assets, tenant_info, measures

providers
  asset_ids    0 row(s)
  asset_types  7 row(s)

endpoints
  alarms       5 request(s)
  assets       5 request(s)
  tenant_info  1 request(s)
  measures     0 request(s)

total 11 request(s)
```

That total is the whole point: a `product` fan-out across two providers multiplies, and
you want to see 900 requests before you send them, not during.

Reruns are idempotent — same params and page mean the same path, and an existing file is
skipped unless you pass `--force`. Skips are recorded in the manifest.

A failed request never aborts the run. It is logged, recorded, and the next one goes out.

## Adding a source

Copy [`config/TEMPLATE.yaml`](config/TEMPLATE.yaml) to `config/sources/<name>.yaml` and
delete what you do not need. Everything shown uncommented in that file is checked by the
test suite, so if it is there it works.

Then, in order:

1. Add any new `env:` vars to `.env.example` (empty) and `.env` (filled).
2. `python main.py validate <name>` until it is clean.
3. `python main.py run <name> --dry-run` and check the request count.
4. `python main.py run <name> --limit 2` before running it wide.

### Validation catches these before any request goes out

`validate` runs 14 checks and reports **all** failures at once — failing on request 400
of 900 because a provider name was misspelled is the exact outcome it exists to prevent.
`--show-checks` lists what ran.

Unknown YAML keys, undeclared providers, unregistered provider functions, provider args
that do not fit their function, chains pointing at endpoints that do not exist, dependency
cycles (with the cycle path), unset env vars, `path` placeholders without a matching
`bind` entry and vice versa, unresolvable `output` placeholders, two markers colliding on
one param name, the page cursor landing on top of a marker, a `label` that appears in no
`output` placeholder, and a paginated endpoint whose output path has no `{page}` in it.

### Things worth knowing

**`{from: name}` is the only special marker.** It works at any depth in `query`, `payload`
or `bind`. Detection is an exact key-set match — `{from}` or `{from, as}` — so a literal
`{from: 2024-01-01, to: 2024-12-31}` date range stays a literal.

**A param is named by its nearest enclosing key**, and `as` renames it where that key is
useless: `filters: [{key: assetType, value: {from: x}}]` would otherwise be called `value`.

**A marker takes the row field matching its param name**, and a single-field row is
unwrapped whatever the field is called — which is why `type: {from: asset_types}` reads
rows of `{assetType: ...}` without ceremony.

**Fields from one provider stay row-wise.** Two markers reading `regions` give you
(EU, gold) and (US, silver), never the cross product. `fan_out` only governs combination
across *separate* providers: `product` crosses them, `zip` pairs them positionally and
raises on unequal lengths rather than truncating.

**Quote flow-context scalars containing `[ ] { }` or `,`.** Inside `{ }`,
`path: $.data[*].id` is a YAML parse error, and `keys: a,b` silently becomes
`{keys: a, b: null}` — the second is the dangerous one.

**A literal string starting with `env:` is not escapable.** It is read as a reference and
fails the `config.env.vars_set` check at validate time with a clear message, so it never
reaches the wire. Known behaviour, and the safe direction to fail in.

## Writing a provider

Drop a `.py` file in [`providers/`](providers/) — outside `src/`, because writing one
should never mean reading the framework's code. Every file there is imported at startup,
so your provider registers itself by existing.

[`providers/README.md`](providers/README.md) is the full guide; the short version:

```python
# providers/csv_column.py
from api_extractor.providers import ProviderContext, provider


@provider("csv_column")
def csv_column(ctx: ProviderContext, *, path: str, column: str) -> list[dict[str, object]]:
    with open(path, encoding="utf-8") as handle:
        return [{column: line.strip()} for line in handle if line.strip()]
```

```yaml
providers:
  regions:
    fn: csv_column
    args: { path: input/regions.csv, column: region }
```

The rules:

- **Always return a list of dicts**, never a list of scalars. That lets one provider fill
  several params off the same source row and keeps them correlated for free.
- **Keyword-only args**, which is what YAML `args` sets. They are checked against your
  signature at validate time, so a typo is a clear message rather than a `TypeError` 400
  requests in.
- **Duplicate names raise.** Last-wins across two files is miserable to debug.
- **YAML supplies args, never code.** No `eval`, no dotted import paths, no expressions.

`ctx` carries `run_id`, `output_root`, `source_name`, and `outputs_for(endpoint)` for
reading envelopes already on disk.

### Chaining off another endpoint

If a provider's args name an endpoint, declare it — this is the only way the dependency
graph is discovered, and it means the planner never learns your provider's name:

```python
@provider("from_output", depends_on=lambda args: [args["endpoint"]])
def from_output(ctx, *, endpoint: str, path: str) -> list[dict[str, object]]: ...
```

Return `__parents__` on a row to record lineage. The runner strips every `__key__` before
the row becomes request params, so config can never reference one.

Only two providers ship with the framework, because only two are generic to it: `literal`
(rows inline in YAML) and `from_output` (a JSONPath into a previous endpoint's envelopes).
Reading a spreadsheet is a fact about a particular business, so `excel_column` lives in
`providers/` alongside yours — a worked example rather than a built-in.

`list-providers` prints everything registered, with its args.

### Parameter files

`tools/build_params.py` turns a business spreadsheet into a **parameter file** — provenance
plus rows — so the messiest input you have is handled once, at a boundary you control,
rather than on every planning pass. `--ffill` declares which columns are merged; without
it a merged cell silently drops every row after its first, and the run still looks healthy.

The container is fixed and the columns are not, so one provider (`param_file`) reads every
parameter file you generate, whatever is in it. `providers/README.md` has the details.

`from_output_joined` covers the other half: a correlated join, where what one endpoint needs
depends on which request produced the last one. `fan_out` cannot express that — `product`
would ask a valve for its temperature — so the join happens in Python, keyed on the
`metadata.params` recorded in each envelope. It is `from_output` that keeps the provenance
instead of discarding it, and it knows nothing about any particular domain.

## What lands on disk

One file per response, one file per page — pages are never merged into one array, because
that would destroy the record of which page a row came from.

```
output/
  test/
    assets/PUMP_p0.json
    measures/PUMP-1.json
  _runs/20260820T101203Z-a1b2c3.jsonl
```

Each file is an envelope: the response body plus how it was fetched.

```json
{
  "metadata": {
    "source": "test",
    "endpoint": "measures",
    "extracted_at": "2026-08-20T10:12:03Z",
    "params": { "id": "PUMP-1", "assetType": "PUMP", "assetName": "Pompe 1" },
    "request": { "method": "GET",
                 "base_url": "https://tb.example.com/api", "path": "/assets/PUMP-1/measures",
                 "query": { "keys": "temperature,humidity" }, "payload": null,
                 "headers": { "Accept": "application/json", "Authorization": "***REDACTED***" } },
    "response": { "status": 200 },
    "parents": ["output/test/assets/PUMP_p0.json"]
  },
  "body": {}
}
```

- `body` is the parsed JSON, **semantically unmodified**. Nothing is validated, cleaned,
  flattened, renamed or reordered. If it is not JSON, `body` is `null` and `body_raw`
  holds the text.
- **Error responses are saved too.** A 403 or an HTML error page is real information about
  the API and belongs on disk, not swallowed.
- `params` is every resolved value, including `label` values that were never sent. It is
  what a chained provider joins on.
- `request` is what went out: `base_url` and `path` reassemble the URL, and `query` or
  `payload` carry the page cursor exactly where the API saw it — which is why there is
  no separate page number.
- `parents` is lineage: which asset file produced this id. It is a list because an asset
  can surface under two asset types, and both paths are worth keeping.
- The run id is not in the file. The manifest is keyed by it, and response headers and
  timing live there and in the `-v` trace, not in every envelope.

`metadata` is a contract. `tools/build_spec.py` describes it, attribute by attribute, in
the specification it generates, and a test pins that description to what `envelope.py`
writes.

`output/_runs/<run_id>.jsonl` has one JSON line per attempted request — endpoint, params,
status, output path, duration, parents, error — including skips. It is the index that
makes partial re-runs sane.

Because `from_output` reads whatever is already on disk,
`python main.py run test --endpoint measures` works against yesterday's
asset files without re-hitting `/assets/search`.

## Handing the extraction to another team

`tools/build_spec.py` turns a source into a **technical specification in French** — a
`.docx`, a companion `.xlsx`, and redacted sample files — for a team that will build its
own pipeline and has never seen this repository. The document never mentions the tool:
providers become phrases (*« assetType du référentiel (7 valeurs) »*), sampling caps are
omitted, and the landing contract of §6.3 is generated from `envelope.py` and pinned to it
by a test, so the document cannot describe metadata the code does not write.

```bash
pip install -r requirements-docs.txt         # docxtpl; the extractor itself never needs it
python tools/build_spec.py test --init       # start the annotation for a source that has none
python tools/build_spec.py test --check      # every problem in one pass, writes nothing
python tools/build_spec.py test              # output/_docs/test/: .docx, .xlsx, spec.json, samples/
python tools/build_spec.py test --variables  # what a template tag may reference
```

Four inputs, assembled into one JSON model before anything is rendered:

| input | holds | who writes it |
|---|---|---|
| `config/sources/<name>.yaml` | the mechanical facts | you, already |
| `config/specs/<name>.spec.yaml` | what the YAML cannot know: purpose, record grain, quirks, where files land | you, in French |
| `config/specs/TEMPLATE.docx` | the prose and layout, with Jinja tags where values go | you, in Word |
| `output/` | response shapes, samples | a run |

Every phrase, sheet name, column header and type name the generator writes on its own
comes from `config/specs/LABELS.yaml` — nothing reader-facing is in code. Types are
Iceberg names everywhere (`string`, `long`, `struct`, `list`), so the modelling team reads
one vocabulary in the request tables, the metadata contract and the response sheets.
Volumes are deliberately absent: how many rows one environment holds is not a fact the
receiving team can use.

The workbook has three fixed sheets — Readme, Endpoints, Metadata — then one sheet per
parameter list holding its actual values, then one sheet per endpoint holding both
directions: the request's parameters above the response's fields. Response fields come
from what a run observed, so run an extraction first or that block says so.

**A parameter list backed by a file ships as a sheet, not as an attachment.** A parameter
file is rows and columns, the workbook is already being delivered, and a spreadsheet is
where a table is read. The sheet carries the referential's generation date, so a reader
can tell whether it is stale, and the document says nothing further about it — the
request shape already names the list and the sheet holds the rest.

A list read out of a previous endpoint's responses has no values to tabulate, so it keeps
its recipe in the section of the endpoint it drives, and shows where each value sits as
the parent response's own shape:

```
{
  "data": [
    {
      "id": { "id": … }   ← id
    }
  ]
}
```

That replaces a `$.data[*]` selector plus paths relative to each record, which asks a
reader to hold two ideas at once. A path too rich to draw — a filter, a recursive descent
— falls back to printing the path itself.

**The document shows the request's shape, not a table of dotted paths.** A nested body is
printed as a body, with each value's origin in a column beside it, because
`pageLink.page` in a table hides the one thing a reader needs to see:

```
{
  "pageLink": {
    "pageSize": 100,   ← valeur fixe
    "page": 0          ← curseur de pagination, incrémenté à chaque page
  },
  "assetType": "<assetType>"   ← types d'actifs
}
```

The response gets the same treatment from a captured sample, with lists cut to a couple of
items — enough to show that they repeat — and the same masking the attached sample file
gets. The exhaustive tables live in that endpoint's sheet, and the document points at the
workbook once, at the head of the endpoint catalogue. So Word says what to send and why;
the workbook is the field-by-field reference for both directions.

The metadata contract works the same way. Section 6.3 states that the attribute names are
normative and points at the workbook's Metadata sheet for the full catalogue; the document
itself shows a real landed file instead, which is what a reader checks against.

**Links are yours to fill in.** The generator writes files, it does not publish them, so
a SharePoint URL is only knowable after the upload. A `links` block in the annotation
carries them:

```yaml
links:
  workbook: https://contoso.sharepoint.com/.../Spec_Annexes.xlsx
  document: https://contoso.sharepoint.com/.../Spec.docx
  samples:  https://contoso.sharepoint.com/.../Samples/
  vendor:   https://vendor.example.com/docs
```

Left as `[À COMPLÉTER]`, every pointer renders as the plain text it already was. Filled,
they become real Word hyperlinks, and the workbook's Readme links back to the document so
the pair is navigable both ways.

**Start the annotation with `--init`.** It reads the source and writes every endpoint in
reading order, each with the marker in every slot and a comment saying what the YAML
already knows (`POST /assets/search — drives measures; paginated`). What it writes passes
every check and produces a complete document with nothing in it yet, which is the honest
starting point. It never overwrites: run it again once a source has grown an endpoint and
it prints just the fragment to paste.

**The annotation never restates the source YAML.** Any field the generator can read
elsewhere is an unknown key there. `[À COMPLÉTER]` is accepted anywhere a string is;
`--check` counts what is left and `--min-complete N` turns that into a gate.

**The template is yours to edit.** Reword, reorder, delete a callout, add a column that
shows another field of the same row, restyle. `--check` catches the rest before a file is
written: a tag Word split across runs, a tag naming a field the model does not have (with
the nearest valid names), a required section removed by accident. A per-source
`config/specs/<name>.template.docx` wins over the default when present.

**The landing key comes in two defaults**, because whether an endpoint paginates is the
only thing that reliably changes a file name. `key_paginated` applies to the endpoints
that walk pages, `key` to the rest, and `endpoints.<name>.key` overrides both for one
endpoint. A single key carrying `{page}` would render `_p0` on every endpoint that
answers once, telling the receiving team the API pages when it does not; one without it
would silently overwrite every page but the last. Both mistakes are checks.

`--check` also fails on an endpoint with no annotation, an annotation naming a vanished
endpoint, a landing-key placeholder the endpoint cannot fill (every file would collapse
onto one key), a paginated endpoint whose key has no `{page}` or a non-paginated one whose
key has it, and a secret value that somehow reached the model or a sample — the same
two-layer defence as the log scrubber.

**Code blocks in the document use a `Code` paragraph style**, so you restyle every JSON
example, pseudocode block and key at once from Word's style pane. It carries the shading,
the keep-with-next that stops a 33-line envelope splitting across a page, and the hanging
indent that makes a long key's wrap read as a continuation. A template made before this
existed gets the style with `python tools/add_code_style.py <template.docx>`, which edits
it in place and keeps your other changes.

`output/_docs/` is under `output/`, so it is gitignored: the samples are real data.

## Secrets

- Credentials are **only ever `env:` references** in YAML. Validation fails on an unset var
  before anything is sent.
- `.env`, `output/` and `input/` are gitignored. Output samples are real production data,
  not just structure.
- `.env.example` lists every key with empty values — it is the only record of what a source
  needs to run. Keep it current.
- Namespace vars by source (`TB_USER`, `TB_PASSWORD`); several sources will each have
  something called "token".
- Secrets are scrubbed **in the logging formatter**, not at call sites, because an
  unhandled exception will otherwise print the whole request. Env values are registered
  automatically; acquired tokens register themselves.
- Request headers are redacted **at write time**. A known-sensitive name list, plus every
  header the auth layer set — so a house-style header like `X-EDF-APIKey` is redacted
  because it is a credential by construction, not because someone remembered it.
- Client certs: the env var holds a **path**, and the file lives outside the repo.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest          # no test makes a real network call
```

The code is type-hinted throughout, but nothing enforces it — there is no linter or type
checker wired in. The tests are the gate.

Layering is strictly one way, and a lower layer never imports a higher one:

```
config    parse YAML, resolve env: refs, validate, build the dependency graph
  |
plan      resolve providers, expand fan-out into request specs — pure, no network
  |
execute   auth, HTTP, retry, rate limit, pagination
  |
persist   envelopes, output paths, run manifest
```

The HTTP client has never heard of an endpoint. The provider registry has never heard of
HTTP. `plan` is testable end to end with no sockets, which is what `--dry-run` exercises.

## Deliberately not here

Not oversights. Adding any of them changes what this tool is:

data validation, cleaning, normalisation or flattening; schema inference or profiling;
checking that the API honoured the filter you sent; scheduling, backfills or incremental
state; a database or warehouse loader; async and concurrency (sequential is fine at
sampling volume — rate limiting is enough).

Bodies are written verbatim. What happens to them next is somebody else's layer.
