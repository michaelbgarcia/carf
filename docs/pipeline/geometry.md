# `pipeline/geometry.py`

## Role in the pipeline

This tiny file (98 lines) solves exactly one problem, and solves it in exactly
one place on purpose: **PDF coordinate systems disagree about which way is
up.**

- **PyMuPDF** (the library this project uses to read/write PDFs, imported as
  `pymupdf`) places the origin `(0, 0)` at the **top-left** of the page, with
  y increasing **downward** — the same convention as screen/image graphics.
- **XFDF and the PDF spec itself** (the `/Rect` attribute on an annotation)
  place the origin at the **bottom-left**, with y increasing **upward** — the
  same convention as ordinary math graphs.

Every rectangle that crosses that boundary has to be reflected: `y_other =
page_height - y_this`. The catch (explained at length in the module
docstring) is that a naive "subtract in place" leaves `y0` and `y1` swapped,
producing a rectangle that's upside-down about its own center — which still
*looks* like a plausible box, just wrong. This module is the single place that
arithmetic happens, called from exactly two boundaries elsewhere in the
codebase: `extract.py` (reading a PDF, fitz → `BBox`) and `xfdf_to_pdf.py`
(writing a PDF, `BBox` → fitz). Everything in between — `BBox` objects,
`fields.json`, `proposals.json`, XFDF's `@rect` attribute — is already in PDF
user space, so no other file in the project does this math.

## Python concepts you'll see here

**Pure functions.** Every function in this file takes plain values in and
returns plain values out, with no side effects (no file I/O, no mutating
arguments, no reliance on global state). That's what makes
`tests/test_geometry.py` able to test the whole module with plain numbers and
no PDF file anywhere in sight — see [tests/test_geometry.md](../tests/test_geometry.md).
This is a good example of why isolating math from I/O is worth doing: the
riskiest code (coordinate math, where an off-by-one-sign bug is easy to miss)
is also the easiest to test exhaustively, *because* it's pure.

**Type aliases.** `RectTuple = Tuple[float, float, float, float]` gives a
name to a shape of data (a 4-tuple of floats) so function signatures read as
`rect: RectTuple` instead of repeating the tuple spelled out everywhere. It's
just a variable holding a type, evaluated once at import time.

**`TYPE_CHECKING` guard.**
```python
from typing import TYPE_CHECKING, Tuple
if TYPE_CHECKING:
    import pymupdf
```
`TYPE_CHECKING` is `False` at runtime and `True` only when a static type
checker (like `mypy` or `pyright`) is analyzing the file. This lets the
function signature below reference `pymupdf.Rect` *as a type hint* — so tools
and readers know what's expected — **without** the module actually requiring
PyMuPDF to be installed to be imported. The docstring explains why that
matters: this module wants to "stay importable without PyMuPDF loaded."

