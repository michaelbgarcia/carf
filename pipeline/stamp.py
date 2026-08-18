"""QC preview stamping, straight from in-memory annotation records.

This is the quick look at pre-populated proposals *before* a human has opened
the control sheet. It renders from ``SdtmAnnotation`` objects, not from a file,
and derives its text from those records.

**Deliberately not the same step as ``xfdf_to_pdf.py`` or ``scripts/annotate.py``.**
Those render the reviewed artifact, and they read it from the file a human
touched rather than from upstream records. They share the drawing helper in
``pipeline.render``; they do not share an entry point, because that is how a
stale pre-review preview gets mistaken for the real deliverable.

Output is named so it cannot be confused for a submission artifact, and every
page carries a banner saying what it is -- including how many of the annotations
on it are still only *suggested*, which is the number that decides whether the
sheet needs a careful pass or a skim.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pymupdf

from pipeline.layout import block_anchor
from pipeline.models import AnnotationKind, AnnotationSet, ReviewStatus, RowSet
from pipeline.msg import apply_colors, page_color_map
from pipeline.render import (
    draw_annotation,
    draw_group_bracket,
    draw_legend,
    legend_origin,
    save_with_annotations,
)

BANNER = "QC PREVIEW - UNREVIEWED PROPOSALS - NOT FOR SUBMISSION"
BANNER_COLOR = (0.75, 0.35, 0.0)


def _stamp_banner(page: pymupdf.Page, n_drawn: int, n_suggested: int) -> None:
    page.insert_text(
        pymupdf.Point(36, 10),
        f"{BANNER}   ({n_drawn} annotations, {n_suggested} unconfirmed suggestions)",
        fontname="hebo",
        fontsize=7,
        color=BANNER_COLOR,
    )


def stamp_annotations(
    pdf_path: str | Path,
    annotations: AnnotationSet,
    out_path: str | Path,
    rows: Optional[RowSet] = None,
    legend: bool = True,
    brackets: bool = True,
) -> Path:
    """Stamp proposals onto a copy of the blank CRF for visual QC.

    Pass ``rows`` to colour the preview the way the final artifact will be
    coloured. Without it there is no form grouping to assign MSG colours by, so
    the preview falls back to uncoloured marks -- readable, but not a preview of
    what the deliverable will look like.

    ``brackets`` defaults **on** here and off in ``scripts/annotate.py``, which
    is a deliberate split rather than an inconsistency. The question a reviewer
    asks of this file is "did it group the right rows?", and a bracket answers
    it directly; the question asked of the artifact is what the guidelines'
    examples answer, which is a plain centred box. It is the same reason this
    file carries a banner and the artifact does not.
    """
    if rows is not None:
        annotations = apply_colors(annotations, rows)
        by_page = page_color_map(annotations, rows)
    else:
        by_page = {}

    doc = pymupdf.open(pdf_path)
    try:
        drawn = 0
        suggested = 0
        for annot in annotations.annotations:
            if annot.review_status is ReviewStatus.REJECTED:
                continue
            text = annot.display_text()
            if not text:
                continue
            draw_annotation(
                doc[annot.page_index],
                annot.bbox,
                text,
                fill=annot.color,
                dashed=annot.kind is AnnotationKind.NOTE,
            )
            drawn += 1
            suggested += bool(annot.suggested)
            if brackets and annot.is_grouped and rows is not None:
                members = [
                    r for r in (rows.by_id(m) for m in annot.covered_row_ids) if r is not None
                ]
                if members:
                    draw_group_bracket(
                        doc[annot.page_index], block_anchor(members), annot.bbox
                    )

        if legend:
            for page_index, colors in by_page.items():
                page = doc[page_index]
                # Below the QC banner this module stamps at the very top, and
                # above the form's own header text.
                x, y = legend_origin(page)
                draw_legend(page, list(colors.items()), origin=(x, max(y, 14.0)))

        for page in doc:
            _stamp_banner(page, drawn, suggested)

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        save_with_annotations(doc, out_path)
        return out_path
    finally:
        doc.close()
