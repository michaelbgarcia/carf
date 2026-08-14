# `pipeline/extract.py`

## Role in the pipeline

This is the first real pipeline step and the largest, most algorithmic file
in the project: given a blank CRF PDF, find every fillable field on it and
figure out its caption text. Its single public entry point,
`extract_fields(pdf_path) -> FieldSet`, is what `scripts/extract_and_prompt.py`
calls to kick off the whole pipeline.

Two detection strategies are tried, per page, in order:
1. **AcroForm** — if the PDF still has real fillable form widgets (the
   "good case"), each widget's rectangle and name are read directly.
2. **Text/line fallback** — if the PDF has been flattened (form widgets baked
   into static page content), fields are *inferred* from drawn boxes,
   drawn underlines, and small square checkboxes.

Whichever path runs, every `CRFField` it produces has its `bbox` already
converted to PDF user space via `pipeline.geometry.fitz_rect_to_bbox` (see
[geometry.md](geometry.md)) — a raw fitz rectangle never escapes this module.

The other half of the file's job is **label association**: matching each
detected field shape to the nearest caption text, since real CRFs use at
least three different layouts (label to the left of a text field, label to
the right of a checkbox, label as a column header above a grid cell) — and
building a `context` string that gives Copilot enough surrounding text to
disambiguate rows that would otherwise look identical (e.g. "Result" in a
Systolic Blood Pressure row versus "Result" in a Pulse Rate row) — see
`build_context` below.

## Python concepts you'll see here

**`@dataclass(frozen=True)`.** `TextRun` and `_Shape` use the standard-library
`dataclasses` module instead of pydantic. The choice matters: these are
short-lived, purely internal intermediate values (a caption's bounding box +
text, or a candidate field shape) that never cross a serialization boundary —
they're built, used, and discarded within a single call to `extract_fields`.
`@dataclass` auto-generates `__init__`, `__repr__`, and `__eq__` from the
class-body annotations, the same annotation-driven style pydantic uses, but
without validation or JSON support — the right tool when you don't need
either. `frozen=True` (on `TextRun`) makes instances immutable and hashable,
useful since `TextRun` objects get compared with `in` (see
`build_context`'s `above not in on_line` check).

**Module-level constants as tunable configuration.** Lines 56–76 define
things like `CHECKBOX_SIDE = (5.0, 24.0)` and `LABEL_MAX_LEFT = 260.0` — all
caps, at module scope. These aren't magic numbers scattered through the
functions below; they're named, grouped, and commented once at the top, so
tuning "how close must a checkbox caption be to count" means editing one
line, not hunting through the file. This is idiomatic Python for
configuration that doesn't need to be a full config object or file.

**`statistics.median`.** From the standard library — used once, in
`_headings`, to find the "typical" caption height on a page so anything
notably taller can be flagged as a section heading. Worth knowing the
`statistics` module exists for exactly this kind of one-off aggregate,
instead of hand-rolling a sort-and-index.

**Tuple destructuring for named ranges.** `lo, hi = CHECKBOX_SIDE` unpacks a
2-tuple constant into two locally-scoped names inside a function — a small
readability trick so `lo <= w <= hi` (a chained comparison, itself valid
Python and equivalent to `lo <= w and w <= hi`) reads clearly without
repeating `CHECKBOX_SIDE[0]` / `CHECKBOX_SIDE[1]`.

**`Optional[X]` return types and the "find nearest, or None" pattern.** Several
functions here (`_nearest_left`, `_nearest_right`, `_nearest_above`,
`associate_label`) return `Optional[TextRun]` and are written as a manual
loop tracking the best candidate seen so far:
```python
best: Optional[tuple[float, TextRun]] = None
for r in runs:
    ...
    if d <= LABEL_MAX_LEFT and (best is None or d < best[0]):
        best = (d, r)
return best[1] if best else None
```
This is a hand-written version of what `min(..., key=..., default=None)`
could partly express, but written explicitly because the candidates also need
to be *filtered* (by overlap and distance) before comparison — not just
ranked.

**Generators as loop targets: `for i, page in enumerate(doc)`.** A
`pymupdf.Document` is iterable — looping over it yields each `Page` in order.
`enumerate(doc)` pairs each page with its integer index, which is exactly the
`page_index` this pipeline treats as ground truth everywhere.

**`try/finally` for manual resource cleanup.** `extract_fields` opens a
`pymupdf.Document` and wraps the whole extraction in `try: ... finally:
doc.close()`. Many Python objects that manage a resource support the `with`
statement (a *context manager*) so cleanup happens automatically even if an
exception occurs — but `pymupdf.Document` predates/doesn't reliably support
that pattern the way, say, a file object opened with `open(...)` does, so this
code falls back to the manual, explicit form: whatever happens inside `try`,
`doc.close()` still runs.

**Sets for dedup and membership tests.** `seen: set[str] = set()` inside
`extract_fields` tracks every `field_id` assigned so far; `if f.field_id in
seen` is an O(1) membership check (versus O(n) for a list), which matters
here because it runs once per field across a potentially large document.

## Functions, in file order

### `TextRun` (dataclass)
A horizontally contiguous run of text — roughly, one caption. Holds a
`pymupdf.Rect` and the joined text string.

### `_v_overlap(a, b) -> float` / `_h_overlap(a, b) -> float`
Fraction of vertical (or horizontal) overlap between two rectangles, as a
number from 0 to 1 — computed as the overlapping span divided by the smaller
of the two rects' extents in that axis:
```python
inter = max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))
return inter / max(1e-6, min(a.height, b.height))
```
`max(0.0, ...)` clamps a negative "overlap" (i.e. no overlap at all) to zero;
`max(1e-6, ...)` in the denominator guards against dividing by zero for a
degenerate zero-height/width rectangle. These two functions are the building
block for both grouping words into lines (`text_runs`) and matching fields to
captions (`associate_label` and friends).

### `clean_label(text) -> str`
Normalizes whitespace (`" ".join(text.split())` collapses any run of spaces/
tabs/newlines to a single space) and strips a trailing colon plus surrounding
whitespace — turning `"  Subject   Identifier:  "` into `"Subject
Identifier"`.

### `text_runs(page) -> list[TextRun]`
Groups PyMuPDF's raw word-level text (`page.get_text("words")`, one entry per
word with its own bounding box) into caption-sized runs, in two passes:

