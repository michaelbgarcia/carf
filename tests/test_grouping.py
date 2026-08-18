"""Grouped (repeating) annotations -- ``pipeline/grouping.py`` and its placement.

The property under test throughout is that a group is **one box and N rows of
coverage**, not one box and one row. Both halves matter, and they fail
differently: losing the box gives a page with five identical annotations on it,
losing the coverage gives a page that looks right and a data set claiming four
of those rows were never mapped.

The fixture below is the shape the feature exists for -- a question followed by
a block of response options that all carry the same SDTM mapping:

    Please record protocol version ...      Original    O
                                          Amendment 1   O
                                          Amendment 2   O
                                          Amendment 3   O
"""

from __future__ import annotations

import pytest

from pipeline import grouping
from pipeline.grouping import (
    AUTO_PREFIX,
    GroupingError,
    collapse_repeats,
    coverage,
    page_runs,
    resolve_groups,
    row_membership,
    summarize,
)
from pipeline.layout import GAP, block_anchor, place_annotations, place_group
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
PAGE_2 = PageGeometry(page_index=1, width=612.0, height=792.0, gutter_x=320.0)

PROTVER = 'SUPPDS.QVAL when QNAM = "PROTVER"'


def _row(
    row_id: str,
    y: float,
    *,
    x1: float = 250.0,
    page_index: int = 0,
    form: str = "Disposition",
    text_2: str = "",
) -> CRFRow:
    return CRFRow(
        row_id=row_id,
        page_index=page_index,
        form=form,
        text_1=f"question {row_id}",
        text_2=text_2,
        bbox_1=BBox(x0=90.0, y0=y, x1=x1, y1=y + 12.0),
        bbox_2=BBox(x0=400.0, y0=y, x1=470.0, y1=y + 12.0) if text_2 else None,
    )


def _annot(row_id: str, text: str, slot: int = 1, **kw) -> SdtmAnnotation:
    return SdtmAnnotation(
        annot_id=f"{row_id}_a{slot}",
        row_id=row_id,
        slot=slot,
        page_index=kw.pop("page_index", 0),
        # Deliberately the wrong place: placement must move it.
        bbox=BBox(x0=0.0, y0=0.0, x1=10.0, y1=10.0),
        kind=kw.pop("kind", AnnotationKind.VARIABLE),
        text=text,
        **kw,
    )


@pytest.fixture
def rows() -> RowSet:
    """A question row plus four option rows, then an unrelated row below."""
    return RowSet(
        source_pdf="synthetic.pdf",
        pages=[PAGE],
        rows=[
            _row("p1_r001", 700.0),
            _row("p1_r002", 686.0),
            _row("p1_r003", 672.0, x1=280.0),  # the widest of the block
            _row("p1_r004", 658.0),
            _row("p1_r005", 620.0),
        ],
    )


def _set(annotations: list[SdtmAnnotation], rows: RowSet) -> AnnotationSet:
    return AnnotationSet(
        source_pdf=rows.source_pdf, pages=rows.pages, annotations=annotations
    )


# --------------------------------------------------------------------------
# The model's own bookkeeping
# --------------------------------------------------------------------------


def test_covered_row_ids_normalises_grouped_and_ungrouped():
    plain = _annot("p1_r001", "SITEID")
    grouped = _annot("p1_r001", PROTVER, member_row_ids=["p1_r001", "p1_r002"])
    assert plain.covered_row_ids == ["p1_r001"]
    assert not plain.is_grouped
    assert grouped.covered_row_ids == ["p1_r001", "p1_r002"]
    assert grouped.is_grouped


def test_members_must_be_led_by_the_anchor():
    """Placement uses row_id; a members list that disagrees would draw the box
    against one row and attribute it to another."""
    with pytest.raises(ValueError, match="anchor row must lead"):
        _annot("p1_r002", PROTVER, member_row_ids=["p1_r001", "p1_r002"])


def test_a_row_cannot_be_listed_twice():
    with pytest.raises(ValueError, match="lists a row twice"):
        _annot("p1_r001", PROTVER, member_row_ids=["p1_r001", "p1_r002", "p1_r001"])


# --------------------------------------------------------------------------
# Declared groups
# --------------------------------------------------------------------------


