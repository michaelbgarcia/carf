"""Where an annotation's text goes on the page.

An annotation belongs beside the question it annotates, on that question's own
baseline, in the gap between the two text columns. That is where a human
annotator writes it, and the two-column model hands us the gap for free::

    Age (years) at time of consent  [ AGE ][ AGEU ]      Fixed Unit: years
    <-------- text_1 -------------->        ^            <---- text_2 ----->
                                            annotations live here

So placement is arithmetic, not search. Start at ``bbox_1.x1 + GAP``, take the
row's own vertical extent, and lay the row's annotations left to right. When one
will not fit before the response column, drop to the next line at the question's
indent -- which is what the guidelines' own examples show for a long condition::

    Is Participant of childbearing potential?  [ RPORRES ]        Yes  O
                                    [ RPTESTCD = CHILDPOT ]        No  O

What this replaces
------------------
The previous implementation had to *search*: annotations were handed the bbox of
the capture field they described, which is precisely where they must not be
drawn, so it tried three candidate positions (right, below, left), tested each
against every field, every already-placed annotation and every printed caption,
and then walked downward in 9pt steps up to forty times before giving up. All of
that existed to reconstruct a position the row model simply knows.

What this is still not
----------------------
Response *widgets* are invisible here, because ``rows.py`` reads text and never
locates a widget. When a row has no ``text_2``, the annotation is free to run to
the right page margin, and a long one can therefore overlap a fill-in underline
or a checkbox sitting in that space. Limiting it at the gutter instead was
considered and rejected: the guidelines' own examples show annotations extending
well past the column break, so the limit would break the common case to protect
the rare one. The overlap is visible in the QC preview and to a reviewer.

Also still absent, and deferred with the "collision layout for dense pages"
phase: reflowing into margins, abbreviating text that will not fit, and leader
lines from a displaced annotation back to its row.

Coordinates here are PDF user space (y up), like every ``BBox``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf

from pipeline.models import AnnotationSet, BBox, PageGeometry, RowSet, SdtmAnnotation

FONT = "helv"
FONT_SIZE = 7.0
GAP = 4.0  # between a row's text and its first annotation
SLOT_GAP = 3.0  # between anno1 and anno2
PAD = 2.0  # padding inside the annotation box
MARGIN = 6.0  # keep annotations this far from the page edge
LINE_STEP = 11.0  # vertical drop when wrapping to the line below
MIN_OVERLAP = 1.0  # ignore sub-point grazes
MAX_WRAPS = 6  # give up rather than walk a long annotation off the page

#: Extra space an annotation must leave around printed text, on top of simply not
#: overlapping it. Two reasons it has to be more than zero. The drawn rect is
#: inflated by half the border width (see ``render.py``), so a box that touches
#: text by its bbox overlaps it on the page. And a 1pt clearance reads as a
#: collision anyway -- an annotation flush against the question text underneath it
#: is unreadable, which in a submission artifact is the same problem as covering
#: it. Measured cost of not having this: a note annotation cleared its neighbour
#: by 0.87pt, passed the ``MIN_OVERLAP`` graze test, and printed through the row
#: below.
CLEARANCE = 1.5

#: How much wider than its ``BBox`` a drawn annotation ends up, per edge. The PDF
#: stroke is centred on the path, so a bordered box placed at x0=260 stores
#: ``/Rect`` starting at 259.5. Lives here rather than in ``render.py`` because
#: both placement and the reverse-layout matching in ``parse_annotated_pdf`` need
#: to reason about it, and they must agree.
BORDER_INFLATION = 0.5


def text_width(text: str, size: float = FONT_SIZE) -> float:
    """Width of ``text`` in the font that will actually be embedded.

    ``pymupdf.get_text_length`` rather than a system-font measurement, so the
    width used to place a box is the width of the glyphs that box will contain.
    """
    return pymupdf.get_text_length(text, fontname=FONT, fontsize=size)


def annotation_size(text: str, size: float = FONT_SIZE) -> tuple[float, float]:
    return text_width(text, size) + 2 * PAD, size + 2 * PAD


@dataclass
class _Box:
    x0: float
    y0: float
    x1: float
    y1: float

    def overlaps(self, other: BBox) -> bool:
        """Whether this box comes within ``CLEARANCE`` of ``other``."""
        return (
            min(self.x1 + CLEARANCE, other.x1) - max(self.x0 - CLEARANCE, other.x0) > MIN_OVERLAP
            and min(self.y1 + CLEARANCE, other.y1) - max(self.y0 - CLEARANCE, other.y0)
            > MIN_OVERLAP
        )

    def blockers(self, obstacles: list[BBox]) -> list[BBox]:
        return [o for o in obstacles if self.overlaps(o)]

    def to_bbox(self) -> BBox:
        return BBox(x0=self.x0, y0=self.y0, x1=self.x1, y1=self.y1)


def _on_page(box: _Box, page: PageGeometry) -> bool:
    return (
        box.x0 >= MARGIN
        and box.x1 <= page.width - MARGIN
        and box.y0 >= MARGIN
        and box.y1 <= page.height - MARGIN
    )


def right_limit(row: CRFRow, page: PageGeometry) -> float:
    """How far right an annotation on this row may extend.

    The response column when the row has one -- printing over "Fixed Unit:
    years" is never acceptable -- otherwise the page margin. See the module
    docstring on why this is not the gutter.

    Takes the row rather than just its ``bbox_2`` because of the option-only
    case: there, ``bbox_2`` is the *anchor*, so treating it as the limit would
    put the boundary to the left of where the annotation starts and wrap every
    option annotation for no reason. Nothing is printed right of a response
    column, so the limit there is the margin.
    """
    if row.bbox_1 is not None and row.bbox_2 is not None:
        return row.bbox_2.x0 - GAP
    return page.width - MARGIN


def place_row(
    anchor: BBox,
    texts: list[str],
    page: PageGeometry,
    limit: float,
    obstacles: list[BBox],
    size: float = FONT_SIZE,
) -> list[BBox]:
    """Lay one row's annotations out left to right, wrapping when they run out of room.

    ``anchor`` is the row's question bbox (or its response bbox on an
    option-only row). ``texts`` is ``[anno1]`` or ``[anno1, anno2]`` in slot
    order; the returned boxes come back in the same order, one per text, always
    the same length as ``texts`` -- an annotation is never dropped, because a
    visible collision is reviewable and a missing annotation is not.
    """
    boxes: list[BBox] = []
    x = anchor.x1 + GAP
    y0 = anchor.y0
    start_x = anchor.x0
    wraps = 0

    for text in texts:
        w, h = annotation_size(text, size)
        # Match the row's own height so the box aligns with the text it annotates
        # rather than floating within the line.
        h = max(h, anchor.height)

        # Wrap only when it buys something. An annotation too wide to fit even at
        # the question's indent gains nothing by dropping below -- it would still
        # overflow, and it would no longer sit beside the row it describes -- so
        # it stays on the baseline where the overflow is obvious.
        if x + w > limit and start_x + w <= limit and wraps < MAX_WRAPS:
            x = start_x
            y0 -= LINE_STEP
            wraps += 1

        box = _Box(x, y0, x + w, y0 + h)
        # A wrapped annotation drops into the next row's territory, so it -- and
        # only it -- has to be checked against what is already there. An
        # unwrapped one sits in the gutter on its own row's baseline, where by
        # construction nothing is.
        while wraps and wraps <= MAX_WRAPS:
            blocking = box.blockers(obstacles)
            if _on_page(box, page) and not blocking:
                break
            # Clear the lowest thing in the way in one move, rather than stepping
            # by a fixed amount. Fixed steps can land in the sliver between two
            # printed rows -- technically clear, visibly a collision -- and take
            # several iterations to cross a row that one jump crosses exactly.
            if blocking:
                y0 = min(o.y0 for o in blocking) - h - CLEARANCE
            else:
                y0 -= LINE_STEP
            wraps += 1
            box = _Box(x, y0, x + w, y0 + h)

        boxes.append(box.to_bbox())
        x = box.x1 + SLOT_GAP

    return boxes


def text_obstacles(pdf_path) -> dict[int, list[BBox]]:
    """Bounding boxes of the form's own printed text, per page.

    Reuses ``pipeline.text.text_runs`` rather than re-deriving text grouping, so
    what layout treats as an obstacle is exactly what extraction treats as row
    content. Only consulted for wrapped annotations -- an unwrapped one sits on
    its own row's baseline in the gutter, where by construction there is nothing.
    """
    from pipeline.geometry import fitz_rect_to_bbox
    from pipeline.text import text_runs

    doc = pymupdf.open(pdf_path)
    try:
        return {
            i: [fitz_rect_to_bbox(r.rect, page.rect.height) for r in text_runs(page)]
            for i, page in enumerate(doc)
        }
    finally:
        doc.close()


def place_annotations(
    annotations: AnnotationSet,
    rows: RowSet,
    size: float = FONT_SIZE,
    obstacles: dict[int, list[BBox]] | None = None,
) -> AnnotationSet:
    """Position every annotation beside the row it annotates.

    Grouped by row so a row's ``anno1`` and ``anno2`` are laid out together --
    placing them independently is what would stack ``AGEU`` on top of ``AGE``.
    Rows are processed in reading order so the obstacle set grows the way a
    reviewer reads, which keeps any displacement predictable.

    Annotations with no ``row_id`` (domain legend banners) are left where they
    are: they belong to the page, not to a row, and ``render.draw_legend``
    positions them.
    """
    pages = {p.page_index: p for p in (annotations.pages or rows.pages)}
    printed: dict[int, list[BBox]] = {k: list(v) for k, v in (obstacles or {}).items()}

    by_row: dict[str, list[SdtmAnnotation]] = {}
    passthrough: list[SdtmAnnotation] = []
    for a in annotations.annotations:
        row = rows.by_id(a.row_id) if a.row_id else None
        if row is None or a.page_index not in pages or not a.display_text():
            passthrough.append(a)
            continue
        by_row.setdefault(a.row_id, []).append(a)  # type: ignore[arg-type]

    def row_key(row_id: str) -> tuple[int, float, float]:
        row = rows.by_id(row_id)
        assert row is not None  # filtered above
        return (row.page_index, -row.anchor.y1, row.anchor.x0)

    placed: list[SdtmAnnotation] = list(passthrough)
    taken: dict[int, list[BBox]] = {}

    for row_id in sorted(by_row, key=row_key):
        row = rows.by_id(row_id)
        assert row is not None
        page = pages[row.page_index]
        group = sorted(by_row[row_id], key=lambda a: a.slot)

        blocking = printed.get(row.page_index, []) + taken.get(row.page_index, [])
        boxes = place_row(
            anchor=row.anchor,
            texts=[a.display_text() for a in group],
            page=page,
            limit=right_limit(row, page),
            obstacles=blocking,
            size=size,
        )
        for a, box in zip(group, boxes):
            taken.setdefault(row.page_index, []).append(box)
            placed.append(a.model_copy(update={"bbox": box}))

    return annotations.model_copy(update={"annotations": placed})
