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
