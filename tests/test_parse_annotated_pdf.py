"""Recovering mappings from an already-annotated CRF, the reverse direction.

The strongest evidence this can work is a full-loop round trip: run the real
pipeline all the way to a finished PDF, then run parse_annotated_pdf.py on
that PDF and confirm every mapping comes back the way it went in. That proves
the geometry and text recovery are both sound *for this pipeline's own aCRF
convention* -- it says nothing about a third-party aCRF drawn a different
way, which is why the hand-built cases below exercise the boxed/muted
classification directly rather than relying on the fixture ever producing one.
"""

from __future__ import annotations

import pymupdf
import pytest

from pipeline import layout, xfdf
from pipeline.extract import extract_fields
from pipeline.models import AnnotationSet, BBox, PageGeometry
from pipeline.parse_annotated_pdf import (
    RawMark,
    attribute_domains,
    match_marks_to_fields,
    parse_annotated_pdf,
    parse_mapping_text,
    read_marks,
    to_lookup_rows,
    write_lookup_csv,
)
from pipeline.parse_response import ingest_response_file
from pipeline.prompt import write_batches
from pipeline.render import draw_annotation, save_with_annotations
from pipeline.xfdf_to_pdf import xfdf_to_pdf
from standin_response import build_response


# --- parse_mapping_text -----------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("DM.SEX", ("DM", "SEX", None)),
        ("VS.VSORRES when VSTESTCD = SYSBP", ("VS", "VSORRES", "VSTESTCD = SYSBP")),
        ("SEX", (None, "SEX", None)),
        ("[Not Submitted]", (None, None, None)),
        ("", (None, None, None)),
    ],
)
def test_parse_mapping_text(text, expected):
    assert parse_mapping_text(text) == expected


# --- reading marks off a hand-built PDF -------------------------------------


@pytest.fixture
def hand_built_pdf(tmp_path):
    """One page with a domain banner, a plain variable mark, and a muted note mark."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    draw_annotation(page, BBox(x0=50, y0=700, x1=90, y1=712), "VS", boxed=True)
    draw_annotation(page, BBox(x0=50, y0=650, x1=110, y1=662), "VSORRES when VSTESTCD = SYSBP")
    draw_annotation(page, BBox(x0=50, y0=600, x1=130, y1=612), "[Not Submitted]", muted=True)
    out = tmp_path / "hand_built.pdf"
    save_with_annotations(doc, out)
    doc.close()
    return out


def test_read_marks_classifies_boxed_and_muted(hand_built_pdf):
    marks = {m.text: m for m in read_marks(hand_built_pdf)}
    assert marks["VS"].boxed is True
    assert marks["VS"].muted is False
    assert marks["VSORRES when VSTESTCD = SYSBP"].boxed is False
    assert marks["VSORRES when VSTESTCD = SYSBP"].muted is False
    assert marks["[Not Submitted]"].muted is True
    assert marks["[Not Submitted]"].boxed is False


def test_read_marks_skips_empty_annotations(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    draw_annotation(page, BBox(x0=50, y0=700, x1=90, y1=712), "")
    out = tmp_path / "empty.pdf"
    save_with_annotations(doc, out)
    doc.close()
    assert read_marks(out) == []


# --- domain attribution ------------------------------------------------------


def test_variable_mark_inherits_domain_from_banner_above_it(hand_built_pdf):
    marks = read_marks(hand_built_pdf)
    from pipeline.parse_annotated_pdf import _mark_to_mapping  # internal, tested directly

    mappings = attribute_domains([_mark_to_mapping(m) for m in marks])
    variable = next(m for m in mappings if m.kind == "variable")
    assert variable.domain == "VS"
    assert variable.domain_inferred is True
    assert variable.variable == "VSORRES"
    assert variable.condition == "VSTESTCD = SYSBP"


def test_domain_attribution_ignores_a_banner_below_the_mark(tmp_path):
    """A banner has to sit at or above a mark to govern it."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    draw_annotation(page, BBox(x0=50, y0=650, x1=110, y1=662), "VSORRES")
    draw_annotation(page, BBox(x0=50, y0=600, x1=90, y1=612), "VS", boxed=True)  # below the mark
    out = tmp_path / "below.pdf"
    save_with_annotations(doc, out)
    doc.close()

    from pipeline.parse_annotated_pdf import _mark_to_mapping

    mappings = attribute_domains([_mark_to_mapping(m) for m in read_marks(out)])
    variable = next(m for m in mappings if m.kind == "variable")
    assert variable.domain is None
    assert variable.domain_inferred is False


# --- spatial matching ---------------------------------------------------------


