"""Annotation placement -- ``pipeline/layout.py``.

Placement is arithmetic now rather than a collision search, so these tests
assert the arithmetic: an annotation starts just right of its question text on
that question's own baseline, a second annotation follows on the same line, and
one that will not fit before the response column drops to the line below at the
question's indent.
"""

from __future__ import annotations

import pytest

from pipeline.layout import (
    GAP,
    SLOT_GAP,
    annotation_size,
    place_annotations,
    place_row,
    right_limit,
    text_width,
)
from pipeline.models import (
    AnnotationKind,
    AnnotationSet,
    BBox,
    CRFRow,
    PageGeometry,
    RowSet,
    SdtmAnnotation,
)

PAGE = PageGeometry(page_index=0, width=612.0, height=792.0, gutter_x=320.0)


def _anchor(x0: float = 90.0, x1: float = 250.0, y0: float = 600.0, y1: float = 612.0) -> BBox:
    return BBox(x0=x0, y0=y0, x1=x1, y1=y1)


def _row(row_id: str, y: float, bbox_2: BBox | None = None) -> CRFRow:
    return CRFRow(
        row_id=row_id,
        page_index=0,
        form="Demographics",
        text_1=f"question {row_id}",
        text_2="Yes" if bbox_2 is not None else "",
        bbox_1=BBox(x0=90.0, y0=y, x1=250.0, y1=y + 12.0),
        bbox_2=bbox_2,
    )


def _annot(annot_id: str, row_id: str | None, slot: int, text: str) -> SdtmAnnotation:
    return SdtmAnnotation(
        annot_id=annot_id,
        row_id=row_id,
        slot=slot,
        page_index=0,
        # Deliberately the wrong place: placement must move it.
        bbox=BBox(x0=0.0, y0=0.0, x1=10.0, y1=10.0),
        kind=AnnotationKind.VARIABLE,
        text=text,
    )


# --------------------------------------------------------------------------
# One row's arithmetic
# --------------------------------------------------------------------------


def test_annotation_starts_just_right_of_the_question_on_its_own_baseline():
    anchor = _anchor()
    (box,) = place_row(anchor, ["BRTHDTC"], PAGE, limit=PAGE.width, obstacles=[])
    assert box.x0 == pytest.approx(anchor.x1 + GAP)
    assert box.y0 == pytest.approx(anchor.y0)
    assert box.y1 == pytest.approx(anchor.y1)


def test_two_annotations_sit_side_by_side():
    """The AGE / AGEU case. Placing them independently would stack them."""
    anchor = _anchor()
    first, second = place_row(anchor, ["AGE", "AGEU"], PAGE, limit=PAGE.width, obstacles=[])
    assert second.x0 == pytest.approx(first.x1 + SLOT_GAP)
    assert first.y0 == pytest.approx(second.y0), "same line"
    assert first.x1 <= second.x0


def test_box_is_wide_enough_for_its_text():
    anchor = _anchor()
    text = "VSORRES when VSTESTCD = SYSBP"
    (box,) = place_row(anchor, [text], PAGE, limit=PAGE.width, obstacles=[])
    assert box.width >= text_width(text)
    assert box.width == pytest.approx(annotation_size(text)[0])


def test_overlong_second_annotation_wraps_to_the_line_below():
    """The RPORRES / RPTESTCD = CHILDPOT case from the guidelines' example."""
    anchor = _anchor(x0=90.0, x1=300.0)
    limit = 380.0
    first, second = place_row(
        anchor, ["RPORRES", "RPTESTCD = CHILDPOT"], PAGE, limit=limit, obstacles=[]
    )
    assert first.y0 == pytest.approx(anchor.y0)
    assert second.y0 < first.y0, "second annotation did not drop to the next line"
    assert second.x0 == pytest.approx(anchor.x0), "wrapped annotation should take the question's indent"


def test_a_first_annotation_wraps_only_when_wrapping_helps():
    """Dropping below buys room only if the annotation fits at the indent.

    A long mapping on a cramped row belongs on the next line rather than printed
    over the response column. One too wide to fit even at the indent gains
    nothing by moving -- it would still overflow and would no longer sit beside
    its row -- so it stays on the baseline where the overflow is obvious.
    """
    anchor = _anchor(x0=90.0, x1=300.0)
    text = "VSORRES when VSTESTCD = SYSBP"
    width = annotation_size(text)[0]

    wraps_down, = place_row(anchor, [text], PAGE, limit=anchor.x0 + width + 5.0, obstacles=[])
    assert wraps_down.y0 < anchor.y0
    assert wraps_down.x0 == pytest.approx(anchor.x0)

    stays_put, = place_row(anchor, [text], PAGE, limit=anchor.x0 + width - 5.0, obstacles=[])
    assert stays_put.y0 == pytest.approx(anchor.y0)


def test_response_column_is_the_right_limit():
    row = _row("p1_r001", 600.0, bbox_2=BBox(x0=400.0, y0=600.0, x1=470.0, y1=612.0))
    assert right_limit(row, PAGE) == pytest.approx(400.0 - GAP)


def test_no_response_column_means_the_page_margin():
    """Deliberately not the gutter -- see the module docstring."""
    limit = right_limit(_row("p1_r001", 600.0), PAGE)
    assert limit > PAGE.gutter_x
    assert limit < PAGE.width


def test_option_only_row_is_not_limited_by_its_own_anchor():
    """``bbox_2`` is the anchor there, so it must not also be the boundary."""
    option_row = CRFRow(
        row_id="p1_r002",
        page_index=0,
        text_2="Female",
        bbox_2=BBox(x0=430.0, y0=660.0, x1=470.0, y1=672.0),
    )
    assert right_limit(option_row, PAGE) > option_row.bbox_2.x1


