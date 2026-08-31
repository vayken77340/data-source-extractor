# Your providers go here

A **provider** supplies values for a request parameter — an Excel column, a literal list, a
JSONPath into responses already on disk. Anything that answers "what should I put in this
parameter, and what should I put there next?".

Drop a `.py` file in this directory. Every one is imported at startup, so a provider
registers itself by existing. Nothing else to wire up, no import list to maintain, and no
reason to read the framework's code.

```python
# providers/csv_column.py
from api_extractor.providers import ProviderContext, provider


@provider("csv_column")
def csv_column(ctx: ProviderContext, *, path: str, column: str) -> list[dict[str, object]]:
    """Read one column out of a headerless CSV."""
    with open(path, encoding="utf-8") as handle:
        return [{column: line.strip()} for line in handle if line.strip()]
```

Then name it from a source, with the args your function takes:

```yaml
providers:
  regions:
    fn: csv_column
    args: { path: input/regions.csv, column: region }

endpoints:
  alarms:
    method: GET
    path: /alarms
    query:
      region: { from: regions }
```

`python -m api_extractor list-providers` will show it. `validate` will tell you if the
args in YAML do not fit your signature.

## The five rules

1. **Return a list of dicts, never a list of scalars.** One dict is one row. This is what
   lets a single provider fill several parameters off the same source row and keep them
   correlated — `assetType` and `region` from one spreadsheet line stay together instead of
   being crossed against each other.

2. **Take keyword-only arguments.** Everything after `ctx, *` is what YAML `args` may set.
   They are checked against your signature during `validate`, so a typo is a clear message
   before anything is sent rather than a `TypeError` 400 requests in.

3. **Names are unique.** Registering a name twice raises, including over a built-in.
   Last-wins across two files is miserable to debug.

4. **Args come from YAML, never code.** No `eval`, no dotted import paths, no expression
   strings. If a provider needs to do something, it does it in Python, here.

5. **Let errors raise.** A missing file or a bad sheet name should be loud. The runner
   catches it, reports that endpoint as unplannable, and still plans the others.

## `ctx`

| | |
|---|---|
| `ctx.run_id` | the current run's id |
| `ctx.output_root` | where envelopes are being written |
| `ctx.source_name` | the source being run |
| `ctx.outputs_for(endpoint)` | envelopes already on disk for an endpoint |

## Chaining off another endpoint

If your provider reads another endpoint's output, say so with `depends_on`. That is the
only way the run order is worked out — there is no `depends_on` key in YAML, and the
planner never learns your provider's name.

```python
@provider("first_id", depends_on=lambda args: [args["endpoint"]])
def first_id(ctx: ProviderContext, *, endpoint: str) -> list[dict[str, object]]:
    return [
        {"id": saved.body["items"][0]["id"], "__parents__": [str(saved.path)]}
        for saved in ctx.outputs_for(endpoint)
    ]
```

`depends_on` takes the YAML `args` and returns the endpoint names they imply. Naming an
endpoint that does not exist, or creating a cycle, is a validation error with the path
spelled out.

`__parents__` records lineage — which file this value came from. The runner strips every
`__key__` before the row becomes request parameters, so a source can never reference one.

## Built-ins

Two ship with the framework, because they are generic to it rather than to anyone's data:

| | |
|---|---|
| `literal` | rows written inline in YAML |
| `from_output` | a JSONPath into a previous endpoint's saved envelopes |

Everything else is local, including `excel_column` in this directory — reading a
spreadsheet is a fact about a particular business, not about HTTP. Treat it as a worked
example of everything above: several columns kept row-wise, blanks dropped, errors raised
loudly, and no import from anywhere inside `src/`.

`list-providers` shows both kinds with their arguments.

## Parameter files

Reading the business spreadsheet at run time works, but it puts the messiest input you have
inside the hot path. `tools/build_params.py` moves it to a boundary you control: it turns a
sheet into a **parameter file**, which is provenance plus rows.

```
python tools/build_params.py input/asset_types.xlsx \
    --sheet Referentiel --columns assetType,measureType --ffill assetType \
    -o config/params/asset_types.json
```

```json
{ "schema": "param-file/1", "source_file": "input/asset_types.xlsx",
  "sheet": "Referentiel", "generated_at": "2026-08-31T09:12:04Z",
  "columns": ["assetType", "measureType"],
  "rows": [{"assetType": "PUMP", "measureType": "temperature"}] }
```

`--ffill` is the merged-cell declaration, and it is the one thing that cannot be guessed:
openpyxl returns `None` for every cell of a merged range but the top-left, so without it a
merged type silently drops every row after its first. That failure produces a run that
looks healthy and makes half the requests it should — which is the whole reason this step
exists rather than a flag on a provider.

The container is fixed; what is inside a row is whatever that sheet has. So one provider,
`param_file`, reads every parameter file you will ever generate, and a new spreadsheet
means a new invocation of the build script rather than new code. It reads JSON or YAML —
a second *encoding* for small hand-maintained tables, never a second structure.

Two rules keep this from rotting:

- **The build script does not live here.** Everything in this directory is imported at
  startup; a build script would run during `validate` and `list-providers`.
- **`param_file` gets no selector argument.** A JSONPath into your own file would make YAML
  `args` a query language, which rule 4 rules out. `from_output` takes one because it reads
  the *API's* JSON, whose shape is not yours. A structure you author yourself does not need
  one — and if something genuinely is not row-shaped, write a second provider.

## Joining a parameter file against fetched output

`measure_keys_for_assets` is the worked example of a correlated join. Measure types are a
fact about an asset's *type*, so asking a valve for its temperature is wrong — and the API
answers it with an empty 200, so the mistake costs a round trip and looks like data.

`fan_out` cannot express this: `product` crosses every asset against every measure type,
and `zip` pairs them positionally. The join has to happen in Python, and the key it joins
on survives the round trip in the envelope's `metadata.params`, which records the params
each request was planned with.

```yaml
providers:
  asset_measures:
    fn: measure_keys_for_assets
    args: { path: config/params/asset_types.json, endpoint: assets }

endpoints:
  measures:
    method: GET
    path: /assets/{id}/measures
    bind:  { id:   { from: asset_measures } }
    query: { keys: { from: asset_measures } }   # "temperature,humidity" for a pump
```

Both markers name the same provider, so `id` and `keys` are filled from one row and are
never re-combined. One row per asset rather than one per (asset, measure) pair is
deliberate: pair rows would collide in `output/…/measures/{id}.json`, and `limit` would
then cap pairs instead of assets.
