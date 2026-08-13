# carf — aCRF Annotation Pipeline

Takes a blank Case Report Form PDF and produces an annotated version (XFDF, then
a submission-ready PDF) with domain and variable-level SDTM annotations placed
next to each capture field.

**Status: core loop complete** (task order steps 1–9, plus a redesign of the
Copilot interaction layer — see "Batches, not pages" below). The full path
runs end to end on the synthetic CRF: extract → batch → paste → ingest → XFDF
→ review → annotated PDF.

**One thing is outstanding and needs a human**: the build instructions require
`parse_response.py` to be exercised against a *real* Copilot 365 reply rather
than a hand-written sample, because its actual formatting quirks are what need
coverage. That round trip has not happened. The parser is built from known
chat-UI behaviours and the test suite uses an explicitly-labelled stand-in
(`tests/standin_response.py`) that proves the plumbing only. Expect real
Copilot output to reveal quirks nobody guessed — see "The one manual step"
below.

## The human-in-the-loop hop

There is no programmatic LLM access here. The only model available is Microsoft
Copilot 365 chat, driven interactively by a person. So the annotation step is
not a function call — it is a copy/paste (or attach/download) hop, and the
pipeline is built in three entry points around it:

```
                    scripts/extract_and_prompt.py
blank_crf.pdf  ─────────────────────────────────────►  build/fields.json
                                                       build/batches.json
                                                       build/copilot_batch1_instructions.txt
                                                       build/copilot_batch1_sheet.csv

     ┌─────────────────── HUMAN, per batch ─────────┐
     │  paste the instructions into Copilot 365     │
     │  chat, attach (or paste) the sheet, save the │
     │  reply to copilot_batch1_response.csv        │
     └──────────────────────────────────────────────┘

                    scripts/ingest_response.py
fields.json    ─────────────────────────────────────►  build/proposals.json
batches.json                                           build/blankcrf.xfdf
responses                                              build/qc_preview.pdf

     ┌─────────────────── HUMAN ────────────────────┐
     │  review build/blankcrf.xfdf in Acrobat (or   │
     │  the Dash review UI, later). This is where   │
     │  review_status leaves `proposed`.            │
     └──────────────────────────────────────────────┘

                    scripts/render_final.py
blank_crf.pdf  ─────────────────────────────────────►  build/annotated_crf.pdf
final.xfdf
```

Three scripts rather than two because XFDF is the artifact a human touches. Once
it has been reviewed it — not `proposals.json` — is authoritative, and
`render_final.py` trusts it over any earlier pipeline output.

The generated instructions are short and static; the sheet carries the field
data and can be pasted or attached. The human's whole job per batch is: paste
the instructions, attach/paste the sheet, save the reply. No added context, no
edits, no explaining the task.

## Batches, not pages

The original design was one self-contained prompt per *page*, with the field
list embedded inline as numbered prose and the reply expected as a JSON array.
That does not scale: a several-hundred-page CRF means several hundred manual
paste-in/paste-out round trips, and round-trip *count* — not per-page prompt
quality — is the actual bottleneck in a pipeline with no API access.

This redesign separates what used to be fused into one prompt file:

* **instructions** (`copilot_batchN_instructions.txt`) — short, static text:
  framing, rules, output format. Does not grow with field count.
* **spec sheet** (`copilot_batchN_sheet.csv`) — one row per field, with the
  proposal columns empty for Copilot to fill in. This is what carries the
  field data, and it can span many pages in one file — which is the actual
  lever for cutting round-trip count.

Fields are grouped into batches by page, under `--max-fields-per-batch`
(default `prompt.DEFAULT_MAX_FIELDS_PER_BATCH`, currently 150). **That ceiling
is a guess, not a measured Copilot 365 limit** — there is no way to know its
practical attachment/context budget without testing against a real session.
Tune it once that's known. The synthetic 2-page CRF collapses to one batch
under the default, which is the redesign's whole point made concrete: one
round trip covers both pages, not two.