def test_annotation_never_covers_the_response_text():
    anchor = _anchor(x0=90.0, x1=250.0)
    row_bbox_2 = BBox(x0=300.0, y0=600.0, x1=470.0, y1=612.0)
    row = _row("p1_r001", 600.0, bbox_2=row_bbox_2)
    boxes = place_row(
        anchor,
        ["a fairly long mapping", "and another one"],
        PAGE,
        limit=right_limit(row, PAGE),
        obstacles=[],
    )
    on_baseline = [b for b in boxes if b.y0 == pytest.approx(anchor.y0)]
    for box in on_baseline:
        assert box.x1 <= row_bbox_2.x0, "annotation ran into the response column"


def test_wrapped_annotation_avoids_obstacles_below():
    """A wrap lands in the next row's territory, so that one must be checked."""
    anchor = _anchor(x0=90.0, x1=300.0, y0=600.0, y1=612.0)
    blocker = BBox(x0=90.0, y0=589.0, x1=280.0, y1=601.0)
    _, second = place_row(
        anchor,
        ["RPORRES", "RPTESTCD = CHILDPOT"],
        PAGE,
        limit=380.0,
        obstacles=[blocker],
    )
    assert min(second.y1, blocker.y1) - max(second.y0, blocker.y0) <= 1.0


# --------------------------------------------------------------------------
# Whole-document placement
# --------------------------------------------------------------------------


def test_place_annotations_moves_every_annotation_beside_its_row():
    rows = RowSet(
        source_pdf="s.pdf",
        pages=[PAGE],
        rows=[_row("p1_r001", 700.0), _row("p1_r002", 660.0)],
    )
    annotations = AnnotationSet(
        source_pdf="s.pdf",
        pages=[PAGE],
        annotations=[
            _annot("a1", "p1_r001", 1, "BRTHDTC"),
            _annot("a2", "p1_r002", 1, "AGE"),
            _annot("a3", "p1_r002", 2, "AGEU"),
        ],
    )
    out = place_annotations(annotations, rows)
    by_id = {a.annot_id: a for a in out.annotations}
    assert len(out.annotations) == 3

    for annot_id, row_id in (("a1", "p1_r001"), ("a2", "p1_r002"), ("a3", "p1_r002")):
        row = rows.by_id(row_id)
        assert by_id[annot_id].bbox.x0 > row.bbox_1.x1, f"{annot_id} not moved right of its row"
        assert by_id[annot_id].bbox.y0 <= row.bbox_1.y0

    # Slots 1 and 2 of the same row are laid out together, not stacked.
    assert by_id["a3"].bbox.x0 > by_id["a2"].bbox.x1


def test_annotations_on_different_rows_do_not_collide():
    rows = RowSet(
        source_pdf="s.pdf",
        pages=[PAGE],
        rows=[_row(f"p1_r{i:03d}", 700.0 - 20.0 * i) for i in range(1, 6)],
    )
    annotations = AnnotationSet(
        source_pdf="s.pdf",
        pages=[PAGE],
        annotations=[_annot(f"a{i}", f"p1_r{i:03d}", 1, f"VAR{i}") for i in range(1, 6)],
    )
    placed = place_annotations(annotations, rows).annotations
    boxes = [a.bbox for a in placed]
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            overlap_x = min(a.x1, b.x1) - max(a.x0, b.x0)
            overlap_y = min(a.y1, b.y1) - max(a.y0, b.y0)
            assert not (overlap_x > 1.0 and overlap_y > 1.0)


def test_option_only_row_anchors_off_its_response_bbox():
    """A row with no question text still has somewhere to hang an annotation."""
    option_row = CRFRow(
        row_id="p1_r002",
        page_index=0,
        form="Demographics",
        text_2="Female",
        bbox_2=BBox(x0=430.0, y0=660.0, x1=470.0, y1=672.0),
    )
    rows = RowSet(source_pdf="s.pdf", pages=[PAGE], rows=[option_row])
    annotations = AnnotationSet(
        source_pdf="s.pdf",
        pages=[PAGE],
        annotations=[_annot("a1", "p1_r002", 1, "SEX = F")],
    )
    (placed,) = place_annotations(annotations, rows).annotations
    assert placed.bbox.y0 == pytest.approx(option_row.bbox_2.y0)
    assert placed.bbox.x0 == pytest.approx(option_row.bbox_2.x1 + GAP)


def test_legend_banners_are_left_where_they_are():
    """A banner belongs to the page, not to a row; render.draw_legend places it."""
    rows = RowSet(source_pdf="s.pdf", pages=[PAGE], rows=[_row("p1_r001", 700.0)])
    banner = SdtmAnnotation(
        annot_id="legend_DM",
        row_id=None,
        page_index=0,
        bbox=BBox(x0=36.0, y0=760.0, x1=150.0, y1=774.0),
        kind=AnnotationKind.DOMAIN,
        domain="DM",
        text="DM (Demographics)",
    )
    annotations = AnnotationSet(source_pdf="s.pdf", pages=[PAGE], annotations=[banner])
    (out,) = place_annotations(annotations, rows).annotations
    assert out.bbox == banner.bbox


def test_annotation_for_an_unknown_row_is_kept_not_dropped():
    """A missing annotation is unreviewable; a misplaced one is at least visible."""
    rows = RowSet(source_pdf="s.pdf", pages=[PAGE], rows=[_row("p1_r001", 700.0)])
    annotations = AnnotationSet(
        source_pdf="s.pdf",
        pages=[PAGE],
        annotations=[_annot("a1", "p9_r999", 1, "ORPHAN")],
    )
    out = place_annotations(annotations, rows).annotations
    assert [a.annot_id for a in out] == ["a1"]
