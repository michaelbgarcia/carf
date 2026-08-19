"""Printed text on a CRF page, grouped into visual lines and runs.

This is the surviving half of the old ``extract.py``: word -> line grouping and
wide-gap splitting, with nothing in it that knows about form widgets, drawn
boxes or fill-in-blank underlines. Those were the parts that guessed, and they
are gone -- see ``pipeline/rows.py`` for what replaced them.

Everything here works in **fitz coordinates** (top-left origin, y increasing
downward), because it is reading PyMuPDF output. Conversion to PDF user space
happens in ``rows.py`` at the moment a ``BBox`` is constructed, via
``pipeline.geometry``. No ``pymupdf.Rect`` should ever escape past that point.

Runs and blocks do different jobs
---------------------------------
**Horizontal** grouping is geometric and ours. PyMuPDF's blocks are no use for
it: two captions drawn separately on the same visual line routinely land in
different blocks, and conversely a block's "line" happily spans both columns
("Sex" and "Male" arrive as one line of one block). Grouping by vertical
overlap and then splitting at wide horizontal gaps is what keeps them two runs
-- and the gap between them *is* the gutter, so this is the basis of the whole
two-column model.

**Vertical** grouping -- which lines form one wrapped paragraph -- is
PyMuPDF's, via ``block``. That is real layout analysis and it is better than
any threshold we could write: measured on the synthetic fixture, the vertical
gap between the two lines of one wrapped question (-2.4pt, the line boxes
actually overlap) is *tighter* than the gap between two separate options in a
list (1.6pt), which is in turn tighter than the gap between a banner and the
line under it (2.9pt). No cutoff separates those three, so ``rows.py`` asks
PyMuPDF instead of guessing. Hence ``block`` on every run.

Lines are also *bands*, not just boxes
--------------------------------------
A line's glyph rect is only as tall as its own ink, which leaves the whitespace
between two lines belonging to neither. :func:`line_bands` divides the page into
one full-width horizontal band per line -- the ruled line of a notebook page --
so that every y on the page belongs to exactly one line and the boundary between
two questions is an explicit number rather than a matter of which glyph rect a
point happens to fall nearest. ``rows.py`` carries it onto the row and
``parse_annotated_pdf`` matches annotations against it.

Annotation text is not page text
--------------------------------
It should not need saying, but on PyMuPDF 1.28.2 it does: ``get_text`` **does**
return the text of a page's annotations, mixed in with the form's own printed
words and indistinguishable from them once extracted. Measured, not assumed --
an aCRF whose ``Year of Birth (yyyy)`` question carries a ``BRTHDTC`` mark
extracts as the single line ``Year of Birth (yyyy) BRTHDTC``. So ``exclude``
below is not a nicety: without it, reading a finished aCRF back keys its lookup
table on question text with the answer glued onto the end of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Optional

import pymupdf

HEADING_RATIO = 1.35  # text this much taller than the page median is a heading

#: How much of a word must lie inside an excluded rect before the word is
#: dropped. A fraction rather than "intersects at all", because an annotation
#: that merely grazes a printed word should not delete it -- the mark is what is
#: unwanted, and a form's own text is not recoverable once discarded. Half a
#: word inside an annotation's box is not a graze.
MASK_CONTAINMENT = 0.5

#: How far a band extends past the first and last line on a page, as a fraction
#: of the median line height. There is no neighbour to split the difference with
#: there, so the band gets half a line of its own.
BAND_EDGE_FRACTION = 0.5


@dataclass(frozen=True)
class TextRun:
    """A horizontally contiguous piece of text -- roughly, one printed phrase.

    ``rect`` is a ``pymupdf.Rect``: top-left origin, y down.

    ``blocks`` carries the PyMuPDF text-block numbers the run's words came
    from. Normally one, but a run can straddle two blocks when words from each
    happen to sit close enough to survive the gap split, so it is a set rather
    than an int. ``rows.py`` uses it to decide whether two consecutive lines are
    one wrapped paragraph -- see this module's docstring for why that question
    is delegated rather than answered with a threshold.
    """

    rect: pymupdf.Rect
    text: str
    blocks: frozenset[int] = frozenset()


def v_overlap(a: pymupdf.Rect, b: pymupdf.Rect) -> float:
    """Shared vertical extent as a fraction of the shorter rect's height."""
    inter = max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))
    return inter / max(1e-6, min(a.height, b.height))


def h_overlap(a: pymupdf.Rect, b: pymupdf.Rect) -> float:
    """Shared horizontal extent as a fraction of the narrower rect's width."""
    inter = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    return inter / max(1e-6, min(a.width, b.width))


def clean_label(text: str) -> str:
    """Collapse whitespace and drop a trailing colon."""
    return " ".join(text.split()).rstrip(":").strip()


def _masked(rect: pymupdf.Rect, exclude: list[pymupdf.Rect]) -> bool:
    """Whether ``rect`` lies far enough inside any excluded rect to be dropped."""
    area = abs(rect.get_area())
    if area <= 0:
        return False
    return any(abs((rect & m).get_area()) / area >= MASK_CONTAINMENT for m in exclude)