**String-literal type hints (forward references).** `def fitz_rect_to_bbox(rect:
"pymupdf.Rect | RectTuple", page_height: float) -> BBox:` — the type hint is
written as a *string* rather than bare code. Because `pymupdf` is only
imported under `TYPE_CHECKING`, writing `pymupdf.Rect` unquoted would raise a
`NameError` at import time in a normal run (where `TYPE_CHECKING` is False and
`pymupdf` was never actually imported into this module's namespace). Quoting
it defers evaluation — Python treats the string as documentation for type
checkers rather than code to execute immediately. (The `from __future__ import
annotations` at the top of the file, explained in [models.md](models.md),
actually makes *all* annotations in this file lazy this way, but the explicit
quotes here make the intent unmistakable even without knowing that.)

**The `X | Y` union syntax.** `"pymupdf.Rect | RectTuple"` means "either a
`pymupdf.Rect` or a `RectTuple`." This pipe-based union syntax (PEP 604) is
newer, terser sugar for what used to require `Union[pymupdf.Rect, RectTuple]`.

**Tuple unpacking with a generator expression.**
```python
x0, y0, x1, y1 = (float(v) for v in tuple(rect))
```
`tuple(rect)` converts a `pymupdf.Rect` (or an existing tuple) into a plain
4-tuple; `(float(v) for v in ...)` is a *generator expression* that lazily
casts each element to `float`; and the surrounding `x0, y0, x1, y1 = ...`
unpacks the four resulting values into four named variables in one line.

## Functions, in file order

### `_flip(y0, y1, page_height) -> tuple[float, float]`
The one piece of real math in the module — everything else calls this.
Reflects a y-interval about `page_height` (`page_height - y0`, `page_height -
y1`) and then returns the two results **sorted low-to-high** rather than in
their original order:
```python
a = page_height - y0
b = page_height - y1
return (b, a) if b <= a else (a, b)
```
That sort is the fix for the "upside-down" bug the whole module exists to
avoid: reflecting an interval always reverses which end is smaller, so if you
don't re-sort, `y0`/`y1` end up swapped relative to what a valid rectangle
needs (`y1 >= y0`). The leading underscore (`_flip`) is Python's convention
for "internal use only, not part of this module's public API" — nothing
enforces that at the language level, it's just a signal to other developers
(and to tools like `from module import *`, similar to the effect of `__all__`
mentioned in [pipeline/__init__.md](__init__.md)).

### `fitz_rect_to_bbox(rect, page_height) -> BBox`
**fitz (top-left, y-down) → `BBox` (PDF user space, y-up).** Called from
`extract.py` every time a field's rectangle is read off a PDF page. Unpacks
the four coordinates, swaps `x0`/`x1` if they came in reversed (defensive —
PyMuPDF rects are usually already normalized, but this doesn't assume it),
calls `_flip` for the y-axis, and constructs a `BBox` — which itself validates
`x1 >= x0` and `y1 >= y0` on construction (see [models.md](models.md)), so a
bug here would be caught immediately rather than silently producing an
inverted rectangle downstream.

### `bbox_to_fitz_rect(bbox, page_height) -> RectTuple`
**The inverse of the function above.** `BBox` (PDF user space) → a plain
`(x0, y0, x1, y1)` tuple in fitz's top-left/y-down convention. Called from
`render.py` and `xfdf_to_pdf.py` whenever an annotation's stored position
needs to be drawn back onto a page with PyMuPDF. Deliberately returns a plain
tuple rather than a `pymupdf.Rect` object — the docstring notes this keeps the
module usable without PyMuPDF installed; callers that need an actual
`pymupdf.Rect` just wrap the result themselves: `pymupdf.Rect(*result)`.

Because reflection is its own inverse (flip it, then flip it back, and you're
where you started), `fitz_rect_to_bbox` and `bbox_to_fitz_rect` compose to the
identity function — feeding one's output into the other returns the original
value. `tests/test_geometry.py`'s `test_round_trip_is_the_identity` asserts
exactly this.

### `format_xfdf_rect(bbox, precision=3) -> str`
Turns a `BBox` into the comma-separated string XFDF's `@rect` attribute
expects, e.g. `"72.000,650.000,300.000,662.000"`. No coordinate flip happens
here — a `BBox` is *already* in the same coordinate space XFDF wants, so this
is pure formatting, not geometry. Uses an f-string with a nested format spec:
```python
",".join(f"{v:.{precision}f}" for v in bbox.as_tuple())
```
`f"{v:.{precision}f}"` is a **nested f-string**: the `precision` variable is
itself interpolated into the format spec (`.{precision}f`), so
`format_xfdf_rect(bbox, precision=1)` would produce one decimal place instead
of three. `bbox.as_tuple()` (defined on `BBox` in `models.py`) supplies the
four floats in order, and `str.join` glues the formatted pieces together with
commas.

### `parse_xfdf_rect(value) -> BBox`
The inverse of `format_xfdf_rect`: parses a `@rect` attribute string back
into a `BBox`. Tolerant of whitespace and either comma- or space-separated
input:
```python
parts = [p for p in value.replace(",", " ").split() if p]
```
This replaces every comma with a space, then calls `.split()` with no
argument — which in Python splits on *any* run of whitespace and discards
empty strings automatically — and the `if p` filter is a second, redundant
safety net against stray empty tokens. Raises `ValueError` if it doesn't find
exactly four numbers (a defensive check — malformed XFDF, e.g. from manual
editing, should fail loudly rather than silently producing a wrong rectangle).
Finally builds the `BBox` with `min`/`max` rather than assuming the four
values already arrive in `x0 <= x1`, `y0 <= y1` order — this function is the
one place in the codebase that reads XFDF a human may have hand-edited in a
text editor, so it can't assume well-formed input the way code that only ever
sees its own output can.
</content>
