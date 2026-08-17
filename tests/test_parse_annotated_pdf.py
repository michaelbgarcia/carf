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
    RecoveredMapping,
    attribute_domains,
    match_marks_to_fields,
    parse_annotated_pdf,
    parse_mapping_text,
    read_marks,
    split_legend_marks,
    to_lookup_rows,
    to_report_rows,
    write_lookup_csv,
    write_report_csv,
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
        ("DM.SEX", ("DM", "SEX", None, None)),
        ("VS.VSORRES when VSTESTCD = SYSBP", ("VS", "VSORRES", "VSTESTCD = SYSBP", None)),
        ("SEX", (None, "SEX", None, None)),
        ("[Not Submitted]", (None, None, None, None)),
        ("", (None, None, None, None)),
        # Fixed-value assignment ("=", no "when") -- distinct from a condition.
        ('DSTERM="INFORMED CONSENT OBTAINED"', (None, "DSTERM", None, "INFORMED CONSENT OBTAINED")),
        ("DSCAT = PROTOCOL MILESTONE", (None, "DSCAT", None, "PROTOCOL MILESTONE")),
        ('DS.DSCAT="PROTOCOL MILESTONE"', ("DS", "DSCAT", None, "PROTOCOL MILESTONE")),
        # SUPPQUAL-style QNAM condition, unaffected by the new "=" branch.
        ('SUPPDS.QVAL when QNAM="ICSAMGBR"', ("SUPPDS", "QVAL", 'QNAM="ICSAMGBR"', None)),
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


# --- fixed-value assignment ("=", not "when") --------------------------------


def test_fixed_value_mark_is_recovered_and_exported(crfs, tmp_path):
    """DSCAT = PROTOCOL MILESTONE -- a constant, not a row-selecting condition."""
    fieldset = extract_fields(crfs["acroform"])
    field = fieldset.fields[0]

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    draw_annotation(page, field.bbox, "DSCAT = PROTOCOL MILESTONE")
    out = tmp_path / "fixed_value.pdf"
    save_with_annotations(doc, out)
    doc.close()

    mappings = parse_annotated_pdf(out, crfs["acroform"])
    variable = next(m for m in mappings if m.kind == "variable")
    assert variable.variable == "DSCAT"
    assert variable.fixed_value == "PROTOCOL MILESTONE"
    assert variable.condition is None

    lookup = to_lookup_rows(mappings)
    assert lookup and lookup[0]["fixed_value"] == "PROTOCOL MILESTONE"
    report = to_report_rows(mappings)
    assert any(r["fixed_value"] == "PROTOCOL MILESTONE" for r in report)


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
    """A banner has to sit at or above a mark to govern it.

    Uses a variable name with no CDISC-standard domain-prefix meaning
    ("RESULT", not "VSORRES") so the built-in constants fallback tier can't
    independently resolve the domain and mask what this test is actually
    checking: banner position.
    """
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    draw_annotation(page, BBox(x0=50, y0=650, x1=110, y1=662), "RESULT")
    draw_annotation(page, BBox(x0=50, y0=600, x1=90, y1=612), "VS", boxed=True)  # below the mark
    out = tmp_path / "below.pdf"
    save_with_annotations(doc, out)
    doc.close()

    from pipeline.parse_annotated_pdf import _mark_to_mapping

    mappings = attribute_domains([_mark_to_mapping(m) for m in read_marks(out)])
    variable = next(m for m in mappings if m.kind == "variable")
    assert variable.domain is None
    assert variable.domain_inferred is False
    assert variable.domain_inference_source is None


