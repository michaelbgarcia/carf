# `pipeline/stamp.py`

## Role in the pipeline

This is the pipeline's "quick look" step — it stamps whatever annotations were
just proposed by Copilot directly onto a copy of the blank CRF, immediately
after `parse_response.py` and `layout.py` have run, and **before any human has
reviewed anything**. It exists purely so someone can eyeball the result of a
Copilot round trip without opening Acrobat or waiting for the full review
workflow.

The module docstring is explicit that this is **deliberately not the same
step** as `xfdf_to_pdf.py`: that module renders from the reviewed, possibly
hand-edited XFDF *file*, because by that point the file is authoritative; this
module renders directly from **in-memory** `SdtmAnnotation` objects, because at
this point in the pipeline nothing has been reviewed yet and no XFDF file may
even exist. Both call the shared drawing helper in `render.py` (see
[render.md](render.md)), but keeping them as separate entry points — rather
than merging them since their implementations converge — is a deliberate
choice: the docstring's reasoning is that conflating "pre-review preview" and
"final submission artifact" is exactly how a stale preview PDF ends up
mistaken for the real deliverable in a regulated workflow, where that mistake
matters.

Two safeguards make this preview hard to confuse with the real thing:
- The output filename (`qc_preview.pdf`, chosen by the caller in
  `scripts/ingest_response.py`) can't be confused for `annotated_crf.pdf`.
- Every page gets a visible banner (`BANNER`) stating in plain English that
  this is an unreviewed preview, not for submission.

## Python concepts you'll see here

**A module-level constant as user-facing copy.** `BANNER = "QC PREVIEW -
UNREVIEWED COPILOT PROPOSALS - NOT FOR SUBMISSION"` is defined once at module
scope and used both in the actual stamped output and, indirectly, checked by
name in tests (`tests/test_pipeline.py::test_qc_preview_is_labelled_as_unreviewed`
asserts on the literal substring `"NOT FOR SUBMISSION"`) — a small but real
example of how a constant keeps a piece of text consistent between the code
that produces it and any code (including tests) that needs to recognize it.

**`try/finally` for guaranteed cleanup, again.** Same pattern as
`extract.py`: `doc = pymupdf.open(pdf_path)` followed by `try: ... finally:
doc.close()`, ensuring the document handle is released even if drawing one
annotation raises partway through.

## Functions, in file order

### `_stamp_banner(page, n_proposed) -> None`
Draws the "QC PREVIEW..." banner text at a fixed position near the bottom of
the page (`pymupdf.Point(36, 14)`), in bold (`fontname="hebo"`, PyMuPDF's
built-in Helvetica-Bold), colored orange (`BANNER_COLOR`), including a count
of how many annotations were actually drawn — `f"{BANNER}   ({n_proposed}
proposed annotations)"`.

### `stamp_annotations(pdf_path, annotations, out_path) -> Path`
**The module's one entry point**, called from `scripts/ingest_response.py`
right after XFDF is written. Opens a *copy* of the blank CRF (the original
`pdf_path` file is never modified — this opens it fresh and writes to
`out_path`), then for every annotation in the set:
```python
for annot in annotations.annotations:
    if annot.review_status is ReviewStatus.REJECTED:
        continue
    text = annot.display_text()
    if not text:
        continue
    page = doc[annot.page_index]
    draw_annotation(page, annot.bbox, text, boxed=annot.kind.value == "domain", muted=not annot.label_text())
    drawn += 1
```
Skips rejected annotations (consistent with `xfdf.py`'s treatment — a
rejected annotation shouldn't visually appear even in a pre-review preview)
and skips anything whose `display_text()` comes back empty (an edge case
`display_text()` itself mostly guards against — see [models.md](models.md) —
but this is a second line of defense against drawing a blank box). `doc[annot.page_index]`
uses `Document`'s support for integer indexing (`__getitem__`) to fetch a
specific `Page` object directly, rather than iterating. `boxed=annot.kind.value
== "domain"` compares the enum's underlying string value (see `AnnotationKind`
in [models.md](models.md)) to decide whether this annotation gets the bordered
"domain marker" treatment. After every annotation is drawn, the function loops
over every page (`for page in doc:` — iterating a `Document` yields its
pages) and stamps the banner on each one, then saves via the shared
`save_with_annotations` helper from `render.py` and returns the output path.
</content>