The join key is `field_id` (already globally unique, assigned by `extract.py`),
not a row position — a dropped or reordered row is still identifiable, and it
needs no accompanying page number to be unambiguous across a multi-page batch.

**Anticipated failure mode:** chat UIs have a strong habit of rendering
tabular data back as a markdown table (`| field_id | domain | ... |`) rather
than literal CSV, because that's idiomatic for a chat reply, even when told
not to. `parse_response.py` expects this as the *primary* case, not a
fallback — same posture as stripping a code fence the prompt said not to add.

**A real bug this surfaced:** `context` used to join same-line captions with
`' | '` ("line: Sex | Male | Female"). That string travels as a CSV cell and,
on the markdown-table path, as a *table* cell — and an unescaped `|` inside a
table cell silently shifts every column after it. It doesn't reliably fail
validation either: two free-text columns swapping is invisible to pydantic. Fixed
by changing the separator to `/` and by making the markdown-table parser check
cell count against the header, so a shift like this fails loudly instead of
landing `"VSPOS"` in the `origin` column. Regression-tested in both
`tests/test_extract.py` and `tests/test_parse_response.py`.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

## Seeing what it did

```bash
pytest -rA                                  # every test by name, pass or skip
pytest --basetemp=build/pytest -rA          # ...and keep the PDFs the tests built

python scripts/make_sample_crf.py           # the synthetic CRFs -> fixtures/
python scripts/preview_extraction.py        # what extract.py found -> build/
open build/extraction_ruled.pdf             # red boxes = detected, blue = caption

python scripts/illustrative_target.py       # hand-drawn picture of the goal
open build/ILLUSTRATIVE_target_annotated_crf.pdf
```

Two previews that are easy to confuse, so: `preview_extraction.py` shows each
field's **caption as printed on the form** ("Subject Identifier") — the *input*
to the Copilot step. `illustrative_target.py` shows the **SDTM annotation**
("DM.SUBJID") — the *output*, hand-written to illustrate the goal. Nothing in
the pipeline produces the latter yet.

The tests build their own CRFs in a temp directory and throw them away;
`--basetemp` is how you keep them. `preview_extraction.py` is the one to look
at when a coordinate change is suspect — a flipped rect still lands on the page
and still looks like a plausible box, so the overlay is the fastest way to see
that it is sitting on the wrong field.

PyMuPDF is the only PDF engine (AGPL-3.0, cleared separately for this internal
pipeline node). No pypdf, no pdfplumber.

Import it as **`import pymupdf`**, not `import fitz` — 1.28 deprecated the
`fitz` alias and warns it will be removed. The name survives in
`geometry.py`'s function names only as shorthand for the top-left/y-down
coordinate convention.

## The coordinate gotcha

PyMuPDF uses a **top-left origin, y increasing downward**. XFDF and the PDF spec
use a **bottom-left origin, y increasing upward** for annotation `/Rect`. Every
rect crossing that boundary is reflected about the page height — which reverses
the two y-coordinates, so they must be re-sorted, not just subtracted in place.

The arithmetic lives in exactly one module, [pipeline/geometry.py](pipeline/geometry.py),
called from two boundaries: `extract.py` on the way in and `xfdf_to_pdf.py` on
the way out. Everything in between — every `BBox` in `models.py`, `fields.json`,
`proposals.json`, and the XFDF `@rect` attribute — is already PDF user space, so
`xfdf.py` does no flipping of its own.

`page_index` is **0-based everywhere**, including the XFDF `@page` attribute.
Add 1 only when displaying to a human (`SdtmAnnotation.display_page`).

## The one manual step

Everything is wired except the hop that structurally cannot be automated. To
close it out on the synthetic CRF:

```bash
python scripts/make_sample_crf.py
python scripts/extract_and_prompt.py fixtures/SYNTHETIC_sample_crf_acroform.pdf
```

