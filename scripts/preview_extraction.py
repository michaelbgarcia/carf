#!/usr/bin/env python3
"""Draw what rows.py found onto a copy of the CRF, so you can look at it.

A debugging aid, not a pipeline step. It answers two questions that are
genuinely hard to answer any other way:

* **Is the gutter where it looks?** Drawn as a vertical line down each page. Get
  this wrong and every row on the page is mis-split, so it is the first thing to
  check when extraction looks strange. A page detected as single-column gets a
  label saying so rather than a line.
* **Did the coordinate flip land?** A reflected rect still sits on the page and
  still looks like a plausible box, so arithmetic alone will not tell you it is
  on the wrong row. The overlay will.

Colour key:
    red      column 1 (question text)
    blue     column 2 (response text)
    orange   a row flagged full_width -- spans the gutter
    green    the detected gutter

Also writes a PNG per page, so you can look without a PDF viewer.

Usage:
    python scripts/preview_extraction.py [pdf ...] [--build-dir build] [--no-png]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402

from pipeline.geometry import bbox_to_fitz_rect  # noqa: E402
from pipeline.rows import extract_rows  # noqa: E402

COL1 = (0.85, 0.10, 0.10)
COL2 = (0.10, 0.30, 0.85)
SPAN = (0.95, 0.55, 0.05)
GUTTER = (0.05, 0.60, 0.20)

DEFAULT_PDFS = [
    Path("fixtures/SYNTHETIC_sample_crf_twocol_acroform.pdf"),
    Path("fixtures/SYNTHETIC_sample_crf_twocol_flat.pdf"),
]


def preview(pdf_path: Path, out_pdf: Path, write_png: bool = True) -> tuple[Path, int]:
    rows = extract_rows(pdf_path)
    doc = pymupdf.open(pdf_path)
    try:
        for page_index, page in enumerate(doc):
            geometry = rows.page(page_index)
            height = page.rect.height

            if geometry is not None and geometry.gutter_x is not None:
                x = geometry.gutter_x
                page.draw_line(
                    pymupdf.Point(x, 0), pymupdf.Point(x, height), color=GUTTER, width=0.8
                )
                page.insert_text(
                    pymupdf.Point(x + 2, 12),
                    f"gutter x={x:.1f}",
                    fontname="helv",
                    fontsize=6,
                    color=GUTTER,
                )
            else:
                page.insert_text(
                    pymupdf.Point(8, 12),
                    "single-column page (gutter_x=None)",
                    fontname="hebo",
                    fontsize=6,
                    color=GUTTER,
                )

            for row in rows.for_page(page_index):
                if row.bbox_1 is not None:
                    page.draw_rect(
                        pymupdf.Rect(*bbox_to_fitz_rect(row.bbox_1, height)),
                        color=SPAN if row.full_width else COL1,
                        width=0.6,
                    )
                if row.bbox_2 is not None:
                    page.draw_rect(
                        pymupdf.Rect(*bbox_to_fitz_rect(row.bbox_2, height)),
                        color=COL2,
                        width=0.6,
                    )
                # row_id in the left margin, so a mismatch against the control
                # sheet can be traced to a specific line on a specific page.
                anchor = bbox_to_fitz_rect(row.anchor, height)
                page.insert_text(
                    pymupdf.Point(4, anchor[3] - 1),
                    row.row_id.split("_", 1)[1],
                    fontname="helv",
                    fontsize=4,
                    color=COL1,
                )

        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        doc.save(out_pdf, garbage=4, deflate=True)

        if write_png:
            for page_index, page in enumerate(doc):
                png = out_pdf.with_name(f"{out_pdf.stem}_p{page_index + 1}.png")
                page.get_pixmap(dpi=110).save(png)
    finally:
        doc.close()
    return out_pdf, len(rows.rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdfs", type=Path, nargs="*", default=None)
    parser.add_argument("--build-dir", type=Path, default=Path("build"))
    parser.add_argument("--no-png", action="store_true")
    args = parser.parse_args(argv)

    pdfs = args.pdfs or DEFAULT_PDFS
    missing = [p for p in pdfs if not p.exists()]
    if missing:
        parser.error(
            f"{', '.join(str(p) for p in missing)} not found -- "
            "run scripts/make_sample_crf.py first, or pass a PDF path"
        )

    for pdf in pdfs:
        suffix = pdf.stem.replace("SYNTHETIC_sample_crf_", "")
        out, n = preview(pdf, args.build_dir / f"rows_{suffix}.pdf", not args.no_png)
        print(f"{n:>4} rows  {pdf}  ->  {out}")
    print("\nred = question column, blue = response column, orange = spans the gutter")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
