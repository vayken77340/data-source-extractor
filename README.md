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

The package is never installed — the venv is just pointed at `src`.

```bash
python -m venv .venv
source .venv/bin/activate                # .venv\Scripts\activate on Windows

pip install -r requirements.txt          # or requirements-dev.txt to run the tests
cp .env.example .env                     # then fill in the values

# put src on the venv's path, once, when the venv is created
python -c "import sysconfig, pathlib; pathlib.Path(sysconfig.get_paths()['purelib'], 'api_extractor.pth').write_text(str(pathlib.Path('src').resolve()))"

python -m api_extractor list-sources
```

That last setup line writes a one-line `.pth` file into the venv's `site-packages` holding
the absolute path to `src`. Python reads `.pth` files at startup, so `src` is on the path
for anything run with this venv — no build, no packaging metadata, no editable install, no
`PYTHONPATH` to remember. **Redo it if you recreate the venv**, since `.venv/` is
gitignored and the path inside is absolute.

Run from the repo root: `config/`, `input/` and `output/` all resolve relative to the
working directory.

`python -m pytest` works regardless — [`conftest.py`](conftest.py) puts `src` on the path
itself, so a fresh clone can run the tests before any of the above.

## Running

```bash
python -m api_extractor list-sources             # what is defined
python -m api_extractor list-providers           # what is registered, and the args each takes
python -m api_extractor validate <source>        # every problem in one pass, no network
python -m api_extractor run <source> --dry-run   # the resolved plan, still no network
python -m api_extractor run <source>             # issue the requests
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
| `-v` | debug logging (before the subcommand) |

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
2. `python -m api_extractor validate <name>` until it is clean.
3. `python -m api_extractor run <name> --dry-run` and check the request count.
4. `python -m api_extractor run <name> --limit 2` before running it wide.

### Validation catches these before any request goes out

`validate` runs 13 checks and reports **all** failures at once — failing on request 400
of 900 because a provider name was misspelled is the exact outcome it exists to prevent.
`--show-checks` lists what ran.

Unknown YAML keys, undeclared providers, unregistered provider functions, provider args
that do not fit their function, chains pointing at endpoints that do not exist, dependency
cycles (with the cycle path), unset env vars, `path` placeholders without a matching
`bind` entry and vice versa, unresolvable `output` placeholders, two markers colliding on
one param name, the page cursor landing on top of a marker, and a paginated endpoint whose
output path has no `{page}` in it.

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
    "run_id": "20260820T101203Z-a1b2c3",
    "source": "test",
    "endpoint": "measures",
    "params": { "id": "PUMP-1" },
    "request": { "method": "GET", "url": "...", "query": {}, "payload": null,
                 "headers": { "Authorization": "***REDACTED***" } },
    "response": { "status": 200, "headers": {}, "elapsed_ms": 143 },
    "page": 0,
    "parents": ["output/test/assets/PUMP_p0.json"],
    "fetched_at": "2026-08-20T10:12:03Z"
  },
  "body": {}
}
```

- `body` is the parsed JSON, **semantically unmodified**. Nothing is validated, cleaned,
  flattened, renamed or reordered. If it is not JSON, `body` is `null` and `body_raw`
  holds the text.
- **Error responses are saved too.** A 403 or an HTML error page is real information about
  the API and belongs on disk, not swallowed.
- `parents` is lineage: which asset file produced this id. It is a list because an asset
  can surface under two asset types, and both paths are worth keeping.

`output/_runs/<run_id>.jsonl` has one JSON line per attempted request — endpoint, params,
status, output path, duration, parents, error — including skips. It is the index that
makes partial re-runs sane.

Because `from_output` reads whatever is already on disk,
`python -m api_extractor run test --endpoint measures` works against yesterday's
asset files without re-hitting `/assets/search`.

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
python -m pytest          # 259 tests, no test makes a real network call
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