1. **Group words into lines** by vertical overlap (≥ 50%), not by PyMuPDF's
   own block/line indices — the docstring explains why: "separately-drawn
   captions on the same visual line land in different [PyMuPDF] blocks," so
   grouping has to be geometric, not structural.
2. **Split each line into runs at wide horizontal gaps** — this is what keeps
   `"Sex:"`, `"Male"`, and `"Female"` (three words that share a printed line)
   as three separate captions rather than one run of joined text, which
   matters because they're captions for three unrelated fields.

The line-grouping loop is a good example of an **accumulator pattern with a
`for...else`**:
```python
for i, (line_rect, items) in enumerate(lines):
    if _v_overlap(line_rect, rect) >= 0.5:
        items.append((rect, text))
        lines[i] = (line_rect | rect, items)
        break
else:
    lines.append((pymupdf.Rect(rect), [(rect, text)]))
```
The `else` on a `for` loop runs only if the loop completes **without** hitting
`break` — i.e., "if this word didn't match any existing line, start a new
one." This is a genuinely useful but often-unfamiliar piece of Python syntax
worth internalizing: `for/else` (and `while/else`) mean "else if we never
broke out." Note also `line_rect | rect` — PyMuPDF overloads `|` on `Rect` to
mean "the smallest rectangle containing both," so this line grows the line's
bounding box to include the new word.

### `_headings(runs) -> list[TextRun]`
Returns every run notably taller than the page's median text height
(`HEADING_RATIO = 1.35`) — a cheap way to spot section headings like
"DEMOGRAPHICS" or "VITAL SIGNS" without any font-name or style inspection.

