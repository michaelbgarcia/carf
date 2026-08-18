"""Builds the Copilot 365 batch materials: instructions + spec sheet.

Copilot is the **gap filler**, not the mapping source
-----------------------------------------------------
This used to be the front door: every field went out to a chat window and came
back as a proposal. It is now the last resort. Mined corpus precedent
pre-populates whatever the historical aCRFs already answer
(``corpus_precedent.match_precedent``), and only the rows that leaves blank come
here -- see :func:`rows_needing_annotation`.

That matters for two reasons beyond volume. Round-trip *count* was always the
bottleneck in a pipeline with no API access, and pre-population cuts it at the
source rather than by packing more rows per trip. And it takes the least
trustworthy step off the critical path: nothing in the main flow now depends on
a chat UI returning parseable text, so a batch that comes back mangled costs a
retry on a handful of rows instead of blocking the document.

Instructions and data stay separate
-----------------------------------
* **instructions** -- short, static text: framing, rules, schema. Does not grow
  with row count.
* **spec sheet** -- a CSV, one row per CRF row, with empty columns for Copilot to
  fill in. This carries the data and can span many pages in one file.

Batching
--------
Rows are grouped by page into batches under a row-count ceiling
(``max_rows_per_batch``). The ceiling is a guess, not a measured Copilot limit --
there is no way to know Copilot 365's practical attachment/context budget without
testing against a real session. Tune it once that is known.

The join key
------------
Each row carries ``row_id`` -- globally unique across the document, assigned by
``rows.py`` -- which Copilot must echo back unchanged. Unlike a page-relative
ordinal it needs no accompanying page number to be unambiguous, so a dropped or
reordered row is still identifiable and a row from the wrong batch cannot be
constructed by accident.

Anticipated failure mode
------------------------
Chat UIs have a strong habit of rendering tabular data back as a markdown table
(``| row_id | domain | ... |`` with a header-separator row) rather than literal
CSV, because that is idiomatic for a chat reply. The instructions explicitly ask
for CSV and no markdown table; ``parse_response.py`` expects the markdown-table
reply anyway, the same way the old JSON prompt asked for "no code fence" and the
parser stripped fences regardless.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Iterable, Optional

from pipeline.models import AnnotationSet, CRFRow, RowSet

INSTRUCTIONS_FILENAME = "copilot_batch{n}_instructions.txt"
SHEET_FILENAME = "copilot_batch{n}_sheet.csv"
RESPONSE_FILENAME = "copilot_batch{n}_response.csv"

DEFAULT_MAX_ROWS_PER_BATCH = 150  # unvalidated -- see module docstring

# Columns Copilot must not alter, echoed back for the human's own reference when
# they read the sheet. The parser keys on row_id alone and does not require these
# to survive the round trip.
READONLY_COLUMNS = ["row_id", "page", "form", "text_1", "text_2"]
# Columns Copilot fills in. `variable2` is the AGE / AGEU case: a second
# annotation on the same printed row. It is a *column* rather than a second row
# on purpose -- "one row in, one row out" is what makes the row_id join safe, and
# letting Copilot add rows would give that up for a case an extra column covers.
FILL_COLUMNS = [
    "kind", "domain", "variable", "variable2", "condition", "codelist",
    "origin", "confidence", "rationale",
]
SHEET_COLUMNS = READONLY_COLUMNS + FILL_COLUMNS

_FRAMING = """\
You are annotating a blank Case Report Form (CRF) so it can be submitted to
the FDA as an annotated CRF (aCRF). Annotations map each data capture point on
the form to the SDTM dataset variable that will hold its value, following
CDISC SDTMIG v3.4.

The CRF is laid out as two columns: a question column and a response column.
Attached is a spec sheet (CSV) with one row per printed line, across
{page_span} of this CRF. "text_1" is the question text and "text_2" is the
response option, unit or fixed value printed beside it. Either can be empty:
a row with no text_2 is a question with a write-in answer, and a row with no
text_1 is a further option belonging to the question above it.

For each row, fill in the empty columns using the rules below.
"""

_RULES = """\
RULES

1. DOMAIN. Assign the two-letter SDTM domain from the row's content and the
   form it sits on (DM for demographics, VS for vital signs, AE for adverse
   events, CM for concomitant medications, and so on). The "form" column names
   the CRF form and is your strongest signal.

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
   Leave "condition" empty for rows that need no such qualifier.

4. TWO VARIABLES ON ONE ROW. Where one printed line captures two SDTM
   variables -- an age and its unit, a result and its unit -- put the second
   in "variable2". Do not add a row for it.

5. OPTION ROWS. A row whose "text_1" is empty is an option under the question
   above it. Annotate it only where the option itself needs its own mapping,
   e.g. "RACE = ASIAN". Otherwise leave it blank.

