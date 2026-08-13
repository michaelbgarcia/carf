"""Field detection, checked against the committed truth file.

The AcroForm variant is held to exact equality on both coordinates and labels
-- widget rects are known precisely, so anything less would be slack. The two
flattened variants are matched by position, because a flattened CRF has no
field names to match on, and with a tolerance, because baking insets rects and
an underline gives no top edge to measure.
"""

import pymupdf
import pytest

from pipeline.extract import (
    associate_label,
    clean_label,
    extract_acroform_fields,
    extract_fields,
    text_runs,
)
from pipeline.models import FieldSource

# Baked rects are inset by half the border width; an underline's top edge is
# inferred from the caption height rather than measured.
POSITION_TOL = 2.0
INFERRED_TOP_TOL = 3.0


def _match(got_fields, target, tol=POSITION_TOL):
    """Find the detected field sitting where `target` should be."""
    return [
        g
        for g in got_fields
        if g.page_index == target.page_index
        and abs(g.bbox.x0 - target.bbox.x0) <= tol
        and abs(g.bbox.x1 - target.bbox.x1) <= tol
        and abs(g.bbox.y0 - target.bbox.y0) <= tol + INFERRED_TOP_TOL
    ]


# --- AcroForm path --------------------------------------------------------


def test_acroform_finds_every_field_with_exact_geometry(crfs, truth):
    got = extract_fields(crfs["acroform"])
    assert len(got.fields) == len(truth.fields)
    by_id = {f.field_id: f for f in got.fields}
    for t in truth.fields:
        g = by_id[t.field_id]
        assert g.page_index == t.page_index
        assert g.bbox.as_tuple() == pytest.approx(t.bbox.as_tuple(), abs=1e-6)


def test_acroform_labels_match_truth_exactly(crfs, truth):
    got = extract_fields(crfs["acroform"])
    by_id = {f.field_id: f for f in got.fields}
    mismatched = {
        t.field_id: (by_id[t.field_id].label, t.label)
        for t in truth.fields
        if by_id[t.field_id].label != t.label
    }
    assert not mismatched


def test_acroform_records_provenance_of_the_detection(crfs):
    got = extract_fields(crfs["acroform"])
    assert all(f.source is FieldSource.ACROFORM for f in got.fields)
    assert all(f.acroform_name for f in got.fields)


def test_page_geometry_is_captured_for_the_later_flip(crfs):
    got = extract_fields(crfs["acroform"])
    assert [p.page_index for p in got.pages] == [0, 1]
    assert all(p.height == pytest.approx(792.0) for p in got.pages)


# --- flattened paths ------------------------------------------------------


@pytest.mark.parametrize("variant", ["flat", "ruled"])
def test_flattened_variants_find_every_field(crfs, truth, variant):
    got = extract_fields(crfs[variant])
    missing = [t.field_id for t in truth.fields if not _match(got.fields, t)]
    assert not missing


@pytest.mark.parametrize("variant", ["flat", "ruled"])
def test_flattened_variants_invent_no_extra_fields(crfs, truth, variant):
    """The separator rules must not come back as fill-in blanks."""
    got = extract_fields(crfs[variant])
    assert len(got.fields) == len(truth.fields)


@pytest.mark.parametrize("variant", ["flat", "ruled"])
def test_flattened_variants_label_fields_correctly(crfs, truth, variant):
    got = extract_fields(crfs[variant])
    wrong = {}
    for t in truth.fields:
        m = _match(got.fields, t)
        if m and m[0].label != t.label:
            wrong[t.field_id] = (m[0].label, t.label)
    assert not wrong


@pytest.mark.parametrize("variant", ["flat", "ruled"])
def test_flattened_fields_are_marked_as_layout_derived(crfs, variant):
    got = extract_fields(crfs[variant])
    assert all(f.source is FieldSource.TEXT_LAYOUT for f in got.fields)
    assert all(f.acroform_name is None for f in got.fields)


def test_underlines_keep_their_measured_baseline(crfs, truth):
    """The ruled variant's top edge is inferred, but the bottom edge is real."""
    got = extract_fields(crfs["ruled"])
    for t in truth.fields:
        m = _match(got.fields, t)
        assert m, t.field_id
        assert m[0].bbox.y0 == pytest.approx(t.bbox.y0, abs=POSITION_TOL)


# --- label association rules ---------------------------------------------


