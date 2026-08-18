"""Metadata Submission Guidelines v2.0 colour rules -- ``pipeline/msg.py``.

The rule under test is the counter-intuitive one: colours are assigned by the
order domains are *encountered within a form*, not by a fixed domain-to-colour
table. A test suite that asserted ``DM == cyan`` would enshrine the mistake.
"""

from __future__ import annotations

import pytest

from pipeline.models import (
    AnnotationKind,
    AnnotationSet,
    BBox,
    CRFRow,
    Origin,
    PageGeometry,
    RowSet,
    SdtmAnnotation,
)
from pipeline.msg import (
    MSG_PALETTE,
    NOT_SUBMITTED_COLOR,
    apply_colors,
    color_map,
    domain_order,
    is_not_submitted,
    page_color_map,
    palette_color,
    to_pdf_color,
)


def _row(row_id: str, page: int, form: str, y: float) -> CRFRow:
    return CRFRow(
        row_id=row_id,
        page_index=page,
        form=form,
        text_1=f"question {row_id}",
        bbox_1=BBox(x0=90.0, y0=y, x1=250.0, y1=y + 10.0),
    )


def _annot(annot_id: str, row_id: str, page: int, y: float, **kw) -> SdtmAnnotation:
    return SdtmAnnotation(
        annot_id=annot_id,
        row_id=row_id,
        page_index=page,
        bbox=BBox(x0=260.0, y0=y, x1=330.0, y1=y + 10.0),
        kind=kw.pop("kind", AnnotationKind.VARIABLE),
        **kw,
    )


@pytest.fixture
def scenario() -> tuple[AnnotationSet, RowSet]:
    """Two forms over three pages, with a domain shared between them.

    ``DS`` is second on the Demographics form and first on the Disposition
    form, which is the case that separates encounter-order colouring from a
    fixed table: the same domain must take a different palette entry on each.
    """
    rows = RowSet(
        source_pdf="synthetic.pdf",
        pages=[
            PageGeometry(page_index=i, width=612.0, height=792.0, gutter_x=320.0)
            for i in range(3)
        ],
        rows=[
            _row("p1_r001", 0, "Demographics", 700.0),
            _row("p1_r002", 0, "Demographics", 660.0),
            _row("p2_r001", 1, "Demographics", 700.0),
            _row("p3_r001", 2, "Disposition", 700.0),
            _row("p3_r002", 2, "Disposition", 660.0),
        ],
    )
    annotations = AnnotationSet(
        source_pdf="synthetic.pdf",
        pages=rows.pages,
        annotations=[
            _annot("a1", "p1_r001", 0, 700.0, domain="DM", variable="BRTHDTC"),
            _annot("a2", "p1_r002", 0, 660.0, domain="DS", variable="DSSTDTC"),
            _annot("a3", "p2_r001", 1, 700.0, domain="DM", variable="AGE"),
            _annot("a4", "p3_r001", 2, 700.0, domain="DS", variable="DSDECOD"),
            _annot("a5", "p3_r002", 2, 660.0, domain="SC", variable="SCORRES"),
        ],
    )
    return annotations, rows


def test_domain_order_is_per_form_and_in_encounter_order(scenario):
    annotations, rows = scenario
    assert domain_order(annotations, rows) == {
        "Demographics": ["DM", "DS"],
        "Disposition": ["DS", "SC"],
    }


def test_same_domain_gets_different_colours_on_different_forms(scenario):
    """The point of encounter-order assignment, and what a fixed table gets wrong."""
    annotations, rows = scenario
    colors = color_map(annotations, rows)
    assert colors["Demographics"]["DS"] == MSG_PALETTE[1]
    assert colors["Disposition"]["DS"] == MSG_PALETTE[0]
    assert colors["Demographics"]["DS"] != colors["Disposition"]["DS"]