6. NOT SUBMITTED. Page furniture that carries no submitted data -- page
   numbers, form version strings, banners, investigator or assessor initials,
   "page 1 of 3" footers, instruction paragraphs -- maps to no SDTM variable.
   For these set "kind" to "note", leave "domain" and "variable" empty, and
   set "origin" to NotSubmitted.

7. ORIGIN. Use the Define-XML v2.1 origin type, exactly one of: Collected,
   Derived, Assigned, Protocol, eDT, Predecessor, NotSubmitted. A value
   written on the form by a site is Collected. A value calculated from other
   fields is Derived. A constant such as a units label is Assigned.

8. CODELIST. Where the row is a controlled-terminology item (sex, race,
   ethnicity, position, units), give the CDISC codelist name or C-code in
   "codelist". Otherwise leave it empty.

9. CONFIDENCE. In the "confidence" column, score your own certainty from 0.0
   to 1.0. Use below 0.7 where the question text is ambiguous, where the
   domain is a guess, or where you had to choose between two plausible
   variables. Do not inflate these -- a human reviews every row and low scores
   are used to prioritise that review.

10. RATIONALE. In the "rationale" column, give one short sentence saying why
   you chose that domain/variable for this row. This is what a reviewer reads
   first, so make it concrete: name the question text or form that drove the
   decision, not a restatement of the mapping itself.

11. Do not add, remove, reorder, merge, or split rows. Do not alter the
   row_id, page, form, text_1, or text_2 columns -- return them exactly as
   given. One filled-in row per input row.
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


# --------------------------------------------------------------------------
# Which rows go out at all
# --------------------------------------------------------------------------


def rows_needing_annotation(
    rows: RowSet, annotations: Optional[AnnotationSet] = None
) -> RowSet:
    """The subset of rows nothing has proposed an annotation for yet.

    This is what narrows Copilot to the gaps. Pass the pre-population result
    from ``corpus_precedent.match_precedent``; rows it matched are already
    answered, and re-asking about them would spend round trips to second-guess
    corroborated historical precedent with a chat reply.

    Returns a ``RowSet`` rather than a list so batching, sheet building and page
    geometry all keep working unchanged on the narrowed set.
    """
    answered = {a.row_id for a in (annotations.annotations if annotations else []) if a.row_id}
    keep = [r for r in rows.rows if r.row_id not in answered]
    pages = [p for p in rows.pages if any(r.page_index == p.page_index for r in keep)]
    return rows.model_copy(update={"rows": keep, "pages": pages})


def batch_pages(
    rows: RowSet, max_rows_per_batch: int = DEFAULT_MAX_ROWS_PER_BATCH
) -> list[list[int]]:
    """Group page indexes into batches under a row-count ceiling.

    Greedy: accumulate whole pages until the next one would push the batch over
    the ceiling. A single page's rows are never split across batches -- a page's
    questions only make sense read together, and keeping a page whole is a
    simpler guarantee than reasoning about a split mid-page. A page that alone
    exceeds the ceiling still becomes its own (oversized) batch rather than being
    dropped or split.
    """
    counts = {p.page_index: len(rows.for_page(p.page_index)) for p in rows.pages}
    batches: list[list[int]] = []
    current: list[int] = []
    current_count = 0

    for page in sorted(counts):
        n = counts[page]
        if current and current_count + n > max_rows_per_batch:
            batches.append(current)
            current, current_count = [], 0
        current.append(page)
        current_count += n

    if current:
        batches.append(current)
    return batches


def _ordered(rows: RowSet, page_indexes: Iterable[int]) -> list[CRFRow]:
    return sorted(
        rows.for_pages(page_indexes),
        key=lambda r: (r.page_index, -r.anchor.y1, r.anchor.x0),
    )


def build_spec_sheet(rows: RowSet, page_indexes: list[int]) -> str:
    """CSV text for one batch: filled read-only columns, empty fill columns."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=SHEET_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in _ordered(rows, page_indexes):
        writer.writerow(
            {
                "row_id": row.row_id,
                "page": row.display_page,  # human-facing only; never rejoined on
                "form": row.form,
                "text_1": row.text_1,
                "text_2": row.text_2,
                **{col: "" for col in FILL_COLUMNS},
            }
        )
    return buf.getvalue()


# --------------------------------------------------------------------------
# Precedent appendix
# --------------------------------------------------------------------------

DEFAULT_MAX_PRECEDENT_EXAMPLES = 20

_PRECEDENT_HEADER = """\
HISTORICAL PRECEDENT

