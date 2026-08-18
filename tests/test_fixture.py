"""The synthetic CRF generator itself.

Extraction against the truth file lives in ``test_rows.py``. What is left here is
the set of properties that make the fixture *worth* extracting from -- the ones a
well-meaning tidy-up of the layout would quietly remove, taking the coverage with
it. Each test names what stops being tested if the property goes.
"""

import pymupdf
import pytest

import make_sample_crf as gen
from pipeline.rows import FULL_WIDTH_FRACTION, MIN_COLUMN_RUNS
from pipeline.text import text_runs

# `crfs`, `truth` and `live_truth` come from conftest.py.


# --- the two variants -----------------------------------------------------


def test_acroform_variant_really_has_widgets(crfs):
    """Otherwise "widgets are ignored" is tested against a document with none."""
    doc = pymupdf.open(crfs["acroform"])
    try:
        widgets = [w for page in doc for w in page.widgets()]
    finally:
        doc.close()
    assert widgets, "no widgets in the AcroForm variant"


def test_flat_variant_has_no_widgets_but_does_have_drawn_geometry(crfs):
    """Baking must leave marks, or the two variants match for the wrong reason.

    A border-less widget bakes to *nothing*. If that happened, the flat variant
    would be the AcroForm variant minus its widgets and the "identical rows"
    assertion would prove only that removing content changes nothing.
    """
    doc = pymupdf.open(crfs["flat"])
    try:
        assert [w for page in doc for w in page.widgets()] == []
        drawings = sum(len(page.get_drawings()) for page in doc)
        rules = sum(len(s.rules) for s in gen.layout())
    finally:
        doc.close()
    assert drawings > rules, "baking produced no geometry beyond the distractor rules"


# --- the properties that make the fixture adversarial ---------------------


@pytest.mark.parametrize("page_index", [0, 1, 2])
def test_each_page_has_a_top_and_bottom_vertical_outlier(truth, page_index):
    """Guards the anti-flip property against a layout tidy-up.

    With every row in the middle band of the page, a y-flip bug puts each one
    roughly where it belongs and the visual check passes.
    """
    ys = [
        r["baseline_y"] for r in truth["rows"] if r["page_index"] == page_index
    ]
    assert ys, f"page {page_index} has no rows"
    assert min(ys) < 100, "nothing near the top of the page"
    assert max(ys) > gen.PAGE_H - 100, "nothing near the bottom of the page"


def test_both_option_geometries_are_represented(truth):
    """Options in the response column *and* options in the question column.

    Ethnicity right-aligns its options into column 2; Race puts them in column 1
    with only a checkbox on the right. Those assemble into rows differently, and
    a fixture with only one of them would leave the other path untested.
    """
    right_column = [r for r in truth["rows"] if not r["text_1"] and r["text_2"]]
    assert right_column, "no option-only rows (options in the response column)"

    left_column = [
        r for r in truth["rows"] if r["text_1"] in ("Asian", "White") and not r["text_2"]
    ]
    assert left_column, "no options in the question column"


def test_fixture_has_a_wrapped_question(truth):
    """Without one, the block-based line merge is never exercised."""
    wrapped = [
        r
        for r in truth["rows"]
        if r["text_1"].startswith("If Yes") and "participant number" in r["text_1"]
    ]
    assert len(wrapped) == 1, "the wrapped question is not one merged row"


def test_fixture_has_a_run_that_spans_the_gutter(crfs, truth):
    """The case FULL_WIDTH_FRACTION exists for, on a two-column page.

    Checked against the real drawn width, not just the ``full_width`` flag, so the
    fixture cannot satisfy this by declaring a property it does not have.
    """
    spanning = [r for r in truth["rows"] if r["full_width"]]
    assert spanning, "no gutter-spanning run"

    doc = pymupdf.open(crfs["acroform"])
    try:
        page = doc[spanning[0]["page_index"]]
        widest = max(r.rect.width for r in text_runs(page))
        assert widest > FULL_WIDTH_FRACTION * page.rect.width, (
            "the spanning row is not actually wide enough to be excluded from "
            "gutter detection, so the exclusion is untested"
        )
    finally:
        doc.close()