def test_unmatched_mark_has_no_field_id_and_is_excluded_from_the_lookup_table(crfs):
    fieldset = extract_fields(crfs["acroform"])
    from pipeline.parse_annotated_pdf import RecoveredMapping

    far_away = RecoveredMapping(
        page_index=0,
        bbox=BBox(x0=0, y0=0, x1=10, y1=10),
        text="DM.SEX",
        kind="variable",
        domain="DM",
        variable="SEX",
    )
    matched = match_marks_to_fields([far_away], fieldset, max_distance=5.0)
    assert matched[0].field_id is None
    assert matched[0].match_distance is None
    assert to_lookup_rows(matched) == []


def test_domain_banner_is_never_matched_to_a_field(crfs):
    fieldset = extract_fields(crfs["acroform"])
    from pipeline.parse_annotated_pdf import RecoveredMapping

    field = fieldset.fields[0]
    banner = RecoveredMapping(
        page_index=field.page_index, bbox=field.bbox, text="DM", kind="domain", domain="DM"
    )
    matched = match_marks_to_fields([banner], fieldset)
    assert matched[0].field_id is None


# --- full-loop round trip: this pipeline's own convention, end to end -------


@pytest.fixture(scope="module")
def fieldset(crfs):
    return extract_fields(crfs["acroform"])


@pytest.fixture(scope="module")
def annotated_pdf(crfs, fieldset, tmp_path_factory):
    """extract -> batch -> stand-in reply -> ingest -> layout -> xfdf -> final PDF."""
    tmp = tmp_path_factory.mktemp("annotated")
    manifest = write_batches(fieldset, tmp)

    collected = []
    for entry in manifest:
        reply = tmp / f"resp-batch{entry['batch']}.csv"
        reply.write_text(build_response(fieldset, entry["pages"]), encoding="utf-8")
        collected.extend(ingest_response_file(reply, fieldset, entry["pages"]).annotations)

    annots = AnnotationSet(source_pdf=fieldset.source_pdf, pages=fieldset.pages, annotations=collected)
    placed = layout.place_annotations(annots, fieldset, obstacles=layout.text_obstacles(crfs["acroform"]))
    xfdf_path = xfdf.write_xfdf(placed, tmp / "blankcrf.xfdf")
    final = xfdf_to_pdf(crfs["acroform"], xfdf_path, tmp / "annotated.pdf")
    return final, placed


def test_every_placed_annotation_is_recovered_and_matched(crfs, fieldset, annotated_pdf):
    """The headline claim: point this at this pipeline's own output and it round-trips."""
    final, placed = annotated_pdf
    mappings = parse_annotated_pdf(final, crfs["acroform"])

    non_domain = [m for m in mappings if m.kind != "domain"]
    assert len(non_domain) == len(placed.annotations)
    unmatched = [m for m in non_domain if m.field_id is None]
    assert unmatched == [], f"expected every mark to match a field, got unmatched: {unmatched}"


def test_recovered_mappings_agree_with_the_originals(crfs, fieldset, annotated_pdf):
    final, placed = annotated_pdf
    mappings = parse_annotated_pdf(final, crfs["acroform"])
    recovered_by_field = {m.field_id: m for m in mappings if m.field_id is not None}

    checked = 0
    for original in placed.annotations:
        recovered = recovered_by_field.get(original.field_id)
        assert recovered is not None, f"{original.field_id} was not matched to any mark"
        assert recovered.domain == original.domain
        assert recovered.variable == original.variable
        assert recovered.condition == original.condition
        checked += 1
    assert checked == len(placed.annotations)


def test_not_submitted_fields_recover_as_muted_notes_with_no_variable(crfs, annotated_pdf):
    final, placed = annotated_pdf
    mappings = parse_annotated_pdf(final, crfs["acroform"])

    from pipeline.parse_annotated_pdf import NOT_SUBMITTED_TEXT

    notes = [m for m in mappings if m.text == NOT_SUBMITTED_TEXT]
    assert notes, "fixture should contain at least one not-submitted field"
    for n in notes:
        assert n.kind == "note"
        assert n.domain is None
        assert n.variable is None


def test_lookup_csv_round_trips_through_a_file(crfs, annotated_pdf, tmp_path):
    final, placed = annotated_pdf
    mappings = parse_annotated_pdf(final, crfs["acroform"])
    out = write_lookup_csv(mappings, tmp_path / "lookup.csv")

    import csv as csv_module

    with out.open(encoding="utf-8") as fh:
        rows = list(csv_module.DictReader(fh))
    matched_variable = [m for m in mappings if m.kind == "variable" and m.field_id is not None]
    assert len(rows) == len(matched_variable)
    assert rows[0]["label"]  # every matched field has a caption in this fixture
