# carf — aCRF annotation pipeline

Takes a blank Case Report Form PDF and produces an annotated version with
domain and variable-level SDTM annotations placed next to each question,
styled per Metadata Submission Guidelines v2.0.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest                              # 258 tests

python scripts/make_sample_crf.py   # the synthetic CRF -> fixtures/
python scripts/worked_example.py    # the whole path, end to end
open build/WORKED_EXAMPLE_annotated_crf.pdf
```

## The assumption everything rests on

**A CRF page is two text columns.** The left column carries the question
prompt, the right column carries the response option, unit or fixed value:

```
Sex                                                        Male  O
                                                         Female  O
Age (years) at time of consent            Fixed Unit: years      ____
<------------ column 1 ------------>|<------ column 2 ------>
                                 gutter
```

So the unit of extraction is a **row** — one printed line, up to two pieces of
text, up to two bboxes — and not a detected capture field:

| | |
|---|---|
| `row_id` | `p1_r010` |
| `form` | Demographics |
| `text_1` / `bbox_1` | "Age (years) at time of consent" @ 90,558,210,570 |
| `text_2` / `bbox_2` | "Fixed Unit: years" @ 402,558,470,570 |

That single assumption dissolves three separate piles of heuristics the earlier
design needed:

1. **Caption association stops being a search.** Column position *is* the
   semantics. No searching left, then above, then right with a distance budget
   per field type; no special case for a grid checkbox whose header sits five
   rows up.
2. **Field detection disappears.** Widgets are never located — not AcroForm
   widgets, not drawn boxes, not near-square checkboxes, not fill-in-blank
   underlines. So the discrimination that was hardest to get right, telling a
   fill-in blank from a section rule, simply does not arise.
3. **Placement stops being a collision search.** The gutter is a known empty
   corridor on every row's own baseline, which is where a human annotator
   writes. See [pipeline/layout.py](pipeline/layout.py).

It also fails better. A field detector silently omits what it fails to detect,
so a missed field is invisible. Every printed line becomes a row here, so a row
nobody has mapped shows up on the control sheet as an unmapped row.

Origin: the PHUSE DH12 presentation *Semi-automating Case Report Form
Annotations using Python and a Metadata Repository* (Alnylam). PyMuPDF remains
the only PDF engine — see "Why not PDFminer or PIL" below.

## The flow

```
                    scripts/build_sheet.py
blank_crf.pdf  ─────────────────────────────────►  build/rows.json
corpus_lookup.csv                                  build/proposals.json
                                                   build/control_sheet.xlsx
                                                   build/qc_preview.pdf

     ┌───────────────────── HUMAN ──────────────────────┐
     │  open control_sheet.xlsx, fill the blank cells,  │
     │  set review_status + reviewed_by.                │
     │  Grey cells were pre-populated and need a look.  │
     └──────────────────────────────────────────────────┘

                    scripts/annotate.py
blank_crf.pdf  ─────────────────────────────────►  build/annotated_crf.pdf
control_sheet.xlsx                                 (+ optional XFDF export)
```

Two scripts, one human step. `scripts/worked_example.py` runs the whole thing on
the synthetic CRF with hand-written mappings, so you can see the output without
a real CRF or a Copilot session.

## Where the mappings come from

**Mined corpus precedent, first.** `parse_annotated_pdf.py` recovers mappings
from a *finished* aCRF, and `corpus_precedent.py` runs that across a directory
of them and consolidates the result into a `question text → domain/variable/
condition` table with a support count. That table plays the role a metadata
repository would, and it is keyed the same way: CRF question text matched
against standard text held elsewhere.

Everything it fills in is marked `suggested`, which the control sheet renders as
a **grey cell** — so "mined from 7 prior CRFs" never looks like "a person
decided this".

```bash
python scripts/mine_corpus.py <dir-of-annotated-crfs> --blank-dir <dir-of-blanks>
python scripts/build_sheet.py blank_crf.pdf --precedent build/corpus_lookup.csv
```

**Copilot 365 chat, only for the gaps.** There is no programmatic LLM access
here; the only model available is Copilot 365 chat, driven by a person pasting
text. That step is now a side path rather than the front door:

```bash
python scripts/build_sheet.py blank_crf.pdf --precedent build/corpus_lookup.csv \
    --copilot-batches
