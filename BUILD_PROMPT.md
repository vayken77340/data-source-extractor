# Project Spec — Config-Driven API Extractor

> **Status: built.** All seven phases in §10 are done and this file describes the system as
> it stands, not a plan for one. Where a decision was made during the build that this spec
> did not anticipate, the reasoning is recorded inline — those are the parts most worth
> reading before changing anything.
>
> Practical usage lives in [README.md](README.md); how to write a provider lives in
> [providers/README.md](providers/README.md). This file is the *why*.

---

## 1. Context

I am a data engineer. I have access to several third-party HTTP APIs, each with its own
auth scheme. My job is to pull sample responses down to local JSON files so I can inspect
their structure and design the bronze layer of a lakehouse.

The complication is that requests are chained and parameterised:

- `/assets/search` is a **POST** whose payload needs an `assetType`. The list of valid
  asset types comes from **an Excel file supplied by the business**.
- `/assets/{id}/measures` is a **GET** whose `id` comes from **the response of the
  previous call**.
- Other endpoints are plain GETs, with or without query params.

The insight this project is built on: *an Excel column and a previous endpoint's response
are the same thing* — a source of values for a parameter. Both are **providers**. There is
one mechanism, not two.

## 2. Goal

A CLI tool that, given a YAML source definition (plus any Python provider functions I
write), issues the right requests in the right order and writes every response to a
predictable path on disk.

## 3. Non-goals — do not build these

Ignoring this section is the main way this project goes wrong. Do not add:

- Data **validation, cleaning, normalisation, or flattening**. Bodies are written verbatim.
- **Schema inference or profiling.** Interesting, but out of scope for v1.
- Checking that the API **honoured the filter** we sent, or any other correctness assertion.
- **Scheduling, backfills, incremental state, or a web UI.**
- A database, a warehouse loader, or any downstream integration.
- Async / concurrency. Sequential is fine at sampling volume. Rate limiting is enough.

If a task starts to feel like a workflow engine, stop and flag it rather than building it.

## 4. Tech constraints

- Python 3.11+, `src/` layout, **no packaging**. There is no `pyproject.toml`, no
  `setup.py` and no install step. Dependencies come from `requirements.txt`, and `src` is
  put on the venv's path with a one-line `.pth` file so `python -m api_extractor` works —
  see README §Setup. `python -m pytest` needs nothing, because the root `conftest.py`
  handles it.
- Framework dependencies: `httpx`, `truststore`, `pydantic` v2, `pyyaml`, `jsonpath-ng`,
  `typer`, `python-dotenv`, `tenacity`. Dev: `pytest`, `pytest-httpx`.
- **`truststore` is not optional.** httpx verifies against certifi, which ships public root
  CAs only. Behind a TLS-inspecting corporate proxy every certificate is re-signed by an
  internal root that certifi has never heard of, so verification fails. The OS trust store
  has that root. `verify=False` would also make the error go away, by making the connection
  interceptable on a network that is already inspecting traffic — never do that.
  Proxy settings come from `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY`, which httpx reads
  itself; `.env` is loaded before the client is built, so they can live there.
- `openpyxl` is in `requirements.txt` but is **not the framework's** — it belongs to
  `providers/excel_column.py`. If that provider goes, so does the dependency.
- No Airflow, Dagster, Prefect, pandas, or any ORM.
- Type hints throughout, but **nothing enforces them**. There is no linter and no type
  checker wired in; the test suite is the gate.

## 5. Architecture

Strict one-way layering. A lower layer must never import a higher one.

```
config   parse YAML -> pydantic models, resolve env: refs, validate, build the DAG
  |
plan     resolve providers, expand bindings into concrete requests
  |
execute  auth + HTTP + retry + rate limit + pagination
  |
persist  envelope, output paths, run manifest
```

Rules that keep this honest:

- The HTTP client knows nothing about YAML, providers, or endpoints. It takes a request
  object and returns a response.
- The provider registry knows nothing about HTTP, and nothing about where output lives.
  `ctx.outputs_for` is a callable the runner supplies, which is why `providers/` can sit
  below `persist/` and still read envelopes.
- `plan` is pure and fully testable without a network: given config + fake provider
  outputs, it emits a list of request specs. `--dry-run` is exactly this code, then stop.

