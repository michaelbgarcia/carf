"""Two-column row extraction -- ``pipeline/rows.py``.

The claims worth testing here are the ones the whole rewrite rests on, so they
are asserted directly rather than through their downstream effects:

* the gutter lands between the two columns, and comes back ``None`` rather than
  a wrong number on a single-column page;
* a run spanning the gutter does not erase it;
* widgets are ignored, not coped with -- the AcroForm and flat variants must
  produce *identical* rows;
* a wrapped question becomes one row and an option list does not;
* the committed truth file still matches, and would fail if the layout drifted.
"""

from __future__ import annotations

import json

import pymupdf
import pytest

import make_sample_crf as gen
from pipeline.geometry import bbox_to_fitz_rect
from pipeline.rows import (
    FULL_WIDTH_FRACTION,
    MIN_COLUMN_RUNS,
    detect_gutter,
    extract_rows,
    form_name,
    splits_into_columns,
)
from pipeline.text import TextRun, text_runs

# Points. Extraction recovers the drawn insertion x to floating-point precision
# (measured worst case across all 54 rows: 0.0000pt), so this is headroom for
# PyMuPDF glyph-bearing differences between versions, not for slop. Kept well
# under 1pt so that a 1pt layout shift fails decisively rather than borderline --
# see test_a_layout_shift_fails_the_truth_check.
ANCHOR_TOL = 0.25


@pytest.fixture(scope="module")
def rows_acroform(crfs):
    return extract_rows(crfs["acroform"])


@pytest.fixture(scope="module")
def rows_flat(crfs):
    return extract_rows(crfs["flat"])


# --------------------------------------------------------------------------
# Gutter detection
# --------------------------------------------------------------------------


def test_gutter_falls_between_the_columns(rows_acroform, truth):
    """Detected gutter sits inside the interval the generator measured.

    The bounds are the right edge of the widest question text and the left edge
    of the leftmost response text, both from real font metrics at draw time. Any
    x between them separates the columns; anything outside does not.
    """
    for page, expected in zip(rows_acroform.pages, truth["pages"]):
        bounds = expected["gutter_bounds"]
        if bounds is None:
            continue
        assert page.gutter_x is not None, f"page {page.page_index} found no gutter"
        assert bounds["lo"] < page.gutter_x < bounds["hi"], (
            f"page {page.page_index}: gutter {page.gutter_x:.1f} outside "
            f"({bounds['lo']}, {bounds['hi']})"
        )


def test_single_column_page_has_no_gutter(rows_acroform, truth):
    """``None`` is the right answer, and the borrowed median must not override it.

    The instruction page's page number is right-aligned into the response
    column on purpose, so this passing requires ``MIN_COLUMN_RUNS`` to actually
    reject one lonely run on the right -- not merely the trivial case of no text
    over there at all.
    """
    single = [p["page_index"] for p in truth["pages"] if p["gutter_bounds"] is None]
    assert single, "fixture no longer contains a single-column page"
    for page_index in single:
        page = rows_acroform.page(page_index)
        assert page is not None and page.gutter_x is None, (
            f"page {page_index} is single-column but was given gutter {page.gutter_x!r}; "
            "the document-median fallback sheared a page that has no gutter"
        )


def test_single_column_page_keeps_its_text_in_one_column(rows_acroform, truth):
    """With no gutter, nothing is column 2 -- including the footer's page number."""
    single = [p["page_index"] for p in truth["pages"] if p["gutter_bounds"] is None]
    for page_index in single:
        for row in rows_acroform.for_page(page_index):
            assert row.bbox_2 is None and row.text_2 == "", (
                f"{row.row_id} was split into two columns on a single-column page"
            )


def test_spanning_run_does_not_erase_the_gutter(crfs, rows_acroform):
    """A full-width note crosses the corridor; the corridor still gets found.

    This is the case ``FULL_WIDTH_FRACTION`` exists for. Removing the exclusion
    must break it, which the second half asserts -- otherwise the constant could
    be dead code and the test would still pass.
    """
    spanning = [r for r in rows_acroform.rows if r.full_width]
    assert spanning, "fixture no longer contains a gutter-spanning run"
    page_index = spanning[0].page_index
    assert rows_acroform.page(page_index).gutter_x is not None

    doc = pymupdf.open(crfs["acroform"])
    try:
        page = doc[page_index]
        runs = text_runs(page)
        wide = [r for r in runs if r.rect.width > FULL_WIDTH_FRACTION * page.rect.width]
        assert wide, "no run wide enough to be excluded -- the exclusion is untested"
        # Same detection with the exclusion defeated: every run made narrow.
        shrunk = [
            TextRun(pymupdf.Rect(r.rect.x0, r.rect.y0, r.rect.x0 + 1.0, r.rect.y1), r.text, r.blocks)
            if r in wide
            else r
            for r in runs
        ]
        assert detect_gutter(shrunk, page.rect.width) is not None
    finally:
        doc.close()


