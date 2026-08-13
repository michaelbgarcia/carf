"""QC preview stamping, straight from in-memory annotation records.

Task order step 7.

This is the quick look immediately after ingesting Copilot's reply, before any
human has reviewed anything. It renders from ``SdtmAnnotation`` objects, not
from a file, and derives its text from those records.

**Deliberately not the same step as ``xfdf_to_pdf.py``.** That module renders
from the XFDF file precisely because the XFDF may have been edited since and
is the authoritative artifact. They share the drawing helper in
``pipeline.render``; they do not share an entry point, because that is how a
stale pre-review preview gets mistaken for the real deliverable.

Output is named so it cannot be confused for a submission artifact, and every
page carries a banner saying what it is.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from pipeline.models import AnnotationSet, ReviewStatus
from pipeline.render import draw_annotation, save_with_annotations

BANNER = "QC PREVIEW - UNREVIEWED COPILOT PROPOSALS - NOT FOR SUBMISSION"
BANNER_COLOR = (0.75, 0.35, 0.0)


def _stamp_banner(page: pymupdf.Page, n_proposed: int) -> None:
    page.insert_text(
        pymupdf.Point(36, 14),
        f"{BANNER}   ({n_proposed} proposed annotations)",
        fontname="hebo",
        fontsize=7,
        color=BANNER_COLOR,
    )


def stamp_annotations(
    pdf_path: str | Path,
    annotations: AnnotationSet,
    out_path: str | Path,
) -> Path:
    """Stamp proposals onto a copy of the blank CRF for visual QC."""
    doc = pymupdf.open(pdf_path)
    try:
        drawn = 0
        for annot in annotations.annotations:
            if annot.review_status is ReviewStatus.REJECTED:
                continue
            text = annot.display_text()
            if not text:
                continue
            page = doc[annot.page_index]
            draw_annotation(
                page,
                annot.bbox,
                text,
                boxed=annot.kind.value == "domain",
                muted=not annot.label_text(),
            )
            drawn += 1

        for page in doc:
            _stamp_banner(page, drawn)

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        save_with_annotations(doc, out_path)
        return out_path
    finally:
        doc.close()
