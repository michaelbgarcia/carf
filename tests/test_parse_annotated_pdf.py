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
from pipeline.rows import extract_rows
from pipeline.models import NOT_SUBMITTED_TEXT, AnnotationSet, BBox, PageGeometry
from pipeline.parse_annotated_pdf import (
    RawMark,
    RecoveredMapping,
    attribute_domains,
    match_marks_to_rows,
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
from pipeline.msg import apply_colors, page_color_map
from pipeline.render import (
    draw_annotation,
    draw_legend,
    legend_origin,
    save_with_annotations,
)
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
    draw_annotation(page, BBox(x0=50, y0=700, x1=90, y1=712), "VS", bordered=True)
    draw_annotation(page, BBox(x0=50, y0=650, x1=110, y1=662), "VSORRES when VSTESTCD = SYSBP")
    draw_annotation(page, BBox(x0=50, y0=600, x1=130, y1=612), NOT_SUBMITTED_TEXT, muted=True)
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
    assert marks[NOT_SUBMITTED_TEXT].muted is True
    assert marks[NOT_SUBMITTED_TEXT].boxed is False


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
    rowset = extract_rows(crfs["acroform"])
    row = rowset.rows[0]

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    draw_annotation(page, row.anchor, "DSCAT = PROTOCOL MILESTONE")
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
    draw_annotation(page, BBox(x0=50, y0=600, x1=90, y1=612), "VS", bordered=True)  # below the mark
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
    rowset = extract_rows(crfs["acroform"])
    top = max(r.anchor.y1 for r in rowset.for_page(0))

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    legend_box = BBox(x0=50, y0=top + 20, x1=170, y1=top + 32)
    draw_annotation(page, legend_box, "DS=Disposition")
    out = tmp_path / "legend.pdf"
    save_with_annotations(doc, out)
    doc.close()

    marks = read_marks(out)
    legend_by_page, remaining = split_legend_marks(marks, rowset)
    assert legend_by_page == {0: {"DS": "Disposition"}}
    assert remaining == []


def test_same_shaped_text_in_field_territory_is_not_treated_as_a_legend(crfs, tmp_path):
    """DSCAT = PROTOCOL MILESTONE has the same 'CODE = phrase' shape as a
    legend, but sits among the fields, not above all of them -- position,
    not text shape, is what has to tell the two apart."""
    rowset = extract_rows(crfs["acroform"])
    top = max(r.anchor.y1 for r in rowset.for_page(0))

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    y = top - 100
    below_box = BBox(x0=50, y0=y, x1=250, y1=y + 12)
    draw_annotation(page, below_box, "DSCAT = PROTOCOL MILESTONE")
    out = tmp_path / "not_legend.pdf"
    save_with_annotations(doc, out)
    doc.close()

    marks = read_marks(out)
    legend_by_page, remaining = split_legend_marks(marks, rowset)
    assert legend_by_page == {}
    assert len(remaining) == 1
    assert remaining[0].text == "DSCAT = PROTOCOL MILESTONE"


def test_page_domain_summary_is_derived_from_resolved_mappings_not_legend_text(crfs, tmp_path):
    rowset = extract_rows(crfs["acroform"])
    page0 = rowset.for_page(0)
    f1, f2 = page0[0], page0[1]

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    draw_annotation(page, f1.anchor, "DM.SEX")
    draw_annotation(page, f2.anchor, "DSTERM")  # bare -- resolves to DS via builtin fallback
    out = tmp_path / "two_domains.pdf"
    save_with_annotations(doc, out)
    doc.close()

    mappings = parse_annotated_pdf(out, crfs["acroform"])
    summaries = {m.domain: m for m in mappings if m.synthesized}
    assert set(summaries) == {"DM", "DS"}
    for s in summaries.values():
        assert s.kind == "domain"
        assert s.row_id is None
        assert s.legend_name is None  # no legend was drawn on this page


def test_page_domain_summary_cross_checks_against_the_legend(crfs, tmp_path):
    rowset = extract_rows(crfs["acroform"])
    f1 = rowset.for_page(0)[0]
    top = max(r.anchor.y1 for r in rowset.for_page(0))

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    draw_annotation(page, BBox(x0=50, y0=top + 20, x1=170, y1=top + 32), "DM=Demographics")
    draw_annotation(page, f1.anchor, "DM.SEX")
    out = tmp_path / "legend_crosscheck.pdf"
    save_with_annotations(doc, out)
    doc.close()

    mappings = parse_annotated_pdf(out, crfs["acroform"])
    summary = next(m for m in mappings if m.synthesized and m.domain == "DM")
    assert summary.legend_name == "Demographics"


# --- spatial matching ---------------------------------------------------------


def test_unmatched_mark_has_no_row_id_and_is_excluded_from_the_lookup_table(crfs):
    rowset = extract_rows(crfs["acroform"])
    from pipeline.parse_annotated_pdf import RecoveredMapping

    far_away = RecoveredMapping(
        page_index=0,
        bbox=BBox(x0=0, y0=0, x1=10, y1=10),
        text="DM.SEX",
        kind="variable",
        domain="DM",
        variable="SEX",
    )
    matched = match_marks_to_rows([far_away], rowset, max_distance=5.0)
    assert matched[0].row_id is None
    assert matched[0].match_distance is None
    assert to_lookup_rows(matched) == []


def test_domain_banner_is_never_matched_to_a_row(crfs):
    rowset = extract_rows(crfs["acroform"])
    from pipeline.parse_annotated_pdf import RecoveredMapping

    row = rowset.rows[0]
    banner = RecoveredMapping(
        page_index=row.page_index, bbox=row.anchor, text="DM", kind="domain", domain="DM"
    )
    matched = match_marks_to_rows([banner], rowset)
    assert matched[0].row_id is None


def test_two_marks_can_share_one_row(crfs):
    """One CRF row legitimately carries two SDTM variables.

    A single collected date populating both DM.RFICDTC and DS.DSSTDTC, or the
    AGE / AGEU pair -- which is exactly the anno1/anno2 shape the control sheet
    has room for, so a row must not be retired once matched.
    """
    rowset = extract_rows(crfs["acroform"])
    row = rowset.rows[0]
    page_geom = rowset.pages[row.page_index]

    box1, box2 = layout.place_row(
        row.anchor, ["DM.RFICDTC", "DS.DSSTDTC"], page_geom,
        limit=layout.right_limit(row, page_geom), obstacles=[],
    )

    m1 = RecoveredMapping(
        page_index=row.page_index, bbox=box1, text="DM.RFICDTC",
        kind="variable", domain="DM", variable="RFICDTC",
    )
    m2 = RecoveredMapping(
        page_index=row.page_index, bbox=box2, text="DS.DSSTDTC",
        kind="variable", domain="DS", variable="DSSTDTC",
    )

    matched = match_marks_to_rows([m1, m2], rowset)
    assert matched[0].row_id == row.row_id
    assert matched[1].row_id == row.row_id
    assert {m.variable for m in matched} == {"RFICDTC", "DSSTDTC"}


def test_two_adjacent_rows_each_keep_their_own_mark(crfs):
    """Two nearby rows' marks must not bleed into each other.

    Both marks here are exact reverse-layout alignments (tier 1), which is
    collision-safe by construction. Rows are a full line of leading apart rather
    than the few points that separated adjacent widgets in a grid, so this is a
    weaker requirement than it used to be -- but it is the requirement that
    stopped a dense VS grid resolving to the neighbouring row, so it stays.
    """
    rowset = extract_rows(crfs["acroform"])
    r1, r2 = rowset.for_page(0)[0], rowset.for_page(0)[1]
    page_geom = rowset.pages[0]

    (box1,) = layout.place_row(
        r1.anchor, ["DM.SITEID"], page_geom,
        limit=layout.right_limit(r1, page_geom), obstacles=[],
    )
    (box2,) = layout.place_row(
        r2.anchor, ["DM.USUBJID"], page_geom,
        limit=layout.right_limit(r2, page_geom), obstacles=[box1],
    )

    m1 = RecoveredMapping(page_index=0, bbox=box1, text="DM.SITEID", kind="variable", domain="DM", variable="SITEID")
    m2 = RecoveredMapping(page_index=0, bbox=box2, text="DM.USUBJID", kind="variable", domain="DM", variable="USUBJID")

    matched = match_marks_to_rows([m1, m2], rowset)
    by_variable = {m.variable: m for m in matched}
    assert by_variable["SITEID"].row_id == r1.row_id
    assert by_variable["USUBJID"].row_id == r2.row_id


# --- question row vs its option rows ----------------------------------------


def test_mark_beside_a_question_matches_it_not_the_option_row_below(crfs, tmp_path):
    """The ambiguity that survives the move to rows.

    Under the old field model a Yes/No question needed a *synthetic group* field
    spanning both option widgets, because a mark beside the question was closer
    to the group's union bbox than to either individual checkbox. The row model
    deletes that machinery: the question row already carries the question text,
    and the option below it is a separate row with an empty question half.

    What remains is telling those two rows apart -- the question row and its
    option-only continuation -- which is what this pins down. They sit one line
    of leading apart, so a mark on the question's own baseline is the question's.
    """
    rowset = extract_rows(crfs["acroform"])
    question = next(
        r for r in rowset.rows if r.text_1.startswith("Was this participant a prior")
    )
    # The continuation row: same question, next option, no question text.
    option = next(
        r
        for r in rowset.rows
        if r.page_index == question.page_index
        and not r.text_1
        and r.anchor.y1 < question.anchor.y1
    )
    assert option.text_2, "expected an option-only row under the question"

    page_geom = rowset.pages[question.page_index]
    (box,) = layout.place_row(
        question.anchor,
        ['SUPPDM.QVAL when QNAM="RESCREEN"'],
        page_geom,
        limit=layout.right_limit(question, page_geom),
        obstacles=[],
    )

    # Annotate the real blank CRF's own page directly, so the mark's page_index
    # lines up with the rows it has to compete against -- a throwaway
    # single-page doc would always put the mark on page 0.
    doc = pymupdf.open(crfs["acroform"])
    page = doc[question.page_index]
    draw_annotation(page, box, 'SUPPDM.QVAL when QNAM="RESCREEN"')
    out = tmp_path / "question_row.pdf"
    save_with_annotations(doc, out)
    doc.close()

    mappings = parse_annotated_pdf(out, crfs["acroform"])
    variable = next(m for m in mappings if m.kind == "variable")
    assert variable.row_id == question.row_id, (
        f"matched {variable.row_id} instead of the question row {question.row_id}"
    )
    assert variable.label == question.text_1


# --- grouped (repeating) annotations ----------------------------------------


def test_one_grouped_box_comes_back_as_a_mapping_for_every_row_it_covered(
    crfs, tmp_path
):
    """The reverse of the whole point of grouping.

    Without this, one box covering five rows returns attached to whichever row
    its centre sits nearest, and the corpus learns the mapping for that row's
    text while forgetting it for the other four -- a wrong answer carrying a
    plausible ``match_distance``, which is worse than no answer.
    """
    rowset = extract_rows(crfs["acroform"])
    question = next(
        r for r in rowset.rows if r.text_1.startswith("Was this participant a prior")
    )
    # The question row plus the two rows under it: the question is the widest,
    # which is the ordinary shape of a question-and-options block.
    start = next(i for i, r in enumerate(rowset.rows) if r.row_id == question.row_id)
    block = rowset.rows[start : start + 3]
    assert len({r.page_index for r in block}) == 1
    assert layout.block_anchor(block).x1 == question.anchor.x1

    text = 'SUPPDM.QVAL when QNAM="RESCREEN"'
    (box,) = layout.place_group(layout.block_anchor(block), [text])

    doc = pymupdf.open(crfs["acroform"])
    page = doc[question.page_index]
    draw_annotation(page, box, text)
    out = tmp_path / "grouped.pdf"
    save_with_annotations(doc, out)
    doc.close()

    mappings = [
        m for m in parse_annotated_pdf(out, crfs["acroform"]) if m.kind == "variable"
    ]

    assert [m.row_id for m in mappings] == [r.row_id for r in block]
    # ...and every one of them still says it came from a single drawn box.
    for m in mappings:
        assert m.member_row_ids == [r.row_id for r in block]
        assert m.variable == "SUPPDM.QVAL" or m.text == text


def test_a_single_row_mark_is_not_read_as_a_group(crfs, tmp_path):
    """The false positive that would matter: a group match ranked ahead of an
    exact single-row hit would spread one row's mapping across its neighbours."""
    rowset = extract_rows(crfs["acroform"])
    question = next(
        r for r in rowset.rows if r.text_1.startswith("Was this participant a prior")
    )
    page_geom = rowset.pages[question.page_index]
    (box,) = layout.place_row(
        question.anchor,
        ["RESCREEN"],
        page_geom,
        limit=layout.right_limit(question, page_geom),
        obstacles=[],
    )

    doc = pymupdf.open(crfs["acroform"])
    draw_annotation(doc[question.page_index], box, "RESCREEN")
    out = tmp_path / "single.pdf"
    save_with_annotations(doc, out)
    doc.close()

    variables = [
        m for m in parse_annotated_pdf(out, crfs["acroform"]) if m.kind == "variable"
    ]
    assert len(variables) == 1
    assert variables[0].row_id == question.row_id
    assert variables[0].member_row_ids == []


# --- full-loop round trip: this pipeline's own convention, end to end -------


@pytest.fixture(scope="module")
def rowset(crfs):
    return extract_rows(crfs["acroform"])


@pytest.fixture(scope="module")
def annotated_pdf(crfs, rowset, tmp_path_factory):
    """rows -> batch -> stand-in reply -> ingest -> colour -> layout -> final PDF.

    The MSG legend is drawn, because under MSG styling it is the only thing on the
    page that names a domain: annotations read ``VSORRES``, not ``VS.VSORRES``.
    Omitting it would make this fixture unrepresentative of real output *and*
    quietly stop exercising ``legend_color_map``.
    """
    tmp = tmp_path_factory.mktemp("annotated")
    manifest = write_batches(rowset, tmp)

    collected = []
    for entry in manifest:
        reply = tmp / f"resp-batch{entry['batch']}.csv"
        reply.write_text(build_response(rowset, entry["pages"]), encoding="utf-8")
        collected.extend(ingest_response_file(reply, rowset, entry["pages"]).annotations)

    annots = AnnotationSet(source_pdf=rowset.source_pdf, pages=rowset.pages, annotations=collected)
    annots = apply_colors(annots, rowset)
    placed = layout.place_annotations(annots, rowset, obstacles=layout.text_obstacles(crfs["acroform"]))

    final = tmp / "annotated.pdf"
    doc = pymupdf.open(crfs["acroform"])
    try:
        for a in placed.submittable():
            text = a.display_text()
            if text:
                draw_annotation(
                    doc[a.page_index], a.bbox, text,
                    fill=a.color, dashed=a.kind.value == "note",
                )
        for page_index, colors in page_color_map(placed, rowset).items():
            page = doc[page_index]
            draw_legend(
                page,
                [(f"{code} (Domain {code})", rgb) for code, rgb in colors.items()],
                origin=legend_origin(page),
            )
        save_with_annotations(doc, final)
    finally:
        doc.close()
    return final, placed


def test_every_placed_annotation_is_recovered_and_matched(crfs, rowset, annotated_pdf):
    """The headline claim: point this at this pipeline's own output and it round-trips.

    Compared against the annotations that were actually *drawn*. An annotation
    with no text -- a row the stand-in reply had no mapping for -- leaves no mark
    on the page, so there is nothing for recovery to find and nothing it should
    invent.
    """
    final, placed = annotated_pdf
    mappings = parse_annotated_pdf(final, crfs["acroform"])

    drawn = [a for a in placed.annotations if a.display_text()]
    non_domain = [m for m in mappings if m.kind != "domain"]
    assert len(non_domain) == len(drawn)
    unmatched = [m for m in non_domain if m.row_id is None]
    assert unmatched == [], f"expected every mark to match a row, got unmatched: {unmatched}"


def test_recovered_mappings_agree_with_the_originals(crfs, rowset, annotated_pdf):
    """Keyed on (row_id, variable), not row_id alone.

    One row legitimately carries two variables -- AGE and AGEU on the same
    printed line -- so a row_id-keyed dict would silently drop one of them and
    the test would then pass by comparing an annotation against itself.
    """
    final, placed = annotated_pdf
    mappings = parse_annotated_pdf(final, crfs["acroform"])
    recovered = {
        (m.row_id, m.variable): m for m in mappings if m.row_id is not None
    }

    checked = 0
    for original in placed.annotations:
        if not original.display_text():
            continue  # never drawn, so nothing to recover
        got = recovered.get((original.row_id, original.variable))
        assert got is not None, (
            f"{original.row_id}/{original.variable} was not matched to any mark"
        )
        # The domain is recovered from the page legend's colour key, not from the
        # mark's own text -- MSG annotations do not carry a domain prefix.
        assert got.domain == original.domain, (
            f"{original.row_id}: domain {got.domain!r} != {original.domain!r}"
        )
        assert got.condition == original.condition
        checked += 1
    assert checked == len([a for a in placed.annotations if a.display_text()])


def test_not_submitted_fields_recover_as_muted_notes_with_no_variable(crfs, annotated_pdf):
    final, placed = annotated_pdf
    mappings = parse_annotated_pdf(final, crfs["acroform"])

    notes = [m for m in mappings if m.text.upper() == NOT_SUBMITTED_TEXT.upper()]
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
    matched_variable = [m for m in mappings if m.kind == "variable" and m.row_id is not None]
    assert len(rows) == len(matched_variable)
    assert rows[0]["label"]  # every matched field has a caption in this fixture


# --- full diagnostic report ---------------------------------------------------


def test_report_has_one_row_per_mark_including_unmatched(crfs):
    """Unlike the lookup table, the report keeps a row even when matching fails."""
    rowset = extract_rows(crfs["acroform"])
    from pipeline.parse_annotated_pdf import RecoveredMapping

    unmatched = RecoveredMapping(
        page_index=0,
        bbox=BBox(x0=0, y0=0, x1=10, y1=10),
        text="DM.SEX",
        kind="variable",
        domain="DM",
        variable="SEX",
    )
    matched = match_marks_to_rows([unmatched], rowset, max_distance=5.0)
    rows = to_report_rows(matched)
    assert len(rows) == 1
    assert rows[0]["row_id"] == ""
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
    # describe the page as a whole and never carry a row_id by design.
    non_summary = [r for r in rows if r["synthesized"] != "True"]
    assert non_summary, "fixture should contain at least one non-summary row"
    assert all(r["row_id"] for r in non_summary)
