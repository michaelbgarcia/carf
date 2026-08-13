"""The synthetic CRF generator and its truth file.

The load-bearing test here is ``test_committed_truth_matches_the_current_layout``.
Everything else in the suite compares extraction against the truth file, so if
the layout could drift away from it unnoticed, those comparisons would quietly
stop meaning anything.
"""

import json
from pathlib import Path

import pymupdf
import pytest

import make_sample_crf as gen
from pipeline.geometry import bbox_to_fitz_rect
from pipeline.models import FieldSet

ROOT = Path(__file__).resolve().parent.parent
TRUTH_PATH = ROOT / "fixtures" / "sample_crf_truth.json"

# Baking a widget insets its rect by half the border width, because the stroke
# is centred on the path. 1pt covers it with room to spare.
BAKE_TOLERANCE = 1.0


# `crfs` and `truth` come from conftest.py -- shared with the extraction tests.


# --- the drift guard ------------------------------------------------------


def test_committed_truth_matches_the_current_layout(truth):
    """Edit the layout without regenerating and this fails loudly.

    The committed truth file is the only thing standing between us and a
    circular test suite, where the generator and the extractor agree with each
    other because the same person wrote both against the same assumption.
    """
    if not TRUTH_PATH.exists():
        pytest.fail(f"{TRUTH_PATH} is missing; run scripts/make_sample_crf.py")
    committed = json.loads(TRUTH_PATH.read_text(encoding="utf-8"))
    assert committed == json.loads(truth.model_dump_json()), (
        "fixtures/sample_crf_truth.json is stale -- "
        "re-run scripts/make_sample_crf.py and commit the result"
    )


# --- the AcroForm variant -------------------------------------------------


def test_acroform_variant_carries_every_field_as_a_widget(crfs, truth):
    doc = pymupdf.open(crfs["acroform"])
    names = sorted(w.field_name for p in doc for w in p.widgets())
    assert names == sorted(f.field_id for f in truth.fields)


def test_widget_rects_match_the_truth_file_exactly(crfs, truth):
    """Truth is written through the y-flip, so this also tests the flip."""
    doc = pymupdf.open(crfs["acroform"])
    by_name = {f.field_id: f for f in truth.fields}
    for page in doc:
        for w in page.widgets():
            expected = bbox_to_fitz_rect(by_name[w.field_name].bbox, page.rect.height)
            assert tuple(w.rect) == pytest.approx(expected, abs=1e-6)


# --- the flattened variants ----------------------------------------------


def test_flat_variant_has_no_widgets_but_keeps_the_geometry(crfs, truth):
    doc = pymupdf.open(crfs["flat"])
    assert [w for p in doc for w in p.widgets()] == []
    # One drawing per field, plus the distractor rules.
    n_rules = sum(len(s.rules) for s in gen.layout())
    assert sum(len(p.get_drawings()) for p in doc) == len(truth.fields) + n_rules


def test_baked_fields_land_within_tolerance_of_the_acroform_originals(crfs, truth):
    """The two variants are the same document, so they must agree."""
    doc = pymupdf.open(crfs["flat"])
    drawn = {
        i: [tuple(d["rect"]) for d in page.get_drawings()]
        for i, page in enumerate(doc)
    }
    for f in truth.fields:
        expected = bbox_to_fitz_rect(f.bbox, gen.PAGE_H)
        assert any(
            max(abs(a - b) for a, b in zip(expected, got)) <= BAKE_TOLERANCE
            for got in drawn[f.page_index]
        ), f"{f.field_id} has no baked counterpart within {BAKE_TOLERANCE}pt"


def test_ruled_variant_draws_text_fields_as_underlines(crfs):
    """The second flat morphology: fill-in blanks, not boxes."""
    doc = pymupdf.open(crfs["ruled"])
    assert [w for p in doc for w in p.widgets()] == []
    specs = {s.index: s for s in gen.layout()}
    for i, page in enumerate(doc):
        flat = [d for d in page.get_drawings() if d["rect"].height < 1.0]
        text_fields = [f for f in specs[i].fields if f.kind == "text"]
        # every text field underline, plus the section/footer rules
        assert len(flat) == len(text_fields) + len(specs[i].rules)


# --- the properties that make the fixture worth having --------------------


@pytest.mark.parametrize("page_index", [0, 1])
def test_each_page_has_a_top_and_bottom_vertical_outlier(truth, page_index):
    """Guards the anti-flip property against a well-meaning layout tidy-up.

    With every field in the middle band of the page, a y-flip bug puts each one
    roughly where it belongs and the visual check passes.
    """
    ys = [f.bbox.y0 for f in truth.fields if f.page_index == page_index]
    assert max(ys) > gen.PAGE_H - 100, "no field near the top of the page"
    assert min(ys) < 100, "no field near the bottom of the page"


def test_no_text_field_is_square(truth):
    """Square bboxes hide an x/y transposition.

    Checkboxes are exempt -- a square is what a checkbox *is*, and distorting
    them to satisfy this would make the fixture unrealistic. The text fields
    carry the property, which is enough: a transposition is a global bug, so
    one detectable case anywhere on the page exposes it.
    """
    kinds = {f.name: f.kind for s in gen.layout() for f in s.fields}
    text_fields = [f for f in truth.fields if kinds[f.field_id] == "text"]
    assert text_fields
    for f in text_fields:
        assert f.bbox.width != pytest.approx(f.bbox.height, abs=0.5), f.field_id


def test_fixture_contains_non_field_rules_as_false_positives(truth):
    """A detector that calls every horizontal line a field must fail here."""
    rules = [r for s in gen.layout() for r in s.rules]
    assert rules, "no distractor rules -- line detection would be untested"
    field_rects = {bbox_to_fitz_rect(f.bbox, gen.PAGE_H) for f in truth.fields}
    for r in rules:
        assert not any(abs(fr[3] - r.y) < 1.0 and abs(fr[0] - r.x0) < 1.0
                       for fr in field_rects), "a distractor rule coincides with a field"


def test_both_label_geometries_are_represented(truth):
    """Page 1 labels sit left of their field; the page 2 grid puts them above."""
    assert any(f.page_index == 0 for f in truth.fields)
    assert any("grid column" in f.context for f in truth.fields)


def test_testcd_grid_covers_the_condition_pattern(truth):
    """The VS rows are what exercise `VSORRES when VSTESTCD = SYSBP`."""
    results = [f for f in truth.fields if f.field_id.endswith("_RES")]
    assert len(results) == len(gen.VS_ROWS)
    assert {f.field_id for f in results} >= {"VS_SYSBP_RES", "VS_DIABP_RES"}


def test_every_page_is_marked_synthetic(crfs):
    """A stray copy has to identify itself without provenance research."""
    for variant in ("acroform", "flat", "ruled"):
        doc = pymupdf.open(crfs[variant])
        for page in doc:
            assert gen.BANNER in page.get_text()
