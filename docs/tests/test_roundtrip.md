# `tests/test_roundtrip.py`

## Role in the pipeline

This file exists to satisfy a specific requirement stated in the project's
own build instructions: two tests, each written **before** the pipeline code
downstream of it, that gate specific steps by asserting on actual numbers and
actual pixels rather than on "does this look right." The file's own docstring
explains *why* that extra rigor is warranted here, more than almost anywhere
else in the codebase: a y-flip bug is not reliably visible. An annotation
placed at `page_height - y` instead of `y` still lands somewhere on the page,
still looks like a plausible annotation sitting near *some* field — and only
reveals itself when a human notices the label is next to the *wrong* one,
which is exactly the kind of subtle, easy-to-miss error class this file is
built to catch mechanically instead of relying on visual review.

The two gated steps:
- The **step-3 gate** (extraction) — proves `fitz_rect_to_bbox` is correct
  *before* anything else in the pipeline is built on top of extracted
  coordinates.
- The **step-9 gate** (final rendering) — proves an `SdtmAnnotation` survives
  a full `xfdf.py` → `xfdf_to_pdf.py` round trip with its position intact.

## Python concepts you'll see here

**A shared test helper with a documented PyMuPDF footgun explained inline.**
```python
def read_annots(pdf_path, page_index: int = 0) -> list[tuple[str, tuple]]:
    """...
    PyMuPDF footgun: ``pymupdf.open(p)[0]`` builds a temporary Document *and*
    a temporary Page, both freed as soon as the expression ends. Any Annot
    read afterwards is a use-after-free and segfaults the interpreter rather
    than raising. So everything is read here while `doc` and `page` are still
    live locals, and only plain tuples escape.
    """
    doc = pymupdf.open(pdf_path)
    page = doc[page_index]
    out = [(a.info.get("content", ""), tuple(a.rect)) for a in page.annots()]
    doc.close()
    return out
```
This is a genuinely valuable piece of documentation for anyone using
PyMuPDF: an expression like `pymupdf.open(path)[0].annots()` looks completely
reasonable in Python (open a document, grab its first page, iterate its
annotations) but is actually dangerous, because the intermediate `Document`
and `Page` objects have no remaining Python reference once the expression
finishes evaluating — they get garbage collected, and because they wrap
*native* (non-Python) memory, using an `Annot` object obtained from them
afterward doesn't raise a clean Python exception, it **crashes the whole
interpreter** (a segmentation fault). The fix demonstrated here — keep `doc`
and `page` as explicit local variables for as long as anything derived from
them is being used, and only let plain, safe Python types (strings, tuples)
"escape" the function — is a broadly useful pattern whenever wrapping a
C-extension library that manages its own memory lifetime, not specific to
PyMuPDF alone.

**A `pytest.fixture` (not `session`/`module`-scoped) built from two other
fixtures.**
```python
@pytest.fixture
def target_page(crfs, truth):
    field = next(f for f in truth.fields if f.field_id == TARGET)
    doc = pymupdf.open(crfs["acroform"])
    return doc[field.page_index], field
```
`next(f for f in truth.fields if f.field_id == TARGET)` — a generator
expression passed directly to `next()` — finds the *first* matching item and
stops immediately (unlike building a full filtered list and indexing `[0]`,
which would search everything even after finding the answer). This fixture
returns a **tuple** `(page, field)`, and every test that requests
`target_page` destructures it: `page, field = target_page`.

**Deliberately writing a test that proves the *other* tests aren't
vacuous.** `test_skipping_the_flip_puts_the_stamp_somewhere_else` doesn't
test the geometry module at all — it tests that the *unflipped* version of
the rect lands somewhere **different** from the correctly-flipped one:
```python
correct = pymupdf.Rect(*bbox_to_fitz_rect(field.bbox, page.rect.height))
unflipped = pymupdf.Rect(*field.bbox.as_tuple())
assert not correct.intersects(unflipped)
```
Its own docstring says exactly why this matters: "Proves the tests above
have teeth. ... If that mistake still landed on the widget, none of the
assertions above would mean anything." This is a valuable, somewhat rare
testing discipline worth internalizing generally: when a test's whole
purpose is to catch a specific bug, it's worth also confirming that
introducing that exact bug *would* actually make the test fail — otherwise
the test could be accidentally insensitive to the very thing it claims to
guard against.

## Tests, by section

### Step 3 gate (extraction correctness)
- **`test_truth_bbox_converts_back_onto_the_real_widget`** — the "numeric
  half": flipping a truth-file `bbox` back to fitz coordinates matches the
  real AcroForm widget's rect to `1e-6`.
- **`test_stamped_field_lands_on_the_form_field`** — the "pixel half":
  actually draws a solid red rectangle at the flipped position, renders the
  page to a bitmap at 72 DPI (chosen specifically so 1 pixel == 1 point,
  keeping pixel and point coordinates numerically interchangeable), and
  samples pixel colors to confirm the field is unmarked *before* stamping
  and correctly marked (center, and both corners — catching a wrong-*size*
  stamp, not just a wrong-*position* one) *after*.
- **`test_the_mirrored_position_stays_blank`** — confirms the *mirror-image*
  position (where an unflipped rect *would* have landed) remains untouched —
  without this, a test only checking "the stamp landed somewhere" would pass
  even with the flip direction reversed.
- **`test_skipping_the_flip_puts_the_stamp_somewhere_else`** — described
  above: proves the tests above have teeth by confirming the deliberately-
  wrong version actually would produce a detectably different result.

### Step 9 gate (XFDF round trip)
- **`test_annotation_survives_xfdf_round_trip`** — a known `SdtmAnnotation`
  written to XFDF (`write_xfdf`) and read back (`parse_xfdf`) has an
  unchanged `bbox`, `page_index`, and rendered `text` — asserting on numbers,
  per the module docstring's reasoning, not on visual appearance.
- **`test_round_trip_lands_on_the_page_where_it_started`** — the *full*
  loop: record → XFDF → rendered PDF, checked against the original bbox
  (converted to fitz coordinates for comparison against the rendered
  annotation's actual on-page rect).
- **`test_xfdf_page_attribute_is_zero_based`** — a direct string check that
  the written XFDF file literally contains `page="0"`.
- **`test_hand_edited_xfdf_is_honoured`** — "the actual scenario the step
  exists for," per the test's own comment: manually edits the written XFDF
  file's rect and contents (simulating what a reviewer does in Acrobat or a
  text editor) and confirms the final PDF reflects the **edited** values, not
  the original ones — a stronger test than a pass-through of an unedited
  file, which the comment notes "proves much less," since the real risk is
  the renderer *quietly preferring* the pre-review data it also happens to
  have access to.
- **`test_renderer_does_not_consult_pre_review_data`** — sets the XFDF's
  `<contents>` to text that **no upstream pipeline step could ever have
  produced** (`"ZZ.ANYTHING"`) and confirms it still renders verbatim — a
  forward-looking regression guard against a future "helpful" change that
  re-derives contents from `proposals.json` and silently discards a human's
  edit.
- **`test_bbox_is_never_constructed_from_a_raw_fitz_rect`** — confirms
  feeding an inverted (fitz-style) rect directly into `BBox`'s constructor
  raises `ValueError` matching `"y-flip"` — the data model's own defense
  against this exact class of bug, tested here alongside the geometry
  functions themselves since it's the last line of defense if a flip is ever
  accidentally skipped somewhere.
</content>
