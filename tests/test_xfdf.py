"""The XFDF writer and the hand-written reader.

XFDF is the human-facing artifact: written by the pipeline, reviewed and
possibly edited in Acrobat, then read back as the authoritative source. These
tests cover the contract on both sides of that hop.
"""

import xml.etree.ElementTree as ET

import pytest

from pipeline.models import (
    AnnotationKind,
    AnnotationSet,
    BBox,
    Origin,
    PageGeometry,
    ReviewStatus,
    SdtmAnnotation,
)
from pipeline.xfdf import XFDF_NS, build_xfdf, write_xfdf
from pipeline.xfdf_to_pdf import overflowing_annotations, parse_xfdf, xfdf_to_pdf

BOX = BBox(x0=200.0, y0=650.0, x1=300.0, y1=662.0)


def _annot(**kw) -> SdtmAnnotation:
    base = dict(
        annot_id="a1",
        field_id="DM_USUBJID",
        page_index=0,
        bbox=BOX,
        kind=AnnotationKind.VARIABLE,
        domain="DM",
        variable="SUBJID",
        origin=Origin.COLLECTED,
        confidence=0.9,
    )
    base.update(kw)
    return SdtmAnnotation(**base)


def _set(*annots) -> AnnotationSet:
    return AnnotationSet(
        source_pdf="blank.pdf",
        pages=[PageGeometry(page_index=0, width=612.0, height=792.0)],
        annotations=list(annots),
    )


# --- writer ---------------------------------------------------------------


def test_output_is_well_formed_xfdf():
    root = ET.fromstring(build_xfdf(_set(_annot()), "blank.pdf"))
    assert root.tag == f"{{{XFDF_NS}}}xfdf"
    # Every element is in the XFDF namespace, so a re-parsed tree agrees with
    # the one the writer built.
    assert root.find(f"{{{XFDF_NS}}}f").get("href") == "blank.pdf"
    assert root.find(f"{{{XFDF_NS}}}annots") is not None


def test_serialized_form_declares_xfdf_as_the_default_namespace():
    """What Acrobat actually reads: bare tags under a default xmlns."""
    xml = build_xfdf(_set(_annot()), "blank.pdf")
    assert f'xmlns="{XFDF_NS}"' in xml
    assert "<freetext " in xml and 'xmlns=""' not in xml


def test_rect_is_written_in_pdf_user_space_unflipped():
    """A BBox is already bottom-left origin; xfdf.py must not flip it again."""
    xml = build_xfdf(_set(_annot()), "blank.pdf")
    assert 'rect="200.000,650.000,300.000,662.000"' in xml


def test_page_attribute_is_zero_based():
    xml = build_xfdf(_set(_annot(page_index=1)), "blank.pdf")
    assert 'page="1"' in xml  # page_index 1 == human page 2


def test_contents_carry_the_rendered_annotation_text():
    xml = build_xfdf(
        _set(_annot(variable="VSORRES", domain="VS", condition="VSTESTCD = SYSBP")),
        "blank.pdf",
    )
    assert "<contents>VS.VSORRES when VSTESTCD = SYSBP</contents>" in xml


def test_unmapped_fields_are_still_written_visibly():
    """A NotSubmitted field must not render as a blank annotation."""
    xml = build_xfdf(
        _set(
            _annot(
                kind=AnnotationKind.NOTE,
                domain=None,
                variable=None,
                origin=Origin.NOT_SUBMITTED,
            )
        ),
        "blank.pdf",
    )
    assert "<contents>[Not Submitted]</contents>" in xml


def test_rejected_annotations_are_excluded():
    """A rejection means it does not exist as far as the submission goes."""
    kept = _annot(annot_id="keep")
    dropped = _annot(
        annot_id="drop",
        review_status=ReviewStatus.REJECTED,
        reviewed_by="mgarcia",
    )
    xml = build_xfdf(_set(kept, dropped), "blank.pdf")
    assert 'name="keep"' in xml
    assert 'name="drop"' not in xml


def test_accepted_annotations_are_kept():
    xml = build_xfdf(
        _set(_annot(review_status=ReviewStatus.ACCEPTED, reviewed_by="mgarcia")),
        "blank.pdf",
    )
    assert 'name="a1"' in xml


def test_provenance_survives_in_the_carf_namespace(tmp_path):
    path = write_xfdf(_set(_annot(rationale="because")), tmp_path / "x.xfdf")
    meta = parse_xfdf(path)[0].meta
    assert meta["field_id"] == "DM_USUBJID"
    assert meta["origin"] == "Collected"
    assert meta["review_status"] == "proposed"