# paste build/copilot_batch1_instructions.txt into Copilot, attach the sheet,
# save the reply verbatim to build/copilot_batch1_response.csv
python scripts/ingest_response.py --pdf blank_crf.pdf
```

Only rows precedent could not answer are batched
(`prompt.rows_needing_annotation`). That matters beyond volume: nothing on the
main path depends on a chat UI returning parseable text, so a batch that comes
back mangled costs a retry on a handful of rows instead of blocking the
document.

⚠️ `parse_response.py` has still never been exercised against a real Copilot
365 reply. Its recovery steps — markdown-table reformatting, smart quotes,
conversational wrappers — are built from known chat-UI behaviour, not from an
observed response. `tests/standin_response.py` is an explicitly-labelled
stand-in that proves the plumbing only. When a real reply arrives, save it
verbatim as a fixture and extend the tests from what it actually did.

## MSG v2.0 styling

Annotation text sits in a coloured box, black on the fill, with a legend at the
top of each page naming the domains that appear on it. Notes get a dashed
border instead of a fill. `[NOT SUBMITTED]` is grey.

**Colour is assigned by encounter order, not by domain.** There is no fixed
domain-to-colour mapping: colours come off an ordered palette in the order
domains are first encountered *within a form*. DM is not "the blue one" — DM is
whichever palette entry comes up first on the form where it appears first. Two
forms in one study can legitimately colour the same domain differently, which
is why [pipeline/msg.py](pipeline/msg.py) builds `form → [domain, ...]` before
it assigns anything.

⚠️ `MSG_PALETTE` is **incomplete**. It holds the four entries readable off the
DH12 deck, and those are *that deck's* encounter-order assignment for its own
example forms — which is exactly why they cannot be treated as a
domain-to-colour table. The rest must be transcribed from the guidelines
document. Running off the end of the palette reuses colours rather than
raising, so a form with more than four domains degrades visibly instead of
failing; guessing plausible pastels would produce output that looks compliant
and is not.

Annotations also drop the domain prefix — `BRTHDTC`, not `DM.BRTHDTC` — because
the domain is carried by the colour and the legend. That has a consequence for
the reverse direction, handled below.

## The two remaining heuristics

Everything else is arithmetic. These two are not, so both are tested directly.

**Where the gutter is** — `rows.detect_gutter`, by exact interval sweep for the
widest x-interval no run crosses, with a right-aligned-cluster fallback for
pages too dense to leave a clean corridor. Runs wider than
`FULL_WIDTH_FRACTION` of the page (headers, footers, spanning notes) are
excluded from detection, because they cross the corridor by design — but kept
as rows, since a spanning note is frequently the thing that needs annotating.

`gutter_x = None` is a **valid answer**, not an error: a title or instruction
page is single-column. A page whose own detection fails borrows the document
median, but only when that would actually split *that* page into two populated
columns (`rows.splits_into_columns`) — otherwise a genuinely single-column page
gets sheared in half at whatever x the rest of the document uses.

**Whether two lines are one wrapped question** — delegated to PyMuPDF's own
text-block analysis, *not* answered with a leading threshold, because no
threshold works. Measured on the synthetic fixture:

| | vertical gap |
|---|---|
| two lines of one wrapped question | **−2.4pt** (the line boxes overlap) |
| two separate options in a list | 1.6pt |
| a banner and the line beneath it | 2.9pt |

The case that must merge is *tighter* than the cases that must not, so any
cutoff gets it backwards. The bias is deliberate: failing to merge a wrapped
question is recoverable (annotate the first half, leave the second blank);
wrongly merging four race options into one row destroys the ability to annotate
them separately and nothing downstream can undo it.

## The control sheet is the Part 11 artifact

Every mapping is a *proposal* until a human explicitly accepts it, and
`build/control_sheet.xlsx` is where that happens — so it is where
`review_status` moves off `proposed` and `reviewed_by` gets recorded. That
argument used to attach to the XFDF; it transfers here whole.

`annotate.py` refuses to render a sheet whose annotations are still `proposed`
unless `--allow-unreviewed` is passed, and says so in its output when it is.

Read-back fails loudly on an unknown `row_id`, a duplicated `(row_id, slot)`,
or a status past `proposed` with nobody named. A silently discarded annotation
is the one defect a reviewer cannot see, and a spreadsheet is exactly where a
stray fill-down produces one.

`row_id`/`page`/`form`/`text_*`/`coord*` are locked. Worksheet protection is
not a security control — it is trivially removable, and openpyxl can strip it —
it is a guard rail that makes editing a coordinate a deliberate act rather than
a stray click.

`build/qc_preview.pdf` is a pre-review preview and never a deliverable; the
submission artifact only ever comes out of `annotate.py`.

## Reading a finished aCRF back

[pipeline/parse_annotated_pdf.py](pipeline/parse_annotated_pdf.py) runs the
opposite direction, which is what makes a corpus of historical aCRFs usable
rather than a pile of PDFs nobody can query. Two things it has to cope with:

**No join key survives to the final PDF**, so marks are re-matched by position.
The row model makes that materially more reliable: `layout.place_row` puts an
annotation at `bbox_1.x1 + GAP` on the row's own baseline, so a mark sharing a
row's vertical extent and starting just past its question text is that row's.
The previous design matched against detected widgets, where a dense grid of
same-sized boxes a few points apart meant nearest-centroid regularly picked the
neighbouring row.

**Two visual conventions**, which conflict on every signal — see
`classify_mark`. MSG output is bordered and black *everywhere*, so those signals
carry no information; the older convention (red text for a mapping, grey for a
note, a border for a banner) has to be checked last or it inverts the
classification completely.

And because MSG annotations carry no domain prefix, the domain is recovered from
the **page legend's colour key** (`legend_color_map`) — an explicit assertion
printed on the page that this colour means this domain, ranked above the
built-in CDISC constants for exactly that reason.

## Coordinates

PyMuPDF uses a **top-left origin, y increasing downward**. XFDF and the PDF spec
use a **bottom-left origin, y increasing upward** for annotation `/Rect`. Every
rect crossing that boundary is reflected about the page height — which reverses
the two y-coordinates, so they must be re-sorted, not just subtracted in place.

The arithmetic lives in exactly one module,
[pipeline/geometry.py](pipeline/geometry.py). Everything in between — every
`BBox` in `models.py`, `rows.json`, `proposals.json`, the control sheet's
`coord1`/`coord2`, the XFDF `@rect` — is already PDF user space. `x` is
unaffected, which is why `gutter_x` needs no conversion.

`page_index` is **0-based everywhere**, including the XFDF `@page` attribute.
Add 1 only when displaying to a human (`display_page`).

Two measured PyMuPDF behaviours worth knowing, both documented at their use
sites and asserted rather than trusted:

* A bordered annotation's stored `/Rect` is **inflated by half the border
  width** (a box placed at x0=260 reads back at 259.5). Under MSG every
  annotation is bordered, so a matching tolerance tighter than that makes
  reverse-layout matching silently unreachable.
* `fill_color` is written to `/C`, which PyMuPDF reports back as
  `colors["stroke"]`. On a FreeText annot `/C` *is* the background.

`preview_extraction.py` is the fastest way to catch a coordinate bug — a
flipped rect still lands on the page and still looks plausible, so the overlay
is what shows it sitting on the wrong row. It draws the detected gutter as a
line, column 1 in red, column 2 in blue, and gutter-spanning rows in orange.

## Why not PDFminer or PIL

The DH12 deck uses PDFminer for extraction and PIL to measure annotation text
width. Neither is needed:

* **PyMuPDF already reads the text.** Adding a second PDF parser would mean two
  coordinate conventions in one codebase, and PyMuPDF is also the engine for
  geometry, stamping and rendering.
* **`pymupdf.get_text_length` measures the font that actually gets embedded** in
  the output. PIL measures a system font, so it can only approximate the width
  of the glyphs the box will contain.

The deck's block-grouping behaviour is not lost either — PDFminer's
`LTTextBox` grouping is what merges a wrapped question there, and PyMuPDF's own
block analysis does the same job, which is what `rows.py` uses.

## Test data

No CRF PDFs live in this repo — not real ones, and not the synthetic one. Real
CRFs are passed by path at runtime and everything derived from them lands in the
gitignored `build/`.

`scripts/make_sample_crf.py` draws a fake CRF from nothing (invented protocol,
invented questions, no study data). Every page is stamped `SYNTHETIC TEST DATA
- NOT A REAL CRF`. Two variants, and the point of having two has changed: they
must produce **identical rows**, because extraction reads text and widgets
contribute none. The flat variant is the AcroForm one `bake()`d, so any
difference could only come from the widgets — and it must be exact, not within
a tolerance, which the old field-detection comparison could never claim.

The layout is deliberately adversarial: a wrapped question, option-only
continuation rows, options in *both* columns, a full-width note crossing the
gutter, a single-column page 3 whose page number is right-aligned on purpose
(so `MIN_COLUMN_RUNS` has to be doing real work), vertical asymmetry so a y-flip
is unmissable, section rules that are not rows, and a `--TESTCD` grid for the
`VSORRES when VSTESTCD = SYSBP` pattern. A rotated page is still deferred until
the unrotated coordinate path is proven.

**`fixtures/sample_crf_rows_truth.json` is the only tracked artifact.** It
commits the generator's *inputs* — each row's text, form, spanning flag, the x
the text was inserted at and its baseline y, plus the interval each page's
gutter must fall inside. Not the extractor's outputs: a glyph's vertical extent
comes from PyMuPDF's font metrics rather than anything the generator chooses, so
committing it would pin a library internal instead of a fact about the layout.

It is committed to break a circularity: if the generator and `rows.py` were both
written against the same wrong assumption they would agree with each other and
the tests would pass while proving nothing. Extraction recovers the committed
insertion x to floating-point precision (worst case across all 54 rows:
0.0000pt), so the tolerance is 0.25pt and a 1pt layout shift fails decisively —
which `test_a_layout_shift_fails_the_truth_check` verifies, because a guard
nobody has tried is not a guard.

## Layout

```
pipeline/
  models.py           pydantic records: BBox, CRFRow, RowSet, SdtmAnnotation
  geometry.py         the y-flip, in one place
  text.py             words -> visual lines -> runs (no widget knowledge)
  rows.py             gutter detection + two-column row assembly
  msg.py              MSG v2.0 palette, per-form domain encounter order
  corpus_precedent.py mines prior aCRFs; pre-populates from what it mined
  control_sheet.py    the XLSX a human reviews -- the Part 11 artifact
  layout.py           gutter placement (arithmetic, not search)
  render.py           shared drawing helper: fills, dashes, legend
  stamp.py            QC stamping from in-memory records (pre-review)
  prompt.py           Copilot batches, for the rows precedent missed
  parse_response.py   parses a filled-in sheet (CSV or markdown-table reply)
  parse_annotated_pdf.py  finished aCRF -> recovered mappings
  xfdf.py             XFDF export (Acrobat interop; off the critical path)
  xfdf_to_pdf.py      XFDF -> annotated PDF, for Acrobat-side edits