**Planning is interleaved with execution.** `build_plan` expands every endpoint up front,
which is right for `--dry-run` and wrong for a real run: a chained endpoint cannot be
planned until its parent's envelopes exist. So `runner.execute` walks the DAG order and
calls `plan_one` for each endpoint immediately before issuing it. Both paths go through the
same `plan_one`, so they cannot drift. This is also what makes `--endpoint measures` work
on its own — it plans against whatever is already on disk.

### File layout

```
requirements.txt                # framework deps (+ openpyxl, for the local provider)
requirements-dev.txt            # -r requirements.txt, plus pytest and pytest-httpx
.env.example
.gitignore
README.md
conftest.py                     # puts src on the path for pytest
config/
  TEMPLATE.yaml                 # annotated, and validated by the test suite
  sources/test.yaml             # the reference source, run end to end by the suite
providers/                      # yours, outside src — loaded by path at startup
  README.md
  excel_column.py
input/                          # excel files etc, gitignored
output/                         # gitignored
src/api_extractor/
  __main__.py                   # python -m api_extractor
  cli.py
  config/  models.py  loader.py  validate.py  graph.py
  providers/  registry.py  builtin.py
  auth/  registry.py  strategies.py
  http/  client.py  pagination.py
  plan/  binding.py
  persist/  envelope.py  paths.py  manifest.py
  runner.py
  logs.py
tests/
```

---

## 6. Core contracts

These three shapes are the whole design.

### 6.1 Provider

```python
ProviderFn = Callable[..., list[dict[str, Any]]]


@provider("literal")
def literal(ctx: ProviderContext, *, values: list[dict[str, Any]]) -> list[dict[str, Any]]: ...


@provider("from_output", depends_on=lambda args: [args["endpoint"]])
def from_output(ctx: ProviderContext, *, endpoint: str, path: str) -> list[dict[str, Any]]: ...
```

- Always returns a **list of dicts**, never a list of scalars. This lets one provider
  supply several fields from the same source row (e.g. `assetType` *and* `region` off one
  Excel row), which keeps them correlated for free.
- `ctx` is injected by the runner and carries: `run_id`, `output_root`, `source_name`,
  and `outputs_for(endpoint) -> list[SavedOutput]` for chaining. A `SavedOutput` is the
  parsed envelope plus the path it came from.
- **`depends_on`** is an optional registration hook returning the endpoint names a
  provider's args imply. It is the *only* way endpoint dependencies are discovered — the
  DAG builder asks every provider rather than pattern-matching the name `from_output`, so
  a future chaining provider works without touching the planner or the validator.
- **Args are checked against the signature** during validation, before `depends_on` is
  ever called. Without that, `endpoints:` for `endpoint:` would raise `KeyError` inside a
  provider's own lambda instead of producing a message.
- **Reserved `__`-prefixed keys.** Providers may return `__parents__` (and later
  `__index__`, `__page__`, …) as out-of-band metadata. The runner strips every `__key__`
  before the dict becomes `params`. Referencing one from a marker or an output template is
  a validation error. Hand-written providers return plain dicts and never think about this
  — that property is worth protecting.
- **Duplicate registration of a name raises** — last-wins across two files is maddening to
  debug. This applies to shadowing a built-in too.
- **YAML supplies args, never code.** No `eval`, no dotted import paths in config, no
  expression strings. Unknown provider name = validation error.

**Where providers live.** The registry and the built-ins are in `src/`. *Your* providers
are in **`providers/` at the repo root**, outside the framework, because writing one should
never mean reading the framework's code. Every `.py` there is loaded at startup by file
path — not by import name — so the directory needs no `__init__.py`, needs nothing on
`sys.path`, and does not care how the CLI was invoked. Files starting with `_` are skipped.
Loading twice is a no-op, since re-executing a module would re-run its decorators and trip
the duplicate-name rule.

Built-ins, and only these two, because only these two are generic to the framework:

| name | purpose |
|---|---|
| `literal` | rows written inline in YAML |
| `from_output` | read a JSONPath out of a prior endpoint's saved envelopes |

`from_output` is what makes chaining work, and it is deliberately *just another provider* —
no special case in the runner.

