"""Two-column row extraction from a blank CRF PDF, via PyMuPDF.

The assumption
--------------
**A CRF page is two text columns.** The left column carries the question
prompt, the right column carries the response option, unit or fixed value::

    Sex                                                        Male  O
                                                             Female  O
    Age (years) at time of consent            Fixed Unit: years      ____
    <------------ column 1 ------------>|<------ column 2 ------>
                                     gutter

That single assumption replaces three separate piles of heuristics the earlier
design needed:

1. **Caption association.** Column position *is* the semantics, so there is no
   searching left, then above, then right with a distance budget per field
   type, and no special case for a grid checkbox whose header sits five rows up.
2. **Field detection.** Widgets are never located at all -- not AcroForm
   widgets, not drawn boxes, not near-square checkboxes, and not fill-in-blank
   underlines. Which means the discrimination that was hardest to get right,
   telling a fill-in blank from a section rule, simply does not arise.
3. **Annotation placement.** The gutter is a known empty corridor on every
   row's own baseline, which is where a human annotator writes. See
   ``pipeline/layout.py``.

It also fails better. A field detector silently omits what it fails to detect,
so a missed field is invisible. Every printed line becomes a row here, so a row
nobody has mapped shows up as an unmapped row.

What is still a heuristic
-------------------------
One thing: **where the gutter is** -- ``detect_gutter``, by interval sweep with
a documented fallback. A single-column page returns ``None``, which is a valid
answer and not an error.

Notably *not* a heuristic here: whether two consecutive lines are one wrapped
question or two separate items. That is delegated to PyMuPDF's own text-block
analysis (``TextRun.blocks``) rather than answered with a leading threshold,
because no threshold works. Measured on the synthetic fixture, the vertical gap
between the two lines of one wrapped question is **-2.4pt** -- the line boxes
overlap -- while two separate options in a list sit **1.6pt** apart and a
banner and the line beneath it **2.9pt**. The case that must merge is *tighter*
than the cases that must not, so any cutoff gets it backwards.

The bias is deliberate: this merges only when PyMuPDF says one paragraph, and
otherwise leaves lines separate. Failing to merge a wrapped question is
recoverable -- a reviewer annotates the first half and leaves the second blank.
Wrongly merging four race options into one row destroys the ability to annotate
them separately, and no downstream step can undo it.

Coordinates
-----------
Everything internal is in fitz space (top-left origin, y down) because that is
what ``pipeline.text`` returns. Conversion to PDF user space happens once, at
``CRFRow`` construction, via ``pipeline.geometry.fitz_rect_to_bbox``. No
``pymupdf.Rect`` escapes this module. Note that ``gutter_x`` needs no
conversion: the y-flip leaves x alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from statistics import median
from typing import Iterable, Optional, Pattern

import pymupdf

from pipeline.geometry import fitz_rect_to_bbox
from pipeline.models import CRFRow, PageGeometry, RowSet
from pipeline.text import TextRun, line_bands, lines_of, text_runs

# --- Gutter detection -----------------------------------------------------
#
# A run wider than this fraction of the page is a header, footer or spanning
# note. Those cross the gutter legitimately, so counting them would erase the
# corridor they cross. Excluded from detection, kept as rows.
FULL_WIDTH_FRACTION = 0.6

# Where in the text span to look. Below the lower bound is question text; above
# the upper bound is the whitespace to the right of the response column, which
# would otherwise win on width alone.
GUTTER_SEARCH_BAND = (0.30, 0.90)

# A corridor narrower than this is a word gap inside one column, not a column
# break.
MIN_GUTTER_WIDTH = 8.0

# Both columns must actually contain text. Without this a single-column page
# reports a spurious gutter in its right-hand whitespace.
MIN_COLUMN_RUNS = 2

# Right-aligned-cluster fallback: column 2 shares a right edge.
RIGHT_CLUSTER_TOL = 4.0
MIN_RIGHT_CLUSTER = 3
RIGHT_CLUSTER_BAND = 0.55  # cluster's x1 must sit past this fraction of the span
RIGHT_CLUSTER_MARGIN = 2.0

# --- Row assembly ---------------------------------------------------------
#
#: Multiple runs inside one column are joined with a plain space. Never '|':
#: this text travels as a CSV cell and, on the markdown-table reply path, as a
#: table cell, where a bare '|' silently shifts every column after it.
COLUMN_JOIN = " "

#: The form name in a page header. An EDC print artifact, hence overridable.
FORM_HEADER_RE = re.compile(r"^\s*Form\s*:\s*(?P<name>.+?)\s*$", re.MULTILINE)


# --------------------------------------------------------------------------
# Gutter detection
# --------------------------------------------------------------------------


def _free_intervals(
    rects: Iterable[pymupdf.Rect], lo: float, hi: float
) -> list[tuple[float, float]]:
    """x intervals within ``[lo, hi]`` that no rect straddles.

    An exact interval sweep rather than sampling a projection profile at some
    step size -- same result, no sampling artifacts, and a narrow real gutter
    cannot fall between two samples.
    """
    covered = sorted(
        (max(lo, r.x0), min(hi, r.x1)) for r in rects if r.x1 > lo and r.x0 < hi
    )
    free: list[tuple[float, float]] = []
    cursor = lo
    for a, b in covered:
        if a > cursor:
            free.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < hi:
        free.append((cursor, hi))
    return free


def _right_edge_cluster(runs: list[TextRun], text_x0: float, span: float) -> Optional[float]:
    """Gutter from a right-aligned response column, when no corridor is clean.

    Column 2 is right-aligned against a shared right edge. On a dense page --
    a question long enough to reach past where options start on some *other*
    line -- there is no x that nothing straddles, but that shared right edge
    is still there. Cluster on it, then take the left-most member's left edge.
    """
    threshold = text_x0 + RIGHT_CLUSTER_BAND * span
    candidates = sorted((r for r in runs if r.rect.x1 >= threshold), key=lambda r: r.rect.x1)
    if not candidates:
        return None

    clusters: list[list[TextRun]] = []
    for run in candidates:
        if clusters and run.rect.x1 - clusters[-1][-1].rect.x1 <= RIGHT_CLUSTER_TOL:
            clusters[-1].append(run)
        else:
            clusters.append([run])

    best = max(clusters, key=len)
    if len(best) < MIN_RIGHT_CLUSTER:
        return None
    return min(r.rect.x0 for r in best) - RIGHT_CLUSTER_MARGIN


def _narrow(runs: list[TextRun], page_width: float) -> list[TextRun]:
    return [r for r in runs if r.rect.width <= FULL_WIDTH_FRACTION * page_width]


def splits_into_columns(runs: list[TextRun], gutter: float, page_width: float) -> bool:
    """Would ``gutter`` actually divide this page's text into two columns?

    The test that keeps the document-median fallback honest. That fallback
    exists for a page dense enough that its *own* detection found no corridor;
    applied blindly it would also fire on a genuinely single-column page -- a
    title or instruction page -- and shear its one column in half at whatever x
    the rest of the document happens to use. Requiring real text on both sides
    tells the two cases apart.
    """
    right = sum(1 for r in _narrow(runs, page_width) if r.rect.x0 >= gutter)
    left = sum(1 for r in _narrow(runs, page_width) if r.rect.x1 <= gutter)
    return left >= MIN_COLUMN_RUNS and right >= MIN_COLUMN_RUNS


def detect_gutter(runs: list[TextRun], page_width: float) -> Optional[float]:
    """The x separating question column from response column, or ``None``.

    ``None`` means single-column -- a title or instruction page -- and is a
    valid answer that callers must handle rather than an error.
    """
    narrow = _narrow(runs, page_width)
    if len(narrow) < 2 * MIN_COLUMN_RUNS:
        return None

    text_x0 = min(r.rect.x0 for r in narrow)
    text_x1 = max(r.rect.x1 for r in narrow)
    span = text_x1 - text_x0
    if span <= MIN_GUTTER_WIDTH:
        return None

    lo = text_x0 + GUTTER_SEARCH_BAND[0] * span
    hi = text_x0 + GUTTER_SEARCH_BAND[1] * span
    rects = [r.rect for r in narrow]

    # Widest corridor first: on a well-behaved page the column break is the
    # largest run of x that nothing crosses.
    for a, b in sorted(_free_intervals(rects, lo, hi), key=lambda t: -(t[1] - t[0])):
        if b - a < MIN_GUTTER_WIDTH:
            continue
        g = (a + b) / 2.0
        # Every narrow run is wholly one side or the other, by construction.
        left = sum(1 for r in rects if r.x1 <= g)
        right = len(rects) - left
        if left >= MIN_COLUMN_RUNS and right >= MIN_COLUMN_RUNS:
            return g

    g = _right_edge_cluster(narrow, text_x0, span)
    if g is not None and lo <= g <= hi:
        return g
    return None


# --------------------------------------------------------------------------
# Row assembly
# --------------------------------------------------------------------------


@dataclass
class _Record:
    """One assembled line, still in fitz coordinates."""

    rect_1: Optional[pymupdf.Rect] = None
    rect_2: Optional[pymupdf.Rect] = None
    parts_1: list[str] = dc_field(default_factory=list)
    parts_2: list[str] = dc_field(default_factory=list)
    blocks_1: frozenset[int] = frozenset()
    band: Optional[pymupdf.Rect] = None
    full_width: bool = False

    @property
    def text_1(self) -> str:
        return COLUMN_JOIN.join(self.parts_1)

    @property
    def text_2(self) -> str:
        return COLUMN_JOIN.join(self.parts_2)


def _assemble_line(line: list[TextRun], gutter: Optional[float]) -> Optional[_Record]:
    """Split one visual line into its two column halves.

    A run straddling the gutter -- a header, footer or spanning note -- is
    treated as column 1 and flags the record ``full_width``, because a spanning
    note is frequently the thing that needs annotating and dropping it would
    lose it.
    """
    rec = _Record()
    for run in line:
        text = " ".join(run.text.split())
        if not text:
            continue
        straddles = gutter is not None and run.rect.x0 < gutter < run.rect.x1
        in_col_2 = gutter is not None and not straddles and run.rect.x0 >= gutter
        if in_col_2:
            rec.parts_2.append(text)
            rec.rect_2 = pymupdf.Rect(run.rect) if rec.rect_2 is None else (rec.rect_2 | run.rect)
        else:
            rec.parts_1.append(text)
            rec.rect_1 = pymupdf.Rect(run.rect) if rec.rect_1 is None else (rec.rect_1 | run.rect)
            rec.blocks_1 = rec.blocks_1 | run.blocks
            rec.full_width = rec.full_width or straddles
    if rec.rect_1 is None and rec.rect_2 is None:
        return None
    return rec


def _merge_wrapped(records: list[_Record]) -> list[_Record]:
    """Fold a question's continuation lines back into one record.

    ``If Yes, please provide the original`` / ``participant number
    (xxxxx-xxxx)`` is one question printed on two lines and must become one row
    -- two rows would ask a reviewer to annotate half a sentence.

    The test is **shared PyMuPDF text block**, not geometry. Two consecutive
    lines merge when they are both column-1-only (a column-2 partner means the
    line stands on its own as a question-and-response pair) and their column-1
    runs came from the same block. See the module docstring for the measurements
    showing why a leading threshold cannot make this call.
    """
    out: list[_Record] = []
    for rec in records:
        prev = out[-1] if out else None
        if (
            prev is not None
            and prev.rect_1 is not None
            and rec.rect_1 is not None
            and prev.rect_2 is None
            and rec.rect_2 is None
            and prev.full_width == rec.full_width
            and prev.blocks_1 & rec.blocks_1
        ):
            prev.rect_1 |= rec.rect_1
            prev.parts_1.extend(rec.parts_1)
            prev.blocks_1 = prev.blocks_1 | rec.blocks_1
            # A wrapped question owns every rule it is written across, so the
            # merged row's band is the union. Leaving the first line's band
            # would strand the continuation line's territory between two rows,
            # belonging to neither.
            if rec.band is not None:
                prev.band = rec.band if prev.band is None else (prev.band | rec.band)
            continue
        out.append(rec)
    return out


# --------------------------------------------------------------------------
# Annotation masking
# --------------------------------------------------------------------------


def annotation_rects(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Rects of this page's text-carrying annotations -- what is *not* form text.

    Needed because ``get_text`` does not distinguish the two: on PyMuPDF 1.28.2 a
    page's annotation text comes back mixed into its printed words (measured --
    see ``text.py``'s docstring). Without subtracting these, reading a finished
    aCRF back produces labels like ``Year of Birth (yyyy) BRTHDTC``: the question
    with its own answer appended, which then becomes the key the whole corpus
    lookup table is built on.

    Filtered on **non-empty ``/Contents``** rather than on subtype. That is the
    property that matters -- an annotation with no text cannot have contributed
    any -- and it is also what keeps the purely graphical markup this codebase
    draws from masking anything: ``render.draw_group_bracket``'s polyline spans
    a whole block of rows vertically, and excluding its rect on the strength of
    its subtype would delete real question text along a 3pt-wide column.

    ``page.annots()`` does not return form widgets, so a CRF's own capture fields
    are unaffected either way.
    """
    return [a.rect for a in page.annots() if (a.info.get("content") or "").strip()]


