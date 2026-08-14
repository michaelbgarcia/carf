# `tests/test_geometry.py`

## Role in the pipeline

Tests the pure coordinate-flip math in [../pipeline/geometry.md](../pipeline/geometry.md)
in complete isolation — no PDF file, no PyMuPDF `Document`, not even a
fixture from `conftest.py`. Just plain numbers in, plain numbers (or a
`BBox`) out. The file's own docstring makes the point directly: because
`geometry.py`'s functions are pure, "these run before any PDF code exists" —
this test file could pass or fail with zero dependency on whether PyMuPDF is
even installed correctly. The two tests that *do* need a real PDF page (a
visual pixel-level check, and a full XFDF round trip) live instead in
`test_roundtrip.py` (see [test_roundtrip.md](test_roundtrip.md)), which is a
deliberate separation: keep the pure-math tests pure, and put the
integration-style tests somewhere clearly labeled as such.

## Python concepts you'll see here

**`pytest.approx` for floating-point comparison.** Comparing floats with `==`
is fragile — accumulated rounding error can make two numbers that are
"mathematically equal" differ in their last bit. `pytest.approx(722.0)`
wraps a value so that `==` against it uses a small tolerance instead of exact
equality — the standard, correct way to assert on floating-point results in
`pytest`. Used in nearly every assertion in this file.

**Testing a function by testing what it *doesn't* do wrong, not just what it
does right.** `test_flip_reorders_y_rather_than_subtracting_in_place`'s own
docstring says it plainly: "The bug this whole module exists to prevent:
y0/y1 left swapped." This is worth calling out as a testing philosophy
independent of Python itself — a test suite that only checks "does this
produce a plausible-looking number" would miss the exact bug this module was
built to guard against (see [geometry.md](geometry.md)'s explanation of why
"subtract in place" is wrong even though it produces a rectangle that
*looks* fine). A good regression test encodes the *specific* failure mode
that was once possible, not just a generic sanity check.

**Testing that composition is the identity.** `test_round_trip_is_the_identity`
constructs a `BBox`, runs it through `bbox_to_fitz_rect` then
`fitz_rect_to_bbox`, and asserts the result equals the original — a
mathematical property (`f⁻¹(f(x)) == x`) rather than a specific expected
number, which is a strong, general way to test a pair of inverse functions
without having to hand-compute what the "right" answer should be for a given
input.

**`pytest.raises` as a context manager.**
```python
with pytest.raises(ValueError):
    BBox(x0=0.0, y0=100.0, x1=10.0, y1=50.0)
```
Asserts that the code inside the `with` block raises exactly the named
exception type — the test *fails* if no exception is raised, or if a
different exception type is raised instead. This is the standard `pytest`
pattern for testing that invalid input is correctly rejected.

## Tests, in file order

- **`test_flip_moves_a_top_of_page_rect_to_the_top_in_pdf_space`** — a
  concrete numeric check: a rect 50pt below the top edge in fitz coordinates
  (y-down) should land 50pt below the top edge in PDF coordinates (y-up) too
  — i.e., visually "near the top" stays "near the top" across the flip, just
  expressed with different numbers. Also confirms the x-coordinates pass
  through completely unchanged, since the flip is a y-axis-only operation.
- **`test_flip_reorders_y_rather_than_subtracting_in_place`** — described
  above: the regression guard for the exact "swapped y0/y1" bug.
- **`test_round_trip_is_the_identity`** — described above.
- **`test_xfdf_rect_serialisation_round_trips`** — checks
  `format_xfdf_rect`/`parse_xfdf_rect` (the string ↔ `BBox` conversions, not
  a coordinate flip at all — see [geometry.md](geometry.md)) compose back to
  the original value.
- **`test_bbox_rejects_inverted_y`** — confirms `BBox`'s own model validator
  (see [../pipeline/models.md](../pipeline/models.md)) raises `ValueError`
  when constructed with `y1 < y0` directly — testing the data model's own
  defense, one layer below the geometry functions themselves.
</content>