def test_source_model_is_recorded_as_the_annotation_author():
    xml = build_xfdf(_set(_annot()), "blank.pdf")
    assert 'title="Copilot 365 chat, manual paste"' in xml


# --- reader ---------------------------------------------------------------


def test_reader_round_trips_the_writer(tmp_path):
    path = write_xfdf(_set(_annot()), tmp_path / "x.xfdf")
    back = parse_xfdf(path)
    assert len(back) == 1
    assert back[0].bbox.as_tuple() == pytest.approx(BOX.as_tuple(), abs=1e-3)
    assert back[0].text == "DM.SUBJID"
    assert back[0].annot_id == "a1"


def test_reader_accepts_a_file_stripped_of_namespaces(tmp_path):
    """Acrobat may hand back XFDF that does not look like what we wrote."""
    path = tmp_path / "bare.xfdf"
    path.write_text(
        '<?xml version="1.0"?>\n'
        "<xfdf><annots>"
        '<freetext page="1" rect="10,20,110,32" name="x1"><contents>VS.VSDTC</contents></freetext>'
        "</annots></xfdf>",
        encoding="utf-8",
    )
    got = parse_xfdf(path)
    assert got[0].page_index == 1 and got[0].text == "VS.VSDTC"


def test_reader_tolerates_whitespace_in_a_hand_edited_rect(tmp_path):
    path = tmp_path / "bare.xfdf"
    path.write_text(
        '<xfdf><annots><freetext page="0" rect="10, 20, 110, 32">'
        "<contents>X</contents></freetext></annots></xfdf>",
        encoding="utf-8",
    )
    assert parse_xfdf(path)[0].bbox.as_tuple() == (10.0, 20.0, 110.0, 32.0)


def test_reader_rejects_a_wrong_root_element(tmp_path):
    path = tmp_path / "bad.xfdf"
    path.write_text("<fdf><annots/></fdf>", encoding="utf-8")
    with pytest.raises(ValueError, match="expected <xfdf>"):
        parse_xfdf(path)


def test_reader_fails_loudly_on_an_annotation_with_no_rect(tmp_path):
    """Silently dropping it would lose an annotation from the submission."""
    path = tmp_path / "bad.xfdf"
    path.write_text(
        '<xfdf><annots><freetext page="0" name="oops">'
        "<contents>X</contents></freetext></annots></xfdf>",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="without a @rect"):
        parse_xfdf(path)


def test_rendering_an_empty_xfdf_fails_rather_than_writing_a_bare_pdf(crfs, tmp_path):
    path = tmp_path / "empty.xfdf"
    path.write_text("<xfdf><annots/></xfdf>", encoding="utf-8")
    with pytest.raises(ValueError, match="no annotations"):
        xfdf_to_pdf(crfs["acroform"], path, tmp_path / "out.pdf")


def test_overflowing_text_is_reported_not_silently_clipped(tmp_path):
    """PyMuPDF truncates FreeText to its rect; a half-annotation must not ship."""
    path = tmp_path / "x.xfdf"
    path.write_text(
        '<xfdf><annots><freetext page="0" rect="10,20,60,32" name="tight">'
        "<contents>DM.USUBJID [retyped by a reviewer in Acrobat]</contents>"
        "</freetext></annots></xfdf>",
        encoding="utf-8",
    )
    over = overflowing_annotations(parse_xfdf(path))
    assert len(over) == 1
    assert over[0][0] == "tight" and over[0][2] > 10


def test_pipeline_sized_annotations_do_not_trip_the_overflow_check(tmp_path):
    """layout.py sizes rects to fit, but @rect is rounded to 3 decimals.

    Without a tolerance every generated annotation reports a 0pt overflow and
    the genuine ones are lost in the noise.
    """
    from pipeline.layout import annotation_size

    text = "VS.VSORRES when VSTESTCD = SYSBP"
    w, h = annotation_size(text)
    annots = _set(
        _annot(domain="VS", variable="VSORRES", condition="VSTESTCD = SYSBP",
               bbox=BBox(x0=100.0, y0=200.0, x1=100.0 + w, y1=200.0 + h))
    )
    path = write_xfdf(annots, tmp_path / "x.xfdf")
    assert overflowing_annotations(parse_xfdf(path)) == []


def test_rendering_fails_when_the_xfdf_names_a_page_the_pdf_lacks(crfs, tmp_path):
    path = tmp_path / "x.xfdf"
    path.write_text(
        '<xfdf><annots><freetext page="9" rect="10,20,110,32">'
        "<contents>X</contents></freetext></annots></xfdf>",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="has 2 pages"):
        xfdf_to_pdf(crfs["acroform"], path, tmp_path / "out.pdf")