# --------------------------------------------------------------------------
# Form header
# --------------------------------------------------------------------------


def form_name(runs: list[TextRun], pattern: Pattern[str] = FORM_HEADER_RE) -> Optional[str]:
    """The form name from a page header, or ``None`` if the page has none.

    Matched against each run's text rather than the whole page, so a header
    drawn as several separate runs still resolves. ``None`` is the signal for
    ``extract_rows`` to carry the previous page's form forward -- a form spans
    pages, and the header is normally printed only on the first of them.
    """
    for run in runs:
        m = pattern.search(run.text)
        if m:
            name = m.group("name") if "name" in (m.groupdict() or {}) else m.group(0)
            cleaned = " ".join(name.split())
            if cleaned:
                return cleaned
    return None


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


@dataclass
class _PageScan:
    page_index: int
    rect: pymupdf.Rect
    rotation: int
    runs: list[TextRun]
    own_gutter: Optional[float]
    own_form: Optional[str]
    masked: int = 0  # text-carrying annotations subtracted from this page

    @property
    def width(self) -> float:
        return self.rect.width

    @property
    def height(self) -> float:
        return self.rect.height


def extract_rows(
    pdf_path: str | Path,
    form_header_pattern: Pattern[str] = FORM_HEADER_RE,
    *,
    mask_annotations: bool = True,
) -> RowSet:
    """Read a blank CRF into two-column rows.

    ``mask_annotations`` subtracts the page's own annotation text before any
    grouping happens (:func:`annotation_rects`), so a row's text is the *form's*
    text and never a mark drawn on top of it. It defaults on, and on a genuinely
    blank CRF it changes nothing -- there are no annotations to subtract. It
    earns its keep on the reverse direction, where ``parse_annotated_pdf`` reads
    rows off a finished aCRF because no blank counterpart was supplied. Pass
    ``False`` only to see what the unfiltered extraction would have produced.

    The one case it cannot help with is a **flattened** aCRF, where the marks
    were baked into page content and no annotation objects survive to locate.
    That degrades safely rather than silently, though: ``read_marks`` finds
    nothing on such a file either, so the result is "no mappings recovered"
    rather than "mappings recovered against contaminated labels".

    Two passes over the pages, because the second needs a document-wide fact
    the first produces. A page whose own gutter detection fails -- dense enough
    to have no clean corridor and no usable right-edge cluster -- borrows the
    document median. CRFs are templated, so a page's siblings are good evidence
    about where its own gutter is.

    The borrowed gutter is applied only when it actually splits *that* page into
    two populated columns (:func:`splits_into_columns`). Otherwise a genuinely
    single-column title or instruction page would be sheared in half at
    whatever x the rest of the document uses, turning a page with no gutter into
    a page with a wrong one -- and ``gutter_x=None`` is a valid answer.
    """
    doc = pymupdf.open(pdf_path)
    try:
        scans: list[_PageScan] = []
        for i, page in enumerate(doc):
            exclude = annotation_rects(page) if mask_annotations else []
            runs = text_runs(page, exclude=exclude)
            scans.append(
                _PageScan(
                    page_index=i,
                    rect=pymupdf.Rect(page.rect),
                    rotation=page.rotation,
                    runs=runs,
                    own_gutter=detect_gutter(runs, page.rect.width),
                    own_form=form_name(runs, form_header_pattern),
                    masked=len(exclude),
                )
            )

        found = [s.own_gutter for s in scans if s.own_gutter is not None]
        fallback = median(found) if found else None

        pages: list[PageGeometry] = []
        rows: list[CRFRow] = []
        carried_form = ""
        for scan in scans:
            gutter = scan.own_gutter
            if (
                gutter is None
                and fallback is not None
                and splits_into_columns(scan.runs, fallback, scan.width)
            ):
                gutter = fallback
            if scan.own_form is not None:
                carried_form = scan.own_form

            pages.append(
                PageGeometry(
                    page_index=scan.page_index,
                    width=scan.width,
                    height=scan.height,
                    rotation=scan.rotation,
                    gutter_x=gutter,
                    masked_annotations=scan.masked,
                )
            )

            lines = lines_of(scan.runs)
            records = []
            for line, band in zip(lines, line_bands(lines, scan.rect)):
                rec = _assemble_line(line, gutter)
                if rec is None:
                    continue
                rec.band = band
                records.append(rec)
            for n, rec in enumerate(_merge_wrapped(records), start=1):
                rows.append(
                    CRFRow(
                        row_id=f"p{scan.page_index + 1}_r{n:03d}",
                        page_index=scan.page_index,
                        form=carried_form,
                        text_1=rec.text_1,
                        text_2=rec.text_2,
                        bbox_1=(
                            fitz_rect_to_bbox(rec.rect_1, scan.height)
                            if rec.rect_1 is not None
                            else None
                        ),
                        bbox_2=(
                            fitz_rect_to_bbox(rec.rect_2, scan.height)
                            if rec.rect_2 is not None
                            else None
                        ),
                        band=(
                            fitz_rect_to_bbox(rec.band, scan.height)
                            if rec.band is not None
                            else None
                        ),
                        full_width=rec.full_width,
                    )
                )

        return RowSet(source_pdf=str(pdf_path), pages=pages, rows=rows)
    finally:
        doc.close()


__all__ = [
    "COLUMN_JOIN",
    "FORM_HEADER_RE",
    "FULL_WIDTH_FRACTION",
    "GUTTER_SEARCH_BAND",
    "MIN_COLUMN_RUNS",
    "MIN_GUTTER_WIDTH",
    "annotation_rects",
    "detect_gutter",
    "extract_rows",
    "form_name",
    "splits_into_columns",
]