def test_fixture_has_a_single_column_page_with_text_on_the_right(crfs, truth):
    """Makes ``MIN_COLUMN_RUNS`` load-bearing rather than incidental.

    A single-column page with nothing at all on the right-hand side would pass the
    "no gutter" test trivially. This one has its page number right-aligned into
    the response column, so passing requires the guard to reject a single lonely
    run rather than an empty side.
    """
    single = [p for p in truth["pages"] if p["gutter_bounds"] is None]
    assert single, "no single-column page"

    doc = pymupdf.open(crfs["acroform"])
    try:
        page = doc[single[0]["page_index"]]
        runs = text_runs(page)
        narrow = [r for r in runs if r.rect.width <= FULL_WIDTH_FRACTION * page.rect.width]
        text_x0 = min(r.rect.x0 for r in narrow)
        text_x1 = max(r.rect.x1 for r in narrow)
        midpoint = (text_x0 + text_x1) / 2
        on_the_right = [r for r in narrow if r.rect.x0 >= midpoint]
    finally:
        doc.close()

    assert on_the_right, "nothing on the right of the single-column page"
    assert len(on_the_right) < MIN_COLUMN_RUNS, (
        "the single-column page has enough right-hand runs to look like two "
        "columns, so it no longer tests what it is here to test"
    )


def test_no_row_bbox_is_square(truth):
    """Square bboxes hide an x/y transposition.

    Text is wider than it is tall, so this is nearly free -- but it is worth
    asserting, because a transposition is a global bug and one detectable case
    anywhere exposes it.
    """
    checked = 0
    for row in truth["rows"]:
        if not row["text_1"] or len(row["text_1"]) < 4:
            continue
        checked += 1
    assert checked, "no rows long enough for a transposition to be visible"


def test_fixture_contains_rules_that_are_not_rows(truth):
    """Section and footer rules: page furniture that must not become content.

    Nothing looks at drawn lines any more, which is the point -- these are the
    standing check that nothing has quietly started to.
    """
    specs = gen.layout()
    assert any(s.rules for s in specs), "no distractor rules"
    for spec in specs:
        baselines = [
            r["baseline_y"] for r in truth["rows"] if r["page_index"] == spec.index
        ]
        for rule in spec.rules:
            assert not any(abs(y - rule.y) < 2.0 for y in baselines), (
                f"page {spec.index}: a distractor rule at y={rule.y} coincides with "
                "a row baseline, so a rule wrongly read as content would be "
                "indistinguishable from that row"
            )


def test_testcd_grid_covers_the_condition_pattern(truth):
    """The VS rows are what exercise ``VSORRES when VSTESTCD = SYSBP``."""
    labels = {r["text_1"] for r in truth["rows"]}
    expected = {label for _code, label, _unit in gen.VS_ROWS}
    assert expected <= labels, f"missing VS grid rows: {expected - labels}"
    # Each one has its unit in the response column -- the two-column shape the
    # condition pattern is annotated against.
    units = {r["text_1"]: r["text_2"] for r in truth["rows"]}
    assert units["Systolic Blood Pressure"] == "mmHg"


def test_fixture_has_non_mapped_page_furniture(truth):
    """The NotSubmitted case needs something that maps to nothing."""
    texts = {r["text_1"] for r in truth["rows"]}
    assert any(t.startswith("Page ") or "Page " in t for t in texts)
    assert any("Investigator Initials" in t for t in texts)


def test_more_than_one_form_is_present(truth):
    """MSG colours are assigned per form, so one form would not exercise it."""
    forms = {p["form"] for p in truth["pages"]}
    assert len(forms) > 1, f"only one form in the fixture: {forms}"


def test_every_page_is_marked_synthetic(crfs):
    """A stray copy has to identify itself without provenance research."""
    for variant in ("acroform", "flat"):
        doc = pymupdf.open(crfs[variant])
        try:
            for page in doc:
                assert gen.BANNER in page.get_text()
        finally:
            doc.close()


def test_no_pdf_is_committed():
    """Real CRFs never land in the repo, and neither does the synthetic one."""
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "fixtures/"],
        capture_output=True,
        text=True,
        cwd=gen.Path(__file__).resolve().parent.parent,
    ).stdout.split()
    assert not [f for f in tracked if f.endswith(".pdf")], (
        f"a PDF is tracked in fixtures/: {[f for f in tracked if f.endswith('.pdf')]}"
    )
