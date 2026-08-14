# `tests/test_extract.py`

## Role in the pipeline

Tests field detection (`pipeline/extract.py`, see
[../pipeline/extract.md](../pipeline/extract.md)) against the committed
ground truth (`fixtures/sample_crf_truth.json`, delivered here via the
`truth` fixture from [conftest.md](conftest.md)). The file's own docstring
explains the asymmetry in how strictly each PDF variant is checked: the
**AcroForm** variant is held to *exact* equality on both coordinates and
labels, because widget rectangles are known precisely from the moment
they're drawn — anything looser would be slack the test doesn't need to
allow. The two **flattened** variants are matched by *position, with a
tolerance* instead, because a flattened CRF has no field names to join on
(so fields have to be matched by "which detected field sits closest to where
the truth file says one should be"), and because two real geometric effects
make exact equality impossible even for a bug-free detector: baking insets a
widget's rectangle by half its border width, and an underline gives the
detector no top edge to measure directly (it has to be inferred, see
[../pipeline/extract.md](../pipeline/extract.md)).

## Python concepts you'll see here

**A shared matching helper with tolerance, used across many tests.**
```python
def _match(got_fields, target, tol=POSITION_TOL):
    return [
        g for g in got_fields
        if g.page_index == target.page_index
        and abs(g.bbox.x0 - target.bbox.x0) <= tol
        and abs(g.bbox.x1 - target.bbox.x1) <= tol
        and abs(g.bbox.y0 - target.bbox.y0) <= tol + INFERRED_TOP_TOL
        ...
    ]
```
A private module-level helper (not a fixture — it's a plain function taking
explicit arguments) used by every test in the "flattened paths" section to
find which detected field, if any, sits at the position a truth-file field
should occupy. Returning a **list** (of zero or more matches) rather than a
single value or `None` lets callers write both "does a match exist"
(`assert m`) and "is there exactly the expected number of fields at all"
checks against the same helper.

**Building an inverted lookup dict for O(1) access by id.**
```python
by_id = {f.field_id: f for f in got.fields}
```
This exact pattern — a dict comprehension turning a list into a
`field_id → object` lookup table — appears in nearly every test in this
file. It's worth recognizing as a standard technique: whenever a test needs
to repeatedly ask "what did the detector find *for this specific field*,"
building the lookup once up front is both clearer and faster than searching
the list again for each check.

**Set difference for "did we find everything" assertions.**
```python
missing = [t.field_id for t in truth.fields if not _match(got.fields, t)]
assert not missing
```
A list comprehension with a filter, collecting every truth-file field that
`_match` found *no* corresponding detected field for — `assert not missing`
reads naturally as "there should be nothing missing," and if the assertion
fails, `pytest` prints the actual `missing` list, giving an immediately
actionable failure message (exactly which field IDs the detector missed)
rather than a bare "assertion failed."

## Tests, by section (matching the file's own comment headers)

### AcroForm path
- **`test_acroform_finds_every_field_with_exact_geometry`** — same field
  count as truth, and every field's `bbox` matches to `1e-6` — effectively
  exact equality for floats.
- **`test_acroform_labels_match_truth_exactly`** — every detected label
  string matches truth exactly.
- **`test_acroform_records_provenance_of_the_detection`** — every field is
  marked `FieldSource.ACROFORM` and has a non-empty `acroform_name`.
- **`test_page_geometry_is_captured_for_the_later_flip`** — page indexes and
  heights are captured correctly, since these feed every downstream y-flip.

### Flattened paths (`@pytest.mark.parametrize("variant", ["flat", "ruled"])`)
Four tests parametrized to run against **both** flattened variants with the
same assertions:
- **`test_flattened_variants_find_every_field`** — nothing from the truth
  file goes unmatched.
- **`test_flattened_variants_invent_no_extra_fields`** — exactly as many
  fields detected as truth expects; the docstring calls out specifically
  that "the separator rules must not come back as fill-in blanks" — this is
  the test that would catch a detector too eager to call every drawn line a
  field.
- **`test_flattened_variants_label_fields_correctly`** — every matched field
  carries the correct caption.
- **`test_flattened_fields_are_marked_as_layout_derived`** — `source` is
  `TEXT_LAYOUT` and `acroform_name` is `None` for every field, confirming
  the flattened path correctly identifies itself as the fallback route.

Plus, non-parametrized:
- **`test_underlines_keep_their_measured_baseline`** — specifically for the
  ruled variant: the *bottom* edge of an inferred field (measured directly
  from the drawn underline) matches truth tightly, even though the *top*
  edge is only inferred and allowed a looser tolerance — confirming the
  module correctly distinguishes "measured" from "guessed" geometry within a
  single field.

### Label association rules
- **`test_checkboxes_read_their_caption_from_the_right`** — `"Male"`,
  `"Female"`, and a longer race option all correctly read from a checkbox's
  right side.
- **`test_grid_checkboxes_fall_through_to_the_column_header_above`** — the
  "Not Done" checkboxes in the vital-signs grid (which have nothing to their
  right) correctly fall through to the column header several rows up —
  directly testing `associate_label`'s ordered-fallback behavior.
- **`test_text_fields_read_their_caption_from_the_left`** — including a case
  (`DM_AGE`) where a decoy label (`"years"`) sits to the *right* — confirming
  "left" correctly wins for a text field even when something plausible-
  looking is nearby in the wrong direction.
- **`test_adjacent_blanks_share_one_caption`** — the three date-of-birth
  boxes (day/month/year) all correctly resolve to the same single label.
- **`test_context_distinguishes_identical_looking_grid_rows`** — the
  Systolic and Diastolic blood pressure rows have *different* `context`
  strings despite sharing the same field type and column header — the direct
  test of why `context` exists at all (see
  [../pipeline/extract.md](../pipeline/extract.md)).
- **`test_context_names_the_section_heading`** — `"DEMOGRAPHICS"` appears in
  a page-1 field's context.
- **`test_context_never_contains_a_bare_pipe`** — the regression guard for
  the real `|`-in-CSV-cell bug described at length in
  [../pipeline/extract.md](../pipeline/extract.md) and the repo README's
  "Batches, not pages" section.

### Text run grouping
- **`test_captions_on_one_line_stay_separate`** — `"Male"` and `"Female"`
  (which share a printed line with "Sex:") come back as **separate**
  `TextRun`s, not merged into one string.
- **`test_multi_word_captions_stay_joined`** — conversely, `"Age at
  Consent"` (three words meant to be one caption) stays joined as a single
  run — the two tests together confirm the gap-width heuristic in
  `text_runs` is calibrated correctly in both directions.
- **`test_clean_label_strips_trailing_colons_and_whitespace`** — a plain
  unit test of `clean_label`, no PDF involved at all.
- **`test_association_returns_none_when_nothing_is_near`** — `associate_label`
  correctly returns `None` (rather than some arbitrary distant match) for a
  rect placed in genuinely blank margin space.

### Fallback selection
- **`test_acroform_path_is_skipped_on_a_page_with_no_widgets`** — calling
  `extract_acroform_fields` directly against the flat variant's page (which
  has no AcroForm widgets) returns `[]`, confirming the `or` fallback in
  `extract_fields` (see [../pipeline/extract.md](../pipeline/extract.md))
  actually has something to fall through to.
</content>