def test_checkboxes_read_their_caption_from_the_right(crfs, truth):
    got = extract_fields(crfs["acroform"])
    by_id = {f.field_id: f for f in got.fields}
    assert by_id["DM_SEX_M"].label == "Male"
    assert by_id["DM_SEX_F"].label == "Female"
    assert by_id["DM_RACE_BLACK"].label == "Black or African American"


def test_grid_checkboxes_fall_through_to_the_column_header_above(crfs):
    """`Not Done` has nothing to its right, so the header five rows up wins."""
    got = extract_fields(crfs["acroform"])
    by_id = {f.field_id: f for f in got.fields}
    for testcd in ("SYSBP", "RESP"):
        assert by_id[f"VS_{testcd}_ND"].label == "Not Done"


def test_text_fields_read_their_caption_from_the_left(crfs):
    got = extract_fields(crfs["acroform"])
    by_id = {f.field_id: f for f in got.fields}
    assert by_id["DM_USUBJID"].label == "Subject Identifier"
    # 'years' sits to the right of this one; left must win.
    assert by_id["DM_AGE"].label == "Age at Consent"


def test_adjacent_blanks_share_one_caption(crfs):
    """The three date-of-birth boxes all hang off a single label."""
    got = extract_fields(crfs["acroform"])
    by_id = {f.field_id: f for f in got.fields}
    labels = {by_id[f"DM_BRTHDTC_{p}"].label for p in ("DD", "MMM", "YYYY")}
    assert labels == {"Date of Birth (DD/MMM/YYYY)"}


def test_context_distinguishes_identical_looking_grid_rows(crfs):
    """Copilot never sees coordinates, so context has to carry the row."""
    got = extract_fields(crfs["acroform"])
    by_id = {f.field_id: f for f in got.fields}
    sys_ctx = by_id["VS_SYSBP_RES"].context
    dia_ctx = by_id["VS_DIABP_RES"].context
    assert sys_ctx != dia_ctx
    assert "Systolic Blood Pressure" in sys_ctx
    assert "VITAL SIGNS" in sys_ctx


def test_context_names_the_section_heading(crfs):
    got = extract_fields(crfs["acroform"])
    by_id = {f.field_id: f for f in got.fields}
    assert "DEMOGRAPHICS" in by_id["DM_USUBJID"].context


def test_context_never_contains_a_bare_pipe(crfs):
    """Regression guard.

    context used to join same-line captions with ' | ' (e.g. "line: Sex |
    Male | Female"). context travels as a CSV cell and, on the markdown-table
    fallback path, as a table cell -- an unescaped '|' inside a cell silently
    shifts every column after it, which sheared kind/origin/confidence
    sideways with no error, just wrong values in the wrong fields. Multi-
    caption lines (Sex/Male/Female, Sitting/Supine/Standing) are exactly the
    fixture content that would catch a reintroduction of '|' here.
    """
    got = extract_fields(crfs["acroform"])
    offenders = [f.field_id for f in got.fields if "|" in f.context]
    assert not offenders, offenders


# --- text run grouping ----------------------------------------------------


def test_captions_on_one_line_stay_separate(crfs):
    """`Sex:` / `Male` / `Female` share a line but are three captions."""
    page = pymupdf.open(crfs["acroform"])[0]
    texts = {r.text for r in text_runs(page)}
    assert "Male" in texts and "Female" in texts
    assert not any("Male" in t and "Female" in t for t in texts)


def test_multi_word_captions_stay_joined(crfs):
    page = pymupdf.open(crfs["acroform"])[0]
    assert any(r.text.startswith("Age at Consent") for r in text_runs(page))


def test_clean_label_strips_trailing_colons_and_whitespace():
    assert clean_label("  Subject   Identifier:  ") == "Subject Identifier"
    assert clean_label("Date of Birth (DD/MMM/YYYY):") == "Date of Birth (DD/MMM/YYYY)"


def test_association_returns_none_when_nothing_is_near(crfs):
    page = pymupdf.open(crfs["acroform"])[0]
    runs = text_runs(page)
    empty = pymupdf.Rect(20, 640, 40, 656)  # blank margin
    assert associate_label(runs, empty, is_checkbox=False) is None


# --- fallback selection ---------------------------------------------------


def test_acroform_path_is_skipped_on_a_page_with_no_widgets(crfs):
    page = pymupdf.open(crfs["flat"])[0]
    assert extract_acroform_fields(page, 0) == []