This writes `build/batches.json` (one batch, both pages, under the default
ceiling) plus `build/copilot_batch1_instructions.txt` and
`build/copilot_batch1_sheet.csv`. Paste the instructions into Copilot 365
chat, attach (or paste) the sheet, and save the reply verbatim to
`build/copilot_batch1_response.csv` — the path is in `batches.json`'s
`expected_response`. **Save it exactly as returned** — the markdown
reformatting, smart quotes and conversational wrapper are the data; whatever
it does that the parser mishandles is the finding this whole step exists to
surface.

```bash
python scripts/ingest_response.py --pdf fixtures/SYNTHETIC_sample_crf_acroform.pdf
python scripts/render_final.py fixtures/SYNTHETIC_sample_crf_acroform.pdf \
    build/blankcrf.xfdf -o build/annotated_crf.pdf
```

Add the raw reply to `fixtures/` as a parser fixture and extend
`tests/test_parse_response.py` from what it actually did, rather than from
what it was supposed to do. Also worth learning from that first real session:
whether Copilot 365 chat genuinely returns a downloadable file for an attached
sheet (bytes preserved exactly) or only lets you copy text out of the chat
window (in which case CSV's delimiter-dependent structure is more exposed to
mangling than the old JSON design was, not less) — that answer should decide
whether the sheet stays CSV or needs to change.

## Test data

No CRF PDFs live in this repo — not real ones, and not the synthetic one.
Real CRFs are passed by path at runtime and everything derived from them lands
in the gitignored `build/`.

`scripts/make_sample_crf.py` draws a fake CRF from nothing (invented protocol,
invented fields, no study data) so the pipeline can be tested without a real
form nearby. Every page is stamped `SYNTHETIC TEST DATA - NOT A REAL CRF`.

```bash
python scripts/make_sample_crf.py     # regenerates fixtures/, ~instant
```

It emits three variants, because `extract.py` has to handle all of them:

| Variant | What it is |
|---|---|
| `..._acroform.pdf` | real AcroForm widgets — the good case |
| `..._flat.pdf` | the same doc `bake()`d — boxed fields, no widgets |
| `..._ruled.pdf` | the other flat morphology — fill-in-blank underlines |

The flat variant is baked from the AcroForm one rather than drawn separately,
so the two are geometrically identical by construction and "do both extraction
paths agree?" is a real assertion. Two gotchas, both verified rather than
assumed: a border-less widget bakes to *nothing*, and baking insets rects by
half the border width (150.0 → 150.5), so cross-variant comparisons need a
~1pt tolerance.

