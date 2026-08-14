# `scripts/make_sample_crf.py`

## Role in the pipeline

Not a pipeline *step* so much as the thing that makes every other step
testable without a real clinical trial form. No real CRF PDFs live in this
repository — not even a fake generic one is committed as a binary file.
Instead, this script **draws** a synthetic two-page CRF from scratch using
PyMuPDF's own drawing API (invented protocol name, invented fields, no real
study data anywhere), and every page carries a printed
`"SYNTHETIC TEST DATA - NOT A REAL CRF"` banner so a stray copy identifies
itself on sight.

It produces **three variants of the same document**, because `extract.py` has
to handle all three real-world CRF export styles:

| Variant | What it represents |
|---|---|
| `..._acroform.pdf` | A CRF exported with live, fillable AcroForm widgets — the easy case |
| `..._flat.pdf` | The same layout with widgets *baked* into flat page content (boxed fields, no real form) |
| `..._ruled.pdf` | The other common "flattened" style: fill-in-the-blank underlines instead of boxes |

The flat variant is produced by literally `bake()`-ing the AcroForm document
rather than being drawn independently — so the two are **geometrically
identical by construction**, which is what makes "do the AcroForm and flat
extraction paths agree with each other?" (tested in
[../tests/test_extract.md](../tests/test_extract.md)) a meaningful assertion
rather than two unrelated implementations that happen to look similar.

Alongside the PDFs, it also builds and writes `sample_crf_truth.json` — a
serialized `FieldSet` recording the *exact*, hand-specified bounding box and
label of every field, independent of any extraction logic. This is the
**only** artifact from this script that gets committed to the repository (see
[../tests/test_fixture.md](../tests/test_fixture.md) for why: without a
committed truth file, the test suite would be circular — the generator and
the extractor, written by the same process, could quietly agree with each
other about a shared wrong assumption and the tests would pass while proving
nothing).

## Python concepts you'll see here

**Frozen dataclasses as a tiny internal "layout DSL."** `Field`, `Label`,
`Rule`, and `PageSpec` are all `@dataclass(frozen=True)` — used here not as a
performance choice but as a lightweight, readable way to describe *what's on
the page* as data (a list of `Field(...)`, `Label(...)`, `Rule(...)` objects)
rather than as a long sequence of imperative `page.insert_text(...)` calls.
The actual drawing functions (`_draw_static`, `_add_widget`, etc.) then just
interpret that data. This separation is deliberate and reused twice: the same
`layout()` function that describes what to *draw* also describes what the
*truth file* should contain (`build_truth` walks the same `Field` objects) —
one source of truth for two different outputs.

**`sys.path.insert(0, ...)` for a script outside the package.** At the top:
```python
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```
Scripts in `scripts/` aren't part of the `pipeline` package and aren't
installed anywhere — they're run directly (`python scripts/make_sample_crf.py`).
For `from pipeline.geometry import ...` to work, Python's import system needs
the *repository root* (the parent of `pipeline/`) on `sys.path`, the list of
directories Python searches when resolving an import. `Path(__file__)` is
this script's own path; `.resolve()` makes it absolute; `.parent.parent`
walks up two directories (from `scripts/make_sample_crf.py` to `scripts/` to
the repo root). Inserting at index `0` puts it *first*, so it's checked
before anything else already on the path. This exact three-line idiom
appears, unchanged, at the top of every script in this project.

**`# noqa: E402` comments.** Immediately after the `sys.path.insert` lines,
imports like `import pymupdf  # noqa: E402` appear *after* other code has
already run (the `sys.path` manipulation) — which linters like `flake8`
normally flag as error `E402` ("module level import not at top of file").
`# noqa: E402` tells the linter to suppress that specific warning on that
specific line, because the ordering is intentional here: the import has to
come *after* the path fix, or it would fail.

**`argparse` for a script's command-line interface.** `main()` builds an
`argparse.ArgumentParser`, adds one optional argument (`--out-dir`, defaulting
to `Path("fixtures")`), and parses `sys.argv` (or an explicit `argv` list,
useful for testing) into a namespace object (`args.out_dir`). This is the
standard-library way to give a script real command-line flags, used
identically across all six scripts in this project.

