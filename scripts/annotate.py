#!/usr/bin/env python3
"""Step 2 of 2: blank CRF + reviewed control sheet -> annotated submission PDF.

The control sheet is authoritative. It is the artifact a human touched, so this
step trusts it over anything in build/rows.json or build/proposals.json --
reintroducing those as a filter or as a source of text would quietly undo the
review. The sheet's anno1/anno2 cells are rendered verbatim.

build/rows.json is still read, for two things the sheet cannot carry: the page
geometry annotations are positioned against, and the form grouping that decides
MSG colour order.

Rows sharing a `group` key render as **one** box covering the block -- the
repeating-annotation case. Note what this step does *not* do: it never folds
identical annotations into a group by itself. Grouping is a decision, it is
visible in the sheet's `group` column, and a reviewer signed off on the sheet;
re-deriving it here would put a box on the artifact that nobody reviewed.
`build_sheet.py` is where runs of identical pre-populated annotations get
collapsed, in time for a human to see it.

Styling follows Metadata Submission Guidelines v2.0 -- black text on a
domain-coloured fill, dashed borders on comment notes, and a domain legend at the
top of each page. Annotations stay as PDF markup, never flattened into page
content: FDA review tools expect them searchable and separable from the form.

Usage:
    python scripts/annotate.py <blank_crf.pdf> <control_sheet.xlsx>
        [-o build/annotated_crf.pdf] [--rows build/rows.json] [--xfdf out.xfdf]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402

from pipeline import control_sheet, grouping, layout, xfdf as xfdf_mod  # noqa: E402
from pipeline.models import AnnotationKind, ReviewStatus, RowSet  # noqa: E402
from pipeline.msg import apply_colors, page_color_map  # noqa: E402
from pipeline.render import (  # noqa: E402
    FONT,
    FONT_SIZE,
    draw_annotation,
    draw_group_bracket,
    draw_legend,
    legend_origin,
    save_with_annotations,
)


def _overflowing(annotations) -> list[tuple[str, str, float]]:
    """Annotations whose text is wider than the rect it renders in.

    PyMuPDF clips FreeText to its rect, so an oversized string is silently
    truncated in the finished PDF. ``layout.py`` sizes every rect to its text, so
    this only fires for something a human retyped longer in the sheet -- and a
    truncated annotation in a submission artifact is a data-integrity problem.
    Reported, never corrected: widening the box would override a placement the
    layout step chose deliberately.
    """
    out = []
    for a in annotations:
        text = a.display_text()
        if not text:
            continue
        needed = pymupdf.get_text_length(text, fontname=FONT, fontsize=FONT_SIZE)
        available = a.bbox.width - 2 * layout.PAD
        if needed - available > 0.5:
            out.append((a.annot_id, text, needed - available))
    return out


def _group_overruns(annotations, rows: RowSet) -> list[tuple[str, float]]:
    """Grouped boxes that run into a member row's response column.

    A single-row annotation cannot do this -- ``layout.place_row`` wraps it to
    the line below when it would. A grouped one has nowhere to wrap to (below it
    are its own member rows), so it stays put and overflows, which is a real
    collision with the form's printed text rather than a cosmetic one. Reported,
    not corrected: shortening the reviewer's text or moving the box are both
    decisions this step has no standing to make.
    """
    pages = {p.page_index: p for p in rows.pages}
    out = []
    for a in annotations:
        if not a.is_grouped or a.page_index not in pages:
            continue
        members = [r for r in (rows.by_id(m) for m in a.covered_row_ids) if r is not None]
        if not members:
            continue
        over = a.bbox.x1 - layout.group_limit(members, pages[a.page_index])
        if over > 0.5:
            out.append((a.annot_id, over))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", type=Path, help="blank CRF PDF")
    parser.add_argument("sheet", type=Path, help="reviewed control sheet (.xlsx)")
    parser.add_argument("-o", "--out", type=Path, default=Path("build/annotated_crf.pdf"))
    parser.add_argument(
        "--rows",
        type=Path,
        default=Path("build/rows.json"),
        help="rows.json from build_sheet.py (page geometry and form grouping)",
    )
    parser.add_argument(
        "--xfdf", type=Path, default=None, help="also export XFDF for Acrobat"
    )
    parser.add_argument(
        "--no-legend", action="store_true", help="skip the page-top domain legend"
    )
    parser.add_argument(
        "--brackets",
        action="store_true",
        help=(
            "draw a bracket spanning the rows each grouped annotation covers "
            "(default: off, matching the plain centred box in the guidelines' "
            "own examples)"
        ),
    )
    parser.add_argument(
        "--allow-unreviewed",
        action="store_true",
        help="render annotations still marked 'proposed' (default: refuse)",
    )
    args = parser.parse_args(argv)

    for path in (args.pdf, args.sheet, args.rows):
        if not path.exists():
            parser.error(f"{path} does not exist")

    rows = RowSet.model_validate_json(args.rows.read_text(encoding="utf-8"))
    annotations = control_sheet.to_annotations(args.sheet, rows, source_pdf=str(args.pdf))

    submittable = annotations.submittable()
    if not submittable:
        print(f"{args.sheet} has no annotations to render", file=sys.stderr)
        return 1

    # Part 11: nothing reaches a submission artifact until a human has said so.
    # Overridable, because a dry run to look at placement is legitimate -- but it
    # has to be asked for, and the output says what it is.
    unreviewed = [a for a in submittable if a.review_status is ReviewStatus.PROPOSED]
    if unreviewed and not args.allow_unreviewed:
        print(
            f"{len(unreviewed)} of {len(submittable)} annotations are still "
            f"review_status='proposed' in {args.sheet}.\n"
            "A submission artifact must not contain unreviewed annotations. Set "
            "review_status (and reviewed_by) in the sheet, or pass "
            "--allow-unreviewed for a placement dry run.",
            file=sys.stderr,
        )
        return 1

    annotations = apply_colors(annotations, rows)
    annotations = layout.place_annotations(
        annotations, rows, obstacles=layout.text_obstacles(args.pdf)
    )

    overflow = _overflowing(annotations.submittable())
    for annot_id, text, by in overflow:
        print(f"warning: {annot_id} text is {by:.1f}pt wider than its box: {text!r}")

    overruns = _group_overruns(annotations.submittable(), rows)
    for annot_id, by in overruns:
        print(
            f"warning: grouped annotation {annot_id} extends {by:.1f}pt into the "
            "response column of a row it covers"
        )

    by_page = {} if args.no_legend else page_color_map(annotations, rows)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(args.pdf)
    try:
        drawn = 0
        for annot in annotations.submittable():
            text = annot.display_text()
            if not text:
                continue
            if annot.page_index >= doc.page_count:
                print(
                    f"error: {annot.annot_id} is on page {annot.page_index + 1} but "
                    f"{args.pdf.name} has {doc.page_count} pages",
                    file=sys.stderr,
                )
                return 1
            draw_annotation(
                doc[annot.page_index],
                annot.bbox,
                text,
                fill=annot.color,
                dashed=annot.kind is AnnotationKind.NOTE,
            )
            drawn += 1
            if args.brackets and annot.is_grouped:
                members = [
                    r for r in (rows.by_id(m) for m in annot.covered_row_ids) if r is not None
                ]
                if members:
                    draw_group_bracket(
                        doc[annot.page_index],
                        layout.block_anchor(members),
                        annot.bbox,
                    )

        for page_index, colors in by_page.items():
            page = doc[page_index]
            draw_legend(page, list(colors.items()), origin=legend_origin(page))

        save_with_annotations(doc, args.out)
    finally:
        doc.close()

    label = " (UNREVIEWED - dry run)" if unreviewed else ""
    print(f"{drawn} annotations -> {args.out}{label}")
    n_groups, n_grouped_rows = grouping.summarize(annotations)
    if n_groups:
        print(
            f"{n_groups} of them are grouped, covering {n_grouped_rows} rows between them"
        )
    if by_page:
        print(f"domain legend drawn on {len(by_page)} page(s)")
    if overflow:
        print(f"{len(overflow)} annotation(s) overflow their box -- see warnings above")
    if overruns:
        print(f"{len(overruns)} grouped annotation(s) overrun a response column")

    if args.xfdf:
        written = xfdf_mod.write_xfdf(annotations, args.xfdf, source_pdf=str(args.pdf))
        print(f"XFDF export -> {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
