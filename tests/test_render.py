"""MSG-styled drawing -- ``pipeline/render.py``.

The load-bearing assertion here is ``contents``. FDA review tools search
annotation text, so a styling change that renders correctly on screen while
emptying ``contents`` is a silent data-integrity regression -- exactly the
failure mode ``render.py``'s docstring records for ``richtext=True``. Fills and
dashes are new, so their effect on ``contents`` is asserted rather than assumed.
"""

from __future__ import annotations

import pymupdf
import pytest

from pipeline.models import BBox
from pipeline.msg import MSG_PALETTE, NOT_SUBMITTED_COLOR, TEXT_COLOR, to_pdf_color
from pipeline.render import (
    BORDER_WIDTH,
    DASH_PATTERN,
    draw_annotation,
    draw_legend,
    save_with_annotations,
)

TEXT = "VSORRES when VSTESTCD = SYSBP"


@pytest.fixture
def page():
    doc = pymupdf.open()
    try:
        yield doc.new_page(width=612.0, height=792.0)
    finally:
        doc.close()


def _bbox(y0: float = 600.0) -> BBox:
    return BBox(x0=260.0, y0=y0, x1=420.0, y1=y0 + 12.0)


def test_contents_survives_a_fill_and_a_border(page):
    """The whole reason this module documents its border findings."""
    annot = draw_annotation(page, _bbox(), TEXT, fill=MSG_PALETTE[0])
    assert annot.info["content"] == TEXT


def test_contents_survives_a_dashed_note(page):
    annot = draw_annotation(page, _bbox(), "see protocol section 7.2", dashed=True)
    assert annot.info["content"] == "see protocol section 7.2"


def test_contents_survives_the_round_trip_to_disk(page, tmp_path):
    """Searchability has to hold in the saved file, not just in memory."""
    draw_annotation(page, _bbox(), TEXT, fill=MSG_PALETTE[0])
    out = tmp_path / "annotated.pdf"
    save_with_annotations(page.parent, out)

    doc = pymupdf.open(out)
    try:
        contents = [a.info["content"] for a in doc[0].annots()]
    finally:
        doc.close()
    assert contents == [TEXT]


def test_fill_is_applied_in_pymupdf_units(page):
    """On a FreeText annot the background lives in ``/C``, which reads back as "stroke".

    Confusing on the way out, but it is what the guidelines' own example writes
    and what viewers treat as the background.
    """
    annot = draw_annotation(page, _bbox(), TEXT, fill=NOT_SUBMITTED_COLOR)
    assert annot.colors["stroke"] == pytest.approx(to_pdf_color(NOT_SUBMITTED_COLOR), abs=1e-3)


def _da(annot) -> str:
    """The annotation's default-appearance string, which ``info`` does not carry."""
    return annot.parent.parent.xref_get_key(annot.xref, "DA")[1]


def test_filled_annotation_uses_black_text(page):
    """MSG puts black text on the coloured ground, not coloured text."""
    annot = draw_annotation(page, _bbox(), TEXT, fill=MSG_PALETTE[2])
    da = _da(annot)
    assert da, "no default-appearance string recorded"
    expected = " ".join(f"{c:g}" for c in to_pdf_color(TEXT_COLOR)) + " rg"
    assert expected in da, f"expected {expected!r} in {da!r}"


def test_note_gets_a_dashed_border(page):
    """The dashes are how MSG distinguishes commentary from a mapping."""
    annot = draw_annotation(page, _bbox(), "a comment", dashed=True)
    assert tuple(annot.border["dashes"]) == DASH_PATTERN


def test_unfilled_annotation_has_no_background(page):
    annot = draw_annotation(page, _bbox(), "a comment", dashed=True)
    assert not annot.colors["stroke"]


def test_mapping_gets_a_solid_border(page):
    annot = draw_annotation(page, _bbox(), TEXT, fill=MSG_PALETTE[0])
    assert not tuple(annot.border["dashes"])
    assert annot.border["width"] == pytest.approx(BORDER_WIDTH)


def test_bbox_is_flipped_into_fitz_space(page):
    """A BBox is y-up; the drawn rect is y-down. Same rectangle, reflected.

    Compared with a half-border-width tolerance: the stored ``/Rect`` is inflated
    by half the stroke width, since the stroke is centred on the path.
    """
    bbox = _bbox(y0=700.0)
    annot = draw_annotation(page, bbox, TEXT, fill=MSG_PALETTE[0])
    rect = annot.rect
    tol = BORDER_WIDTH / 2 + 1e-3
    assert rect.x0 == pytest.approx(bbox.x0, abs=tol)
    assert rect.y0 == pytest.approx(page.rect.height - bbox.y1, abs=tol)
    assert rect.y1 == pytest.approx(page.rect.height - bbox.y0, abs=tol)


def test_no_stray_callout_line_is_left_behind(page):
    """PyMuPDF writes a ``/CL`` from a page corner to every freetext annot.

    Inert as generated -- the appearance stream draws no such line -- but a
    submission artifact should not carry a key instructing a viewer to draw a
    leader line from the corner of the page and depend on every reader ignoring
    it. See ``render._drop_callout``.
    """
    annot = draw_annotation(page, _bbox(), TEXT, fill=MSG_PALETTE[0])
    kind, _ = page.parent.xref_get_key(annot.xref, "CL")
    assert kind == "null", "the callout line survived"

    (legend,) = draw_legend(page, [("DM (Demographics)", MSG_PALETTE[0])])
    assert page.parent.xref_get_key(legend.xref, "CL")[0] == "null"


def test_legend_draws_one_box_per_domain_in_order(page):
    domains = [("DM (Demographics)", MSG_PALETTE[0]), ("DS (Disposition)", MSG_PALETTE[1])]
    annots = draw_legend(page, domains)
    assert [a.info["content"] for a in annots] == [d[0] for d in domains]
    # Left to right, on the same line, at the top of the page.
    assert annots[0].rect.x1 <= annots[1].rect.x0
    assert annots[0].rect.y0 == pytest.approx(annots[1].rect.y0)
    assert annots[0].rect.y0 < 40.0


def test_legend_wraps_onto_a_second_line_when_it_runs_out_of_width(page):
    domains = [(f"D{i} (A Domain With A Long Name {i})", MSG_PALETTE[i % 4]) for i in range(8)]
    annots = draw_legend(page, domains)
    assert len(annots) == len(domains)
    rows = sorted({round(a.rect.y0, 1) for a in annots})
    assert len(rows) > 1, "legend did not wrap"
    for a in annots:
        assert a.rect.x1 <= page.rect.width, "legend box ran off the page"


def test_empty_legend_draws_nothing(page):
    assert draw_legend(page, []) == []


def test_save_does_not_flatten_annotations(page, tmp_path):
    """Baking would look identical and be a regression -- annots must stay markup."""
    draw_annotation(page, _bbox(), TEXT, fill=MSG_PALETTE[0])
    out = tmp_path / "annotated.pdf"
    save_with_annotations(page.parent, out)

    doc = pymupdf.open(out)
    try:
        assert len(list(doc[0].annots())) == 1
    finally:
        doc.close()