def test_one_text_on_the_first_row_covers_the_whole_block(rows):
    """The sheet shape: text typed once, the key typed on every member row."""
    annotations = _set([_annot("p1_r001", PROTVER)], rows)
    membership = row_membership(
        {"p1_r001": "g1", "p1_r002": "g1", "p1_r003": "g1", "p1_r004": "g1"}
    )

    out = resolve_groups(annotations, rows, membership)

    (annot,) = out.annotations
    assert annot.is_grouped
    assert annot.row_id == "p1_r001"
    assert annot.member_row_ids == ["p1_r001", "p1_r002", "p1_r003", "p1_r004"]
    assert annot.display_text() == PROTVER


def test_the_text_may_be_typed_on_any_member_but_the_first_row_anchors_it(rows):
    annotations = _set([_annot("p1_r003", PROTVER)], rows)
    membership = row_membership({"p1_r002": "g1", "p1_r003": "g1", "p1_r004": "g1"})

    (annot,) = resolve_groups(annotations, rows, membership).annotations

    assert annot.row_id == "p1_r002"  # first in document order, not the typed one
    assert annot.member_row_ids == ["p1_r002", "p1_r003", "p1_r004"]


def test_a_group_of_one_is_not_a_group(rows):
    annotations = _set([_annot("p1_r001", "SITEID")], rows)
    (annot,) = resolve_groups(
        annotations, rows, row_membership({"p1_r001": "g1"})
    ).annotations
    assert not annot.is_grouped
    assert annot.member_row_ids == []
    assert annot.group_id == "g1"  # the sheet cell still round-trips


def test_members_disagreeing_on_the_text_is_an_error_not_a_coin_toss(rows):
    annotations = _set(
        [_annot("p1_r001", PROTVER), _annot("p1_r002", "DSDECOD")], rows
    )
    membership = row_membership({"p1_r001": "g1", "p1_r002": "g1"})

    with pytest.raises(GroupingError, match="different"):
        resolve_groups(annotations, rows, membership)


def test_a_group_may_not_straddle_a_row_that_says_something_else(rows):
    """One box covering rows 1-4 while row 3 maps elsewhere asserts two
    contradictory things about row 3, and no placement makes that readable."""
    annotations = _set(
        [
            _annot("p1_r001", PROTVER),
            _annot("p1_r003", "DSSTDTC"),
            _annot("p1_r004", ""),
        ],
        rows,
    )
    membership = row_membership({"p1_r001": "g1", "p1_r002": "g1", "p1_r004": "g1"})

    with pytest.raises(GroupingError) as exc:
        resolve_groups(annotations, rows, membership)
    assert "p1_r003" in str(exc.value)
    assert exc.value.row_ids == ["p1_r003"]


def test_a_gap_of_unannotated_rows_is_allowed(rows):
    """A reviewer who grouped across a blank row made that call deliberately,
    and the box is then the only claim on the page about that row."""
    annotations = _set([_annot("p1_r001", PROTVER)], rows)
    membership = row_membership({"p1_r001": "g1", "p1_r003": "g1"})

    (annot,) = resolve_groups(annotations, rows, membership).annotations

    assert annot.member_row_ids == ["p1_r001", "p1_r003"]


def test_an_empty_slot_two_group_is_dropped_rather_than_raising(rows):
    """The `group` cell belongs to the row, so it claims anno2 as well -- and a
    block whose members only fill anno1 leaves an empty slot-2 group behind."""
    annotations = _set([_annot("p1_r001", PROTVER)], rows)
    out = resolve_groups(
        annotations, rows, row_membership({"p1_r001": "g1", "p1_r002": "g1"})
    )
    assert [a.slot for a in out.annotations] == [1]


def test_a_group_key_naming_an_unknown_row_is_an_error(rows):
    annotations = _set([_annot("p1_r001", PROTVER)], rows)
    with pytest.raises(GroupingError, match="not in the extracted rows"):
        resolve_groups(annotations, rows, row_membership({"p1_r099": "g1"}))


def test_membership_defaults_to_the_annotations_own_group_id(rows):
    """The round trip: proposals.json read back is already resolved."""
    annotations = _set(
        [
            _annot(
                "p1_r001",
                PROTVER,
                group_id="g1",
                member_row_ids=["p1_r001", "p1_r002"],
            )
        ],
        rows,
    )
    (annot,) = resolve_groups(annotations, rows).annotations
    assert annot.member_row_ids == ["p1_r001", "p1_r002"]


# --------------------------------------------------------------------------
# Groups that span pages
# --------------------------------------------------------------------------


