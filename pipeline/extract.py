"""Field detection from a blank CRF PDF, via PyMuPDF.

Task order step 3.

Two detection paths, tried in that order per page:

1. **AcroForm** (``page.widgets()``) -- if the export still carries form
   fields, each widget is a capture field and ``field_name`` gives a
   meaningful ``acroform_name``. This is the good case.
2. **Text/line fallback** (``get_text`` + ``get_drawings()``) -- when the
   export is flattened. Fields show up as boxes, as small near-square
   checkboxes, or as fill-in-the-blank underlines.

Every ``CRFField`` leaves here with its ``bbox`` already in PDF user space:
the fitz rect is converted at the moment of construction via
``pipeline.geometry.fitz_rect_to_bbox`` and never escapes this module.

Label association
-----------------
Which text belongs to a field depends on what kind of field it is, and real
CRFs use at least three geometries:

* text fields are captioned from the **left** ("Subject Identifier: ____")
* checkboxes are captioned from the **right** ("[ ] Male")
* grid columns are captioned from **above** ("Not Done" as a column header)

So each field type gets an ordered list of directions to try. A checkbox in a
grid ("Not Done") has nothing to its right and falls through to the header
above it, which is why the fallbacks are ordered rather than exclusive.

Distinguishing a fill-in blank from a separator rule
----------------------------------------------------
Both are horizontal lines. Two discriminators, needed together:

* a separator spans the text column, so anything wider than
  ``RULE_WIDTH_FRACTION`` of the page is a rule, not a field
* a fill-in blank has a caption; a separator generally does not

The synthetic fixture deliberately contains section and footer rules for
exactly this reason -- without them a detector that calls every horizontal
line a field would pass and prove nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Iterable, Optional

import pymupdf

from pipeline.geometry import fitz_rect_to_bbox
from pipeline.models import CRFField, FieldSet, FieldSource, PageGeometry

# Shape classification
CHECKBOX_SIDE = (5.0, 24.0)
CHECKBOX_ASPECT = (0.6, 1.7)
FIELD_MIN_WIDTH = 25.0
FIELD_HEIGHT = (8.0, 40.0)
LINE_FLATNESS = 1.5  # max |y0-y1| for a line to count as horizontal
RULE_WIDTH_FRACTION = 0.6  # wider than this share of the page => separator

# Label association: how far to look, in points
LABEL_MAX_LEFT = 260.0
LABEL_MAX_RIGHT = 120.0
LABEL_MAX_ABOVE = 200.0  # generous: grid column headers sit far above row 5
MIN_V_OVERLAP = 0.4
MIN_H_OVERLAP = 0.3

# An underline gives no top edge, so the field box above it is inferred.
INFERRED_FIELD_HEIGHT = 16.0
INFERRED_HEIGHT_RANGE = (10.0, 24.0)
LABEL_HEIGHT_TO_FIELD = 1.5

HEADING_RATIO = 1.35  # text this much taller than the page median is a heading


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TextRun:
    """A horizontally contiguous piece of text -- roughly, one caption."""

    rect: pymupdf.Rect
    text: str


def _v_overlap(a: pymupdf.Rect, b: pymupdf.Rect) -> float:
    inter = max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))
    return inter / max(1e-6, min(a.height, b.height))


def _h_overlap(a: pymupdf.Rect, b: pymupdf.Rect) -> float:
    inter = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    return inter / max(1e-6, min(a.width, b.width))


def clean_label(text: str) -> str:
    return " ".join(text.split()).rstrip(":").strip()


def text_runs(page: pymupdf.Page) -> list[TextRun]:
    """Group words into lines, then split lines at wide horizontal gaps.

    Grouping is by vertical overlap rather than by PyMuPDF's block/line
    indices, because separately-drawn captions on the same visual line land in
    different blocks. Splitting at gaps is what keeps "Sex:", "Male" and
    "Female" three captions instead of one.
    """
    words = [
        (pymupdf.Rect(w[0], w[1], w[2], w[3]), w[4])
        for w in page.get_text("words")
        if w[4].strip()
    ]
    if not words:
        return []

    lines: list[tuple[pymupdf.Rect, list]] = []
    for rect, text in sorted(words, key=lambda t: (t[0].y0, t[0].x0)):
        for i, (line_rect, items) in enumerate(lines):
            if _v_overlap(line_rect, rect) >= 0.5:
                items.append((rect, text))
                lines[i] = (line_rect | rect, items)
                break
        else:
            lines.append((pymupdf.Rect(rect), [(rect, text)]))

    runs: list[TextRun] = []
    for line_rect, items in lines:
        items.sort(key=lambda t: t[0].x0)
        gap_limit = max(3.0, 0.6 * line_rect.height)
        cur: Optional[pymupdf.Rect] = None
        buf: list[str] = []
        for rect, text in items:
            if cur is not None and rect.x0 - cur.x1 > gap_limit:
                runs.append(TextRun(cur, " ".join(buf)))
                cur, buf = None, []
            cur = pymupdf.Rect(rect) if cur is None else (cur | rect)
            buf.append(text)
        if cur is not None:
            runs.append(TextRun(cur, " ".join(buf)))
    return runs


def _headings(runs: list[TextRun]) -> list[TextRun]:
    if not runs:
        return []
    med = median(r.rect.height for r in runs)
    return [r for r in runs if r.rect.height > HEADING_RATIO * med]


# --------------------------------------------------------------------------
# Label association
# --------------------------------------------------------------------------


def _nearest_left(runs: Iterable[TextRun], rect: pymupdf.Rect) -> Optional[TextRun]:
    best: Optional[tuple[float, TextRun]] = None
    for r in runs:
        if _v_overlap(r.rect, rect) < MIN_V_OVERLAP or r.rect.x1 > rect.x0 + 1:
            continue
        d = rect.x0 - r.rect.x1
        if d <= LABEL_MAX_LEFT and (best is None or d < best[0]):
            best = (d, r)
    return best[1] if best else None


def _nearest_right(runs: Iterable[TextRun], rect: pymupdf.Rect) -> Optional[TextRun]:
    best: Optional[tuple[float, TextRun]] = None
    for r in runs:
        if _v_overlap(r.rect, rect) < MIN_V_OVERLAP or r.rect.x0 < rect.x1 - 1:
            continue
        d = r.rect.x0 - rect.x1
        if d <= LABEL_MAX_RIGHT and (best is None or d < best[0]):
            best = (d, r)
    return best[1] if best else None


def _nearest_above(runs: Iterable[TextRun], rect: pymupdf.Rect) -> Optional[TextRun]:
    best: Optional[tuple[float, TextRun]] = None
    for r in runs:
        if _h_overlap(r.rect, rect) < MIN_H_OVERLAP or r.rect.y1 > rect.y0 + 1:
            continue
        d = rect.y0 - r.rect.y1
        if d <= LABEL_MAX_ABOVE and (best is None or d < best[0]):
            best = (d, r)
    return best[1] if best else None


def associate_label(
    runs: list[TextRun], rect: pymupdf.Rect, is_checkbox: bool
) -> Optional[TextRun]:
    """Caption for a field, trying directions in the order its type implies."""
    order = (
        (_nearest_right, _nearest_above, _nearest_left)
        if is_checkbox
        else (_nearest_left, _nearest_above, _nearest_right)
    )
    for finder in order:
        found = finder(runs, rect)
        if found is not None:
            return found
    return None


def _section_heading(headings: Iterable[TextRun], rect: pymupdf.Rect) -> Optional[TextRun]:
    """Nearest heading above the field, at any horizontal position.

    Unlike caption lookup, this deliberately ignores horizontal overlap: a
    section heading governs everything below it across the full width of the
    page, and is usually left-aligned while its fields are indented.
    """
    best: Optional[tuple[float, TextRun]] = None
    for h in headings:
        if h.rect.y1 > rect.y0 + 1:
            continue
        d = rect.y0 - h.rect.y1
        if best is None or d < best[0]:
            best = (d, h)
    return best[1] if best else None


def build_context(
    runs: list[TextRun], headings: list[TextRun], rect: pymupdf.Rect
) -> str:
    """Surrounding text, for Copilot to disambiguate with.

    Copilot never sees coordinates, so this is the only way it can tell
    "Result" in the Systolic row from "Result" in the Pulse row.

    Deliberately avoids '|' as an internal separator (uses '/' instead) even
    though this string was originally embedded in prose. It now also travels
    as a CSV cell and, on the markdown-table fallback path, as a table cell --
    and an unescaped '|' inside a markdown-table cell silently shifts every
    column after it, which is a real, previously-hit bug: a context string
    like "line: A | B | C" sheared kind/origin/confidence sideways by one to
    three columns with no error, just wrong values landing in the wrong
    fields. ';' is still used between the section/line/above parts and is
    unambiguous for that purpose since none of the individual parts contain
    it.
    """
    parts: list[str] = []
    heading = _section_heading(headings, rect)
    if heading is not None:
        parts.append(f"section: {clean_label(heading.text)}")

    on_line = sorted(
        (r for r in runs if _v_overlap(r.rect, rect) >= MIN_V_OVERLAP),
        key=lambda r: r.rect.x0,
    )
    if on_line:
        parts.append("line: " + " / ".join(clean_label(r.text) for r in on_line))

    above = _nearest_above(runs, rect)
    if above is not None and above not in on_line:
        parts.append(f"above: {clean_label(above.text)}")
    return "; ".join(parts)


# --------------------------------------------------------------------------
# Page geometry
# --------------------------------------------------------------------------


def page_geometry(doc: pymupdf.Document) -> list[PageGeometry]:
    """Collect width/height/rotation per page, for later coordinate flips."""
    return [
        PageGeometry(
            page_index=i,
            width=page.rect.width,
            height=page.rect.height,
            rotation=page.rotation,
        )
        for i, page in enumerate(doc)
    ]


# --------------------------------------------------------------------------
# Path 1: AcroForm
# --------------------------------------------------------------------------

_CHECKBOX_WIDGETS = {
    pymupdf.PDF_WIDGET_TYPE_CHECKBOX,
    pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON,
}


def extract_acroform_fields(page: pymupdf.Page, page_index: int) -> list[CRFField]:
    """Detect capture fields from AcroForm widgets. Empty list if none."""
    widgets = list(page.widgets())
    if not widgets:
        return []

    runs = text_runs(page)
    headings = _headings(runs)
    height = page.rect.height
    out: list[CRFField] = []

    for w in widgets:
        rect = pymupdf.Rect(w.rect)
        is_checkbox = w.field_type in _CHECKBOX_WIDGETS
        label = associate_label(runs, rect, is_checkbox)
        out.append(
            CRFField(
                field_id=w.field_name or f"p{page_index + 1}_widget{len(out) + 1:03d}",
                page_index=page_index,
                bbox=fitz_rect_to_bbox(rect, height),
                label=clean_label(label.text) if label else "",
                source=FieldSource.ACROFORM,
                context=build_context(runs, headings, rect),
                acroform_name=w.field_name,
            )
        )
    return out


# --------------------------------------------------------------------------
# Path 2: text / line fallback
# --------------------------------------------------------------------------


@dataclass
class _Shape:
    rect: pymupdf.Rect
    is_checkbox: bool
    inferred: bool  # rect reconstructed from an underline, so top edge is a guess


def _dedupe(rects: list[pymupdf.Rect], tol: float = 1.0) -> list[pymupdf.Rect]:
    kept: list[pymupdf.Rect] = []
    for r in rects:
        if not any(
            max(abs(a - b) for a, b in zip(tuple(r), tuple(k))) <= tol for k in kept
        ):
            kept.append(r)
    return kept


def _shapes(page: pymupdf.Page) -> list[_Shape]:
    """Classify drawn geometry into checkboxes, field boxes and fill-in blanks."""
    rects: list[pymupdf.Rect] = []
    lines: list[tuple[pymupdf.Point, pymupdf.Point]] = []
    for d in page.get_drawings():
        for item in d["items"]:
            if item[0] == "re":
                rects.append(pymupdf.Rect(item[1]))
            elif item[0] == "l":
                lines.append((item[1], item[2]))

    out: list[_Shape] = []
    lo, hi = CHECKBOX_SIDE
    a_lo, a_hi = CHECKBOX_ASPECT
    h_lo, h_hi = FIELD_HEIGHT

    for r in _dedupe(rects):
        w, h = r.width, r.height
        if lo <= w <= hi and lo <= h <= hi and a_lo <= (w / max(h, 1e-6)) <= a_hi:
            out.append(_Shape(r, is_checkbox=True, inferred=False))
        elif w >= FIELD_MIN_WIDTH and h_lo <= h <= h_hi:
            out.append(_Shape(r, is_checkbox=False, inferred=False))

    max_blank_width = RULE_WIDTH_FRACTION * page.rect.width
    for p1, p2 in lines:
        if abs(p1.y - p2.y) > LINE_FLATNESS:
            continue
        x0, x1 = sorted((p1.x, p2.x))
        width = x1 - x0
        # A separator rule spans the text column; a fill-in blank does not.
        if width < FIELD_MIN_WIDTH or width > max_blank_width:
            continue
        y = (p1.y + p2.y) / 2.0
        out.append(
            _Shape(
                pymupdf.Rect(x0, y - INFERRED_FIELD_HEIGHT, x1, y),
                is_checkbox=False,
                inferred=True,
            )
        )
    return out


def extract_layout_fields(page: pymupdf.Page, page_index: int) -> list[CRFField]:
    """Fallback detection from text words and drawn lines/rects."""
    runs = text_runs(page)
    headings = _headings(runs)
    height = page.rect.height

    out: list[CRFField] = []
    shapes = sorted(_shapes(page), key=lambda s: (s.rect.y0, s.rect.x0))
    for shape in shapes:
        label = associate_label(runs, shape.rect, shape.is_checkbox)
        # An uncaptioned line is a rule, not a blank -- the width test above
        # catches full-width separators, this catches the rest.
        if shape.inferred and label is None:
            continue
        rect = shape.rect
        if shape.inferred and label is not None:
            inferred_h = min(
                max(label.rect.height * LABEL_HEIGHT_TO_FIELD, INFERRED_HEIGHT_RANGE[0]),
                INFERRED_HEIGHT_RANGE[1],
            )
            rect = pymupdf.Rect(rect.x0, rect.y1 - inferred_h, rect.x1, rect.y1)
        out.append(
            CRFField(
                field_id=f"p{page_index + 1}_{len(out) + 1:03d}",
                page_index=page_index,
                bbox=fitz_rect_to_bbox(rect, height),
                label=clean_label(label.text) if label else "",
                source=FieldSource.TEXT_LAYOUT,
                context=build_context(runs, headings, rect),
                acroform_name=None,
            )
        )
    return out


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def extract_fields(pdf_path: str | Path) -> FieldSet:
    """Open the blank CRF and return every detected field, page by page.

    AcroForm first, falling back to layout detection on any page that yields
    no widgets -- a CRF can be mixed.
    """
    doc = pymupdf.open(pdf_path)
    try:
        pages = page_geometry(doc)
        fields: list[CRFField] = []
        seen: set[str] = set()
        for i, page in enumerate(doc):
            found = extract_acroform_fields(page, i) or extract_layout_fields(page, i)
            for f in found:
                # AcroForm names are only unique per document by convention.
                if f.field_id in seen:
                    f = f.model_copy(update={"field_id": f"{f.field_id}#p{i + 1}"})
                seen.add(f.field_id)
                fields.append(f)
        return FieldSet(source_pdf=str(pdf_path), pages=pages, fields=fields)
    finally:
        doc.close()
