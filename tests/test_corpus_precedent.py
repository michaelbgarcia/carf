"""Mining a corpus of historical annotated CRFs for reusable SDTM precedent.

Each hand-built "historical" PDF here is a single FreeText mark placed beside a
real row of the synthetic fixture, with a copy of the fixture's own blank PDF
alongside it (so extraction has real rows to match against) -- the same
hand-built-PDF style test_parse_annotated_pdf.py uses, run across several files
instead of one.

The marks are drawn in the *legacy* convention -- unfilled text, a bordered
banner -- because that is what a corpus of pre-MSG aCRFs looks like, and reading
it is the whole reason this module exists. ``classify_mark`` handles both.
"""

from __future__ import annotations

import shutil

import pymupdf
import pytest

from pipeline.corpus_precedent import (
    build_lookup_table,
    build_variable_domain_precedent,
    mine_corpus,
)
from pipeline.rows import extract_rows
from pipeline.parse_annotated_pdf import parse_annotated_pdf
from pipeline.render import draw_annotation, save_with_annotations


@pytest.fixture
def corpus_setup(crfs, tmp_path):
    """Three PDFs sharing one field: two establish QVAL -> SUPPDS via a real
    boxed banner (trustworthy evidence), one has only a bare QVAL mark with
    no banner nearby (mirrors a page with no local domain evidence)."""
    rowset = extract_rows(crfs["acroform"])
    row = rowset.rows[0]

    pdf_dir = tmp_path / "corpus"
    blank_dir = tmp_path / "blanks"
    pdf_dir.mkdir()
    blank_dir.mkdir()

    names = ["doc_a.pdf", "doc_a2.pdf", "doc_b.pdf"]
    for name in names:
        shutil.copy(crfs["acroform"], blank_dir / name)

    def _make(name: str, with_banner: bool) -> None:
        doc = pymupdf.open()
        page = doc.new_page(width=612, height=792)
        if with_banner:
            banner_box = row.anchor.model_copy(
                update={"y0": row.anchor.y1 + 20, "y1": row.anchor.y1 + 32}
            )
            draw_annotation(page, banner_box, "SUPPDS", bordered=True)
        draw_annotation(page, row.anchor, "QVAL")
        save_with_annotations(doc, pdf_dir / name)
        doc.close()

    _make("doc_a.pdf", with_banner=True)
    _make("doc_a2.pdf", with_banner=True)
    _make("doc_b.pdf", with_banner=False)

    return pdf_dir, blank_dir, row


def test_pass_one_only_leaves_the_bannerless_doc_unattributed(corpus_setup):
    pdf_dir, blank_dir, _row = corpus_setup
    mappings = parse_annotated_pdf(pdf_dir / "doc_b.pdf", blank_dir / "doc_b.pdf")
    variable = next(m for m in mappings if m.kind == "variable")
    assert variable.domain is None


def test_mine_corpus_resolves_the_bannerless_doc_via_precedent(corpus_setup):
    pdf_dir, blank_dir, _row = corpus_setup
    mined = mine_corpus(pdf_dir, blank_dir)
    doc_b = next(m for m in mined if m.source_pdf == "doc_b.pdf" and m.kind == "variable")
    assert doc_b.domain == "SUPPDS"
    assert doc_b.domain_inference_source == "precedent"

    doc_a = next(m for m in mined if m.source_pdf == "doc_a.pdf" and m.kind == "variable")
    assert doc_a.domain == "SUPPDS"
    assert doc_a.domain_inference_source == "banner"


def test_min_support_suppresses_a_singleton(corpus_setup):
    pdf_dir, blank_dir, _row = corpus_setup
    mined = mine_corpus(pdf_dir, blank_dir, min_support=3)
    doc_b = next(m for m in mined if m.source_pdf == "doc_b.pdf" and m.kind == "variable")
    # Only 2 trustworthy (banner) occurrences exist (doc_a, doc_a2) -- short
    # of the 3 required, so no precedent is learned and doc_b stays unresolved.
    assert doc_b.domain is None


def test_lookup_table_dedupes_and_counts_across_the_corpus(corpus_setup):
    pdf_dir, blank_dir, _row = corpus_setup
    mined = mine_corpus(pdf_dir, blank_dir)
    rows = build_lookup_table(mined)
    qval_rows = [r for r in rows if r["variable"] == "QVAL"]
    assert len(qval_rows) == 1
    row = qval_rows[0]
    assert row["count"] == 3
    assert set(row["sample_pdfs"].split("; ")) == {"doc_a.pdf", "doc_a2.pdf", "doc_b.pdf"}


def test_build_variable_domain_precedent_ignores_untrustworthy_sources():
    """A precedent-sourced mapping must never corroborate further precedent
    -- otherwise the miner could amplify its own guesses."""
    from pipeline.models import BBox
    from pipeline.parse_annotated_pdf import RecoveredMapping

    zero = BBox(x0=0, y0=0, x1=1, y1=1)
    mappings = [
        RecoveredMapping(
            page_index=0, bbox=zero, text="", kind="variable",
            domain="DM", variable="XXVAR", domain_inference_source="builtin",
        ),
        RecoveredMapping(
            page_index=0, bbox=zero, text="", kind="variable",
            domain="DM", variable="XXVAR", domain_inference_source="precedent",
        ),
    ]
    assert build_variable_domain_precedent(mappings, min_support=1) == {}


# --- applying the table: pre-populating a control sheet -----------------------


