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
one row each; the thing to open and skim to see what actually happened. It
carries each mark's position (absolute rect, plus its offset from the row it
annotates) and its styling (font, size, colours, border, print flags), which is
what a Metadata Submission Guidelines QC pass needs and what tells you where to
put an annotation on the next CRF.
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


def _load_precedent(path: Path) -> dict[str, str]:
    """Corpus lookup CSV (from scripts/mine_corpus.py) -> variable -> domain table.

    Majority-vote per variable, weighted by each row's ``count`` column --
    the same evidence ``mine_corpus`` already vetted (see
    ``pipeline.corpus_precedent.build_variable_domain_precedent``), just
    re-read back from the CSV rather than re-derived from raw mappings.
    """
    import csv as csv_module
    from collections import Counter, defaultdict

    votes: dict[str, Counter] = defaultdict(Counter)
    with path.open(encoding="utf-8") as fh:
        for row in csv_module.DictReader(fh):
            variable, domain = row.get("variable"), row.get("domain")
            if not variable or not domain:
                continue
            try:
                weight = int(row.get("count") or 1)
            except ValueError:
                weight = 1
            votes[variable][domain] += weight
    return {variable: counter.most_common(1)[0][0] for variable, counter in votes.items()}


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
    parser.add_argument(
        "--precedent-csv", type=Path, default=None,
        help="corpus lookup table from scripts/mine_corpus.py, used as a domain-attribution fallback",
    )
    args = parser.parse_args(argv)

    precedent = _load_precedent(args.precedent_csv) if args.precedent_csv else None
    mappings = parse_annotated_pdf(args.pdf, args.blank, args.max_distance, precedent)

    by_kind: dict[str, int] = {}
    for m in mappings:
        by_kind[m.kind] = by_kind.get(m.kind, 0) + 1
    matched = [m for m in mappings if m.kind != "domain" and m.row_id is not None]
    unmatched = [m for m in mappings if m.kind != "domain" and m.row_id is None]
    inferred = [m for m in mappings if m.domain_inferred]

    print(f"{args.pdf.name}: {len(mappings)} annotation(s) found")
    for kind, count in sorted(by_kind.items()):
        print(f"  {kind}: {count}")
    print(f"matched to a field: {len(matched)}")
    print(f"unmatched (no field within {args.max_distance:.0f}pt): {len(unmatched)}")
    if inferred:
        by_source: dict[str, int] = {}
        for m in inferred:
            source = m.domain_inference_source or "unknown"
            by_source[source] = by_source.get(source, 0) + 1
        print(f"domain inferred (not stated on the mark itself): {len(inferred)}")
        for source, count in sorted(by_source.items()):
            print(f"  via {source}: {count}")
    placements: dict[str, int] = {}
    for m in matched:
        placements[m.placement or "unknown"] = placements.get(m.placement or "unknown", 0) + 1
    if placements:
        # "nearest" is the fallback tier: no reconstructed placement, just the
        # closest row. A corpus that is mostly "nearest" is a corpus whose
        # annotations were not placed by this convention, and every one of those
        # matches is a guess -- which is worth seeing before the row counts are
        # taken at face value.
        print("placement (how the match was recognised):")
        for placement, count in sorted(placements.items(), key=lambda kv: -kv[1]):
            print(f"  {placement}: {count}")

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