def test_a_group_spanning_pages_draws_once_per_page():
    """The repeating-form case. A page whose fields are annotated only by a box
    on the previous page is unreadable on its own, and unreviewable printed."""
    rows = RowSet(
        source_pdf="synthetic.pdf",
        pages=[PAGE, PAGE_2],
        rows=[
            _row("p1_r001", 700.0),
            _row("p1_r002", 686.0),
            _row("p2_r001", 700.0, page_index=1),
            _row("p2_r002", 686.0, page_index=1),
        ],
    )
    annotations = _set([_annot("p1_r001", "VSDTC")], rows)
    membership = row_membership(
        {"p1_r001": "g1", "p1_r002": "g1", "p2_r001": "g1", "p2_r002": "g1"}
    )

    out = resolve_groups(annotations, rows, membership)

    assert len(out.annotations) == 2
    first, second = sorted(out.annotations, key=lambda a: a.page_index)
    assert first.page_index == 0 and first.member_row_ids == ["p1_r001", "p1_r002"]
    assert second.page_index == 1 and second.member_row_ids == ["p2_r001", "p2_r002"]
    assert first.annot_id != second.annot_id
    assert first.group_id == second.group_id == "g1"


def test_page_runs_splits_in_document_order():
    rows = RowSet(
        source_pdf="x.pdf",
        pages=[PAGE, PAGE_2],
        rows=[
            _row("p1_r001", 700.0),
            _row("p2_r001", 700.0, page_index=1),
            _row("p2_r002", 686.0, page_index=1),
        ],
    )
    assert page_runs(["p2_r002", "p1_r001", "p2_r001"], rows) == [
        ["p1_r001"],
        ["p2_r001", "p2_r002"],
    ]


# --------------------------------------------------------------------------
# Collapsing what precedent pre-populated
# --------------------------------------------------------------------------


def test_identical_annotations_on_consecutive_rows_collapse(rows):
    annotations = _set(
        [
            _annot("p1_r001", PROTVER, domain="DS"),
            _annot("p1_r002", PROTVER, domain="DS"),
            _annot("p1_r003", PROTVER, domain="DS"),
        ],
        rows,
    )

    out = collapse_repeats(annotations, rows)

    (annot,) = out.annotations
    assert annot.member_row_ids == ["p1_r001", "p1_r002", "p1_r003"]
    assert annot.group_id == f"{AUTO_PREFIX}p1_r001"
    assert summarize(out) == (1, 3)


def test_a_gap_breaks_a_run(rows):
    """Strictly adjacent only: a box centred across an unannotated row would
    assert a mapping for a row nobody mapped."""
    annotations = _set(
        [_annot("p1_r001", PROTVER), _annot("p1_r003", PROTVER)], rows
    )
    out = collapse_repeats(annotations, rows)
    assert [a.is_grouped for a in out.annotations] == [False, False]


def test_different_text_never_collapses(rows):
    """The Race case: adjacent option rows whose mappings differ per option."""
    annotations = _set(
        [
            _annot("p1_r001", "RACE = ASIAN"),
            _annot("p1_r002", "RACE = WHITE"),
            _annot("p1_r003", "RACE = BLACK OR AFRICAN AMERICAN"),
        ],
        rows,
    )
    out = collapse_repeats(annotations, rows)
    assert not any(a.is_grouped for a in out.annotations)


def test_same_text_under_different_domains_never_collapses(rows):
    """Two boxes reading alike in different colours are not one box."""
    annotations = _set(
        [
            _annot("p1_r001", "VISITNUM", domain="VS"),
            _annot("p1_r002", "VISITNUM", domain="LB"),
        ],
        rows,
    )
    assert not any(a.is_grouped for a in collapse_repeats(annotations, rows).annotations)


def test_a_form_boundary_breaks_a_run():
    """MSG colours are assigned per form, so two identical mappings either side
    of a form boundary are two annotations, not one waiting to happen."""
    rows = RowSet(
        source_pdf="x.pdf",
        pages=[PAGE],
        rows=[
            _row("p1_r001", 700.0, form="Vital Signs"),
            _row("p1_r002", 686.0, form="Vital Signs"),
            _row("p1_r003", 672.0, form="Laboratory"),
        ],
    )
    annotations = _set(
        [_annot(r.row_id, "VISITNUM") for r in rows.rows], rows
    )

    out = collapse_repeats(annotations, rows)

    grouped = [a for a in out.annotations if a.is_grouped]
    assert len(grouped) == 1
    assert grouped[0].member_row_ids == ["p1_r001", "p1_r002"]