def test_domain_falls_back_to_builtin_constants_with_no_banner(tmp_path):
    """DSSTDTC has no banner nearby, but its prefix names its own domain."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    draw_annotation(page, BBox(x0=50, y0=650, x1=110, y1=662), "DSSTDTC")
    out = tmp_path / "builtin.pdf"
    save_with_annotations(doc, out)
    doc.close()

    from pipeline.parse_annotated_pdf import _mark_to_mapping

    mappings = attribute_domains([_mark_to_mapping(m) for m in read_marks(out)])
    variable = next(m for m in mappings if m.kind == "variable")
    assert variable.domain == "DS"
    assert variable.domain_inferred is True
    assert variable.domain_inference_source == "builtin"


def test_domain_falls_back_to_mined_precedent_when_nothing_else_resolves_it(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    draw_annotation(page, BBox(x0=50, y0=650, x1=110, y1=662), "QVAL")
    out = tmp_path / "precedent.pdf"
    save_with_annotations(doc, out)
    doc.close()

    from pipeline.parse_annotated_pdf import _mark_to_mapping

    marks = [_mark_to_mapping(m) for m in read_marks(out)]

    without_precedent = attribute_domains(marks)
    variable = next(m for m in without_precedent if m.kind == "variable")
    assert variable.domain is None

    with_precedent = attribute_domains(marks, precedent={"QVAL": "SUPPDS"})
    variable = next(m for m in with_precedent if m.kind == "variable")
    assert variable.domain == "SUPPDS"
    assert variable.domain_inferred is True
    assert variable.domain_inference_source == "precedent"


def test_banner_wins_over_a_conflicting_precedent_entry(hand_built_pdf):
    marks = read_marks(hand_built_pdf)
    from pipeline.parse_annotated_pdf import _mark_to_mapping

    mappings = attribute_domains(
        [_mark_to_mapping(m) for m in marks], precedent={"VSORRES": "XX"}
    )
    variable = next(m for m in mappings if m.kind == "variable")
    assert variable.domain == "VS"
    assert variable.domain_inference_source == "banner"


# --- page-level domain legend -------------------------------------------------


def test_page_legend_is_excluded_from_field_mappings(crfs, tmp_path):
    fieldset = extract_fields(crfs["acroform"])
    top = max(f.bbox.y1 for f in fieldset.for_page(0))

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    legend_box = BBox(x0=50, y0=top + 20, x1=170, y1=top + 32)
    draw_annotation(page, legend_box, "DS=Disposition")
    out = tmp_path / "legend.pdf"
    save_with_annotations(doc, out)
    doc.close()

    marks = read_marks(out)
    legend_by_page, remaining = split_legend_marks(marks, fieldset)
    assert legend_by_page == {0: {"DS": "Disposition"}}
    assert remaining == []


def test_same_shaped_text_in_field_territory_is_not_treated_as_a_legend(crfs, tmp_path):
    """DSCAT = PROTOCOL MILESTONE has the same 'CODE = phrase' shape as a
    legend, but sits among the fields, not above all of them -- position,
    not text shape, is what has to tell the two apart."""
    fieldset = extract_fields(crfs["acroform"])
    top = max(f.bbox.y1 for f in fieldset.for_page(0))

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    y = top - 100
    below_box = BBox(x0=50, y0=y, x1=250, y1=y + 12)
    draw_annotation(page, below_box, "DSCAT = PROTOCOL MILESTONE")
    out = tmp_path / "not_legend.pdf"
    save_with_annotations(doc, out)
    doc.close()

    marks = read_marks(out)
    legend_by_page, remaining = split_legend_marks(marks, fieldset)
    assert legend_by_page == {}
    assert len(remaining) == 1
    assert remaining[0].text == "DSCAT = PROTOCOL MILESTONE"


def test_page_domain_summary_is_derived_from_resolved_mappings_not_legend_text(crfs, tmp_path):
    fieldset = extract_fields(crfs["acroform"])
    page0 = fieldset.for_page(0)
    f1, f2 = page0[0], page0[1]

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    draw_annotation(page, f1.bbox, "DM.SEX")
    draw_annotation(page, f2.bbox, "DSTERM")  # bare -- resolves to DS via builtin fallback
    out = tmp_path / "two_domains.pdf"
    save_with_annotations(doc, out)
    doc.close()

    mappings = parse_annotated_pdf(out, crfs["acroform"])
    summaries = {m.domain: m for m in mappings if m.synthesized}
    assert set(summaries) == {"DM", "DS"}
    for s in summaries.values():
        assert s.kind == "domain"
        assert s.field_id is None
        assert s.legend_name is None  # no legend was drawn on this page


def test_page_domain_summary_cross_checks_against_the_legend(crfs, tmp_path):
    fieldset = extract_fields(crfs["acroform"])
    f1 = fieldset.for_page(0)[0]
    top = max(f.bbox.y1 for f in fieldset.for_page(0))

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    draw_annotation(page, BBox(x0=50, y0=top + 20, x1=170, y1=top + 32), "DM=Demographics")
    draw_annotation(page, f1.bbox, "DM.SEX")
    out = tmp_path / "legend_crosscheck.pdf"
    save_with_annotations(doc, out)
    doc.close()

    mappings = parse_annotated_pdf(out, crfs["acroform"])
    summary = next(m for m in mappings if m.synthesized and m.domain == "DM")
    assert summary.legend_name == "Demographics"


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


def test_two_marks_can_share_one_field(crfs):
    """One collected date can populate two SDTM variables in two domains --
    used_fields exclusivity used to forbid this; removing it should not."""
    fieldset = extract_fields(crfs["acroform"])
    field = fieldset.fields[0]
    page_geom = fieldset.pages[field.page_index]

    box1 = layout.place_one(field.bbox, "DM.RFICDTC", page_geom, obstacles=[])
    box2 = layout.place_one(field.bbox, "DS.DSSTDTC", page_geom, obstacles=[box1])

    m1 = RecoveredMapping(
        page_index=field.page_index, bbox=box1, text="DM.RFICDTC",
        kind="variable", domain="DM", variable="RFICDTC",
    )
    m2 = RecoveredMapping(
        page_index=field.page_index, bbox=box2, text="DS.DSSTDTC",
        kind="variable", domain="DS", variable="DSSTDTC",
    )

    matched = match_marks_to_fields([m1, m2], fieldset)
    assert matched[0].field_id == field.field_id
    assert matched[1].field_id == field.field_id
    assert {m.variable for m in matched} == {"RFICDTC", "DSSTDTC"}


def test_two_adjacent_fields_each_keep_their_own_mark(crfs):
    """Dense-grid regression guard: removing used_fields must not let two
    distinct nearby fields' marks bleed into each other. Both marks here are
    exact reverse-layout alignments (tier 1), which is collision-safe by
    construction -- this pins that down directly rather than relying only on
    the full round-trip fixture below."""
    fieldset = extract_fields(crfs["acroform"])
    f1, f2 = fieldset.for_page(0)[0], fieldset.for_page(0)[1]
    page_geom = fieldset.pages[0]

    box1 = layout.place_one(f1.bbox, "DM.SITEID", page_geom, obstacles=[])
    box2 = layout.place_one(f2.bbox, "DM.USUBJID", page_geom, obstacles=[box1])

    m1 = RecoveredMapping(page_index=0, bbox=box1, text="DM.SITEID", kind="variable", domain="DM", variable="SITEID")
    m2 = RecoveredMapping(page_index=0, bbox=box2, text="DM.USUBJID", kind="variable", domain="DM", variable="USUBJID")

    matched = match_marks_to_fields([m1, m2], fieldset)
    by_variable = {m.variable: m for m in matched}
    assert by_variable["SITEID"].field_id == f1.field_id
    assert by_variable["USUBJID"].field_id == f2.field_id


# --- checkbox-group matching --------------------------------------------------


def test_mark_beside_a_checkbox_row_matches_the_whole_group_not_one_option(crfs, tmp_path):
    """DM_SEX_M/DM_SEX_F -- a 2-member row -- rather than the 3-member VS
    Position row: an odd-count, evenly-spaced row's middle option sits at
    exactly the same centroid as the row's own union bbox, which would tie
    (not merely resolve) the very distance comparison this test checks."""
    fieldset = extract_fields(crfs["acroform"])
    members = [f for f in fieldset.fields if f.field_id in ("DM_SEX_M", "DM_SEX_F")]
    assert len(members) == 2
    group_ids = {f.group_id for f in members}
    assert len(group_ids) == 1 and None not in group_ids
    group_id = members[0].group_id

    x0 = min(f.bbox.x0 for f in members)
    x1 = max(f.bbox.x1 for f in members)
    y1 = max(f.bbox.y1 for f in members)
    mid_x = (x0 + x1) / 2.0

    # Annotate the real blank CRF's own page directly, so the mark's
    # page_index lines up with the fields it needs to compete against -- a
    # throwaway single-page doc would always put the mark on page 0.
    doc = pymupdf.open(crfs["acroform"])
    page = doc[members[0].page_index]
    # Centered above the row's midpoint, which coincides with neither
    # option's own centroid -- unambiguously closer to the group.
    mark_bbox = BBox(x0=mid_x - 25, y0=y1 + 6, x1=mid_x + 25, y1=y1 + 16)
    draw_annotation(page, mark_bbox, 'SUPPDM.QVAL when QNAM="SEX"')
    out = tmp_path / "checkbox_group.pdf"
    save_with_annotations(doc, out)
    doc.close()

    mappings = parse_annotated_pdf(out, crfs["acroform"])
    variable = next(m for m in mappings if m.kind == "variable")
    assert variable.field_id == f"grp_{group_id}"


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


# --- full diagnostic report ---------------------------------------------------


def test_report_has_one_row_per_mark_including_unmatched(crfs):
    """Unlike the lookup table, the report keeps a row even when matching fails."""
    fieldset = extract_fields(crfs["acroform"])
    from pipeline.parse_annotated_pdf import RecoveredMapping

    unmatched = RecoveredMapping(
        page_index=0,
        bbox=BBox(x0=0, y0=0, x1=10, y1=10),
        text="DM.SEX",
        kind="variable",
        domain="DM",
        variable="SEX",
    )
    matched = match_marks_to_fields([unmatched], fieldset, max_distance=5.0)
    rows = to_report_rows(matched)
    assert len(rows) == 1
    assert rows[0]["field_id"] == ""
    assert rows[0]["domain"] == "DM"
    assert rows[0]["variable"] == "SEX"


def test_report_csv_has_a_row_for_every_recovered_mark(crfs, annotated_pdf, tmp_path):
    final, placed = annotated_pdf
    mappings = parse_annotated_pdf(final, crfs["acroform"])
    out = write_report_csv(mappings, tmp_path / "report.csv")

    import csv as csv_module

    with out.open(encoding="utf-8") as fh:
        rows = list(csv_module.DictReader(fh))
    assert len(rows) == len(mappings)
    # Every *real* mark in this fixture matches, so the report should show no
    # gaps there -- but summarize_page_domains' derived page-level summaries
    # describe the page as a whole and never carry a field_id by design.
    non_summary = [r for r in rows if r["synthesized"] != "True"]
    assert non_summary, "fixture should contain at least one non-summary row"
    assert all(r["field_id"] for r in non_summary)
