#!/usr/bin/env python3
"""Draw what extract.py found onto a copy of the CRF, so you can look at it.

A debugging aid for field detection, not a pipeline step. It answers "did the
detector land on the right things?" -- which is the question the coordinate
flip makes genuinely hard to answer any other way, since a flipped rect still
lands on the page and still looks like a plausible box.

Not to be confused with:
  stamp.py        QC preview of *annotations*, after Copilot has proposed them
  xfdf_to_pdf.py  the actual submission artifact, after human review

Each detected field gets a red outline and its caption in blue. Anything the
detector missed has no box; anything it invented has a box over nothing.

Usage:
    python scripts/preview_extraction.py [pdf ...] [--out-dir build] [--dpi 110]

With no arguments it previews all three synthetic variants, generating them
first if fixtures/ is empty.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402

from pipeline.extract import extract_fields  # noqa: E402
from pipeline.geometry import bbox_to_fitz_rect  # noqa: E402

BOX = (0.85, 0.1, 0.1)
CAPTION = (0.0, 0.0, 0.8)
DEFAULT_VARIANTS = ("acroform", "flat", "ruled")


def preview(pdf_path: Path, out_dir: Path, dpi: int = 110) -> tuple[Path, list[Path]]:
    """Write an annotated PDF plus one PNG per page. Returns (pdf, pngs)."""
    fieldset = extract_fields(pdf_path)
    doc = pymupdf.open(pdf_path)

    for f in fieldset.fields:
        page = doc[f.page_index]
        rect = pymupdf.Rect(*bbox_to_fitz_rect(f.bbox, page.rect.height))
        page.draw_rect(rect, color=BOX, width=1.2)
        page.insert_text(
            pymupdf.Point(rect.x1 + 4, rect.y1 - 3),
            f.label or "(no caption)",
            fontsize=6,
            color=CAPTION,
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = pdf_path.stem.replace("SYNTHETIC_sample_crf_", "")
    out_pdf = out_dir / f"extraction_{stem}.pdf"
    doc.save(out_pdf, garbage=4, deflate=True)

    pngs = []
    for i, page in enumerate(doc):
        png = out_dir / f"extraction_{stem}_p{i + 1}.png"
        page.get_pixmap(dpi=dpi).save(png)
        pngs.append(png)
    doc.close()
    return out_pdf, pngs


def _default_inputs() -> list[Path]:
    fixtures = Path("fixtures")
    paths = [fixtures / f"SYNTHETIC_sample_crf_{v}.pdf" for v in DEFAULT_VARIANTS]
    if not all(p.exists() for p in paths):
        from make_sample_crf import make_sample_crf  # noqa: PLC0415

        make_sample_crf(fixtures)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", nargs="*", type=Path, help="CRF PDFs (default: all variants)")
    parser.add_argument("--out-dir", type=Path, default=Path("build"))
    parser.add_argument("--dpi", type=int, default=110)
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    for pdf in args.pdf or _default_inputs():
        fieldset = extract_fields(pdf)
        out_pdf, pngs = preview(pdf, args.out_dir, args.dpi)
        print(f"{pdf.name}: {len(fieldset.fields)} fields")
        print(f"  {out_pdf}")
        for p in pngs:
            print(f"  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
