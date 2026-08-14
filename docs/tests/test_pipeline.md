# `tests/test_pipeline.py`

## Role in the pipeline

The broadest integration test file in the suite: layout, QC stamping, and —
per its own docstring — "the whole loop wired together." It uses
`standin_response.py` (see [standin_response.md](standin_response.md)) to
generate a plausible fake Copilot reply, run entirely without a human at the
keyboard — again with the explicit caveat that this proves the pipeline is
*connected*, not that the parser tolerates real chat output.

The docstring also calls out something worth noticing about the fixture
itself: the synthetic CRF is two pages, and under the default batch ceiling,
`batch_pages` collapses that to **one** batch — which the docstring describes
as "itself the thing this redesign exists to demonstrate": one round trip
covering both pages, not two. Several tests in this file exist specifically
to make that collapse an explicit, checked assertion rather than an
incidental fact nobody verifies.

## Python concepts you'll see here

**A fixture built by orchestrating several pipeline modules together.**
```python
@pytest.fixture(scope="module")
def placed(crfs, fieldset, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ingest")
    collected = []
    for pages in batch_pages(fieldset):
        path = tmp / f"batch-{pages[0]}.csv"
        path.write_text(build_response(fieldset, pages), encoding="utf-8")
        collected.extend(ingest_response_file(path, fieldset, pages).annotations)
    annots = AnnotationSet(...)
    obstacles = layout.text_obstacles(crfs["acroform"])
    return layout.place_annotations(annots, fieldset, obstacles=obstacles)
```
This fixture is itself a miniature version of `scripts/ingest_response.py`'s
main loop, built as reusable setup for every test in the "layout" and "QC
stamping" sections below — a good example of a fixture doing real,
multi-step orchestration rather than just returning a simple value, when
many tests genuinely need the same nontrivial setup.

**Importing a test helper from another test file.**
```python
from tests.test_roundtrip import read_annots  # noqa: F401  (shared helper)
```
`read_annots` (documented in [test_roundtrip.md](test_roundtrip.md)) is
defined in `test_roundtrip.py` but reused here rather than duplicated — test
files can import from each other just like any other Python modules. The
`# noqa: F401` comment suppresses a linter warning that would otherwise flag
this as an "unused import" — misleading here, since the function *is* used
throughout this file, but a linter doing simple static analysis on the
import line alone might not always connect an import to every later usage
depending on how it's configured; the comment documents this is intentional,
not an oversight.

**Pairwise comparison with `enumerate` and slicing to avoid duplicate
checks.**
```python
for i, a in enumerate(annots):
    for b in annots[i + 1:]:
        ...
```
`annots[i + 1:]` slices the list starting just after index `i` — so every
pair `(a, b)` is checked exactly once (comparing item 0 against 1,2,3,...,
then item 1 against 2,3,..., etc.), instead of the wasteful and
order-duplicated approach of comparing every item against every other item
including itself and each pair twice.

## Tests, by section

### Layout
- **`test_placement_moves_annotations_off_their_own_field`** — for every
  placed annotation, its box no longer meaningfully overlaps the field it
  describes.
- **`test_placed_annotations_do_not_overlap_each_other`** — pairwise, no two
  placed annotations on the same page overlap.
- **`test_placed_annotations_stay_on_the_page`** — every placed box's
  coordinates fall within `[0, page.width]` × `[0, page.height]`.
- **`test_text_obstacles_keep_annotations_off_printed_captions`** — no
  placed annotation meaningfully overlaps any of the form's own printed text
  runs — the test's own docstring notes: "Without this the position
  annotations printed over Sitting/Supine/Standing," describing a real
  problem the `obstacles` parameter to `place_annotations` was added to fix.
- **`test_layout_without_obstacles_still_produces_valid_boxes`** — calling
  `place_annotations` with no `obstacles` argument at all (the parameter's
  default) still produces the same number of annotations, none dropped —
  confirming the obstacle-avoidance feature is additive, not load-bearing
  for correctness.

### QC stamping
- **`test_qc_preview_is_labelled_as_unreviewed`** — the "NOT FOR SUBMISSION"
  banner text is present in the rendered PDF's extracted text.
- **`test_qc_preview_keeps_annotations_as_markup`** — the stamped PDF has
  real PDF annotation objects readable back via `read_annots`, not just
  painted pixels.
- **`test_qc_preview_skips_rejected_annotations`** — every annotation
  marked `REJECTED` (via `.model_copy(update={...})` on the whole placed
  set) is absent from the stamped preview.

### End to end
- **`test_full_loop_from_pdf_to_annotated_pdf`** — the complete chain:
  `write_batches` → mangled stand-in reply (markdown table + chatty wrapper)
  → `ingest_response_file` → `place_annotations` → `write_xfdf` →
  `xfdf_to_pdf`, asserting the final PDF carries exactly as many annotations
  as the fieldset has fields, spread correctly across both pages.
- **`test_default_batching_needs_only_one_round_trip_for_the_fixture`** —
  "the actual point of the redesign, made explicit as an assertion," per the
  test's own comment: `write_batches` on the synthetic fixture produces
  exactly one manifest entry, covering both pages.
- **`test_final_pdf_is_not_flattened`** — every annotation in the final PDF
  is a real `FreeText` annotation type (not a rendered image), and its text
  is findable via `page.get_text()` — confirming annotations remain
  searchable, per the invariant `render.py`/`save_with_annotations`
  establishes (see [../pipeline/render.md](../pipeline/render.md)).
- **`test_conditional_annotations_reach_the_final_pdf`** — the exact
  findings-pattern strings (`"VS.VSORRES when VSTESTCD = SYSBP"`, `"...
  RESP"`) appear in the rendered page's annotation contents — confirming the
  `--TESTCD` condition pattern survives the entire pipeline intact.
- **`test_unmapped_fields_are_visibly_marked_in_the_final_pdf`** — the
  literal string `"[Not Submitted]"` appears in the final PDF's annotations
  — confirming page-furniture fields are visibly, not silently, marked as
  deliberately excluded.
</content>