def test_a_domain_keeps_one_colour_across_its_form_pages(scenario):
    """DM appears on pages 1 and 2 of one form and must not change colour."""
    annotations, rows = scenario
    by_page = page_color_map(annotations, rows)
    assert by_page[0]["DM"] == by_page[1]["DM"]


def test_page_colour_map_lists_only_that_page_s_domains(scenario):
    annotations, rows = scenario
    by_page = page_color_map(annotations, rows)
    assert set(by_page[0]) == {"DM", "DS"}
    assert set(by_page[1]) == {"DM"}
    assert set(by_page[2]) == {"DS", "SC"}


def test_palette_cycles_rather_than_raising():
    """The palette is knowingly incomplete, so running off the end must degrade.

    Reusing a colour is visible to a reviewer; refusing to render the fifth
    domain on a form would lose an annotation.
    """
    n = len(MSG_PALETTE)
    assert palette_color(n) == MSG_PALETTE[0]
    assert palette_color(n + 2) == MSG_PALETTE[2]
    assert palette_color(0) == MSG_PALETTE[0]


def test_not_submitted_is_grey_and_stays_out_of_the_ordering():
    """Not being submitted is not a domain: no palette entry, no legend slot."""
    rows = RowSet(
        source_pdf="s.pdf",
        pages=[PageGeometry(page_index=0, width=612.0, height=792.0, gutter_x=320.0)],
        rows=[_row("p1_r001", 0, "Demographics", 700.0), _row("p1_r002", 0, "Demographics", 660.0)],
    )
    annotations = AnnotationSet(
        source_pdf="s.pdf",
        pages=rows.pages,
        annotations=[
            _annot("a1", "p1_r001", 0, 700.0, origin=Origin.NOT_SUBMITTED),
            _annot("a2", "p1_r002", 0, 660.0, domain="DM", variable="AGE"),
        ],
    )
    assert domain_order(annotations, rows) == {"Demographics": ["DM"]}

    colored = {a.annot_id: a.color for a in apply_colors(annotations, rows).annotations}
    assert colored["a1"] == NOT_SUBMITTED_COLOR
    assert colored["a2"] == MSG_PALETTE[0]


def test_not_submitted_recognised_from_literal_text_too():
    """A hand-typed ``[NOT SUBMITTED]`` in anno1 means the same as the origin column."""
    typed = _annot("a", "p1_r001", 0, 700.0, text="[not submitted]")
    assert is_not_submitted(typed)
    assert not is_not_submitted(_annot("b", "p1_r001", 0, 700.0, domain="DM", variable="AGE"))


def test_notes_get_no_fill(scenario):
    """MSG distinguishes commentary by a dashed border, not by a colour."""
    annotations, rows = scenario
    note = _annot("n1", "p1_r001", 0, 640.0, kind=AnnotationKind.NOTE, text="see protocol s7.2")
    annotations = annotations.model_copy(
        update={"annotations": annotations.annotations + [note]}
    )
    colored = {a.annot_id: a.color for a in apply_colors(annotations, rows).annotations}
    assert colored["n1"] is None


def test_legend_banner_takes_the_colour_of_the_domain_it_names(scenario):
    """Otherwise the legend is decoration rather than a key."""
    annotations, rows = scenario
    banner = SdtmAnnotation(
        annot_id="legend_DM",
        row_id=None,
        page_index=0,
        bbox=BBox(x0=36.0, y0=760.0, x1=150.0, y1=774.0),
        kind=AnnotationKind.DOMAIN,
        domain="DM",
        text="DM (Demographics)",
    )
    annotations = annotations.model_copy(
        update={"annotations": [banner] + annotations.annotations}
    )
    out = apply_colors(annotations, rows)
    by_id = {a.annot_id: a for a in out.annotations}
    assert by_id["legend_DM"].color == by_id["a1"].color


def test_to_pdf_color_converts_to_pymupdf_range():
    assert to_pdf_color((255, 0, 51)) == pytest.approx((1.0, 0.0, 0.2))
