#!/usr/bin/env python3
"""Step 3 of 3: blank CRF + reviewed XFDF -> annotated submission PDF.

Stub -- task order step 9.

The XFDF passed in is authoritative. It may have been hand-edited in Acrobat or
re-exported from the review UI since ingest, and this step trusts it over
anything in build/fields.json or build/proposals.json. Reintroducing those as a
filter here would quietly undo human review.

Annotations stay as PDF markup -- never flattened into page content. FDA review
tools expect them searchable and separable from the base form.

Usage:
    python scripts/render_final.py <blank_crf.pdf> <final.xfdf> [-o build/annotated_crf.pdf]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import xfdf_to_pdf  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", type=Path, help="blank CRF PDF")
    parser.add_argument("xfdf", type=Path, help="reviewed XFDF")
    parser.add_argument(
        "-o", "--out", type=Path, default=Path("build/annotated_crf.pdf")
    )
    args = parser.parse_args(argv)

    args.out.parent.mkdir(parents=True, exist_ok=True)

    annotations = xfdf_to_pdf.parse_xfdf(args.xfdf)
    out = xfdf_to_pdf.xfdf_to_pdf(args.pdf, args.xfdf, args.out)
    print(f"wrote {out}  ({len(annotations)} annotations)")

    # Clipped text is silently truncated in the finished PDF, so say so rather
    # than shipping a submission artifact with half an annotation on it.
    overflow = xfdf_to_pdf.overflowing_annotations(annotations)
    if overflow:
        print(
            f"\nWARNING: {len(overflow)} annotation(s) are wider than their rect and "
            "will be clipped.\nWiden the rect in the XFDF, or shorten the text:"
        )
        for annot_id, text, over in overflow:
            print(f"  {annot_id}: {text!r} overflows by {over:.0f}pt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