### `_nearest_left(runs, rect)` / `_nearest_right(runs, rect)` / `_nearest_above(runs, rect)`
Three near-identical "find the closest candidate in one direction" functions,
each filtering by overlap in the *perpendicular* axis (vertical overlap for
left/right search, horizontal overlap for above search) before ranking by
distance in the direction searched. Kept as three separate functions rather
than one parameterized function — a case where the three bodies differ enough
in which coordinate they compare that a single generic version would likely
be harder to read, not easier.

### `associate_label(runs, rect, is_checkbox) -> Optional[TextRun]`
Tries the three direction-finders **in an order that depends on field type**:
```python
order = (
    (_nearest_right, _nearest_above, _nearest_left)
    if is_checkbox
    else (_nearest_left, _nearest_above, _nearest_right)
)
for finder in order:
    found = finder(runs, rect)
    if found is not None:
        return found
return None
```
`order` is a tuple of **functions themselves** — Python functions are
first-class objects, so they can be stored in a data structure and called
later via `finder(runs, rect)`. This is what lets a checkbox try "caption to
my right" first, then fall back to "column header above me" (for a grid
checkbox with nothing to its right, like "Not Done"), then finally "caption
to my left" — encoding the priority order from the module docstring as data,
not a chain of `if/elif`.

### `_section_heading(headings, rect) -> Optional[TextRun]`
Finds the nearest heading strictly above a field, ignoring horizontal
position entirely (unlike caption lookup) — because a section heading governs
everything below it across the page's full width, and is usually left-aligned
while its fields are indented.

### `build_context(runs, headings, rect) -> str`
Builds the `context` string attached to every `CRFField` — the only
information Copilot has to disambiguate visually-identical rows, since it
never sees coordinates. Assembles up to three parts (`section: ...`, `line:
...`, `above: ...`) and joins them with `"; "`:
```python
parts: list[str] = []
...
return "; ".join(parts)
```
Same-line captions within the `line:` part are joined with `" / "`, **not**
`" | "` — the docstring calls out a real bug this fixed: `context` travels as
a CSV cell and, on the markdown-table parsing fallback path (see
[parse_response.md](parse_response.md)), as a *table* cell, where an
unescaped `|` silently shifts every subsequent column. `tests/test_extract.py`
has a regression test (`test_context_never_contains_a_bare_pipe`) guarding
against this exact bug reappearing.

### `page_geometry(doc) -> list[PageGeometry]`
One-line-per-page list comprehension collecting each page's width, height,
and rotation into `PageGeometry` records — the data every later y-flip
(`geometry.py`) needs.

### `extract_acroform_fields(page, page_index) -> list[CRFField]`
**Path 1.** Returns `[]` immediately if the page has no widgets (a page could
still have widgets and go this route while a later page in the same doc falls
back to path 2 — CRFs can be mixed). For each widget, determines whether it's
a checkbox (`w.field_type in _CHECKBOX_WIDGETS`, a `set` of PyMuPDF widget-
type constants), finds its label via `associate_label`, converts its rect via
`fitz_rect_to_bbox`, and constructs a `CRFField`. Field IDs default to the
widget's own `field_name` (`w.field_name`), falling back to a generated
`f"p{page_index + 1}_widget{len(out) + 1:03d}"` if the widget has no name —
the `:03d` format spec zero-pads to at least 3 digits (`widget007`,
`widget042`).

### `_Shape` (dataclass)
An internal record for path 2: a detected rectangle, whether it's a checkbox,
and whether its top edge was *inferred* (from an underline, which has no top
edge to measure) rather than measured directly.

