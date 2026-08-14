#!/usr/bin/env python3
"""Recover a label -> SDTM mapping table from an already-annotated CRF PDF.

Not a pipeline step -- a standalone tool for turning a historical aCRF (this
pipeline's own past output, or a third party's, as long as it follows the
same FDA aCRF convention of small FreeText markup near each field) into a
reference table: label/context -> domain/variable/condition. See
pipeline/parse_annotated_pdf.py for what is and is not recoverable -- in
particular, every field match is a spatial best guess reported with the
distance it was found at, never a certainty.

Usage:
    python scripts/parse_annotated_crf.py <annotated.pdf> [--blank blank.pdf] [-o report.csv]

``-o`` writes the full diagnostic report -- every mark found, matched or not,
one row each; the thing to open and skim to see what actually happened.
``--lookup-out`` writes the narrower reference table instead: matched
variable-kind mappings only, keyed by label, meant for reuse as Copilot-
prompt precedent rather than for review.

Without --blank, fields are detected directly on the annotated PDF itself --
works as long as the annotations were never flattened into page content.
Pass --blank when you have the un-annotated counterpart: a clean field
detection pass is more reliable than detecting through markup.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.parse_annotated_pdf import (  # noqa: E402
    parse_annotated_pdf,
    write_lookup_csv,
    write_report_csv,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", type=Path, help="already-annotated CRF PDF")
    parser.add_argument("--blank", type=Path, default=None, help="un-annotated counterpart, if you have it")
    parser.add_argument("--max-distance", type=float, default=200.0, help="max mark-to-field distance, in points")
    parser.add_argument(
        "-o", "--out", type=Path, default=None,
        help="write the full diagnostic report here as CSV (every mark, matched or not)",
    )
    parser.add_argument(
        "--lookup-out", type=Path, default=None,
        help="also write the narrower label-keyed reference table here as CSV",
    )
    args = parser.parse_args(argv)

    mappings = parse_annotated_pdf(args.pdf, args.blank, args.max_distance)

    by_kind: dict[str, int] = {}
    for m in mappings:
        by_kind[m.kind] = by_kind.get(m.kind, 0) + 1
    matched = [m for m in mappings if m.kind != "domain" and m.field_id is not None]
    unmatched = [m for m in mappings if m.kind != "domain" and m.field_id is None]
    inferred = [m for m in mappings if m.domain_inferred]

    print(f"{args.pdf.name}: {len(mappings)} annotation(s) found")
    for kind, count in sorted(by_kind.items()):
        print(f"  {kind}: {count}")
    print(f"matched to a field: {len(matched)}")
    print(f"unmatched (no field within {args.max_distance:.0f}pt): {len(unmatched)}")
    if inferred:
        print(f"domain inferred from a nearby banner: {len(inferred)}")
    if unmatched:
        print("\nunmatched marks:")
        for m in unmatched:
            print(f"  p{m.page_index + 1}: {m.text!r} at {m.bbox.as_tuple()}")

    if args.out:
        out = write_report_csv(mappings, args.out)
        print(f"\nwrote {out}  ({len(mappings)} row(s))")
    if args.lookup_out:
        out = write_lookup_csv(mappings, args.lookup_out)
        print(f"wrote {out}  ({len(matched)} row(s))")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