**`if __name__ == "__main__": raise SystemExit(main())`.** A very common
Python script pattern. `__name__` is `"__main__"` only when the file is *run*
directly (not when it's imported by something else — note
`tests/conftest.py` does `import make_sample_crf as gen`, at which point this
guard prevents `main()` from executing just because the module got imported).
`raise SystemExit(main())` treats `main()`'s return value as the process exit
code — by convention `0` means success, and `main()` here always returns `0`
in the success path, so a real error path would need to raise an exception or
return non-zero instead (this particular script only ever returns `0`).

## Functions, in file order

### `Field`, `Label`, `Rule`, `PageSpec` (frozen dataclasses)
The layout description types, described above. `Field` holds an AcroForm
field name (used as `field_id`), its kind (`"text"` or `"checkbox"`), its
rect in **fitz coordinates** (top-left origin — this whole module works in
fitz space and converts to `BBox` only when building the truth file), its
label, and its context string. `Label` is static printed text (position,
text, size, bold, color). `Rule` is a drawn horizontal line that is
**deliberately not a field** — a "distractor" meant to confuse a naive
line-detector (see "Deliberately adversarial layout" below). `PageSpec`
bundles a page's labels, rules, and fields together.

### `_chrome(page_no, form) -> tuple[list[Label], list[Rule]]`
Builds the elements present on **every** page: the SYNTHETIC banner, a
protocol/study line, a form-version footer, and a page-number footer — plus a
footer rule. The footer text (`"Form {form} v1.0"`, `"Page {page_no} of 2"`)
is deliberately non-mapped page furniture, meant to exercise the
`NotSubmitted` origin case downstream.