def test_gutter_needs_text_on_both_sides():
    """``splits_into_columns`` is what keeps the median fallback honest."""
    left = [
        TextRun(pymupdf.Rect(90, 100 + 12 * i, 200, 110 + 12 * i), f"question {i}", frozenset({i}))
        for i in range(4)
    ]
    lone_right = [TextRun(pymupdf.Rect(400, 100, 460, 110), "Page 1 of 3", frozenset({9}))]

    assert not splits_into_columns(left + lone_right, 320.0, 612.0)
    assert detect_gutter(left + lone_right, 612.0) is None

    enough_right = lone_right + [
        TextRun(pymupdf.Rect(400, 112 + 12 * i, 460, 122 + 12 * i), f"opt {i}", frozenset({20 + i}))
        for i in range(MIN_COLUMN_RUNS)
    ]
    assert splits_into_columns(left + enough_right, 320.0, 612.0)


def test_right_edge_cluster_fallback_when_no_corridor_is_clean():
    """A page dense enough that nothing leaves a gap still has a shared right edge.

    Question text reaching past where options start on other lines means no x is
    un-straddled, so the corridor sweep finds nothing. The response column is
    still right-aligned, and that is what the fallback keys on.
    """
    runs = [
        # Options right-aligned to x1=470.
        TextRun(pymupdf.Rect(380, 100, 470, 110), "Hispanic or Latino", frozenset({1})),
        TextRun(pymupdf.Rect(360, 112, 470, 122), "Not Hispanic or Latino", frozenset({2})),
        TextRun(pymupdf.Rect(410, 124, 470, 134), "Not Reported", frozenset({3})),
        TextRun(pymupdf.Rect(420, 136, 470, 146), "Unknown", frozenset({4})),
        # A question long enough to reach past where those options begin.
        TextRun(pymupdf.Rect(90, 148, 430, 158), "a question that runs very long", frozenset({5})),
        TextRun(pymupdf.Rect(90, 160, 250, 170), "Ethnicity", frozenset({6})),
    ]
    page_width = 612.0
    assert all(r.rect.width <= FULL_WIDTH_FRACTION * page_width for r in runs)

    gutter = detect_gutter(runs, page_width)
    assert gutter is not None, "right-edge cluster fallback did not fire"
    # Left of the options' shared left-most edge, right of the short question.
    assert 250.0 < gutter <= 360.0


def test_no_text_means_no_gutter():
    assert detect_gutter([], 612.0) is None


# --------------------------------------------------------------------------
# Row assembly
# --------------------------------------------------------------------------


def test_widgets_are_ignored_not_handled(rows_acroform, rows_flat):
    """AcroForm and flat produce identical rows.

    The two variants are the same document, one with live widgets and one with
    those widgets baked into page content as drawn outlines. Extraction reads
    text, so neither contributes anything and the rows must match exactly --
    not within a tolerance. Under the old field-detection design this comparison
    needed a ~1pt tolerance because baking insets a rect by half its border
    width; that whole class of concern is gone.
    """
    assert [r.model_dump() for r in rows_acroform.rows] == [
        r.model_dump() for r in rows_flat.rows
    ]
    assert [p.gutter_x for p in rows_acroform.pages] == [p.gutter_x for p in rows_flat.pages]


def test_wrapped_question_becomes_one_row(rows_acroform):
    """Two printed lines of one question merge; annotating half a sentence is useless."""
    matches = [r for r in rows_acroform.rows if r.text_1.startswith("If Yes, please provide")]
    assert len(matches) == 1
    row = matches[0]
    assert row.text_1 == "If Yes, please provide the original participant number (xxxxx-xxxx)"
    # The merged bbox covers both printed lines, so it is taller than one.
    assert row.bbox_1 is not None and row.bbox_1.height > 14.0


def test_option_list_is_not_merged(rows_acroform):
    """Four race options stay four rows.

    The direction that matters: these sit *closer together* than the two lines
    of the wrapped question above, so a leading threshold would collapse them.
    Collapsing them would make ``RACE = ASIAN`` and ``RACE = WHITE``
    unannotatable, and nothing downstream could recover it.
    """
    options = ["American Indian or Alaska Native", "Asian", "Black or African American", "White"]
    found = [r for r in rows_acroform.rows if r.text_1 in options]
    assert sorted(r.text_1 for r in found) == sorted(options)


def test_option_only_rows_have_an_empty_question_half(rows_acroform):
    """A continuation option ("Female", "No") is a row with no left column."""
    female = [r for r in rows_acroform.rows if r.text_2 == "Female"]
    assert len(female) == 1
    row = female[0]
    assert row.text_1 == "" and row.bbox_1 is None
    assert row.bbox_2 is not None
    # It still has somewhere for an annotation to hang off.
    assert row.anchor == row.bbox_2


def test_question_and_response_land_in_separate_columns(rows_acroform):
    sex = [r for r in rows_acroform.rows if r.text_1 == "Sex"]
    assert len(sex) == 1
    row = sex[0]
    assert row.text_2 == "Male"
    assert row.bbox_1 is not None and row.bbox_2 is not None
    gutter = rows_acroform.page(row.page_index).gutter_x
    assert row.bbox_1.x1 <= gutter <= row.bbox_2.x0


