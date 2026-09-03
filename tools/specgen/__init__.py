"""Generate the extraction specification a source hands to an external team.

A layer *above* the extractor: it reads the config layer, the plan, the persist layer and
whatever a run left on disk, and it writes documents. Nothing under `src/` imports it, and
it never writes anything the extractor reads.

Four inputs, assembled into one intermediate model before anything is rendered:

    config/sources/<name>.yaml        the mechanical facts (method, path, params, graph)
    config/specs/<name>.spec.yaml     the human knowledge the YAML cannot hold
    config/specs/TEMPLATE.docx        the French Word template, edited in Word
    output/_runs + envelopes          the empirical facts (volumes, shapes, samples)

`model.py` is the checkpoint: everything downstream is a projection of the JSON it builds,
so the document is testable without opening Word. Identifiers, comments, tests and CLI
output are English like the rest of the repo; French appears only in text a reader of the
generated document will see, and that lives in `labels.py`, `contract.py` and the template.
"""