def test_normalize_key_folds_what_a_pdf_round_trip_loses():
    from pipeline.corpus_precedent import normalize_key

    # Runs of spaces never survive a PDF: get_text returns words, not the source.
    assert normalize_key("Year  of   Birth") == normalize_key("Year of Birth")
    assert normalize_key("Sex:") == "sex"
    assert normalize_key("  SEX  ") == "sex"
    # ...but nothing more aggressive: punctuation that changes the meaning stays.
    assert normalize_key("Age (years)") != normalize_key("Age")


def test_match_precedent_pre_populates_a_recognised_row(crfs):
    """The mined table plays the metadata repository's role.

    Same join the deck's text-path fallback uses: CRF question text against
    standard text held elsewhere. Here "elsewhere" is prior annotated CRFs.
    """
    from pipeline.corpus_precedent import match_precedent

    rowset = extract_rows(crfs["acroform"])
    lookup = [
        {
            "label": "Year of Birth (yyyy)", "context": "", "domain": "DM",
            "variable": "BRTHDTC", "condition": "", "fixed_value": "",
            "count": "7", "sample_pdfs": "a.pdf; b.pdf",
        }
    ]
    out = match_precedent(rowset, lookup)
    assert len(out.annotations) == 1
    annot = out.annotations[0]

    target = next(r for r in rowset.rows if r.text_1 == "Year of Birth (yyyy)")
    assert annot.row_id == target.row_id
    assert (annot.domain, annot.variable) == ("DM", "BRTHDTC")
    assert annot.bbox == target.anchor


def test_pre_populated_annotations_are_marked_suggested(crfs):
    """Drives the grey fill, so a reviewer can tell a guess from a decision."""
    from pipeline.corpus_precedent import match_precedent

    rowset = extract_rows(crfs["acroform"])
    lookup = [{
        "label": "Sex", "context": "", "domain": "DM", "variable": "SEX",
        "condition": "", "fixed_value": "", "count": "4", "sample_pdfs": "a.pdf",
    }]
    (annot,) = match_precedent(rowset, lookup).annotations
    assert annot.suggested is True
    assert annot.source_model == "mined precedent (n=4)"
    assert annot.review_status.value == "proposed"


def test_no_confidence_score_is_invented(crfs):
    """Support count is real; any mapping of it onto 0..1 would not be.

    An invented confidence is worse than an honest absence, because a reviewer
    would prioritise their review by it.
    """
    from pipeline.corpus_precedent import match_precedent

    rowset = extract_rows(crfs["acroform"])
    lookup = [{
        "label": "Sex", "context": "", "domain": "DM", "variable": "SEX",
        "condition": "", "fixed_value": "", "count": "4", "sample_pdfs": "a.pdf",
    }]
    (annot,) = match_precedent(rowset, lookup).annotations
    assert annot.confidence is None
    assert "4" in annot.rationale


def test_competing_precedent_is_named_in_the_rationale(crfs):
    """The same question text genuinely maps differently across studies.

    The best-supported one is proposed and the rest are named, so a reviewer can
    see there was a choice rather than a fact.
    """
    from pipeline.corpus_precedent import match_precedent

    rowset = extract_rows(crfs["acroform"])
    lookup = [
        {"label": "Sex", "context": "", "domain": "DM", "variable": "SEX",
         "condition": "", "fixed_value": "", "count": "9", "sample_pdfs": "a.pdf"},
        {"label": "Sex", "context": "", "domain": "SC", "variable": "SCORRES",
         "condition": "", "fixed_value": "", "count": "2", "sample_pdfs": "b.pdf"},
    ]
    (annot,) = match_precedent(rowset, lookup).annotations
    assert annot.variable == "SEX", "did not prefer the better-supported mapping"
    assert "SC.SCORRES" in annot.rationale
    assert "n=2" in annot.rationale


def test_an_unmatched_row_gets_no_annotation(crfs):
    """Deliberate: the control sheet lists every row anyway.

    An unmatched row shows up as a blank cell for a human to fill, rather than a
    guess for them to un-guess.
    """
    from pipeline.corpus_precedent import match_precedent

    rowset = extract_rows(crfs["acroform"])
    lookup = [{
        "label": "A Question From Some Other Study", "context": "", "domain": "ZZ",
        "variable": "ZZFOO", "condition": "", "fixed_value": "", "count": "5",
        "sample_pdfs": "z.pdf",
    }]
    assert match_precedent(rowset, lookup).annotations == []


def test_min_support_filters_weak_precedent(crfs):
    from pipeline.corpus_precedent import match_precedent

    rowset = extract_rows(crfs["acroform"])
    lookup = [{
        "label": "Sex", "context": "", "domain": "DM", "variable": "SEX",
        "condition": "", "fixed_value": "", "count": "1", "sample_pdfs": "a.pdf",
    }]
    assert match_precedent(rowset, lookup, min_support=2).annotations == []
    assert match_precedent(rowset, lookup, min_support=1).annotations


def test_an_option_only_row_is_keyed_on_its_response_text(crfs):
    """A continuation option has no question text, so text_2 is the only key."""
    from pipeline.corpus_precedent import match_precedent

    rowset = extract_rows(crfs["acroform"])
    option = next(r for r in rowset.rows if not r.text_1 and r.text_2 == "Female")
    lookup = [{
        "label": "Female", "context": "", "domain": "DM", "variable": "SEX = F",
        "condition": "", "fixed_value": "", "count": "3", "sample_pdfs": "a.pdf",
    }]
    matched = {a.row_id for a in match_precedent(rowset, lookup).annotations}
    assert option.row_id in matched
