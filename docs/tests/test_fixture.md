# `tests/test_fixture.py`

## Role in the pipeline

Tests the **synthetic CRF generator itself** (`scripts/make_sample_crf.py`,
see [../scripts/make_sample_crf.md](../scripts/make_sample_crf.md)) and,
critically, the committed truth file it produces. The file's own docstring
names the single most important test here directly:
`test_committed_truth_matches_the_current_layout` — described as "load-
bearing" because *every other test in the entire suite* that compares
extraction output against ground truth is only meaningful if the committed
`fixtures/sample_crf_truth.json` genuinely still matches what the generator
currently produces. If those two could silently drift apart, every
downstream comparison would quietly stop meaning anything, while still
appearing to pass.

## Python concepts you'll see here

**Testing a "drift guard" by comparing two independently-serialized JSON
blobs.**
```python
def test_committed_truth_matches_the_current_layout(truth):
    if not TRUTH_PATH.exists():
        pytest.fail(f"{TRUTH_PATH} is missing; run scripts/make_sample_crf.py")
    committed = json.loads(TRUTH_PATH.read_text(encoding="utf-8"))
    assert committed == json.loads(truth.model_dump_json()), (...)
```
Both sides are parsed back into plain Python data (`json.loads(...)`) before
comparing with `==`, rather than comparing raw JSON *strings* — this avoids
false failures from harmless formatting differences (key order, whitespace)
that don't reflect an actual difference in the data. `pytest.fail(message)`
immediately fails the test with a custom message, used here instead of a
bare `assert` because the situation ("the truth file is entirely missing")
deserves a more specific, actionable error than an `AttributeError` from
trying to read a nonexistent file would give.

**Passing a custom failure message as the second element of an `assert`
tuple-looking expression.**
```python
assert committed == json.loads(truth.model_dump_json()), (
    "fixtures/sample_crf_truth.json is stale -- "
    "re-run scripts/make_sample_crf.py and commit the result"
)
```
This isn't actually a tuple — it's Python's `assert expr, message` syntax:
if `expr` is falsy, `AssertionError(message)` is raised. The parenthesized
multi-line string after the comma is just a single string spanning several
source lines (adjacent string literals concatenate automatically in Python),
providing a specific, actionable instruction rather than letting `pytest`
print only its own (admittedly quite good) automatic diff of the two JSON
values.

**A second `@pytest.mark.parametrize` example, over integers rather than
strings.**
```python
@pytest.mark.parametrize("page_index", [0, 1])
def test_each_page_has_a_top_and_bottom_vertical_outlier(truth, page_index):
```
Same mechanism as seen in [test_models.md](test_models.md), applied to page
indexes instead of string spellings — confirms the parametrize pattern
generalizes to any hashable value, not just strings.

## Tests, by section

### The drift guard
- **`test_committed_truth_matches_the_current_layout`** — described above.

### The AcroForm variant
- **`test_acroform_variant_carries_every_field_as_a_widget`** — every truth
  field's `field_id` appears as an actual AcroForm widget name in the
  generated PDF.
- **`test_widget_rects_match_the_truth_file_exactly`** — converts each truth
  `bbox` back to fitz coordinates via `bbox_to_fitz_rect` and compares
  against the widget's actual on-page rectangle — the docstring notes this
  "also tests the flip," since the truth file itself was written *through*
  `fitz_rect_to_bbox` when generated, so equality here confirms the
  round-trip is faithful, not just that the generator and the PDF happen to
  agree by coincidence.

### The flattened variants
- **`test_flat_variant_has_no_widgets_but_keeps_the_geometry`** — confirms
  zero widgets exist in the flat PDF, and that the total count of drawn
  shapes equals field count *plus* the number of distractor rules —
  confirming nothing was silently dropped or duplicated during baking.
- **`test_baked_fields_land_within_tolerance_of_the_acroform_originals`** —
  for every truth field, at least one drawn shape in the flat variant sits
  within `BAKE_TOLERANCE` (1pt) of where the AcroForm widget would have
  been — the direct test of the "the two variants are the same document"
  property `make_sample_crf.py`'s bake-based construction is designed to
  guarantee.
- **`test_ruled_variant_draws_text_fields_as_underlines`** — confirms the
  ruled variant has zero widgets and that its flat, thin (`height < 1.0`)
  drawn shapes number exactly "one underline per text field, plus the
  section/footer rules."

### The properties that make the fixture worth having
This section directly tests the "deliberately adversarial layout" choices
described in [../scripts/make_sample_crf.md](../scripts/make_sample_crf.md):
- **`test_each_page_has_a_top_and_bottom_vertical_outlier`** (parametrized
  over both pages) — confirms a field exists near the very top and very
  bottom of each page, with a docstring explaining exactly why this matters:
  "With every field in the middle band of the page, a y-flip bug puts each
  one roughly where it belongs and the visual check passes" — i.e., this
  test isn't checking the *output* of extraction, it's checking that the
  *fixture itself* is capable of exposing a specific class of bug at all.
- **`test_no_text_field_is_square`** — confirms every text field's width and
  height differ meaningfully (checkboxes are explicitly exempted, since
  "square" is what a checkbox correctly *is*) — guarding against an
  x/y-transposition bug that a square bbox would hide.
- **`test_fixture_contains_non_field_rules_as_false_positives`** — confirms
  the distractor rules exist at all, and that none of them happens to
  coincide with a real field's position (which would make the fixture
  accidentally *not* test what it's meant to).
- **`test_both_label_geometries_are_represented`** — confirms both the
  "label to the left" (page 1) and "label as column header above" (page 2
  grid) geometries actually appear in the fixture.
- **`test_testcd_grid_covers_the_condition_pattern`** — confirms the
  vital-signs `_RES` fields exist and match `gen.VS_ROWS` in count — the
  fixture content that exercises the `VSORRES when VSTESTCD = SYSBP`
  condition pattern downstream.
- **`test_every_page_is_marked_synthetic`** — confirms the literal
  `SYNTHETIC TEST DATA` banner text appears in every page of all three
  variants — checked via PyMuPDF's `page.get_text()`, i.e. actually reading
  back the rendered text content, not just trusting the drawing code ran.
</content>