**`fixtures/sample_crf_truth.json` is the only tracked artifact** — the exact
bbox and label of every field, in PDF user space, no form and no PDF. It is
committed to break a circularity: if the generator and `extract.py` were both
written against the same wrong assumption they would agree with each other and
the extraction tests would pass while proving nothing. The committed numbers
fail loudly the moment the layout drifts. (That guard is itself verified — a
1pt shift in one field's rect does fail the suite.)

The layout is deliberately adversarial: vertically asymmetric fields (a y-flip
bug is invisible if everything sits mid-page), non-square text fields, section
and footer rules that are *not* fields, both label geometries (left-of-field on
page 1, column-header-above in the Vital Signs grid), non-mapped page furniture
for the `NotSubmitted` case, and a `--TESTCD` grid for the
`VSORRES when VSTESTCD = SYSBP` condition pattern. A rotated page is
deliberately deferred until the unrotated coordinate path is proven.

## Field detection

`extract.py` tries AcroForm widgets first and falls back to text/line layout
detection per page, so a mixed CRF works. All three fixture variants extract
31/31 fields with correct captions and no false positives.

Captions are read in a different order depending on field type, because real
CRFs use at least three geometries: text fields are captioned from the **left**,
checkboxes from the **right** (`[ ] Male`), and grid columns from **above**
(`Not Done` as a header). Each type tries its directions in order and falls
through, which is how a grid checkbox with nothing to its right still finds the
column header five rows up.

Telling a fill-in blank from a section rule takes two tests together: a
separator spans the text column (anything wider than 60% of the page is a
rule), and a fill-in blank has a caption. Underline-derived fields keep their
measured baseline; only the top edge is inferred, from the caption height.

`context` carries what Copilot needs to disambiguate rows that look identical
in isolation, since it never sees coordinates:

```
VS_SYSBP_RES  label:   'Systolic Blood Pressure'
              context: 'section: VITAL SIGNS; line: Systolic Blood Pressure / mmHg; above: Result'
```

Same-line captions are joined with `/`, not `|` — `context` travels as a CSV
cell and, on the markdown-table fallback path, as a table cell, and a bare `|`
there silently shifts every column after it. See "Batches, not pages" below.

## GxP / 21 CFR Part 11

Every Copilot-sourced annotation is a *proposal* until a human explicitly
accepts it. Nothing writes to a submission artifact automatically.

The provenance fields on `SdtmAnnotation` (`source_model`, `review_status`,
`reviewed_by`, `created_at`, `reviewed_at`) stay populated even without
API-level determinism, and the model enforces that any status other than
`proposed` names the human who set it. The audit trail needs to show a human was
in the loop at the annotation step — with a manual paste that is a stronger
Part 11 story than a pure API call, so it is explicit in the data model rather
than implicit.

`build/qc_preview.pdf` is a pre-review preview and never a deliverable; the
submission artifact only ever comes out of `render_final.py`.

## Layout

```
pipeline/
  models.py         pydantic records: BBox, CRFField, SdtmAnnotation, enums
  geometry.py       the y-flip, in one place
  extract.py        PyMuPDF field detection (AcroForm + text/line fallback)
  prompt.py         builds the batched instructions + CSV spec sheet
  parse_response.py parses the filled-in sheet (CSV or markdown-table reply)
  layout.py         moves annotations off the fields they annotate
  xfdf.py           XFDF writer
  render.py         shared drawing helper for the two renderers
  stamp.py          QC stamping from in-memory records (pre-review)
  xfdf_to_pdf.py    XFDF -> annotated PDF (post-review, authoritative)
scripts/
  make_sample_crf.py     synthetic blank CRF + truth file
  extract_and_prompt.py  step 1
  ingest_response.py     step 2
  render_final.py        step 3
  preview_extraction.py  debugging aid: what extract.py found
  illustrative_target.py hand-drawn picture of the goal
fixtures/           test PDFs (synthetic only — no real study data)
build/              generated artifacts, all disposable
tests/
```

`layout.py` and `render.py` are not in the original plan. `layout.py` exists
because `parse_response.py` can only give an annotation the bbox of the field
it annotates, so without it every annotation renders on top of the box a site
writes in. `render.py` is the shared drawing helper the instructions sanction
for `stamp.py` and `xfdf_to_pdf.py` — they still have separate entry points
and separate purposes.

Layout avoids fields, other annotations, and the form's printed text, then
walks downward when blocked. It does **not** reflow into margins, abbreviate
text that will not fit, or draw leader lines from a displaced annotation back
to its field — that is the "collision layout for dense pages" later phase.
`render_final.py` warns when an annotation's text is wider than its rect,
since PyMuPDF clips silently and a truncated annotation in a submission
artifact is a data-integrity problem.

`stamp.py` and `xfdf_to_pdf.py` stay separate entry points even where their
implementations converge: one is a pre-review preview, the other is the
submission artifact, and conflating them is how a stale preview gets mistaken
for the real thing.

## Later phases

Dash review UI (`dash-ag-grid`), collision layout for dense pages, bookmarking
by domain, define.xml reconciliation, full audit-trail persistence of every
review-status transition. Deployment target is Posit Connect; Dash, not R Shiny,
with a possible FastAPI sidecar.