The following question text -> mapping pairs were recovered from prior
annotated CRFs and may help with similar rows in this batch. Treat them as
precedent, not a rule -- confirm against this row's own text before reusing
one.
"""


def _label_words(label: str) -> set[str]:
    return {w for w in re.findall(r"[A-Za-z0-9]+", label.lower()) if len(w) > 2}


def _row_count(row: dict) -> int:
    try:
        return int(row.get("count", 0) or 0)
    except (TypeError, ValueError):
        return 0


def build_precedent_appendix(
    lookup_rows: list[dict] | None,
    rows: RowSet,
    page_indexes: list[int],
    max_examples: int = DEFAULT_MAX_PRECEDENT_EXAMPLES,
) -> str:
    """Historical precedent relevant to this batch's rows, as prompt text.

    Note this is *near-miss* precedent by construction. Any row whose text
    matched the corpus exactly has already been pre-populated and filtered out by
    :func:`rows_needing_annotation`, so what reaches a prompt is precedent that
    shares a significant word with a row rather than answering it -- which is
    exactly what is useful to show a model that has to generalise.

    Returns ``""`` (no section at all) when there is nothing to show, so a caller
    can always append this without a stray empty heading.
    """
    if not lookup_rows:
        return ""

    batch_words: set[str] = set()
    for row in rows.for_pages(page_indexes):
        batch_words |= _label_words(row.text_1 or row.text_2)
    if not batch_words:
        return ""

    scored = [(len(_label_words(r.get("label", "")) & batch_words), r) for r in lookup_rows]
    scored = [(overlap, r) for overlap, r in scored if overlap]
    if not scored:
        return ""
    scored.sort(key=lambda t: (-t[0], -_row_count(t[1]), t[1].get("label", "")))

    lines = [_PRECEDENT_HEADER]
    for _overlap, row in scored[:max_examples]:
        mapping = ".".join(p for p in (row.get("domain"), row.get("variable")) if p)
        mapping = mapping or row.get("variable", "")
        if row.get("condition"):
            mapping = f"{mapping} when {row['condition']}"
        if row.get("fixed_value"):
            mapping = f"{mapping} = {row['fixed_value']}"
        lines.append(f'  "{row.get("label", "")}" -> {mapping}')
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def build_batch_instructions(
    rows: RowSet,
    page_indexes: list[int],
    batch_num: int,
    total_batches: int,
    precedent_rows: list[dict] | None = None,
) -> str:
    """Short instructions text for one batch -- references the attached sheet."""
    n_rows = len(rows.for_pages(page_indexes))
    total_pages = len(rows.pages) or 1
    header = (
        f"CRF annotation batch {batch_num} of {total_batches}  "
        f"(pages {page_indexes[0] + 1}-{page_indexes[-1] + 1} of {total_pages}, "
        f"source: {Path(rows.source_pdf).name})"
    )
    first_page, last_page = page_indexes[0] + 1, page_indexes[-1] + 1
    page_span = f"page {first_page}" if first_page == last_page else f"pages {first_page}-{last_page}"

    parts = [
        header,
        "=" * len(header),
        "",
        _FRAMING.format(page_span=page_span),
        f"This batch covers {n_rows} rows. Attach or paste the accompanying "
        f"{SHEET_FILENAME.format(n=batch_num)} alongside this text.",
        "",
        _RULES,
    ]
    appendix = build_precedent_appendix(precedent_rows, rows, page_indexes)
    if appendix:
        parts.append(appendix)
    parts += [
        _FORMAT.format(n_columns=len(SHEET_COLUMNS)),
        _REMINDER,
        "",
    ]
    return "\n".join(parts)


def write_batches(
    rows: RowSet,
    out_dir: str | Path,
    max_rows_per_batch: int = DEFAULT_MAX_ROWS_PER_BATCH,
    precedent_rows: list[dict] | None = None,
) -> list[dict]:
    """Write instructions + spec sheet for every batch.

    Returns one manifest entry per batch -- ``{"batch": n, "pages": [...],
    "rows": n, "instructions": Path, "sheet": Path, "expected_response": Path}``
    -- which ``scripts/ingest_response.py`` reads back to know what to look for
    and where each batch's rows came from, without re-deriving batching logic.

    Pass a narrowed ``rows`` (from :func:`rows_needing_annotation`) to ask only
    about the gaps. An empty ``RowSet`` writes nothing and returns ``[]``, which
    is the good outcome: precedent covered the document.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    batches = batch_pages(rows, max_rows_per_batch)

    manifest: list[dict] = []
    for i, pages in enumerate(batches, start=1):
        instructions_path = out_dir / INSTRUCTIONS_FILENAME.format(n=i)
        sheet_path = out_dir / SHEET_FILENAME.format(n=i)
        instructions_path.write_text(
            build_batch_instructions(rows, pages, i, len(batches), precedent_rows),
            encoding="utf-8",
        )
        sheet_path.write_text(build_spec_sheet(rows, pages), encoding="utf-8")
        manifest.append(
            {
                "batch": i,
                "pages": pages,
                "rows": len(rows.for_pages(pages)),
                "instructions": str(instructions_path),
                "sheet": str(sheet_path),
                "expected_response": str(out_dir / RESPONSE_FILENAME.format(n=i)),
            }
        )
    return manifest
