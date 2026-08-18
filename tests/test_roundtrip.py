"""Coordinate tests that gate their pipeline steps.

The instructions require two, each written *before* the work downstream of it:

* the stamp/pixmap tests below prove fitz -> BBox is right, before anything is
  built on top of extracted coordinates
* ``test_annotation_survives_xfdf_round_trip`` proves BBox -> fitz is its exact
  inverse, through the XFDF export and back

Both exist because a y-flip bug is *not* reliably visible. An annotation
placed at ``page_height - y`` instead of ``y`` still lands on the page, still
looks like a plausible annotation, and only reveals itself when someone
notices the label is next to the wrong field. So these assert on numbers and
on pixels, not on appearance.
"""

import pymupdf
import pytest

from pipeline.geometry import bbox_to_fitz_rect, format_xfdf_rect
from pipeline.models import (
    AnnotationKind,
    AnnotationSet,
    BBox,
    Origin,
    PageGeometry,
    SdtmAnnotation,
)
from pipeline.rows import extract_rows
from pipeline.xfdf import write_xfdf
from pipeline.xfdf_to_pdf import parse_xfdf, xfdf_to_pdf

STAMP = (1.0, 0.0, 0.0)
WHITE_ISH = 0.9  # channel value above this counts as unmarked page

# The row nearest the top of the page. Deliberately chosen: a mid-page row would
# land close to its own mirror image and weaken every assertion below.
TARGET_TEXT = "Site Identifier"


def read_annots(pdf_path, page_index: int = 0) -> list[tuple[str, tuple]]:
    """(contents, rect) for every annotation on a page, as plain data.

    PyMuPDF footgun: ``pymupdf.open(p)[0]`` builds a temporary Document *and*
    a temporary Page, both freed as soon as the expression ends. Any Annot
    read afterwards is a use-after-free and segfaults the interpreter rather
    than raising. So everything is read here while `doc` and `page` are still
    live locals, and only plain tuples escape.
    """
    doc = pymupdf.open(pdf_path)
    page = doc[page_index]
    out = [(a.info.get("content", ""), tuple(a.rect)) for a in page.annots()]
    doc.close()
    return out


def _is_stamped(pixel) -> bool:
    r, g, b = pixel[0] / 255, pixel[1] / 255, pixel[2] / 255
    return r > 0.5 and g < 0.4 and b < 0.4


def _center(rect) -> tuple[int, int]:
    return int(rect.x0 + rect.width / 2), int(rect.y0 + rect.height / 2)


@pytest.fixture
def target_page(crfs):
    """The page carrying TARGET_TEXT, plus that row."""
    rows = extract_rows(crfs["acroform"])
    row = next(r for r in rows.rows if r.text_1 == TARGET_TEXT)
    doc = pymupdf.open(crfs["acroform"])
    return doc[row.page_index], row


# --- extraction coordinate gate -------------------------------------------


def test_row_bbox_flips_back_onto_its_own_printed_text(target_page):
    """The strongest available check: read the text back out of the flipped rect.

    Under the old field model this compared a bbox against an AcroForm widget
    rect -- numbers against numbers, both produced by the same code. Rows are
    extracted from *text*, so the flipped rect can be asked what text it
    contains, and the answer either is the row's own text or the flip is wrong.
    """
    page, row = target_page
    rect = pymupdf.Rect(*bbox_to_fitz_rect(row.bbox_1, page.rect.height))
    recovered = " ".join(page.get_textbox(rect).split())
    assert recovered == row.text_1


def test_stamped_row_lands_on_the_printed_text(target_page):
    """Pixel half: stamp the row, render, confirm the ink is on the row.

    Rendering at 72 dpi keeps one pixel to one point, so pixel coordinates and
    fitz coordinates are the same numbers.
    """
    page, row = target_page
    rect = pymupdf.Rect(*bbox_to_fitz_rect(row.bbox_1, page.rect.height))

    before = page.get_pixmap(dpi=72)
    cx, cy = _center(rect)
    assert not _is_stamped(before.pixel(cx, cy)), "row is already red before stamping"

    page.draw_rect(rect, color=STAMP, fill=STAMP)
    after = page.get_pixmap(dpi=72)

    assert _is_stamped(after.pixel(cx, cy)), "stamp did not land on the row"
    # ...and inside its corners too, so a stamp of the wrong size fails.
    for x, y in (
        (int(rect.x0) + 2, int(rect.y0) + 2),
        (int(rect.x1) - 2, int(rect.y1) - 2),
    ):
        assert _is_stamped(after.pixel(x, y))


def test_the_mirrored_position_stays_blank(target_page):
    """Where an unflipped rect *would* have landed must be untouched.

    Without this, a test that only checks the stamp is somewhere on the page
    passes just as happily with the flip inverted.
    """
    page, row = target_page
    rect = pymupdf.Rect(*bbox_to_fitz_rect(row.bbox_1, page.rect.height))
    page.draw_rect(rect, color=STAMP, fill=STAMP)
    pix = page.get_pixmap(dpi=72)

    cx, cy = _center(rect)
    mirror_y = int(page.rect.height - cy)
    assert abs(mirror_y - cy) > 50, "target too close to the page centre to test"
    assert not _is_stamped(pix.pixel(cx, mirror_y))


