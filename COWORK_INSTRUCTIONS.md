# aCRF Annotation Pipeline — Cowork Build Instructions (from scratch)

## Project goal

Build a pipeline that takes a blank Case Report Form (CRF) PDF and produces an
annotated version (XFDF) suitable for FDA submission: domain and variable-level
SDTM annotations placed next to each capture field on the form.

Nothing exists yet. Build this from an empty directory.

## Key constraint: no API-based LLM access

There is no programmatic access to any LLM. The only available model is
**Microsoft Copilot 365 chat**, used interactively by a human — no API, no
automation into it. This changes the shape of the pipeline: the annotation
step cannot be a function call. It has to be a **human-in-the-loop hop**:

1. Pipeline extracts fields from the blank CRF and generates a **complete,
   self-contained prompt** (system instructions + field data baked in, nothing
   else required) as a plain text file.
2. Human opens that file, copies the whole contents, pastes into Copilot 365
   chat, and pastes Copilot's reply into a designated location (a file, or a
   paste-in CLI prompt).
3. Pipeline parses that pasted response back into structured annotation
   records and continues (validation → XFDF).

Design the prompt so a human never has to add context, explain the task, or
edit anything before pasting it in — the generated `.txt` file must be
complete and copy-paste ready. Design the parser to be forgiving of the kind
of light reformatting a chat UI copy/paste tends to introduce (stray
whitespace, smart quotes, a wrapping ```json fence) since Copilot's output
won't be as strictly controllable as an API call with `temperature=0`.

## PDF engine: PyMuPDF (fitz)

Use PyMuPDF as the primary and only PDF engine — no pypdf, no pdfplumber.
Licensing (AGPL-3.0) has been cleared separately for this internal pipeline
node; do not raise it again or substitute an alternative library for that
reason.

Relevant PyMuPDF surface:
- `fitz.open(path)` → document
- `page.widgets()` → AcroForm fields, if the blank CRF export still has them
- `page.get_text("words")` / `page.get_text("dict")` → text with coordinates
- `page.get_drawings()` → lines/rects, for inferring fill-in blanks and
  checkboxes when there's no AcroForm
- `page.add_freetext_annot(rect, text, ...)` → stamping annotations for QC
  preview
- `page.rect`, `page.rotation` → page geometry

**Coordinate system warning — the single most important gotcha in this
project:** PyMuPDF uses **top-left origin, y increasing downward**
(`fitz.Rect` matches this). XFDF and the PDF spec itself use **bottom-left
origin, y increasing upward** for `/Rect` on annotations. Every rect that
goes into XFDF must be flipped: `y_pdf = page_height - y_fitz`, applied to
both y-coordinates of a rect (and remember which one becomes y0 vs y1 after
the flip — don't just subtract in place and leave them swapped). Do this
conversion in exactly one place in the code, at the point where a rect is
about to be written to XFDF, and write a test that stamps one known field,
renders it with `page.get_pixmap()`, and visually confirms it lands on top of
the right form field before building anything downstream of it.

XFDF `@page` is 0-based; PDF page numbers as humans read them are 1-based.
Keep an internal `page_index` that's 0-based everywhere and only add 1 when
displaying to a human.

## Suggested project layout

```
carf/
  pipeline/
    __init__.py
    models.py       # pydantic: BBox, CRFField, SdtmAnnotation, AnnotationKind, Origin
    extract.py       # PyMuPDF-based field detection (AcroForm + text/line fallback)
    prompt.py         # builds the self-contained Copilot prompt file
    parse_response.py # parses pasted Copilot output back into SdtmAnnotation records
    xfdf.py            # XFDF writer (coordinate flip happens here)
    xfdf_to_pdf.py       # final step: XFDF -> annotated PDF (coordinate flip back)
    stamp.py            # PyMuPDF-based QC stamping onto a copy of the PDF (kept separate from xfdf_to_pdf — see note below)
  scripts/
    make_sample_crf.py   # synthetic blank CRF fixture for testing without real study data
    extract_and_prompt.py # step 1: PDF -> fields -> prompt .txt
    ingest_response.py    # step 2: pasted Copilot response -> validated annotations -> XFDF + QC PDF
    render_final.py       # step 3: blank PDF + (possibly hand-edited) XFDF -> final annotated PDF
  fixtures/
  build/
  requirements.txt
  README.md
```

Three entry-point scripts, not two — because XFDF is the artifact a human may
touch by hand (accept/edit/reject in Acrobat, or edits made in the eventual
Dash review UI get re-exported to XFDF) before the final PDF gets built:
- `extract_and_prompt.py <blank_crf.pdf>` → writes `build/copilot_prompt.txt`
  and `build/fields.json` (the field data the prompt was built from, needed
  again at parse time to reattach bbox/page_index to whatever Copilot returns).
- Human: open `build/copilot_prompt.txt`, paste into Copilot 365 chat, save
  the reply to `build/copilot_response.txt`.
- `ingest_response.py` → reads `build/fields.json` + `build/copilot_response.txt`,
  parses, validates, writes `build/blankcrf.xfdf` and a QC PDF.
- Human: reviews/edits `build/blankcrf.xfdf` (in Acrobat or the review UI once
  it exists). This is the point where `review_status` moves from `proposed`
  to `accepted`/`edited`/`rejected`.
- `render_final.py <blank_crf.pdf> <final.xfdf>` → the new step described
  below, produces the submission-ready annotated PDF.

## Data model (pydantic)

- `BBox(x0, y0, x1, y1)` — always PDF user-space (bottom-left origin) once it
  leaves `extract.py`. Store the fitz-native rect only transiently during
  extraction.
- `CRFField` — `field_id`, `page_index` (0-based), `bbox`, `label`, `source`
  (`acroform` | `text_layout`), `context` (surrounding text, for
  disambiguation), `acroform_name` (if applicable).
- `AnnotationKind` — `domain` | `variable` | `note`.
- `Origin` — Define-XML v2.1 origin types: `Collected`, `Derived`, `Assigned`,
  `Protocol`, `eDT`, `Predecessor`, `NotSubmitted`.
- `SdtmAnnotation` — `annot_id`, `field_id` (nullable, for page-level domain
  annotations), `page_index`, `bbox`, `kind`, `domain`, `variable`,
  `condition` (e.g. `"VSTESTCD = SYSBP"`), `codelist`, `origin`, plus
  provenance fields: `confidence`, `rationale`, `source_model` (free text,
  e.g. `"Copilot 365 chat, manual paste"` — there's no API model_id here),
  `review_status` (`proposed`/`accepted`/`edited`/`rejected`),
  `reviewed_by`. Keep the provenance fields even without an API — GxP audit
  trail still wants to know *something* generated this, even if it's "human
  pasted from Copilot on this date."

## The prompt file (`prompt.py`)

Build one prompt per CRF page (Copilot chat has practical length/context
limits, and a human is going to be doing this once per page anyway). The
prompt must contain, self-contained, no external context assumed:

- A short framing: SDTM annotation of a blank CRF for FDA submission, per
  CDISC SDTMIG v3.4.
- The task rules (domain assignment, `--TESTCD`/`--TEST` condition pattern,
  `NotSubmitted` for non-mapped fields like page numbers, confidence scoring).
- The numbered field list for that page (`label`, `context`, `acroform_name`
  if present) exactly as extracted — this is what replaces the API's
  structured field data.
- An explicit output format instruction: **JSON array only, no prose, no
  markdown fences**, one object per annotation, with a fixed schema spelled
  out field-by-field in the prompt itself (mirror `SdtmAnnotation`'s proposal
  fields — not the provenance ones, those get filled in after parsing).
- A one-line reminder at the very end of the prompt: "Return only the JSON
  array. Do not include any explanation before or after it." Copilot chat
  UIs are more likely than a raw API call to add a conversational wrapper
  around the answer; the parser needs to be robust to this anyway (see
  below), but reducing it at the source is worth doing.

Write each page's prompt to its own file (`build/copilot_prompt_page1.txt`,
etc.) if a CRF has multiple pages, so the human can process one page at a
time without losing track of which reply goes with which page.

## The parser (`parse_response.py`)

Must handle a pasted chat response, not a clean API payload:
- Strip leading/trailing prose the model may have added despite instructions
  — look for the first `[` and last `]` and take that span, rather than
  assuming `response.strip()` is valid JSON on its own.
- Strip ```json fences if present.
- Normalize smart quotes (`" " ' '` → `" '`) before parsing — a common
  side effect of pasting through a chat UI.
- On a parse failure, don't silently drop the page: fail loudly with the
  raw text shown, so the human can fix it and re-paste rather than getting a
  silently incomplete annotation set.
- After parsing, re-attach each item to its source `CRFField` (via the
  `field_index` the prompt asked Copilot to echo back) to get `page_index`
  and `bbox` — Copilot never sees or needs to reason about coordinates.
- Fill in `review_status="proposed"`, `source_model="Copilot 365 chat"`, and
  a timestamp before writing out.

## Final conversion: XFDF → annotated PDF (`xfdf_to_pdf.py`)

This is the last pipeline step: take the blank CRF PDF plus a (possibly
hand-edited) XFDF file and produce the actual submission-ready annotated PDF.

**PyMuPDF has no built-in XFDF importer.** There's no `fitz.import_xfdf()` or
equivalent — XFDF import/export is an Acrobat/pdf-lib-ecosystem feature, not
something the PyMuPDF C core implements. So this has to be a small
hand-written XML parser feeding `fitz`'s native annotation API. That's fine —
XFDF is simple XML and we control the subset we emit — but don't go looking
for a library shortcut here; write it directly.

Steps:
1. Parse the XFDF with `xml.etree.ElementTree` (standard library — no need
   for a new dependency). Walk `<annots>/<freetext>` elements (and any other
   annotation subtypes you're emitting — check what `xfdf.py` actually
   writes, e.g. also `<square>` if boxed domain markers are drawn as separate
   rect annotations rather than bordered freetext).
2. For each annotation, read `@page` (0-based — matches `page_index`
   already), `@rect`, `<contents>`, `@color`, and whatever style attributes
   `xfdf.py` wrote (font size, etc.).
3. **Flip the rect back.** `xfdf.py` converted fitz's top-left/y-down rects to
   PDF's bottom-left/y-up for the `@rect` attribute. This step reverses that
   exact conversion: `y_fitz = page_height - y_pdf`, applied to both
   coordinates, with the same care about which becomes top vs. bottom after
   the flip. If `xfdf.py`'s conversion function was written as a single
   reusable function rather than inlined, reuse it here (import and call with
   swapped args, or add a small `pdf_rect_to_fitz_rect()` alongside it) —
   don't reimplement the flip a second time from scratch, since a
   transcription slip here is exactly the kind of bug that's invisible until
   someone opens the final PDF and everything is offset.
4. Create the annotation on the page with `page.add_freetext_annot(rect, text,
   fontsize=..., text_color=..., ...)`. For boxed domain markers, either use
   `add_freetext_annot` with a border (test this renders correctly — the
   pypdf equivalent silently dropped text when a border was set; confirm
   PyMuPDF doesn't have the same issue before relying on it) or
   `add_rect_annot` plus a separate freetext for the label, matching whatever
   `xfdf.py` actually emits structurally.
5. Skip any annotation whose `review_status` would resolve to `rejected` —
   in practice this means: XFDF as written by `xfdf.py` already excludes
   rejected annotations (confirm this is still true), but if a human hand-
   edited the XFDF in Acrobat, trust the XFDF file as the final word rather
   than cross-checking against `fields.json`/`proposals.json` again. The XFDF
   at this point *is* the reviewed, authoritative source — don't reintroduce
   the pre-review data as a filter.
6. Save with `doc.save(out_path, garbage=4, deflate=True)`. **Do not flatten
   the annotations into page content.** FDA submission review tools expect
   annotations to remain as PDF markup (searchable, layer-separable from the
   base form) — flattening into a rendered image is a regression, not a
   simplification, even though it might look identical on screen.
7. Confirm the round trip: `extract.py`'s coordinate-flip test proved
   fitz→XFDF was correct. Write the mirror-image test here — take a known
   `SdtmAnnotation`, run it through `xfdf.py` then `xfdf_to_pdf.py`, and
   confirm the final on-page position matches the original `bbox` exactly
   (not just visually — assert on coordinates). Round-trip tests catch flip
   bugs that visual review alone won't, since an off-by-one-page-height error
   can still look "close enough" at a glance.

**Relationship to `stamp.py`:** `stamp.py` (built earlier, for QC previews
right after Copilot ingestion) and `xfdf_to_pdf.py` both end up calling
similar PyMuPDF annotation APIs, but they're not the same step and shouldn't
be collapsed into one function — `stamp.py` renders directly from in-memory
`SdtmAnnotation` records for a quick look immediately after parsing, before
any human review has happened. `xfdf_to_pdf.py` renders from the XFDF file
specifically because that file may have been edited since then and is the
thing that's actually authoritative. If it turns out `stamp.py` and
`xfdf_to_pdf.py` end up nearly identical in implementation, that's fine — a
shared internal helper is reasonable — but keep them as separate entry points
with separate purposes, since conflating "pre-review preview" and "final
submission artifact" is the kind of thing that causes a stale preview to get
mistaken for the real deliverable.

## Task order

1. **Scaffold the package** — directory layout, `models.py`, empty stubs for
   the rest, `requirements.txt` (see below).
2. **`scripts/make_sample_crf.py`** — generate a synthetic 1–2 page blank CRF
   (e.g. Demographics + Vital Signs) using PyMuPDF's drawing API, so there's
   something to test against without real study data.
3. **`extract.py`** — PyMuPDF-based extraction, AcroForm path first, text/line
   fallback second. Confirm against the synthetic fixture. Write the
   coordinate-flip test described above before moving on.
4. **`prompt.py`** — build the per-page prompt file from extracted fields.
   Manually sanity check one generated prompt reads cleanly top to bottom
   with zero missing context.
5. **Do one real manual round-trip**: run step 4's output through Copilot 365
   chat yourself, save the reply, and use that real (not synthetic) response
   to build and test `parse_response.py`. Don't hand-write a fake response to
   test against — Copilot's actual formatting quirks are the thing that needs
   coverage.
6. **`xfdf.py`** — XFDF writer, coordinate flip happens here, `@page` 0-based.
7. **`stamp.py`** — PyMuPDF-native `add_freetext_annot` QC stamping.
8. **Wire up `extract_and_prompt.py` and `ingest_response.py`** as entry
   points, confirm the full extract → prompt → (manual Copilot step) →
   ingest → XFDF path works end to end on the synthetic fixture.
9. **`xfdf_to_pdf.py` + `render_final.py`** — implement the final conversion
   as described above. Write the round-trip coordinate test before
   considering this step done, not after. Manually edit one field in the
   generated XFDF (change a rect or add an annotation in a text editor or in
   Acrobat if available) and confirm `render_final.py` picks up the edit —
   this is the actual scenario the step exists for, so it's worth testing
   explicitly rather than only testing the unedited pass-through case.
10. Everything past this point (Dash review UI with `dash-ag-grid`, collision
    layout for dense pages, bookmarking by domain, define.xml reconciliation,
    full audit-trail persistence of every review-status transition) — treat
    as later phases once the core loop above is proven out.

## requirements.txt (starting point)

```
pymupdf          # PDF engine: extraction, geometry, stamping
pydantic>=2       # typed annotation records, validation at the parse boundary
dash>=2.18        # review UI (later phase)
dash-ag-grid>=31.2 # review UI (later phase)
```

## Standing constraints

- GxP / 21 CFR Part 11 context: every Copilot-sourced annotation is a
  *proposal* until a human explicitly accepts it in review. Nothing writes to
  a submission artifact automatically.
- Keep provenance fields (`source_model`, `review_status`, `reviewed_by`,
  timestamps) populated even without API-level determinism guarantees —
  the audit trail needs to show a human was in the loop at the annotation
  step, which is actually a *stronger* story for Part 11 than a pure API
  call, so make sure that's visible in the data model and not just implicit.
- Deployment target for the eventual review UI is Posit Connect, Dash (not R
  Shiny) for this project, possible FastAPI sidecar — consistent with prior
  project decisions.