### `_dedupe(rects, tol=1.0) -> list[pymupdf.Rect]`
Removes near-duplicate rectangles (drawn twice, or drawn as overlapping
strokes) using a per-coordinate tolerance:
```python
if not any(
    max(abs(a - b) for a, b in zip(tuple(r), tuple(k))) <= tol for k in kept
):
    kept.append(r)
```
Reads inside-out: `zip(tuple(r), tuple(k))` pairs up the four coordinates of
the candidate rect `r` with an already-kept rect `k`; `max(abs(a-b) for a, b
in ...)` finds the single largest per-coordinate difference; `any(... for k in
kept)` asks "is `r` within tolerance of *any* already-kept rect." `not any(...)`
means "keep it only if it's not a near-duplicate of anything already kept."

### `_shapes(page) -> list[_Shape]`
**Path 2's** geometry classifier. Walks `page.get_drawings()` (PyMuPDF's raw
vector-drawing data — rectangles and lines drawn as page content) and buckets
them:
- A drawn rectangle whose size falls in the "small and roughly square" range
  (`CHECKBOX_SIDE`, `CHECKBOX_ASPECT`) becomes a checkbox shape.
- A drawn rectangle wide/tall enough (`FIELD_MIN_WIDTH`, `FIELD_HEIGHT`)
  becomes a boxed field shape.
- A horizontal line (`abs(p1.y - p2.y) <= LINE_FLATNESS`) whose width falls
  between "too short to be a field" and "wide enough to be a section rule"
  becomes an **inferred** fill-in-blank field, with a synthetic rectangle
  built above the line: `pymupdf.Rect(x0, y - INFERRED_FIELD_HEIGHT, x1, y)`.

The width check on lines (`width < FIELD_MIN_WIDTH or width > max_blank_width:
continue`) is the "distinguish a fill-in blank from a separator rule" logic
described in the module docstring — implemented as two `continue` guards that
skip a line if it's either too narrow or too wide to plausibly be a
fill-in-blank field.

### `extract_layout_fields(page, page_index) -> list[CRFField]`
**Path 2's** entry point: classifies drawn shapes via `_shapes`, sorts them
into reading order (`sorted(_shapes(page), key=lambda s: (s.rect.y0,
s.rect.x0))` — a **sort key returning a tuple**, sorting by y first and x as
a tiebreaker), then builds a `CRFField` for each — skipping an inferred
underline that found no caption at all (`if shape.inferred and label is None:
continue` — that's a bare separator rule, not a field a human would fill in).
For an inferred field *with* a caption, it estimates a top edge from the
caption's own height, clamped to a reasonable range:
```python
inferred_h = min(
    max(label.rect.height * LABEL_HEIGHT_TO_FIELD, INFERRED_HEIGHT_RANGE[0]),
    INFERRED_HEIGHT_RANGE[1],
)
```
`max(x, lo)` then `min(that, hi)` is the standard idiom for clamping a value
into `[lo, hi]` without a dedicated `clamp()` function (Python's standard
library doesn't have one built in).

### `extract_fields(pdf_path) -> FieldSet`
**The module's one real entry point.** Opens the document, computes page
geometry, then for each page:
```python
found = extract_acroform_fields(page, i) or extract_layout_fields(page, i)
```
This uses Python's **truthiness of an empty list**: `extract_acroform_fields`
returns `[]` (falsy) when a page has no widgets, so `or` falls through to
`extract_layout_fields` — a compact way to express "try path 1, and only if
it found nothing, try path 2," without an explicit `if`.

Then, for every field found, it checks the running `seen` set of `field_id`s
and disambiguates a collision by appending the page number:
```python
if f.field_id in seen:
    f = f.model_copy(update={"field_id": f"{f.field_id}#p{i + 1}"})
```
`.model_copy(update={...})` is pydantic's way to produce a modified copy of
an immutable-by-convention model without mutating the original — necessary
here because `CRFField` (like `BBox`) is meant to be treated as a value, and
because AcroForm field names are, per the comment, "only unique per document
*by convention*" — nothing structurally prevents a malformed PDF from reusing
one, so this is a defensive fallback, not the expected path.

Finally wraps everything in `try/finally` to guarantee `doc.close()` runs, and
returns the assembled `FieldSet`.
</content>