def test_skipping_the_flip_puts_the_stamp_somewhere_else(target_page):
    """Proves the tests above have teeth.

    Treating the PDF-space bbox as if it were already a fitz rect is exactly
    the bug this whole module guards against. If that mistake still landed on
    the row, none of the assertions above would mean anything.
    """
    page, row = target_page
    correct = pymupdf.Rect(*bbox_to_fitz_rect(row.bbox_1, page.rect.height))
    unflipped = pymupdf.Rect(*row.bbox_1.as_tuple())
    assert not correct.intersects(unflipped)


# --- step 9 gate ----------------------------------------------------------

KNOWN_BBOX = BBox(x0=201.5, y0=654.25, x1=333.75, y1=665.5)


def _one_annotation(bbox=KNOWN_BBOX) -> AnnotationSet:
    return AnnotationSet(
        source_pdf="SYNTHETIC_sample_crf_twocol_acroform.pdf",
        pages=[PageGeometry(page_index=0, width=612.0, height=792.0)],
        annotations=[
            SdtmAnnotation(
                annot_id="rt-001",
                row_id="p1_r004",
                page_index=0,
                bbox=bbox,
                kind=AnnotationKind.VARIABLE,
                domain="DM",
                variable="SUBJID",
                origin=Origin.COLLECTED,
            )
        ],
    )


def test_annotation_survives_xfdf_round_trip(tmp_path):
    """A known SdtmAnnotation through xfdf.py and back must land unchanged.

    Asserts on coordinates, not appearance: an error of one page height still
    puts the annotation on the page and still looks approximately plausible.
    """
    path = write_xfdf(_one_annotation(), tmp_path / "rt.xfdf")
    back = parse_xfdf(path)
    assert len(back) == 1
    assert back[0].bbox.as_tuple() == pytest.approx(KNOWN_BBOX.as_tuple(), abs=1e-3)
    assert back[0].page_index == 0
    # No domain prefix: under MSG the domain is carried by the annotation's
    # colour and the page legend, not repeated inside every box.
    assert back[0].text == "SUBJID"


def test_round_trip_lands_on_the_page_where_it_started(crfs, tmp_path):
    """The full loop: record -> XFDF -> PDF, checked against the original bbox."""
    path = write_xfdf(_one_annotation(), tmp_path / "rt.xfdf")
    out = xfdf_to_pdf(crfs["acroform"], path, tmp_path / "rt.pdf")

    annots = read_annots(out)
    assert len(annots) == 1
    expected = bbox_to_fitz_rect(KNOWN_BBOX, 792.0)
    assert annots[0][1] == pytest.approx(expected, abs=0.5)


def test_xfdf_page_attribute_is_zero_based(tmp_path):
    """@page is 0-based and matches page_index; display_page is 1-based."""
    path = write_xfdf(_one_annotation(), tmp_path / "rt.xfdf")
    assert 'page="0"' in path.read_text(encoding="utf-8")


def test_hand_edited_xfdf_is_honoured(crfs, tmp_path):
    """Edit the XFDF by hand; the renderer must pick the edit up.

    The actual scenario the step exists for. A pass-through test of an
    unedited file proves much less, because the failure mode is the renderer
    quietly preferring the pre-review data it also has access to.
    """
    path = write_xfdf(_one_annotation(), tmp_path / "rt.xfdf")

    # What a reviewer does in Acrobat or a text editor: move it and retype it.
    moved = BBox(x0=90.0, y0=120.0, x1=222.25, y1=131.25)
    edited = (
        path.read_text(encoding="utf-8")
        .replace(format_xfdf_rect(KNOWN_BBOX), format_xfdf_rect(moved))
        .replace("<contents>SUBJID</contents>", "<contents>DM.USUBJID</contents>")
    )
    path.write_text(edited, encoding="utf-8")

    out = xfdf_to_pdf(crfs["acroform"], path, tmp_path / "edited.pdf")
    contents, rect = read_annots(out)[0]

    assert contents == "DM.USUBJID", "hand-edited text was discarded"
    expected = bbox_to_fitz_rect(moved, 792.0)
    assert rect == pytest.approx(expected, abs=0.5), "hand-edited position was discarded"


def test_renderer_does_not_consult_pre_review_data(crfs, tmp_path):
    """The XFDF is authoritative; nothing upstream may filter or override it.

    Text that no upstream artifact could have produced still renders, which
    guards against a future change that "helpfully" re-derives contents from
    proposals.json and silently undoes human review.
    """
    path = write_xfdf(_one_annotation(), tmp_path / "rt.xfdf")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "<contents>SUBJID</contents>", "<contents>ZZ.ANYTHING</contents>"
        ),
        encoding="utf-8",
    )
    out = xfdf_to_pdf(crfs["acroform"], path, tmp_path / "o.pdf")
    assert read_annots(out)[0][0] == "ZZ.ANYTHING"


def test_bbox_is_never_constructed_from_a_raw_fitz_rect():
    """A fitz rect fed to BBox directly is inverted, and the model says so."""
    with pytest.raises(ValueError, match="y-flip"):
        BBox(x0=470, y0=742, x1=570, y1=726)
