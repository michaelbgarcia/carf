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
from pipeline.models import NOT_SUBMITTED_TEXT, AnnotationSet, BBox, PageGeometry, RowSet
from pipeline.parse_annotated_pdf import (
    POSITION_COLUMNS,
    REPORT_COLUMNS,
    STYLE_COLUMNS,
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


# --- position and styling, recorded rather than interpreted -------------------


@pytest.fixture
def styled_pdf(tmp_path):
    """One page carrying every styling shape this module has to read back.

    Includes a mark whose print flag is cleared and one whose /DA states its
    colour in grayscale rather than RGB -- neither is something this pipeline
    writes, and both are things a third-party aCRF does.
    """
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    draw_annotation(page, BBox(x0=50, y0=700, x1=140, y1=712), "MAPPED", fill=(191, 255, 255))
    draw_annotation(page, BBox(x0=50, y0=650, x1=140, y1=662), "a comment", dashed=True)
    gray = draw_annotation(page, BBox(x0=50, y0=600, x1=140, y1=612), "GRAYNOTE")
    doc.xref_set_key(gray.xref, "DA", "(0.5 g /Helv 7 Tf)")
    unprinted = draw_annotation(page, BBox(x0=50, y0=550, x1=140, y1=562), "INVISIBLE", fill=(191, 255, 255))
    unprinted.set_flags(0)
    unprinted.update()

    out = tmp_path / "styled.pdf"
    save_with_annotations(doc, out)
    doc.close()
    return out


def _by_text(marks):
    return {m.text: m for m in marks}


def test_styling_is_recovered_from_the_annotation_layer(styled_pdf):
    """Font, size, colours and border style all come back off a drawn annotation."""
    marks = _by_text(read_marks(styled_pdf))

    mapped = marks["MAPPED"].style
    assert (mapped.font, mapped.font_size) == ("Helv", layout.FONT_SIZE)
    assert mapped.text_color == (0.0, 0.0, 0.0)  # MSG: black text on the fill
    assert (mapped.border_style, mapped.border_width) == ("solid", 1.0)
    assert not mapped.bold and not mapped.italic
    assert (mapped.opacity, mapped.rotation, mapped.subtype) == (1.0, 0, "FreeText")
    # The searchability MSG depends on: text in /Contents, not a rich-text stream.
    assert not mapped.rich_text

    # MSG's only mapping-versus-comment distinction is the border style.
    assert marks["a comment"].style.border_style == "dashed"
    assert marks["GRAYNOTE"].style.border_style == "none"


def test_a_grayscale_da_states_a_colour_like_an_rgb_one(styled_pdf):
    """``0.5 g`` is a colour, not an absence of one -- and so still reads as a note.

    A third-party aCRF is under no obligation to have written its /DA in RGB,
    and a note whose grey went unread would classify as a mapping.
    """
    gray = _by_text(read_marks(styled_pdf))["GRAYNOTE"]
    assert gray.style.text_color == (0.5, 0.5, 0.5)
    assert gray.muted


def test_a_mark_that_does_not_print_is_reported_as_such(styled_pdf):
    """An annotation with the print flag clear is absent from the submitted document.

    It looks correct in a viewer, which is exactly why it has to be a column
    rather than something a reviewer is expected to notice.
    """
    marks = _by_text(read_marks(styled_pdf))
    assert marks["INVISIBLE"].style.printable is False
    assert marks["MAPPED"].style.printable is True


def test_offsets_invert_place_row_for_this_pipelines_own_output(crfs, annotated_pdf):
    """Every recovered mark reports the placement ``layout.place_row`` gave it.

    The point of the relative columns: whatever the study, the page size or the
    question's length, a mark this pipeline drew sits ``GAP`` past the question
    text on its own baseline. The half-border inflation of the stored /Rect is
    what makes those numbers 3.5 and -0.5 rather than 4.0 and 0.0 -- see
    ``_position_cells``; it is measured on the rect the PDF actually holds.
    """
    final, _placed = annotated_pdf
    mappings = parse_annotated_pdf(final, crfs["acroform"])
    matched = [m for m in mappings if m.row_id is not None]
    assert matched

    assert {m.placement for m in matched} <= {"slot1", "slot2", "wrap", "group"}
    slot1 = [m for m in matched if m.placement == "slot1"]
    assert slot1
    for m in slot1:
        assert m.dx_from_text == pytest.approx(layout.GAP - layout.BORDER_INFLATION, abs=0.05)
        assert m.dy_from_text == pytest.approx(-layout.BORDER_INFLATION, abs=0.05)
        assert m.wrap_lines == 0


def test_page_frame_is_attached_to_matched_and_unmatched_marks_alike(crfs, annotated_pdf):
    """An unmatched mark has no row to measure against but is still *somewhere*."""
    final, _placed = annotated_pdf
    mappings = parse_annotated_pdf(final, crfs["acroform"])
    real = [m for m in mappings if not m.synthesized]
    assert real
    for m in real:
        assert (m.page_width, m.page_height) == (612.0, 792.0)


def test_a_synthesized_page_domain_row_claims_no_position_or_styling(crfs, annotated_pdf):
    """It borrows a representative mark's bbox; reporting that would be a fabrication."""
    final, _placed = annotated_pdf
    rows = to_report_rows(parse_annotated_pdf(final, crfs["acroform"]))
    derived = [r for r in rows if r["synthesized"]]
    assert derived
    for row in derived:
        assert all(row[c] == "" for c in STYLE_COLUMNS)
        assert all(row[c] == "" for c in POSITION_COLUMNS if not c.startswith("page_"))
        assert row["page_width"] == 612.0  # the page frame is still a fact


def test_report_rows_carry_exactly_the_declared_columns(crfs, annotated_pdf):
    """A column added to one of the three lists and not the other is a silent hole."""
    final, _placed = annotated_pdf
    rows = to_report_rows(parse_annotated_pdf(final, crfs["acroform"]))
    assert rows
    for row in rows:
        assert list(row) == REPORT_COLUMNS


# --- labels are the form's text, never a mark's -------------------------------


def _strip_bands(rows: RowSet) -> RowSet:
    """The same page with no ruled lines -- i.e. the nearest-centroid path alone."""
    return RowSet(
        source_pdf=rows.source_pdf,
        pages=rows.pages,
        rows=[r.model_copy(update={"band": None}) for r in rows.rows],
    )


def test_labels_are_identical_with_and_without_the_blank_counterpart(crfs, annotated_pdf):
    """The headline invariant, and the one that was broken.

    ``get_text`` returns a page's annotation text mixed into its printed words,
    so reading rows off the annotated PDF itself used to produce labels like
    ``Year of Birth (yyyy) BRTHDTC`` -- the question with its own answer glued
    on, which then keyed every lookup row derived from it. Matching the
    blank-counterpart result exactly is the strongest available statement that
    the annotation layer is no longer being read as form text.
    """
    final, _placed = annotated_pdf
    with_blank = parse_annotated_pdf(final, crfs["acroform"])
    without_blank = parse_annotated_pdf(final)

    keyed = lambda ms: sorted((m.page_index, m.text, m.label or "") for m in ms)
    assert keyed(without_blank) == keyed(with_blank)
    assert any(m.label for m in without_blank)


def test_no_recovered_label_contains_the_mark_that_annotates_it(crfs, annotated_pdf):
    """Stated directly, in case the comparison above ever agrees for a worse reason."""
    final, _placed = annotated_pdf
    for m in parse_annotated_pdf(final):
        if not m.label:
            continue
        assert NOT_SUBMITTED_TEXT not in m.label
        if m.variable:
            assert m.variable not in m.label.split()
        assert m.variable is None or not m.label.endswith(m.variable)


def test_context_does_not_pick_up_a_mark_in_the_response_column(crfs, annotated_pdf):
    """``context`` carries the row's response text, which is a label too."""
    final, _placed = annotated_pdf
    for m in parse_annotated_pdf(final):
        assert NOT_SUBMITTED_TEXT not in (m.context or "")


def test_extraction_masks_the_annotation_layer_it_was_handed(crfs, annotated_pdf):
    """The count is the evidence: a "blank" reporting nonzero is not blank."""
    final, _placed = annotated_pdf
    assert all(p.masked_annotations == 0 for p in extract_rows(crfs["acroform"]).pages)
    assert sum(p.masked_annotations for p in extract_rows(final).pages) > 0


# --- the ruled line as a matching tier ---------------------------------------


def test_a_mark_on_a_rows_ruled_line_beats_a_nearer_neighbouring_row(crfs):
    """A mark written far right of its question still belongs to that question.

    The shape a human annotator produces and ``layout.place_row`` never does, so
    no exact alignment can be reconstructed and the old code fell to
    nearest-centroid -- which picks the option row on the line above. The band
    settles it by containment.
    """
    rows = extract_rows(crfs["acroform"])
    page_rows = [r for r in rows.rows if r.page_index == 0]
    target = next(r for r in page_rows if r.text_1.startswith("If Yes, please provide"))

    mark = BBox(
        x0=target.anchor.x1 + 150,
        y0=target.band.y0 + 1.0,
        x1=target.anchor.x1 + 210,
        y1=target.band.y0 + 11.0,
    )
    m = RecoveredMapping(page_index=0, bbox=mark, text="DM.SEX", kind="variable")

    matched = match_marks_to_rows([m], rows)[0]
    assert (matched.row_id, matched.placement) == (target.row_id, "band")

    # And that this is a change, not a coincidence: without the rules, the same
    # mark lands on the row above.
    fallback = match_marks_to_rows([m], _strip_bands(rows))[0]
    assert fallback.placement == "nearest"
    assert fallback.row_id != target.row_id


def test_a_wrapped_mark_belongs_to_its_own_row_not_the_band_it_sits_in(crfs):
    """Tier ordering, which is the one thing bands could have broken.

    ``place_row`` drops an over-long annotation a full ``LINE_STEP`` below its
    row, so it physically sits on the *next* row's rule. Reverse-layout
    reconstruction has to be consulted first, or every wrapped annotation would
    be reassigned to the row beneath it.
    """
    rows = extract_rows(crfs["acroform"])
    page_rows = [r for r in rows.rows if r.page_index == 0]
    row = next(r for r in page_rows if r.text_1 == "Sex")
    below = min(
        (r for r in page_rows if r.band.y1 <= row.band.y0),
        key=lambda r: row.band.y0 - r.band.y1,
    )

    # Exactly where place_row puts a wrapped annotation: the question's own
    # indent, one LINE_STEP down.
    wrapped = BBox(
        x0=row.anchor.x0,
        y0=row.anchor.y0 - layout.LINE_STEP,
        x1=row.anchor.x0 + 60,
        y1=row.anchor.y0 - layout.LINE_STEP + row.anchor.height,
    )
    assert below.band.y0 <= (wrapped.y0 + wrapped.y1) / 2.0 <= below.band.y1  # in the next band

    matched = match_marks_to_rows(
        [RecoveredMapping(page_index=0, bbox=wrapped, text="SEX", kind="variable")], rows
    )[0]
    assert (matched.row_id, matched.placement, matched.wrap_lines) == (row.row_id, "wrap", 1)


def test_a_mark_beyond_the_distance_budget_is_still_unmatched(crfs):
    """Band containment does not become a way around ``max_distance``.

    Out in the left margin: on the "Sex" row's rule, but far from every anchor
    on the page -- asserted as a precondition, since "far" here has to mean far
    from *any* row, not just from the one whose band it is in.
    """
    from pipeline.parse_annotated_pdf import _proximity

    rows = extract_rows(crfs["acroform"])
    page_rows = [r for r in rows.rows if r.page_index == 0]
    row = next(r for r in page_rows if r.text_1 == "Sex")

    stray = BBox(x0=2.0, y0=row.band.y0 + 1, x1=32.0, y1=row.band.y0 + 11)
    budget = 20.0
    assert min(_proximity(stray, r.anchor) for r in page_rows) > budget
    assert row.band.y0 <= (stray.y0 + stray.y1) / 2.0 <= row.band.y1  # still on the rule

    m = RecoveredMapping(page_index=0, bbox=stray, text="SEX", kind="variable")
    assert match_marks_to_rows([m], rows, max_distance=budget)[0].row_id is None