def test_collapse_is_per_slot(rows):
    """A run of identical anno1 cells says nothing about an anno2 on one of
    those rows -- claiming it for the group would attribute AGEU to four rows
    that never carried it."""
    annotations = _set(
        [
            _annot("p1_r001", "AGE"),
            _annot("p1_r001", "AGEU", slot=2),
            _annot("p1_r002", "AGE"),
        ],
        rows,
    )

    out = collapse_repeats(annotations, rows)

    grouped = {a.slot: a for a in out.annotations}
    assert grouped[1].member_row_ids == ["p1_r001", "p1_r002"]
    assert grouped[2].member_row_ids == []


def test_a_declared_group_is_never_re_derived(rows):
    annotations = _set(
        [
            _annot("p1_r001", PROTVER, group_id="mine"),
            _annot("p1_r002", PROTVER, group_id="mine"),
        ],
        rows,
    )
    (annot,) = collapse_repeats(annotations, rows).annotations
    assert annot.group_id == "mine"


def test_collapse_leaves_an_unrepeated_document_untouched(rows):
    annotations = _set([_annot("p1_r001", "SITEID")], rows)
    assert collapse_repeats(annotations, rows) == annotations


def test_coverage_reports_every_member(rows):
    annotations = _set(
        [_annot("p1_r001", PROTVER, member_row_ids=["p1_r001", "p1_r002"])], rows
    )
    assert set(coverage(annotations)) == {"p1_r001", "p1_r002"}


# --------------------------------------------------------------------------
# Placement
# --------------------------------------------------------------------------


def test_a_grouped_box_clears_the_widest_member_not_just_the_anchor(rows):
    """A block whose longest line is in the middle would otherwise have the box
    printing through it."""
    block = block_anchor(rows.rows[:4])
    assert block.x1 == 280.0  # p1_r003's, not the anchor's 250.0

    (box,) = place_group(block, [PROTVER])
    assert box.x0 == pytest.approx(280.0 + GAP)


def test_a_grouped_box_is_centred_on_its_block_at_text_height(rows):
    block = block_anchor(rows.rows[:4])
    (box,) = place_group(block, [PROTVER])

    assert (box.y0 + box.y1) / 2 == pytest.approx((block.y0 + block.y1) / 2)
    # Text height, not block height: a box the size of the block would bury the
    # rows it annotates.
    assert box.height < block.height


def test_place_annotations_routes_a_group_through_place_group(rows):
    annotations = _set(
        [
            _annot(
                "p1_r001",
                PROTVER,
                group_id="g1",
                member_row_ids=["p1_r001", "p1_r002", "p1_r003", "p1_r004"],
            )
        ],
        rows,
    )

    (placed,) = place_annotations(annotations, rows).annotations

    block = block_anchor(rows.rows[:4])
    assert placed.bbox.x0 == pytest.approx(block.x1 + GAP)
    assert (placed.bbox.y0 + placed.bbox.y1) / 2 == pytest.approx(
        (block.y0 + block.y1) / 2
    )


def test_an_ungrouped_slot_on_a_grouped_row_starts_past_the_group_box(rows):
    """The mixed case. Both boxes hang off the same anchor row, so placing them
    independently would print one through the other."""
    annotations = _set(
        [
            _annot(
                "p1_r001",
                "AGE",
                group_id="g1",
                member_row_ids=["p1_r001", "p1_r002"],
            ),
            _annot("p1_r001", "AGEU", slot=2),
        ],
        rows,
    )

    placed = {a.slot: a for a in place_annotations(annotations, rows).annotations}

    assert placed[2].bbox.x0 >= placed[1].bbox.x1
    # ...and the ungrouped one stays on its own row's baseline.
    assert placed[2].bbox.y0 == pytest.approx(rows.rows[0].anchor.y0)


def test_placement_leaves_a_single_row_annotation_where_it_always_was(rows):
    """Grouping is additive: nothing about the ordinary path changes."""
    annotations = _set([_annot("p1_r001", "SITEID")], rows)
    (placed,) = place_annotations(annotations, rows).annotations
    anchor = rows.rows[0].anchor
    assert placed.bbox.x0 == pytest.approx(anchor.x1 + GAP)
    assert placed.bbox.y0 == pytest.approx(anchor.y0)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def test_summarize_counts_groups_and_the_rows_they_cover(rows):
    annotations = _set(
        [
            _annot("p1_r001", PROTVER, member_row_ids=["p1_r001", "p1_r002"]),
            _annot("p1_r003", "DSSTDTC"),
        ],
        rows,
    )
    assert summarize(annotations) == (1, 2)


def test_document_order_is_row_set_order(rows):
    order = grouping.document_order(rows)
    assert [r for r, _ in sorted(order.items(), key=lambda kv: kv[1])] == [
        r.row_id for r in rows.rows
    ]