scripts/
  make_sample_crf.py  synthetic blank CRF + truth file
  build_sheet.py      step 1
  annotate.py         step 2
  worked_example.py   the whole path with hand-written mappings
  ingest_response.py  optional: merge Copilot replies into the sheet
  mine_corpus.py      build the precedent table from historical aCRFs
  preview_extraction.py  debugging aid: gutter + both columns, drawn
  parse_annotated_crf.py recover mappings from one finished aCRF
fixtures/           truth file only -- no PDFs, synthetic or otherwise
build/              generated artifacts, all disposable
tests/
```

XFDF stays as an **export** for Acrobat interop rather than the review surface.
When an XFDF *has* been hand-edited it is authoritative over upstream records
for the same reason the sheet is — a human touched it — and `xfdf_to_pdf.py`
deliberately does not cross-check it against anything.

## Known limitations

* **Response widgets are invisible to placement.** `rows.py` reads text, so a
  long annotation on a row with no `text_2` can overlap a fill-in underline or
  checkbox to its right. Limiting annotations at the gutter was considered and
  rejected: the guidelines' own examples show them extending well past the
  column break, so the limit would break the common case to protect the rare
  one. The overlap is visible in the QC preview.
* **Dense pages.** Placement wraps to the line below and jumps clear of
  obstacles, but does not reflow into margins, abbreviate text that will not
  fit, or draw leader lines from a displaced annotation back to its row.
  `annotate.py` warns when an annotation's text is wider than its box, since
  PyMuPDF clips silently and a truncated annotation in a submission artifact is
  a data-integrity problem.
* **The MSG palette is incomplete** — see above.
* **`parse_response.py` is unexercised against real Copilot output** — see
  above. No longer a gate on producing an annotated CRF.

## Later phases

Dash review UI (`dash-ag-grid`) as an alternative to the spreadsheet, collision
layout for dense pages, bookmarking by domain, define.xml reconciliation, full
audit-trail persistence of every review-status transition. Deployment target is
Posit Connect; Dash, not R Shiny, with a possible FastAPI sidecar.
