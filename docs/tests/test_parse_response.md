# `tests/test_parse_response.py`

## Role in the pipeline

Tests `pipeline/parse_response.py` (see
[../pipeline/parse_response.md](../pipeline/parse_response.md)) — the
recovery logic for a pasted/attached Copilot reply. The file's own docstring
carries the same warning as `tests/standin_response.py`
(see [standin_response.md](standin_response.md)): every case in this file is
constructed from *known chat-UI behaviors*, not from an actually-observed
Copilot 365 reply, because that real round trip hadn't happened yet as of
this codebase's current state. The docstring is explicit that this file is
"necessary but not sufficient" — a real reply, whenever it arrives, is
expected to reveal quirks nobody guessed here.

## Python concepts you'll see here

**Building input fixtures with local helper functions, not just constants.**
```python
def _full_batch_reply(fieldset, page_indexes=(0,)) -> str:
    fields = [f for f in fieldset.fields if f.page_index in set(page_indexes)]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=SHEET_COLUMNS, lineterminator="\n")
    ...
```
Rather than one static string covering every scenario, this file builds
several **input-generating helpers** (`_full_batch_reply`, `_as_markdown_table`)
that construct a syntactically valid reply on demand, parameterized by which
pages/fields it should cover. This matters because several tests need "a
complete, valid reply covering every field on a page" as a starting point,
then mangle *that* in a specific way — building it programmatically (rather
than hand-writing a second static string) guarantees the "before mangling"
baseline always genuinely matches whatever fields the current fixture
actually contains.

**A single clean baseline string, then many small mutations of it.**
```python
CLEAN_HEADER = ",".join(SHEET_COLUMNS)
CLEAN = (
    f"{CLEAN_HEADER}\n"
    "DM_SITEID,1,Site ID,section: DEMOGRAPHICS,DM_SITEID,variable,DM,SITEID,,,"
    "Collected,0.9,Site identifier in the page header.\n"
)
```
Most of the "mangling" tests in the first section take this one `CLEAN`
constant and apply a targeted `.replace(...)` to introduce exactly one
specific problem (a smart quote, a code fence, extra conversational text) —
keeping every test focused on one variable at a time, since everything else
about the input stays identical to a known-good baseline.

**Testing an internal helper (`_as_markdown_table`) as test infrastructure,
not part of the module under test.** Defined at the top of this file (not
imported from `pipeline.parse_response`), `_as_markdown_table` converts a
CSV string into a markdown table by reading it back with `csv.reader` and
reformatting each row — used to derive a markdown-table version of `CLEAN`
for the tests that specifically check markdown-table recovery, keeping the
two input formats guaranteed to represent identical underlying data.

## Tests, by section

### The mangling a chat UI introduces
Each test name states exactly the one thing being tolerated:
- **`test_parses_a_clean_csv`** — the baseline sanity check.
- **`test_parses_a_markdown_table_reformatting`** — "the predicted primary
  failure mode," per the test's own docstring.
- **`test_strips_a_code_fence_around_csv`** / **`test_strips_a_code_fence_around_a_markdown_table`**
  — a ` ```csv ` fence around either format is stripped.
- **`test_ignores_conversational_wrapping`** — a "Sure! Here's the completed
  spec sheet..." wrapper around the CSV doesn't prevent parsing.
- **`test_normalizes_smart_quotes_in_a_quoted_csv_field`** — a curly quote
  inside a properly CSV-quoted cell is normalized back to a straight quote
  before CSV parsing, so the `csv` module's own quote-matching still works.
- **`test_treats_the_models_stand_ins_for_empty_as_null`** — `"N/A"` and
  `"None"` values become actual `None` on the parsed `CopilotProposal`.
- **`test_ignores_extra_columns_the_model_volunteers`** — an unexpected
  extra column (`sdtm_class`) doesn't break parsing.
- **`test_column_headers_are_matched_case_and_space_insensitively_in_markdown`**
  — `"Field Id"` as a markdown header still resolves to `field_id`.
- **`test_accepts_lowercase_origin_spellings`** — `"collected"` parses to
  the canonical `Origin.COLLECTED`.
- **`test_tab_separated_reply_still_parses`** — confirms `csv.Sniffer`
  correctly recovers a reply that came back tab-separated instead of comma-
  separated, guarding against exactly the CSV-vs-TSV fragility flagged as a
  risk in the design docs.

### Failing loudly
- **`test_empty_response_raises_rather_than_returning_nothing`** — an
  empty/whitespace-only response raises `ResponseParseError` matching
  `"empty"`.
- **`test_prose_only_response_raises`** — a reply that's pure refusal text
  ("I'm sorry, I can't help with that request.") raises rather than somehow
  parsing to zero rows silently.
- **`test_a_row_missing_field_id_is_rejected_not_silently_dropped`** — a row
  with an empty `field_id` cell raises `ResponseParseError` matching
  `"field_id"`, rather than being quietly excluded from the result.
- **`test_report_truncates_a_huge_paste_but_says_so`** —
  `ResponseParseError.report(limit=100)` on a 5000-character raw text both
  truncates the output and says how many characters were cut.
- **`test_a_short_reply_is_never_silently_accepted`** — a reply covering
  only one of a page's several fields raises `IncompleteResponseError` by
  default, naming the missing field IDs and suggesting a re-paste.
- **`test_a_short_reply_can_be_accepted_deliberately`** — the same short
  reply *does* succeed when `allow_partial=True` is passed explicitly —
  confirming the escape hatch works when a human deliberately wants it.
- **`test_a_stray_pipe_inside_a_markdown_cell_fails_loudly_not_silently`** —
  the regression test (with a detailed docstring explaining the exact bug
  history) for the markdown-table column-shift defense described at length
  in [../pipeline/parse_response.md](../pipeline/parse_response.md).
- **`test_a_row_naming_an_unknown_field_id_is_caught`** — a `field_id` that
  doesn't exist in an (empty, in this test) `FieldSet` raises
  `ResponseParseError` matching `"not in this document"` when
  `attach_geometry` is called — simulating a reply pasted against the wrong
  batch entirely.

### Rejoining geometry and provenance
- **`test_geometry_comes_from_the_fieldset_not_the_reply`** — confirms the
  resulting `SdtmAnnotation`'s `bbox`/`field_id`/`page_index` all come from
  the `FieldSet`, not anything present (or absent) in the reply itself
  (which never carries coordinates at all).
- **`test_join_survives_row_reordering`** — reverses the row order of a full
  reply before parsing, and confirms the resulting annotations still cover
  exactly the expected set of fields — the direct proof that `field_id`,
  not row position, is genuinely the join key.
- **`test_everything_arrives_as_an_unreviewed_proposal`** — every resulting
  annotation has `review_status == PROPOSED` and `reviewed_by is None`.
- **`test_provenance_records_the_manual_paste`** — `source_model` is set to
  the module's `SOURCE_MODEL` constant and contains the word "Copilot";
  `created_at` is populated.
- **`test_full_batch_reply_round_trips_every_field`** — a complete reply for
  one page, written to a temp file and passed through
  `ingest_response_file`, produces exactly the expected set of annotations.
- **`test_a_batch_spanning_multiple_pages_round_trips`** — the same, but for
  a reply spanning *both* synthetic pages at once — the test's own docstring
  calls this "the actual point of the redesign: one reply, many pages."
</content>