### `demographics_page() -> PageSpec`
Builds page 1: a Demographics section with a site ID, subject identifier,
date-of-birth (split into three adjacent fields sharing one caption — testing
that adjacent blanks correctly share a single label), age, sex/race/ethnicity
checkboxes, country, and investigator initials. Comments throughout call out
*why* each field's position was chosen: e.g. `DM_SITEID` sits near the very
top of the page and `DM_INVINIT` near the very bottom — described as "Top
outlier" / "Bottom outlier," a pair specifically placed to expose a
coordinate y-flip bug (a flipped field lands roughly correctly if it started
near the page's vertical middle, but swaps top and bottom outliers into each
other's positions unmistakably).

### `VS_ROWS` (module-level constant) and `vital_signs_page() -> PageSpec`
`VS_ROWS` is a list of `(testcd, label, unit)` tuples describing the five
vital-signs measurements (Systolic/Diastolic Blood Pressure, Pulse, Body
Temperature, Respiratory Rate). `vital_signs_page` builds page 2: a repeating
grid where **column headers sit above each field** (Assessment / Result /
Unit / Not Done) rather than to the field's left — deliberately the *other*
label-association geometry from page 1, so `extract.py`'s "try left, then
above, then right" fallback logic (see [../pipeline/extract.md](../pipeline/extract.md))
gets exercised both ways. The row-building loop:
```python
for i, (testcd, test_label, unit) in enumerate(VS_ROWS):
    y0 = 155.0 + i * 30.0
    ...
```
computes each row's vertical position from its index (`155.0 + i * 30.0`),
rather than hand-specifying five nearly-identical blocks — and gives each
row's field a `context` string embedding its own test name (`f"{ctx}; row
'{test_label}' ..."`), which is exactly what lets Copilot later distinguish
"Result" in the Systolic row from "Result" in the Pulse row despite the
field labels themselves being identical.

### `layout() -> list[PageSpec]`
`[demographics_page(), vital_signs_page()]` — the single source of truth for
the entire synthetic document, called by every other function in this file
(and by `tests/conftest.py`, `tests/test_fixture.py`) rather than any of them
re-deriving the layout independently.

### `_draw_static(page, spec) -> None`
Draws whatever's common to all three PDF variants — the rules and labels —
using PyMuPDF's `page.draw_line` and `page.insert_text`.

### `_add_widget(page, f) -> None`
Builds a real AcroForm `pymupdf.Widget` for one `Field` and adds it to the
page — used only for the AcroForm variant. Sets `border_width=1`
unconditionally, with a comment explaining why that's required rather than
optional: **a border-less widget bakes to nothing** — if `bake()` is later
called to produce the flat variant, a widget with no border leaves no visible
geometry behind at all, so the flat variant would be a blank page.

### `_draw_ruled_field(page, f) -> None`
Draws one field for the "ruled" flat variant: a checkbox becomes a drawn
rectangle; a text field becomes a horizontal underline at the field's bottom
edge (`page.draw_line(Point(x0, y1), Point(x1, y1), ...)`) — the second flat
morphology `extract.py` has to detect (a fill-in blank represented purely as
a line, with no top edge to measure directly).

### `_new_doc() -> pymupdf.Document`
Creates a blank in-memory PDF document and sets its metadata (title, subject,
author, keywords) to clearly identify it as synthetic test data even at the
file-properties level, not just in the printed banner.

### `build_acroform_crf() -> pymupdf.Document`
Assembles the AcroForm variant: for each `PageSpec` in `layout()`, creates a
page, draws the static chrome, and adds a real widget per field via
`_add_widget`.

### `build_ruled_crf() -> pymupdf.Document`
Same shape as `build_acroform_crf` but calls `_draw_ruled_field` instead of
`_add_widget` for every field — an independently-drawn variant (unlike the
flat variant, which is baked rather than separately drawn).

### `build_truth(source_pdf) -> FieldSet`
Builds the ground-truth `FieldSet` directly from `layout()`'s `Field` objects
— a nested list comprehension:
```python
fields=[
    CRFField(
        field_id=f.name,
        page_index=s.index,
        bbox=fitz_rect_to_bbox(f.rect, PAGE_H),
        label=f.label,
        source=FieldSource.ACROFORM,
        context=f.context,
        acroform_name=f.name,
    )
    for s in specs
    for f in s.fields
]
```
Two `for` clauses in one comprehension (`for s in specs for f in s.fields`)
iterate a nested structure (pages, then each page's fields) and flatten the
result into one list — equivalent to a nested `for` loop, written as a single
expression. Every field's rect is converted to `BBox` via the shared
`fitz_rect_to_bbox` (see [../pipeline/geometry.md](../pipeline/geometry.md))
— the *same* function `extract.py` itself calls, so this truth file is
subject to exactly the same coordinate conversion any real extraction would
be, rather than being hand-computed in PDF space and risking a second,
independent (and possibly differently-buggy) conversion. `extracted_at` is
pinned to a fixed constant (`TRUTH_TIMESTAMP`) rather than the current time —
explicitly noted as necessary "so the committed truth file is byte-stable
across regenerations," since a real timestamp would make every regeneration
produce a spurious diff even when nothing about the layout actually changed.

### `make_sample_crf(out_dir) -> dict[str, Path]`
**The module's top-level entry point.** Builds and saves all three PDF
variants plus the truth JSON file into `out_dir`, returning a dict mapping a
short name (`"acroform"`, `"flat"`, `"ruled"`, `"truth"`) to the `Path`
written for each — this return value is what `tests/conftest.py`'s `crfs`
fixture hands to every test that needs one of the generated files. Notably,
the flat variant is produced by *mutating* the already-built AcroForm
document in place:
```python
doc = build_acroform_crf()
doc.save(acro_path, ...)
doc.bake(widgets=True)
flat_path = ...
doc.save(flat_path, ...)
```
— saving once before baking, then baking the *same* `doc` object and saving
again — which is exactly what guarantees the two variants are geometrically
identical: there is only one document being drawn, saved twice at two
different stages of the same transformation.

### `main(argv=None) -> int`
Parses `--out-dir`, calls `make_sample_crf`, and prints a short summary
(each variant's path, total field count, a reminder that only the truth JSON
is meant to be committed). `argv: list[str] | None = None` lets tests call
`main([...])` with an explicit argument list instead of reading real
`sys.argv`, while a normal CLI invocation passes `None` and lets `argparse`
read `sys.argv` itself.
</content>