def test_row_ids_are_unique_and_resolvable(rows_acroform):
    ids = [r.row_id for r in rows_acroform.rows]
    assert len(ids) == len(set(ids))
    for row_id in ids:
        assert rows_acroform.by_id(row_id) is not None
    assert rows_acroform.by_id("p99_r999") is None


def test_form_name_carries_forward_from_the_page_header(rows_acroform):
    forms = rows_acroform.forms()
    assert forms == ["Demographics", "Vital Signs", "Instructions"]
    # Every row on a page inherits that page's form, header row included.
    for row in rows_acroform.rows:
        assert row.form


def test_form_name_returns_none_without_a_header():
    runs = [TextRun(pymupdf.Rect(90, 100, 200, 110), "Year of Birth (yyyy)", frozenset({0}))]
    assert form_name(runs) is None
    runs.append(TextRun(pymupdf.Rect(90, 50, 200, 60), "Form: Demographics", frozenset({1})))
    assert form_name(runs) == "Demographics"


# --------------------------------------------------------------------------
# The committed truth file
# --------------------------------------------------------------------------


def test_rows_match_committed_truth(rows_acroform, truth):
    """Every row matches the generator's committed inputs.

    Read from ``fixtures/sample_crf_rows_truth.json`` on disk, not rebuilt in
    process: a truth file regenerated from the current layout spec would agree
    with it by construction and could never catch a drift.
    """
    assert len(rows_acroform.rows) == len(truth["rows"])
    height = truth["page_height"]

    for expected, row in zip(truth["rows"], rows_acroform.rows):
        assert row.row_id == expected["row_id"]
        assert row.page_index == expected["page_index"]
        assert row.form == expected["form"]
        assert row.text_1 == expected["text_1"]
        assert row.text_2 == expected["text_2"]
        assert row.full_width == expected["full_width"]

        for key, bbox in (("anchor_x_1", row.bbox_1), ("anchor_x_2", row.bbox_2)):
            anchor = expected[key]
            if anchor is None:
                assert bbox is None, f"{row.row_id}: {key} expected absent"
                continue
            assert bbox is not None, f"{row.row_id}: {key} expected present"
            assert abs(bbox.x0 - anchor) <= ANCHOR_TOL, (
                f"{row.row_id}: {key} drawn at x={anchor}, extracted x0={bbox.x0:.2f}"
            )
            # The bbox must contain the baseline the text was drawn on. This is
            # the y-flip check: a rect reflected without re-sorting still lands
            # on the page and still looks plausible, but stops containing it.
            _, fy0, _, fy1 = bbox_to_fitz_rect(bbox, height)
            assert fy0 <= expected["baseline_y"] <= fy1, (
                f"{row.row_id}: baseline y={expected['baseline_y']} outside extracted "
                f"bbox [{fy0:.1f}, {fy1:.1f}] -- suspect the y-flip"
            )


def test_truth_file_is_regenerated_identically(tmp_path, truth):
    """The committed file is exactly what the generator currently produces.

    Guards the other direction from ``test_rows_match_committed_truth``: that one
    catches the generated PDF drifting from the committed numbers, this one
    catches the committed numbers going stale against the generator.
    """
    written = gen.make_sample_crf(tmp_path)
    regenerated = json.loads(written["truth"].read_text(encoding="utf-8"))
    assert regenerated == truth, (
        "fixtures/sample_crf_rows_truth.json is stale -- re-run "
        "scripts/make_sample_crf.py and commit the result"
    )


def test_a_layout_shift_fails_the_truth_check(crfs, monkeypatch, tmp_path, truth):
    """The guard is itself verified: nudge one row 1pt and the check must fail.

    Without this, a truth check that silently compared nothing would pass and
    the anti-circularity argument for committing the file would be hollow.
    """
    original = gen.demographics_page

    def shifted() -> gen.PageSpec:
        spec = original()
        moved = [
            (
                line
                if line.col1 != "Year of Birth (yyyy)"
                else gen.Line(**{**line.__dict__, "indent": (line.indent or gen.COL1_X) + 1.0})
            )
            for line in spec.lines
        ]
        return gen.PageSpec(
            index=spec.index, form=spec.form, lines=moved, rules=spec.rules,
            single_column=spec.single_column,
        )

    monkeypatch.setattr(gen, "demographics_page", shifted)
    written = gen.make_sample_crf(tmp_path)
    shifted_rows = extract_rows(written["acroform"])

    row = next(r for r in shifted_rows.rows if r.text_1 == "Year of Birth (yyyy)")
    expected = next(r for r in truth["rows"] if r["text_1"] == "Year of Birth (yyyy)")
    assert abs(row.bbox_1.x0 - expected["anchor_x_1"]) > ANCHOR_TOL, (
        "a 1pt shift did not move the extracted bbox past the tolerance, so the "
        "truth check cannot detect layout drift"
    )
