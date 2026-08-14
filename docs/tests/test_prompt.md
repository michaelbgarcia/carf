# `tests/test_prompt.py`

## Role in the pipeline

Tests `pipeline/prompt.py` (see [../pipeline/prompt.md](../pipeline/prompt.md))
— the generated Copilot instructions text and CSV spec sheet. The file's own
docstring states the actual contract under test plainly: *"a human pastes the
instructions, attaches or pastes the sheet, and does nothing else before
sending."* Anything that would force a human to add context, explain the
task, or edit something first is treated as a defect — and so is anything
that would teach Copilot to produce output the parser can't read later (an
unparseable worked example, an ambiguous column header). Because batching
(grouping multiple pages into one round trip) is the entire point of this
module's redesign — see the repo README's "Batches, not pages" — `batch_pages`
itself gets tested as thoroughly as the text-generation functions.

## Python concepts you'll see here

**Fixtures with dependencies on other fixtures, at module scope.**
```python
@pytest.fixture(scope="module")
def fieldset(crfs):
    return extract_fields(crfs["acroform"])

@pytest.fixture(scope="module")
def one_batch(fieldset):
    return batch_pages(fieldset)
```
`one_batch` depends on `fieldset`, which depends on the session-scoped `crfs`
fixture from `conftest.py`. `scope="module"` here means both are computed
**once per test file**, shared across every test in this module — a middle
ground between `session` (shared across the *entire* test run) and the
default `function` (recomputed for every single test) scope, appropriate
because extraction is moderately expensive and every test in this file uses
the same fieldset/batching result without needing a fresh one each time.

**Round-tripping generated CSV text back through `csv.DictReader` to assert
on it.** Nearly every spec-sheet test in this file follows the same shape:
generate the sheet as a string, then parse it right back with
`csv.DictReader(io.StringIO(sheet))` to check its structure — testing the
*output contract* (valid CSV, right columns, right values) rather than
inspecting the string's raw characters directly. This is a robust testing
style: it doesn't care about incidental formatting details (exact spacing,
line-ending choices) as long as the data a real CSV parser would extract is
correct.

## Tests, by section

### Batching
- **`test_small_document_collapses_to_one_batch`** — the synthetic 2-page
  CRF, under the default field ceiling, produces exactly one batch covering
  both pages — described as "the whole point" of the redesign, made into a
  direct assertion.
- **`test_batches_cover_every_page_exactly_once`** — with a low ceiling
  forcing multiple batches, every page still appears in exactly one batch
  (no page dropped, none duplicated).
- **`test_a_page_is_never_split_across_batches`** — across several
  different ceiling values, confirms the batching output only ever contains
  whole-page groupings.
- **`test_an_oversized_page_still_becomes_its_own_batch`** — with
  `max_fields_per_batch=1` (guaranteed smaller than any real page), each
  oversized page still becomes its own batch rather than being dropped,
  split, or raising an error.
- **`test_default_ceiling_is_generous_enough_for_the_fixture`** — a sanity
  check that `DEFAULT_MAX_FIELDS_PER_BATCH` (150) comfortably exceeds the
  synthetic CRF's total field count, i.e. the "one batch" behavior above
  isn't a coincidence of the specific ceiling chosen.

### Spec sheet
- **`test_sheet_is_valid_csv_with_the_declared_columns`** — parses back to
  exactly `SHEET_COLUMNS`, one row per field.
- **`test_readonly_columns_are_filled_and_fill_columns_start_empty`** —
  every read-only column has a value present (even if it's a legitimately
  empty string, like a field with no caption); every fill-in column is
  empty.
- **`test_field_id_in_the_sheet_matches_extraction_exactly`** — the set of
  `field_id`s in the sheet exactly matches the fieldset — no extras, none
  missing.
- **`test_page_column_is_one_based_for_humans_only`** — the sheet's `page`
  column is `page_index + 1` for every row, confirming the human-facing
  column uses 1-based numbering while nothing internal does.
- **`test_sheet_rows_are_in_reading_order`** — the `page` values across the
  sheet are non-decreasing, confirming rows are grouped and ordered
  sensibly rather than in arbitrary iteration order.
- **`test_context_survives_commas_in_the_sheet`** — round-trips every
  field's `context` string through the CSV writer/reader unchanged,
  confirming `csv.DictWriter`'s automatic quoting correctly protects any
  punctuation the context string might contain.

### Instructions
(fixture: `instructions = build_batch_instructions(fieldset, one_batch[0], 1, 1)`)
- **`test_instructions_state_the_task_and_the_standard`** — the phrase
  `"annotated CRF"` and the standard `"SDTMIG v3.4"` both appear.
- **`test_instructions_carry_every_rule_the_build_requires`** — every
  required rule keyword (`DOMAIN`, `TESTCD`, `NotSubmitted`, `ORIGIN`,
  `CONFIDENCE`) is present.
- **`test_instructions_reference_the_attached_sheet_not_embed_the_fields`**
  — confirms the word "sheet" appears, and that **no individual field's
  label text is embedded directly in the instructions** — the direct test of
  the redesign's core idea: field data lives in the attached sheet, not in
  prose that would have to keep growing with every additional field.
- **`test_instructions_never_leak_coordinates`** — confirms no bbox value,
  and neither the literal word `"bbox"` nor `"page_index"`, appears anywhere
  in the instructions — Copilot is never meant to see or reason about
  geometry at all.
- **`test_instructions_name_columns_by_the_sheets_own_header`** — every
  `FILL_COLUMNS` name is mentioned in the instructions text, so a human (or
  Copilot) reading both together sees consistent column names.
- **`test_instructions_demand_csv_and_reject_markdown_tables`** — confirms
  the instructions explicitly say "CSV" and explicitly say not to return a
  markdown table — fighting the anticipated failure mode at the source, even
  though the parser also has to tolerate it regardless (see
  [../pipeline/parse_response.md](../pipeline/parse_response.md)).
- **`test_instructions_end_with_the_one_line_reminder`** — the instructions
  text ends with the exact literal reminder sentence.
- **`test_instructions_name_their_batch_for_a_human`** — the instructions
  begin with `"CRF annotation batch 1 of 1"`.

### File output
- **`test_write_batches_produces_one_instructions_and_one_sheet_per_batch`**
  — with a low ceiling forcing two batches, confirms both files exist for
  each batch and the instructions file isn't empty.
- **`test_manifest_records_each_batchs_pages_and_expected_response_path`** —
  confirms the manifest's `pages` field and generated
  `expected_response` filename are both correct for a multi-batch split.
- **`test_each_sheet_only_contains_its_own_batchs_fields`** — confirms
  batch 2's sheet contains **zero** field IDs that belong to page 1 —
  described in the test's own docstring as guarding against "how a reply
  ends up applied to the wrong geometry," i.e. batch cross-contamination.
</content>
