# `scripts/illustrative_target.py`

## Role in the pipeline

**Not pipeline output, ever** — the file's own docstring says this three
separate times, in increasingly emphatic language, and the generated PDF
carries a large on-page warning banner saying the same thing. This script
exists to answer a different question than any real pipeline step: *"what is
this whole project actually trying to produce?"* It hand-writes a plausible
finished annotated CRF — real SDTM domain codes, variable names, and
codelists, chosen by a human, not proposed by Copilot, not reviewed, not
derived from any actual data — and draws it onto the synthetic CRF so a
newcomer to the project (or a reviewer of the design) can see the target
shape of the deliverable without waiting for a real end-to-end Copilot round
trip to happen.

What's real here and what's invented, stated directly in the docstring:

| | |
|---|---|
| **Real** | Field *positions* — taken from `extract.py` run against the actual synthetic CRF, so this also functions as a rough prototype of where `stamp.py`/`xfdf_to_pdf.py` should be placing text |
| **Invented** | Every domain, variable, condition, and codelist shown |

This distinction matters for a specific, easily-made mistake: confusing this
script's output with `preview_extraction.py`'s output. That other script
shows a field's **caption as printed on the form** ("Subject Identifier") —
the *input* to the Copilot step. This script shows the **SDTM annotation**
("DM.SUBJID") — the *output* of the Copilot step. They look superficially
similar (boxes and labels on a copy of the same PDF) but represent opposite
ends of the pipeline.

## Python concepts you'll see here

**A dict of lists of dicts as inline configuration data.**
```python
PAGE_ANNOTATIONS: dict[int, list[dict]] = {
    0: [
        dict(anchor="DM_SITEID", text="DM.SITEID", place="below"),
        ...
    ],
    1: [ ... ],
}
```
Rather than pydantic models or dataclasses, this script's annotation list is
just plain dicts, keyed by page index. This is a reasonable, deliberate
downgrade in structure for a script that is explicitly *not* pipeline logic —
there's no need for validation or reuse elsewhere, so the lightest-weight
data shape that gets the job done (a literal dict, built with the `dict(...)`
constructor call syntax as an alternative to `{...}` literal syntax — both
produce the same thing, `dict(key=value)` just reads slightly more like a
function call) is a fine choice here, in contrast to the heavier `BaseModel`/
`dataclass` types used for anything that crosses a real pipeline boundary.