**`excel_column` is not a built-in.** Reading a spreadsheet is a fact about a particular
business, not about HTTP, so it lives in `providers/excel_column.py` as a worked example:
several columns kept row-wise, blanks dropped, duplicates collapsed with first occurrence
winning, and errors raised loudly.

### 6.2 Envelope (one per response, one file per page)

```json
{
  "metadata": {
    "run_id": "20260820T101203Z-a1b2c3",
    "source": "test",
    "endpoint": "measures",
    "params": { "id": "9f3c..." },
    "request": {
      "method": "GET",
      "url": "https://tb.example.com/api/assets/9f3c.../measures",
      "query": { "keys": "temperature,humidity" },
      "payload": null,
      "headers": { "Authorization": "***REDACTED***" }
    },
    "response": { "status": 200, "headers": {}, "elapsed_ms": 143 },
    "page": 0,
    "parents": ["output/test/assets/PUMP_p0.json"],
    "fetched_at": "2026-08-20T10:12:03Z"
  },
  "body": {}
}
```

- `body` is the parsed JSON, **byte-for-byte semantically unmodified** — no key
  reordering, no coercion, no unwrapping.
- Non-JSON or unparseable response: `body` is `null` and `body_raw` holds the text.
- **Error responses are saved too.** A 403 or an HTML error page is real information about
  the API and I want it on disk, not swallowed. An error response is a `Response`, not an
  exception; only transport failures raise, and the runner records those and continues.
- `parents` gives lineage: measures file -> asset file -> the assetType that produced it.
  It is a **list**, usually of length 0 or 1 — but a `product` fan-out across two chained
  providers genuinely has two, as does an asset that surfaced under two asset types.
  It is populated from the `__parents__` key that `from_output` attaches to each row.
- Sensitive request headers are redacted **at write time**, not at read time. Redaction
  covers a known-sensitive name list **plus every header name the auth layer set** — so a
  house-style header like `X-EDF-APIKey` is redacted because it is a credential by
  construction, not because somebody remembered to list it.

### 6.3 Run manifest — `output/_runs/{run_id}.jsonl`

One JSON line per attempted request: endpoint, params, page, status, output path, duration,
parents, error. Skips are recorded as `"status": "skipped"`, and an endpoint that could not
be planned at all as `"status": "unplanned"` with the reason. This is the index that makes
resume and partial re-runs possible.

---

## 7. YAML schema

Reference example — `config/sources/test.yaml`, which runs end to end in the tests.
`config/TEMPLATE.yaml` is the annotated version to copy when adding a source; everything
uncommented in it is checked by the suite, so if it is there it works.

```yaml
source: test
base_url: https://tb.example.com/api

auth:
  # Basic auth on every request: Authorization: Basic base64(user:pass).
  # No login step, no token exchange.
  type: basic
  username: env:TB_USER
  password: env:TB_PASSWORD

defaults:
  headers: { Accept: application/json }
  timeout: 30              # seconds, whole request
  retries: 2
  rate_limit: 5            # requests/second, source-wide
  limit: 5                 # sampling cap: max values fanned out per provider
  max_pages: 20            # hard stop per pagination walk
  output: output/{source}/{endpoint}/{slug}.json

providers:
  asset_types:
    fn: excel_column
    args: { path: input/asset_types.xlsx, sheet: Referentiel, columns: [assetType] }
  asset_ids:
    fn: from_output
    args: { endpoint: assets, path: "$.data[*].id.id" }

endpoints:

  tenant_info:                          # plain GET, nothing to resolve
    method: GET
    path: /tenant/info

  alarms:                               # GET, static + bound query params
    method: GET
    path: /alarms
    query:
      pageSize: 100
      searchStatus: ACTIVE
      type: { from: asset_types }
    output: output/{source}/alarms/{type}.json

  assets:                               # POST, payload from Excel, paginated
    method: POST
    path: /assets/search
    payload:
      assetType: { from: asset_types }
      pageSize: 100
    fan_out: product
    paginate: { style: page_number, at: payload.page, has_more: $.hasNext }
    output: output/{source}/assets/{assetType}_p{page}.json

  measures:                             # GET chained off `assets`, id in path
    method: GET
    path: /assets/{id}/measures
    bind:
      id: { from: asset_ids }
    query: { keys: "temperature,humidity" }
    limit: 20
    output: output/{source}/measures/{id}.json
```

