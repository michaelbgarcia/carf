"""Mining a corpus of historical annotated CRFs for reusable SDTM precedent.

Each hand-built "historical" PDF here is a single FreeText mark placed near
a real field of the synthetic fixture, annotated onto a copy of the
fixture's own blank PDF (so extract_fields detects a real field to match
against) -- the same hand-built-PDF style test_parse_annotated_pdf.py uses,
just run across several files instead of one.
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
from pipeline.extract import extract_fields
from pipeline.parse_annotated_pdf import parse_annotated_pdf
from pipeline.render import draw_annotation, save_with_annotations


@pytest.fixture
def corpus_setup(crfs, tmp_path):
    """Three PDFs sharing one field: two establish QVAL -> SUPPDS via a real
    boxed banner (trustworthy evidence), one has only a bare QVAL mark with
    no banner nearby (mirrors a page with no local domain evidence)."""
    fieldset = extract_fields(crfs["acroform"])
    field = fieldset.fields[0]

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
            banner_box = field.bbox.model_copy(
                update={"y0": field.bbox.y1 + 20, "y1": field.bbox.y1 + 32}
            )
            draw_annotation(page, banner_box, "SUPPDS", boxed=True)
        draw_annotation(page, field.bbox, "QVAL")
        save_with_annotations(doc, pdf_dir / name)
        doc.close()

    _make("doc_a.pdf", with_banner=True)
    _make("doc_a2.pdf", with_banner=True)
    _make("doc_b.pdf", with_banner=False)

    return pdf_dir, blank_dir, field


def test_pass_one_only_leaves_the_bannerless_doc_unattributed(corpus_setup):
    pdf_dir, blank_dir, _field = corpus_setup
    mappings = parse_annotated_pdf(pdf_dir / "doc_b.pdf", blank_dir / "doc_b.pdf")
    variable = next(m for m in mappings if m.kind == "variable")
    assert variable.domain is None


def test_mine_corpus_resolves_the_bannerless_doc_via_precedent(corpus_setup):
    pdf_dir, blank_dir, _field = corpus_setup
    mined = mine_corpus(pdf_dir, blank_dir)
    doc_b = next(m for m in mined if m.source_pdf == "doc_b.pdf" and m.kind == "variable")
    assert doc_b.domain == "SUPPDS"
    assert doc_b.domain_inference_source == "precedent"

    doc_a = next(m for m in mined if m.source_pdf == "doc_a.pdf" and m.kind == "variable")
    assert doc_a.domain == "SUPPDS"
    assert doc_a.domain_inference_source == "banner"


def test_min_support_suppresses_a_singleton(corpus_setup):
    pdf_dir, blank_dir, _field = corpus_setup
    mined = mine_corpus(pdf_dir, blank_dir, min_support=3)
    doc_b = next(m for m in mined if m.source_pdf == "doc_b.pdf" and m.kind == "variable")
    # Only 2 trustworthy (banner) occurrences exist (doc_a, doc_a2) -- short
    # of the 3 required, so no precedent is learned and doc_b stays unresolved.
    assert doc_b.domain is None


def test_lookup_table_dedupes_and_counts_across_the_corpus(corpus_setup):
    pdf_dir, blank_dir, _field = corpus_setup
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