**Building up a shared structure with a `for` loop, after its literal
definition.**
```python
VS_TESTS = [("SYSBP", "VS_SYSBP_RES"), ...]
for testcd, anchor in VS_TESTS:
    PAGE_ANNOTATIONS[1].append(
        dict(anchor=anchor, text=f"VSORRES when VSTESTCD = {testcd}", place="fixed", at=(470, None))
    )
```
This is module-level code that runs once at import time, *after*
`PAGE_ANNOTATIONS` has already been defined as a dict literal — appending
five more entries to page 1's list programmatically rather than writing them
out by hand, since they follow a completely regular pattern (one entry per
vital-signs test). Worth noticing: this isn't inside any function — it's
top-level module code, which is legal in Python (a module's body is just a
sequence of statements executed in order the first time it's imported), if
somewhat less common to see used for data construction like this outside of
a script meant to be run directly rather than imported as a library module.

**Chained string splitting to pull structure back out of display text.**
```python
def _parse_text(text: str) -> dict:
    if text.startswith("["):
        return dict(kind=AnnotationKind.NOTE, origin=Origin.NOT_SUBMITTED)
    body, _, condition = text.partition(" when ")
    body = body.split("  (")[0].split("  --")[0].strip()
    domain, _, variable = body.partition(".")
    ...
```
`str.partition(sep)` splits a string into a 3-tuple: `(before, sep,
after)` — if `sep` isn't found, it returns `(original_string, "", "")`. It's
a cleaner tool than `str.split(sep, 1)` when you specifically want to know
*whether* the separator was present (via checking if the middle element is
empty) as well as the two halves. This function reverses the formatting
`_place`/the dict literals did (`"VS.VSORRES when VSTESTCD = SYSBP"` → domain
`"VS"`, variable `"VSORRES"`, condition `"VSTESTCD = SYSBP"`) so that, further
down, an actual `SdtmAnnotation` record can be constructed alongside the
drawn text — the point being that this hand-drawn mockup still produces a
real, structurally valid `AnnotationSet`, useful for exercising downstream
code (like a future review UI) against realistic-looking data even before any
Copilot round trip has happened.

**Unpacking a dict into keyword arguments with `**`.**
```python
annotations.append(
    SdtmAnnotation(
        annot_id=f"p{page_index + 1}-{n:02d}",
        field_id=anchor,
        page_index=page_index,
        bbox=fitz_rect_to_bbox(placed, h),
        source_model=MOCKUP_SOURCE,
        **_parse_text(spec["text"]),
    )
)
```
`**_parse_text(spec["text"])` spreads that function's returned dict
(`{"kind": ..., "domain": ..., "variable": ..., "condition": ..., "origin":
...}`) into the `SdtmAnnotation(...)` call as individual keyword arguments —
equivalent to writing `kind=..., domain=..., variable=..., condition=...,
origin=...` by hand, but generated dynamically from the parsed text instead.

## Functions, in file order

### `_place(page, rect, text, spec) -> pymupdf.Rect`
Computes where a piece of annotation text should be drawn, given a
placement mode (`"fixed"`, `"right"`, or the implicit "below" fallback) from
the spec dict. For `"fixed"`, uses an explicit hand-chosen `(x, y)` — and if
`y` is `None`, inherits the anchor field's own baseline
(`y = rect.y1 - 3`) — this is the trick used for the repeating VS test rows,
where only the x-position needs to be fixed (all annotations line up in one
column) but each row's y-position should follow its own field. For `"right"`,
checks whether the text would actually fit before the page's right margin;
falls back to placing it below the field if not.

### `_parse_text(text) -> dict`
Described above — reverses the display-text formatting back into structured
fields.

### `build(pdf_path, out_dir) -> tuple[Path, AnnotationSet]`
The main construction function. For each page: draws a bordered "domain
marker" box (e.g. a boxed "DM" or "VS") near the top of the page and records
it as a `DOMAIN`-kind `SdtmAnnotation`; then, for every entry in that page's
`PAGE_ANNOTATIONS` list, resolves the anchor field's position (or a fixed
absolute position if `anchor is None`, used for a page-furniture
`[Not Submitted]` marker with no specific field to attach to), calls
`_place` to decide exact placement, draws the text, and appends a matching
`SdtmAnnotation`. Finally calls `_draw_notice` on each page (the "this is not
real" banner) and saves both a PDF and a PNG-per-page rendering, plus sets
document metadata explicitly labeling it as an illustrative mockup.

### `_draw_notice(page, page_index) -> None`
Draws the "ILLUSTRATIVE MOCKUP — NOT PIPELINE OUTPUT" warning box, using
`page.insert_textbox` (a PyMuPDF method that wraps text within a rectangle
automatically, unlike `insert_text` which draws a single line at a fixed
baseline — used here because the warning text is a full paragraph, not a
short label). Page 2 additionally gets a legend listing SDTM annotations a
*real* finished aCRF would also carry but that this mockup omits for
clarity — an honest acknowledgment that even this hand-drawn "goal" picture
is itself simplified relative to a truly complete example.

### `main(argv=None) -> int`
Parses `--out-dir` and `--pdf` (defaulting to the synthetic AcroForm fixture,
generating it first via `make_sample_crf` if it doesn't exist yet — the same
"generate on demand" pattern seen in `preview_extraction.py`), calls `build`,
and prints the output paths plus a closing reminder that this is a
hand-written mockup, not a deliverable.
</content>
