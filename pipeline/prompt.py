"""Builds the self-contained Copilot 365 batch materials: instructions + spec sheet.

Task order step 4 (redesigned; see README "Batches, not pages").

Original design was one giant prompt per page, with the field list embedded
inline as numbered prose and the reply expected back as a JSON array. That
does not scale: a several-hundred-page CRF means several hundred manual
paste-in/paste-out round trips, which is the actual bottleneck in this
pipeline (there is no API -- see COWORK_INSTRUCTIONS.md). Per-page prompt
quality was never the constraint; round-trip *count* was.

This version separates the two things that used to be fused into one prompt
file:

* **instructions** -- short, static text: framing, rules, schema. Does not
  grow with field count.
* **spec sheet** -- a CSV, one row per field, with empty columns for Copilot
  to fill in. This is what carries the field data, and it can span many pages
  in one file, which is the actual lever for cutting round-trip count. A human
  pastes the instructions and attaches (or pastes) the sheet once per batch,
  not once per page.

Batching
--------
Fields are grouped by page into batches under a field-count ceiling
(``max_fields_per_batch``). The ceiling is a guess, not a measured Copilot
limit -- there is no way to know Copilot 365's practical attachment/context
budget without testing against a real session. Tune it once that is known;
see README "The one manual step".

The join key
------------
Each row carries ``field_id`` -- already globally unique across the document,
assigned by ``extract.py`` -- which Copilot must echo back unchanged.
Unlike a page-relative ordinal, it needs no accompanying page number to be
unambiguous, which is what makes it safe to use across a multi-page batch: a
dropped or reordered row is still identifiable, and a row from the wrong batch
is impossible to construct by accident.

Anticipated failure mode
-------------------------
Chat UIs have a strong habit of rendering tabular data back as a markdown
table (``| field_id | domain | ... |`` with a header-separator row) rather
than literal CSV, because that is idiomatic for a chat reply. The instructions
explicitly ask for CSV and no markdown table; ``parse_response.py`` expects
the markdown-table reply anyway, the same way the old JSON prompt asked for
"no code fence" and the parser stripped fences regardless.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from pipeline.models import CRFField, FieldSet

INSTRUCTIONS_FILENAME = "copilot_batch{n}_instructions.txt"
SHEET_FILENAME = "copilot_batch{n}_sheet.csv"
RESPONSE_FILENAME = "copilot_batch{n}_response.csv"

DEFAULT_MAX_FIELDS_PER_BATCH = 150  # unvalidated -- see module docstring

# Columns Copilot must not alter, echoed back for the human's own reference
# when they read the sheet. The parser keys on field_id alone and does not
# require these to survive the round trip.
READONLY_COLUMNS = ["field_id", "page", "label", "context", "acroform_name"]
# Columns Copilot fills in.
FILL_COLUMNS = [
    "kind", "domain", "variable", "condition", "codelist",
    "origin", "confidence", "rationale",
]
SHEET_COLUMNS = READONLY_COLUMNS + FILL_COLUMNS

_FRAMING = """\
You are annotating a blank Case Report Form (CRF) so it can be submitted to
the FDA as an annotated CRF (aCRF). Annotations map each data capture field on
the form to the SDTM dataset variable that will hold its value, following
CDISC SDTMIG v3.4.

Attached is a spec sheet (CSV) listing every capture field in this batch, one
row per field, across {page_span} of this CRF. For each row, fill in the
empty columns using the rules below.
"""

_RULES = """\
RULES

1. DOMAIN. Assign the two-letter SDTM domain from the field's content and the
   section it sits in (DM for demographics, VS for vital signs, AE for
   adverse events, CM for concomitant medications, and so on). Use the
   "context" column as your strongest signal -- it names the section heading.

2. VARIABLE. Give the SDTM variable name without the domain prefix where the
   domain is already stated, e.g. domain "DM", variable "SEX". For findings
   domains the result variable is normally --ORRES.

3. TESTCD CONDITION. Findings-class domains (VS, LB, EG, ...) reuse one result
   variable across many rows, so a row of the CRF is identified by a condition
   rather than a distinct variable. Where a spec-sheet row is one such CRF
   row, set "variable" to the result variable and "condition" to the test
   that selects it:
       variable:  VSORRES
       condition: VSTESTCD = SYSBP
   Leave "condition" empty for fields that need no such qualifier.

4. NOT SUBMITTED. Page furniture that carries no submitted data -- page
   numbers, form version strings, investigator or assessor initials, "page 1
   of 2" footers -- maps to no SDTM variable. For these set "kind" to "note",
   leave "domain" and "variable" empty, and set "origin" to NotSubmitted.

5. ORIGIN. Use the Define-XML v2.1 origin type, exactly one of: Collected,
   Derived, Assigned, Protocol, eDT, Predecessor, NotSubmitted. A value
   written on the form by a site is Collected. A value calculated from other
   fields is Derived. A constant such as a units label is Assigned.

6. CODELIST. Where the field is a controlled-terminology item (sex, race,
   ethnicity, position, units), give the CDISC codelist name or C-code in
   "codelist". Otherwise leave it empty.

7. CONFIDENCE. In the "confidence" column, score your own certainty from 0.0
   to 1.0. Use below 0.7 where the field label is ambiguous, where the domain
   is a guess, or where you had to choose between two plausible variables. Do
   not inflate these -- a human reviews every row and low scores are used to
   prioritise that review.