### Semantics

- **`{from: name}` is the only special marker**, optionally `{from: name, as: alias}`.
  Everything else in `query` / `payload` / `bind` is a literal. One parser, one rule,
  identical behaviour for query strings, JSON bodies, and path placeholders. `payload` may
  nest arbitrarily; markers resolve at any depth.
- Detection is an **exact key-set match** — `{from}` or `{from, as}` — so a literal
  `{from: 2024-01-01, to: 2024-12-31}` date range stays a literal. The gap this opens (a
  typo'd `{from: x, az: y}` silently becoming a literal) is closed by a check: a literal
  `from` value that names a declared provider is not a coincidence.
- **`params` is the union of every resolved marker in a request**, wherever it sat — query,
  payload at any depth, or bind — keyed by its **leaf key name**. A collision between two
  markers resolving to the same key is a **validation** error, not a runtime one. `as`
  renames the key and is required where the leaf name is useless:
  `filters: [{key: assetType, value: {from: x}}]` would otherwise key on `value`. `as` is
  **rejected inside `bind`**, where the bind key already names the param and must match the
  path placeholder — an alias there could only desynchronise the two.
- **A marker takes the row field matching its param name**, and a **single-field row is
  unwrapped whatever the field is called**. That is what lets `type: {from: asset_types}`
  read rows of `{assetType: ...}` without ceremony. A multi-field row with no matching
  field is an error naming the fields it does have.
- **Quote any flow-context scalar containing `[ ] { }` or `,`.** Inside `{ }`,
  `path: $.data[*].id.id` is a parse error, and `keys: temperature,humidity` silently
  becomes `{keys: temperature, humidity: null}`. The second is the dangerous one.
- **No `depends_on` key.** `measures` depends on `assets` because `asset_ids` names it.
  The DAG is derived from provider references. Topologically sorted, alphabetical within a
  level so `--dry-run` does not shuffle between runs; a cycle raises with the cycle path in
  the message.
- **`bind` vs `query`** is a readability split only — both feed the same resolution step.
  `bind` is for values that land in the path template.
- **`fan_out`** governs combination across *separate* providers in one request:
  `product` = cartesian, `zip` = positional. Unequal lengths under `zip` must **raise**,
  never truncate silently, and the message names each provider and its row count. Fields
  from a single provider are already row-wise and are never re-combined. Default `product`.
- **`limit`** cascades default -> endpoint -> `--limit` CLI flag, and `--no-limit` beats
  all three. It caps **rows per provider**, not requests per endpoint: with two providers
  under `product`, `--limit 2` is four requests, not two. `null` means unlimited, and is
  distinct from the key being absent (which inherits). The dry-run request count is what
  makes the difference visible before it costs anything.
- **`env:NAME`** is resolvable anywhere a string is expected. A literal string that starts
  with `env:` is **not escapable** — it is read as a reference and fails the unset-var
  check at validate time with a clear message, so it never reaches the wire. Known
  behaviour, and the safe direction to fail in.

### Pagination

One style ships, because one style is what the sources actually use. Both real shapes are
the same walk — a page number incrementing from `start` — differing only in *where* the
cursor goes, which is what `at` says:

```yaml
paginate: { style: page_number, at: query.page, has_more: $.has_next }
paginate: { style: page_number, at: payload.pageLink.page, has_more: $.hasNext }
```

| key | meaning |
|---|---|
| `style` | `page_number`. Adding another is a new `Literal` member plus a strategy function. |
| `at` | dotted path rooted at `query.` or `payload.`; the payload may nest, a query param may not |
| `start` | first cursor value, default `0` |
| `has_more` | JSONPath; falsy stops. Absent: stop on an empty page. |

- **Page size is not a pagination concept.** `per_page` / `pageSize` are ordinary literals
  in `query` or `payload`, sitting wherever the API wants them. Writing `size:` in a
  `paginate` block is a validation error.
- **One file per page.** Never merge pages into one array — merging is a downstream
  concern and it destroys the record of which page a row came from. A paginated endpoint
  whose `output` template has no `{page}` is a validation error, because every page would
  otherwise overwrite the last.
- An empty page stops the walk even if `has_more` says otherwise, and a `has_more` path
  that matches nothing stops rather than continuing — an unreadable stop condition is not
  a reason to keep hitting production. "Empty" means a falsy body, or a mapping whose
  list-valued keys are all empty, so `{"data": [], "total": 0}` ends the walk with no flag.
- `max_pages` is a hard cap enforced regardless. A misread stop condition must not turn
  into an unbounded loop against a production API. A warning is logged when it bites.
- **A skipped page is read back off disk**, not re-fetched. Without that, resuming a walk
  without `--force` would skip page 0, have no body to test `has_more` against, and stop
  dead — never reaching page 1.

### Auth

One strategy per `auth.type`, behind a single interface that returns the headers to apply.
Ship: `basic`, `bearer`, `header`, `oauth_client_credentials`, `login_token`.

```yaml
auth: { type: basic, username: env:U, password: env:P }
auth: { type: bearer, token: env:T }                    # Authorization: Bearer <token>

auth:                                                   # one or more credential headers
  type: header
  headers:
    X-API-Key: env:K                                    # sent as-is
    X-Authorization: { value: env:K, template: "ApiKey {value}" }
```

- **`header` takes a mapping**, not a single pair, because some APIs want two at once.
  A bare string means "send the value as-is"; `{value, template}` wraps it.
- **A secret template must contain its placeholder and nothing else** — `{value}` for
  `header`, `{token}` for `apply`. Writing `template: "ApiKey {API_KEY}"` is the natural
  first guess and would send a *constant* header with the secret dropped, failing as a 401
  that looks like a credentials problem rather than a config typo.
- Auth is acquired **once per run per source** and cached in memory.
- On a `401` mid-run: refresh the credential **once**, retry the request, then fail.
  A chained fan-out can run for an hour and outlive a token — this will happen. For
  `basic` and `header` there is nothing to refresh, so this only matters for the two
  token-exchange types.
- Secrets are only ever `env:` references in YAML, and acquired tokens register themselves
  with the log scrubber.

---

## 8. Behaviour

### Validation runs before any network call

`python -m api_extractor validate <source>`, and implicitly at the start of every run.
Checks live behind an **id registry**, with a declared set of expected ids asserted against
it before anything runs: a check that silently fails to register would make validation pass
vacuously, which is worse than not having the check. `--show-checks` prints what ran, and
what is deferred to a later phase with its reason (nothing is deferred today).

Report **all** failures at once, not the first. Failing on request 400 of 900 because a
provider name was misspelled is the exact outcome this prevents.

| check | catches |
|---|---|
| `config.parse` | YAML errors, unknown keys, wrong shapes — pydantic reports all of them |
| `config.providers.declared` | `{from: x}` naming a provider that is not declared |
| `config.providers.fn_registered` | a `fn` that is not a registered provider function |
| `config.providers.args_match` | YAML `args` that do not fit the function's signature |
| `config.providers.depends_on_targets` | chaining off an endpoint that does not exist |
| `config.dag.acyclic` | a dependency cycle, reported with its path |
| `config.markers.malformed` | a typo'd marker that fell through to a literal dict |
| `config.params.no_collision` | two markers resolving to one param name; an unnameable marker |
| `config.path.bind_match` | `{placeholder}` in `path` with no `bind` entry, and vice versa |
| `config.output.template_resolvable` | an `output` placeholder that resolves from nothing |
| `config.reserved.namespace` | config referencing a runner-owned `__name__` |
| `config.paginate.target` | the cursor landing on a marker, a missing payload, a nested query param |
| `config.paginate.output_page` | a paginated endpoint whose output path has no `{page}` |
| `config.env.vars_set` | every `env:` var, all of the missing ones at once |

### Output paths

- `{slug}` is the default: resolved params sorted by key, joined and slugified. No params
  slugs to `all`, so a parameterless endpoint lands on `.../tenant_info/all.json`.
- Slugify: lowercase, non-alphanumerics -> `-`, collapse repeats, truncate to 100 chars,
  and if truncation occurred append a short hash of the full value so files stay unique.
- Available placeholders are the endpoint's params plus `source`, `endpoint`, `page` and
  `slug`. Intrinsics win a name clash, so `output/{source}/...` is always the source.
- Same params + same page = same path. Reruns are idempotent.
- Skip if the file exists unless `--force`. Record the skip in the manifest.

### Retry and rate limiting

- Retry on `429` and `5xx` with exponential backoff + jitter; honour `Retry-After`, in
  either seconds or HTTP-date form, capped at 300s so a hostile header cannot park the run.
  A malformed `Retry-After` falls back to normal backoff rather than raising.
- **Never retry other `4xx`** — save the envelope and move on.
- Rate limit is a simple source-wide sleep between requests.
- A failed request does not abort the run. Log it, record it in the manifest, continue.
  One dead endpoint should not cost me the other twenty. The same holds for an endpoint
  that cannot be *planned* — a missing spreadsheet, a parent that has not run — which is
  recorded and skipped while the rest still run.

### Logging

- Structured, one line per request: endpoint, params, status, duration, path.
- `-v` traces each request and response at DEBUG: method, URL, query, payload, headers,
  then status, response headers and a 500-character body preview. This is for debugging a
  new source, where the payload you sent is usually the thing you got wrong. Headers are
  redacted **at the call site**, so a credential never reaches a log record — the scrubbing
  formatter below is the second line of defence, not the only one.
- **Scrub secrets in the logging formatter**, not at call sites. An unhandled `httpx`
  exception will otherwise print the full request including headers.
- The scrubber has a public `register_secret(value, *, name=None)`, not just
  auto-registration of resolved `env:` values. The `login_token` bearer never passes
  through the environment, and it is the single most likely thing to leak.
- The minimum-length floor (so a `pageSize=100` read from env does not redact every `100`
  in the logs) applies only to values that came from a named env var, and **vars whose name
  matches `PASSWORD|PASS|TOKEN|SECRET|KEY` are exempt** from it. Short dev passwords are
  exactly the credentials that get reused elsewhere. A value registered without a name is
  unconditional.
- `env:` refs are **not** substituted into the parsed config object; they stay as refs and
  resolve at point of use. This keeps the whole config safe to print in `--dry-run`.
- `--dry-run` prints the resolved plan — DAG order, row counts per provider, request count
  per endpoint, and any endpoint that could not be planned — then exits without issuing
  anything. This is the main safety valve before a large fan-out.

### CLI

```
python -m api_extractor run <source> [--endpoint NAME]... [--limit N] [--no-limit]
                                     [--dry-run] [--force] [--run-id ID]
python -m api_extractor validate <source> [--show-checks]
python -m api_extractor list-sources
python -m api_extractor list-providers
```

`-v` before the subcommand turns on debug logging.

`--endpoint` re-runs a subset. `from_output` reads whatever is already on disk, so
`python -m api_extractor run test --endpoint measures` works against yesterday's
asset files without re-hitting `/assets/search`.

---

## 9. Secrets hygiene

- `.gitignore` covers `.env`, `output/`, `input/`, `*.pem`, `*.key`. Output samples are
  real production data, not just structure.
- `.env.example` ships every key with empty values. It is the only documentation of what a
  source needs to run.
- Fail fast on missing env vars during validation, reporting all of them.
- Namespace vars by source (`TB_USER`, `TB_PASSWORD`) — several sources will each have
  something called "token".
- Client certs: the env var holds a **path**, and the file lives outside the repo.

---

## 10. Build order — complete

Built one phase per session, tests passing before moving on.

1. **Skeleton + config.** Pydantic models, YAML loader, `env:` resolution, `validate` with
   full error aggregation behind a check-id registry.
2. **Providers.** Registry + decorator with the `depends_on` hook and duplicate-name
   raising, loading from `providers/`, `literal`. Plus `config/graph.py`: dependency graph
   and cycle detection, with `fn_registered` / `args_match` / `depends_on_targets` /
   `dag.acyclic` going live.
3. **Plan.** `{from:}` resolution at any depth, `fan_out`, limits, path/output templating,
   consuming the ordering from `config/graph.py`. Pure, no network. `--dry-run` works.
4. **Execute + persist.** HTTP client, auth strategies, retry, rate limit, envelope writer
   + redaction, output paths, manifest. Single-page requests only.
5. **Pagination.** The walk, cursor injection at `at`, one file per page, `max_pages`.
6. **`from_output` + chaining.** The full `test.yaml` runs end to end.
7. **README.**

Two things the original order did not anticipate:

- **`from_output` was registered in phase 2 but implemented in phase 6.** Registration and
  the `depends_on` hook are config-layer facts; only the body needs envelopes. Splitting
  them is what made the dependency graph real and testable two phases early — and kept the
  reference source validating in the meantime.
- **Phase 6 forced planning to interleave with execution** (see §5). Planning everything up
  front cannot work once an endpoint reads another's output.

## 11. Testing

269 tests. **No test makes a real network call** — `pytest-httpx` throughout.

**The suite owns its own data.** Nothing under `tests/` reads `config/sources/`, `input/`
or `output/` — those belong to whoever is using the tool, and a suite that depends on them
breaks the moment a source is renamed or replaced with a real one. It did, once, which is
how this rule got written. The suite's complete source is `tests/fixtures/reference.yaml`,
which uses only built-in providers so it needs no spreadsheet either. CLI tests build a
throwaway project in `tmp_path` rather than running against the working tree.

Two tests are *about* project-owned files — `config/TEMPLATE.yaml` and
`providers/excel_column.py` — and load what they need themselves, so the coupling is
visible at the point where it exists rather than hidden in a shared fixture.

- Config: valid file parses; each validation rule has a failing fixture that produces a
  clear message. One deliberately broken source trips **every** registered check in a
  single pass, so the aggregation guarantee is not a fiction.
- Plan: `product` vs `zip`; `zip` length mismatch raises; multi-field provider stays
  row-wise; single-field rows unwrap; limits at every level of the cascade; nested payload
  markers resolved; slug collisions get distinct paths.
- DAG: correct order, deterministic within a level, cycle detected and named.
- Pagination: the walk stops on `has_more`, on an empty page, and at `max_pages` when the
  stop condition never fires; the cursor lands in a query param and at depth in a payload;
  a resumed walk continues past pages it already has.
- Persist: redaction actually removes the token, including a header known only because auth
  set it; non-JSON body lands in `body_raw`; a 403 is saved rather than dropped.
- Auth: every strategy acquires and applies; `login_token` is acquired once per run; a
  mid-run 401 refreshes once then succeeds, and a persistent 401 gives up after one.
- Chaining: the reference source runs end to end — all four endpoint shapes, `measures`
  chained off `assets`, `parents` lineage, and an asset under two types carrying both.
- Docs: `config/TEMPLATE.yaml` validates clean and is asserted to demonstrate every auth
  type, pagination style and `fan_out` mode; `README.md` is checked for its command list,
  provider list and check count.

## 12. Definition of done — met

- `validate test` reports every problem in one pass on a deliberately broken file.
- `run test --dry-run` prints the DAG order and an accurate request count.
- `run test` produces envelopes for all four endpoint shapes, with `measures`
  correctly chained off `assets` and carrying `parents` lineage.
- Rerunning is a no-op without `--force`.
- No secret appears in any file under `output/`, in the manifest, or in any log line.
- `python -m pytest` passes.

---

## 13. Working agreement

- **Ask before adding a dependency** not listed in §4.
- **Ask before adding a YAML key** not in §7. Config surface area is the thing most likely
  to rot; every key needs to earn its place. `auth.request.basic` was added and then
  removed on the same day for failing that test, and three pagination styles went with it.
- If something in this spec is ambiguous or looks wrong once you are in the code, say so
  and propose the fix. Do not silently reinterpret it.
- Prefer small pure functions with explicit arguments over classes holding state.
- Do not write defensive `try/except` around logic errors. Let bugs surface. Catch only
  at the network and filesystem boundaries, where failure is expected. The one broad
  `except` is around calling a provider, because a provider body is an I/O boundary and one
  unplannable endpoint should not hide the other nineteen.
- Do not add features from §3 even if they seem obviously useful.
- Anything that is documentation — the template, the README — gets a test. Documentation
  that drifts costs a reader time before it costs them trust.
