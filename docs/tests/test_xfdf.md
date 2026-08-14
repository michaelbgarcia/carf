# `tests/test_xfdf.py`

## Role in the pipeline

Tests both halves of `pipeline/xfdf.py` (the writer, see
[../pipeline/xfdf.md](../pipeline/xfdf.md)) and the reading side of
`pipeline/xfdf_to_pdf.py` (see [../pipeline/xfdf_to_pdf.md](../pipeline/xfdf_to_pdf.md)),
together — appropriate given the file's own framing: XFDF is "the
human-facing artifact: written by the pipeline, reviewed and possibly edited
in Acrobat, then read back as the authoritative source," so testing writer
and reader as a pair covers the *whole* contract on both sides of that human
hop, not just one direction of it in isolation.

## Python concepts you'll see here

**Two small local factory functions used throughout the file.**
```python
def _annot(**kw) -> SdtmAnnotation:
    base = dict(annot_id="a1", field_id="DM_USUBJID", ..., confidence=0.9)
    base.update(kw)
    return SdtmAnnotation(**base)

def _set(*annots) -> AnnotationSet:
    return AnnotationSet(source_pdf="blank.pdf", pages=[...], annotations=list(annots))
```
`_annot` is the same "sensible-defaults-plus-overrides" pattern seen in
`test_models.py` (see [test_models.md](test_models.md)). `_set(*annots)`
uses `*annots` to accept **any number of positional annotation arguments**
(`_set(a)`, `_set(a, b)`, `_set()`) and collects them into a tuple, then
`list(annots)` converts that tuple to a list for the model field — a small
but genuinely convenient signature for a test helper that needs to build an
`AnnotationSet` from a varying number of annotations depending on the test.

**Testing XML output by parsing it back with a *different* tool than the one
that wrote it.**
```python
def test_output_is_well_formed_xfdf():
    root = ET.fromstring(build_xfdf(_set(_annot()), "blank.pdf"))
    assert root.tag == f"{{{XFDF_NS}}}xfdf"
```
Even though `xfdf.py` itself uses `xml.etree.ElementTree` to *build* the
document, this test re-parses the resulting *string* with `ET.fromstring`
independently — confirming the output is genuinely well-formed XML that any
standard parser can read, not merely that the in-memory tree the writer built
looked correct before serialization (a subtly different, weaker guarantee).

**Testing exact string content alongside structural parsing.** Several tests
assert on literal substrings of the XML text directly (e.g.
`assert 'rect="200.000,650.000,300.000,662.000"' in xml`) rather than only
parsing and checking structured values — appropriate here because the *exact
formatting* (three decimal places, specific attribute ordering conventions)
is itself part of what's being verified, not just the underlying data.

## Tests, by section

### Writer
- **`test_output_is_well_formed_xfdf`** — described above; also confirms
  `<f href="blank.pdf">` and an `<annots>` element are both present.
- **`test_serialized_form_declares_xfdf_as_the_default_namespace`** —
  confirms the serialized text uses `xmlns="..."` (default namespace, no
  prefix) and plain `<freetext ...>` tags, not prefixed ones — the direct
  test of the `ET.register_namespace("", XFDF_NS)` call discussed in
  [../pipeline/xfdf.md](../pipeline/xfdf.md).
- **`test_rect_is_written_in_pdf_user_space_unflipped`** — confirms the
  exact `rect="..."` string for a known `BBox`, verifying **no** coordinate
  flip happens inside `xfdf.py` itself (a `BBox` already is PDF user space).
- **`test_page_attribute_is_zero_based`** — `page_index=1` writes
  `page="1"` (not `page="2"`).
- **`test_contents_carry_the_rendered_annotation_text`** — the findings-
  pattern display text renders correctly inside `<contents>`.
- **`test_unmapped_fields_are_still_written_visibly`** — a `NOTE`-kind,
  `NOT_SUBMITTED`-origin annotation still writes visible
  `<contents>[Not Submitted]</contents>`, not an empty tag.
- **`test_rejected_annotations_are_excluded`** — of two annotations, one
  `PROPOSED` and one `REJECTED`, only the non-rejected one's `name`
  attribute appears anywhere in the output.
- **`test_accepted_annotations_are_kept`** — conversely, an `ACCEPTED`
  annotation (a "kept" status, not rejected) is present.
- **`test_provenance_survives_in_the_carf_namespace`** — the `<carf:meta>`
  element's attributes (`field_id`, `origin`, `review_status`) round-trip
  correctly through a real file write + `parse_xfdf` read.
- **`test_source_model_is_recorded_as_the_annotation_author`** — the
  `title="Copilot 365 chat, manual paste"` attribute is present.

### Reader
- **`test_reader_round_trips_the_writer`** — the most basic write-then-read
  check: bbox, text, and annot_id all survive.
- **`test_reader_accepts_a_file_stripped_of_namespaces`** — a hand-written,
  bare (no namespace declarations at all) XFDF file still parses correctly
  — the direct test of `_localname`'s tolerance, discussed in
  [../pipeline/xfdf_to_pdf.md](../pipeline/xfdf_to_pdf.md).
- **`test_reader_tolerates_whitespace_in_a_hand_edited_rect`** — a `@rect`
  value with spaces after each comma (`"10, 20, 110, 32"`, as a human might
  type it) still parses correctly.
- **`test_reader_rejects_a_wrong_root_element`** — a file whose root is
  `<fdf>` instead of `<xfdf>` raises `ValueError` matching `"expected
  <xfdf>"`.
- **`test_reader_fails_loudly_on_an_annotation_with_no_rect`** — an
  annotation element missing `@rect` entirely raises `ValueError` matching
  `"without a @rect"`, rather than being silently skipped (which, per the
  reasoning documented in [../pipeline/xfdf_to_pdf.md](../pipeline/xfdf_to_pdf.md),
  would mean quietly losing an annotation from the submission).
- **`test_rendering_an_empty_xfdf_fails_rather_than_writing_a_bare_pdf`** —
  an XFDF with zero annotations raises `ValueError` matching `"no
  annotations"` when passed to `xfdf_to_pdf`, rather than silently producing
  an unmarked "annotated" PDF.
- **`test_overflowing_text_is_reported_not_silently_clipped`** — a
  deliberately tiny rect with long text is correctly flagged by
  `overflowing_annotations`.
- **`test_pipeline_sized_annotations_do_not_trip_the_overflow_check`** —
  confirms the `OVERFLOW_TOLERANCE` constant (see
  [../pipeline/xfdf_to_pdf.md](../pipeline/xfdf_to_pdf.md)) correctly absorbs
  the tiny rounding loss from `format_xfdf_rect`'s 3-decimal-place
  formatting, so a pipeline-generated (correctly-sized) annotation never
  falsely reports overflow.
- **`test_rendering_fails_when_the_xfdf_names_a_page_the_pdf_lacks`** — an
  XFDF referencing `page="9"` against a 2-page PDF raises `ValueError`
  matching `"has 2 pages"`.
</content>