8. RATIONALE. In the "rationale" column, give one short sentence saying why
   you chose that domain/variable for this field. This is what a reviewer
   reads first, so make it concrete: name the caption or section that drove
   the decision, not a restatement of the mapping itself.

9. Do not add, remove, reorder, merge, or split rows. Do not alter the
   field_id, page, label, context, or acroform_name columns -- return them
   exactly as given. One filled-in row per input row.
"""

_FORMAT = """\
OUTPUT FORMAT

Return the complete sheet, all {n_columns} columns, as CSV -- the same format
it was given to you in. Every row from the input, in the same order, with the
empty columns now filled in.

Do NOT return a markdown table. Do NOT wrap the CSV in a code fence. Do NOT
add commentary before, after, or between rows. Your entire reply must be
parseable as a CSV file and nothing else.
"""

_REMINDER = (
    "Return only the CSV. No markdown table, no code fence, no prose before "
    "or after it."
)


def batch_pages(
    fieldset: FieldSet, max_fields_per_batch: int = DEFAULT_MAX_FIELDS_PER_BATCH
) -> list[list[int]]:
    """Group page indexes into batches under a field-count ceiling.

    Greedy: accumulate whole pages until the next one would push the batch
    over the ceiling. A single page's fields are never split across batches --
    section context in a page's captions is only meaningful together, and
    keeping a page whole is a simpler guarantee than reasoning about a field
    split mid-page. A page that alone exceeds the ceiling still becomes its
    own (oversized) batch rather than being dropped or split.
    """
    counts = {p.page_index: len(fieldset.for_page(p.page_index)) for p in fieldset.pages}
    batches: list[list[int]] = []
    current: list[int] = []
    current_count = 0

    for page in sorted(counts):
        n = counts[page]
        if current and current_count + n > max_fields_per_batch:
            batches.append(current)
            current, current_count = [], 0
        current.append(page)
        current_count += n

    if current:
        batches.append(current)
    return batches


def build_spec_sheet(fieldset: FieldSet, page_indexes: list[int]) -> str:
    """CSV text for one batch: filled read-only columns, empty fill columns."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=SHEET_COLUMNS, lineterminator="\n")
    writer.writeheader()
    fields = sorted(
        fieldset.for_pages(page_indexes), key=lambda f: (f.page_index, -f.bbox.y1, f.bbox.x0)
    )
    for f in fields:
        writer.writerow(
            {
                "field_id": f.field_id,
                "page": f.page_index + 1,  # human-facing only; never rejoined on
                "label": f.label,
                "context": f.context,
                "acroform_name": f.acroform_name or "",
                **{col: "" for col in FILL_COLUMNS},
            }
        )
    return buf.getvalue()


def build_batch_instructions(
    fieldset: FieldSet, page_indexes: list[int], batch_num: int, total_batches: int
) -> str:
    """Short instructions text for one batch -- references the attached sheet."""
    n_fields = len(fieldset.for_pages(page_indexes))
    total_pages = len(fieldset.pages) or 1
    header = (
        f"CRF annotation batch {batch_num} of {total_batches}  "
        f"(pages {page_indexes[0] + 1}-{page_indexes[-1] + 1} of {total_pages}, "
        f"source: {Path(fieldset.source_pdf).name})"
    )
    first_page, last_page = page_indexes[0] + 1, page_indexes[-1] + 1
    page_span = f"page {first_page}" if first_page == last_page else f"pages {first_page}-{last_page}"
    framing = _FRAMING.format(page_span=page_span)
    return "\n".join(
        [
            header,
            "=" * len(header),
            "",
            framing,
            f"This batch covers {n_fields} fields. Attach or paste the accompanying "
            f"{SHEET_FILENAME.format(n=batch_num)} alongside this text.",
            "",
            _RULES,
            _FORMAT.format(n_columns=len(SHEET_COLUMNS)),
            _REMINDER,
            "",
        ]
    )


def write_batches(
    fieldset: FieldSet,
    out_dir: str | Path,
    max_fields_per_batch: int = DEFAULT_MAX_FIELDS_PER_BATCH,
) -> list[dict]:
    """Write instructions + spec sheet for every batch.

    Returns one manifest entry per batch -- ``{"batch": n, "pages": [...],
    "instructions": Path, "sheet": Path, "expected_response": Path}`` -- which
    ``scripts/ingest_response.py`` reads back to know what to look for and
    where each batch's fields came from, without re-deriving batching logic.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    batches = batch_pages(fieldset, max_fields_per_batch)

    manifest: list[dict] = []
    for i, pages in enumerate(batches, start=1):
        instructions_path = out_dir / INSTRUCTIONS_FILENAME.format(n=i)
        sheet_path = out_dir / SHEET_FILENAME.format(n=i)
        instructions_path.write_text(
            build_batch_instructions(fieldset, pages, i, len(batches)), encoding="utf-8"
        )
        sheet_path.write_text(build_spec_sheet(fieldset, pages), encoding="utf-8")
        manifest.append(
            {
                "batch": i,
                "pages": pages,
                "instructions": str(instructions_path),
                "sheet": str(sheet_path),
                "expected_response": str(out_dir / RESPONSE_FILENAME.format(n=i)),
            }
        )
    return manifest