def text_runs(
    page: pymupdf.Page, exclude: Optional[list[pymupdf.Rect]] = None
) -> list[TextRun]:
    """Group words into visual lines, then split each line at wide gaps.

    Returned in reading order (top to bottom, then left to right), which
    ``rows.py`` relies on: row ids are assigned in that order.

    ``exclude`` is a list of rects whose contents are not the form's own text --
    in practice the page's annotations, which ``get_text`` otherwise hands back
    mixed in with it (see the module docstring). Words are dropped *before* line
    grouping, so an annotation cannot merge into the line it sits beside and
    cannot widen that line's rect either. A word is dropped only when
    ``MASK_CONTAINMENT`` of it is inside; a graze leaves it alone.
    """
    # get_text("words") yields (x0, y0, x1, y1, word, block_no, line_no, word_no).
    words = [
        (pymupdf.Rect(w[0], w[1], w[2], w[3]), w[4], int(w[5]))
        for w in page.get_text("words")
        if w[4].strip()
    ]
    if exclude:
        words = [w for w in words if not _masked(w[0], exclude)]
    if not words:
        return []

    lines: list[tuple[pymupdf.Rect, list]] = []
    for item in sorted(words, key=lambda t: (t[0].y0, t[0].x0)):
        rect = item[0]
        for i, (lr, items) in enumerate(lines):
            if v_overlap(lr, rect) >= 0.5:
                items.append(item)
                lines[i] = (lr | rect, items)
                break
        else:
            lines.append((pymupdf.Rect(rect), [item]))

    runs: list[TextRun] = []
    for lr, items in sorted(lines, key=lambda t: t[0].y0):
        items.sort(key=lambda t: t[0].x0)
        gap_limit = max(3.0, 0.6 * lr.height)
        cur: Optional[pymupdf.Rect] = None
        buf: list[str] = []
        blocks: set[int] = set()
        for rect, text, block in items:
            if cur is not None and rect.x0 - cur.x1 > gap_limit:
                runs.append(TextRun(cur, " ".join(buf), frozenset(blocks)))
                cur, buf, blocks = None, [], set()
            cur = pymupdf.Rect(rect) if cur is None else (cur | rect)
            buf.append(text)
            blocks.add(block)
        if cur is not None:
            runs.append(TextRun(cur, " ".join(buf), frozenset(blocks)))
    return runs


def lines_of(runs: list[TextRun]) -> list[list[TextRun]]:
    """Regroup runs into visual lines, preserving reading order.

    ``text_runs`` splits lines apart at gaps; row assembly needs them back
    together to decide which column-2 run shares a baseline with which
    column-1 run. Grouping by the same vertical-overlap test used to build the
    lines in the first place keeps the two views consistent.
    """
    lines: list[list[TextRun]] = []
    for run in runs:
        for line in lines:
            if v_overlap(line[0].rect, run.rect) >= 0.5:
                line.append(run)
                break
        else:
            lines.append([run])
    for line in lines:
        line.sort(key=lambda r: r.rect.x0)
    return sorted(lines, key=lambda line: line[0].rect.y0)


def headings(runs: list[TextRun]) -> list[TextRun]:
    """Runs taller than ``HEADING_RATIO`` times the page's median line height."""
    if not runs:
        return []
    med = median(r.rect.height for r in runs)
    return [r for r in runs if r.rect.height > HEADING_RATIO * med]


def line_rect(line: list[TextRun]) -> pymupdf.Rect:
    """Union of a line's run rects."""
    out = pymupdf.Rect(line[0].rect)
    for run in line[1:]:
        out |= run.rect
    return out


def _band_edge(prev: pymupdf.Rect, nxt: pymupdf.Rect) -> float:
    """The ruled line between two consecutive text lines.

    The midpoint of the space between them -- except that the space is
    frequently negative. The two lines of one wrapped question overlap by a
    measured 2.4pt (see the module docstring), so the "midpoint of the gap" can
    land outside one of the boxes. Clamping it strictly between the two lines'
    *centres* guarantees the one property the band model rests on: every line's
    own centre lies inside its own band, so the bands really do partition the
    page and a point cannot fall in two of them or in neither.
    """
    edge = (prev.y1 + nxt.y0) / 2.0
    lo = (prev.y0 + prev.y1) / 2.0
    hi = (nxt.y0 + nxt.y1) / 2.0
    if lo >= hi:  # a tall line followed by a short one inside it
        return (lo + hi) / 2.0
    return min(max(edge, lo), hi)


def line_bands(lines: list[list[TextRun]], page_rect: pymupdf.Rect) -> list[pymupdf.Rect]:
    """One full-width horizontal band per line -- the ruled line each sits on.

    Think of the light blue rules on notebook paper: every line of writing owns
    the band it sits in, and the boundary between one question and the next is
    the rule between them rather than a judgement about which glyph rect a stray
    point is nearest.

    Full page width on purpose. A band is not the line's bounding box -- that is
    ``line_rect`` and it already exists. The band claims the whitespace to either
    side too, which is what makes it useful: the gutter between the two text
    columns is exactly where an annotation goes, and it belongs to the row on
    whose rule it sits.

    Returned one per input line, in the same order, with adjacent bands sharing
    an edge and the first and last extending ``BAND_EDGE_FRACTION`` of a line
    past the outermost text (clamped to the page).
    """
    if not lines:
        return []
    rects = [line_rect(line) for line in lines]
    pad = BAND_EDGE_FRACTION * median(r.height for r in rects)

    edges = [max(page_rect.y0, rects[0].y0 - pad)]
    edges.extend(_band_edge(prev, nxt) for prev, nxt in zip(rects, rects[1:]))
    edges.append(min(page_rect.y1, rects[-1].y1 + pad))
    return [
        pymupdf.Rect(page_rect.x0, edges[i], page_rect.x1, edges[i + 1])
        for i in range(len(rects))
    ]


__all__ = [
    "BAND_EDGE_FRACTION",
    "HEADING_RATIO",
    "MASK_CONTAINMENT",
    "TextRun",
    "clean_label",
    "h_overlap",
    "headings",
    "line_bands",
    "line_rect",
    "lines_of",
    "text_runs",
    "v_overlap",
]
