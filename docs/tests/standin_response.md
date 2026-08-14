# `tests/standin_response.py`

## Role in the pipeline

**Not a pipeline file, and explicitly not a Copilot reply**, in the most
emphatic terms the docstring can manage (a `.. danger::` reStructuredText
admonition — a directive normally used to flag genuinely hazardous
situations, repurposed here to make sure nobody mistakes this for real test
coverage of the parser's chat-UI resilience). Its purpose is narrower and
more honest: it lets tests exercise the **full pipeline wiring** — extract →
batch → ingest → XFDF → PDF — without a human sitting at a keyboard pasting
into Copilot 365 for every test run.

The distinction matters enough that it's worth restating precisely, because
it maps onto a real, still-open risk in the project (see the repo root
README's "The one manual step"): this module proves the pipeline is
*connected end-to-end*. It proves **nothing** about whether
`pipeline/parse_response.py`'s recovery logic actually handles what a real
Copilot 365 reply looks like — that round trip, per the project's own status
notes, has not yet happened. When it does, the plan is to save the real reply
as a fixture and extend `tests/test_parse_response.py` from what Copilot
*actually* did, not from what this file guessed it might do.

## Python concepts you'll see here

**A lookup-by-prefix dict, plus a special-cased pattern matcher.**
```python
_MAPPINGS = {
    "DM_SITEID": ("variable", "DM", "SITEID", None, None, "Collected"),
    ...
}

def _map_field(acroform_name: str):
    if acroform_name.startswith("VS_"):
        parts = acroform_name.split("_")
        if len(parts) == 3 and parts[1] in _VS_TESTS:
            ...
    for prefix, mapping in _MAPPINGS.items():
        if acroform_name.startswith(prefix):
            return mapping
    return ("variable", None, None, None, None, "Collected")
```
This hand-writes, in miniature, the same kind of decision a real Copilot
reply would represent — mapping a field's AcroForm name to a plausible SDTM
domain/variable — but as plain, deterministic Python logic instead of a
model's judgment. The vital-signs case is handled specially first (because it
needs to extract a test code like `SYSBP` from the middle of the name and
build a `condition` string, which a flat prefix table can't express), then
falls through to a simple prefix-matching loop over `_MAPPINGS`, with an
unconditional fallback tuple as the last resort so this function always
returns *something* rather than raising for an unrecognized field.

**Returning a plain tuple instead of a named structure.** `_map_field`
returns a bare 6-tuple (`kind, domain, variable, condition, codelist,
origin`), immediately unpacked by its one caller:
```python
kind, domain, variable, condition, codelist, origin = _map_field(f.acroform_name or "")
```
No dataclass or `NamedTuple` here — a reasonable choice for a private
helper used in exactly one place, where a named structure would add
ceremony without adding clarity (the unpacking site names each element
immediately anyway).

**Building CSV or markdown-table output from the same row data.**
`build_response` constructs one list of plain dicts (`rows`), then branches on
`as_markdown_table` to decide how to *render* it — either as a CSV via
`csv.DictWriter` (the same technique `prompt.py`'s `build_spec_sheet` uses,
see [../pipeline/prompt.md](../pipeline/prompt.md)) or by hand-joining `" | "`
around each cell to fake a markdown table. Keeping the row-data construction
and the rendering format as two separate steps means both formats are
guaranteed to represent the *same* underlying data — useful for a test module
whose whole purpose is checking that both formats parse to the same result.

**Simulating specific chat-UI mangling with targeted `str.replace` calls.**
```python
if smart_quotes and rows:
    body = body.replace('"', "“", 1).replace('"', "”", 1)
if chatty:
    body = (
        "Sure! Here's the completed spec sheet with SDTM annotations:\n\n"
        f"{body}\n\n"
        "Let me know if you'd like me to revisit any of these mappings."
    )
```
`str.replace(old, new, 1)` — the third argument limits the replacement to
just the **first** occurrence, used here to simulate a chat renderer swapping
only the first straight quote to a curly one (a more realistic, partial
corruption than swapping every quote in the document, and specifically
exercises the parser's ability to recover from *inconsistent* mangling rather
than a fully-transformed document).

## Functions, in file order

### `_map_field(acroform_name) -> tuple`
Described above: maps a synthetic CRF's AcroForm field name to a plausible
`(kind, domain, variable, condition, codelist, origin)` tuple.

### `build_response(fieldset, page_indexes, *, as_markdown_table=False, chatty=False, smart_quotes=False, drop=()) -> str`
Builds a complete, syntactically-plausible "Copilot reply" covering every
field on the given pages (sorted into the same top-to-bottom reading order
used elsewhere — `sorted(..., key=lambda f: (f.page_index, -f.bbox.y1,
f.bbox.x0))`), skipping any `field_id` listed in `drop` (used by tests that
want to simulate an *incomplete* reply). All four keyword-only flags
(`as_markdown_table`, `chatty`, `smart_quotes`, `drop`) are opt-in
"mangling" toggles a calling test can combine to simulate different failure
modes — e.g. `test_full_loop_from_pdf_to_annotated_pdf` in
[test_pipeline.md](test_pipeline.md) passes `as_markdown_table=True,
chatty=True` together, simulating the *combination* of failures a real chat
reply is likely to exhibit simultaneously, not just one at a time.
</content>
