#!/usr/bin/env python3
"""Mine a directory of historical annotated CRF PDFs for reusable SDTM precedent.

Not a pipeline step -- a standalone tool that runs pipeline/parse_annotated_pdf.py
over every PDF in a directory (the same FDA aCRF convention it already
expects: small FreeText markup near each field) and consolidates the results
into one deduplicated reference table: label/context -> domain/variable/
condition, with a corroborating-occurrence count and the source PDFs that
contributed. See pipeline/corpus_precedent.py for the two-pass mining process
this wraps.

The output CSV is meant for two downstream uses: pass it to
``attribute_domains(..., precedent=...)`` (or `pipeline.parse_annotated_pdf.
parse_annotated_pdf(..., precedent=...)`) as a fallback for pages with no
local domain banner, and/or pass it to `scripts/build_sheet.py
--precedent-csv` to give Copilot real historical precedent in its
instructions.

Usage:
    python scripts/mine_corpus.py <pdf_dir> [--blank-dir dir] [--min-support N] -o lookup.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.corpus_precedent import (  # noqa: E402
    DEFAULT_MIN_SUPPORT,
    build_lookup_table,
    build_variable_domain_precedent,
    mine_corpus,
    write_corpus_lookup_csv,
)
from pipeline.parse_annotated_pdf import DEFAULT_MAX_MATCH_DISTANCE  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf_dir", type=Path, help="directory of already-annotated CRF PDFs")
    parser.add_argument(
        "--blank-dir", type=Path, default=None,
        help="directory of same-named un-annotated counterparts, if you have them",
    )
    parser.add_argument("--max-distance", type=float, default=DEFAULT_MAX_MATCH_DISTANCE, help="max mark-to-field distance, in points")
    parser.add_argument(
        "--min-support", type=int, default=DEFAULT_MIN_SUPPORT,
        help="corroborating occurrences required before a variable's domain becomes precedent",
    )
    parser.add_argument("-o", "--out", type=Path, required=True, help="write the corpus lookup table here as CSV")
    args = parser.parse_args(argv)

    mappings = mine_corpus(args.pdf_dir, args.blank_dir, args.max_distance, args.min_support)
    precedent = build_variable_domain_precedent(mappings, args.min_support)
    rows = build_lookup_table(mappings)

    n_pdfs = len(sorted(args.pdf_dir.glob("*.pdf")))
    print(f"mined {n_pdfs} PDF(s) in {args.pdf_dir}")
    print(f"variable -> domain precedent learned: {len(precedent)}")
    print(f"lookup table rows: {len(rows)}")

    out = write_corpus_lookup_csv(mappings, args.out)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
