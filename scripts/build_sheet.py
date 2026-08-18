#!/usr/bin/env python3
"""Step 1 of 2: blank CRF PDF -> two-column rows -> pre-populated control sheet.

Writes:
    build/rows.json         extracted rows, needed again at annotate time to
                             reattach geometry
    build/proposals.json    annotations pre-populated from mined corpus
                             precedent, all review_status=proposed
    build/control_sheet.xlsx  the sheet a human reviews and completes
    build/qc_preview.pdf    pre-review visual check -- NOT a deliverable

Optionally, for the rows precedent could not answer:
    build/batches.json                      manifest of Copilot batches
    build/copilot_batch1_instructions.txt   short, static per batch
    build/copilot_batch1_sheet.csv          the row data
    ...

The normal path stops at the control sheet: open it, fill the blank annotation
cells, set review_status, then run annotate.py. Grey cells were pre-populated
from prior annotated CRFs and still need a look.

Runs of consecutive rows that precedent gave the *same* annotation are folded
into one grouped annotation (--no-group-repeats opts out), which is how a
repeating block gets one box instead of a column of identical ones. It shows up
as a shared key in the sheet's `group` column, greyed like any other suggestion;
clearing a row's key takes it back out of the group.

The Copilot batches are for when precedent leaves too many rows blank to type
out by hand. Paste a batch's instructions into Copilot 365 chat, attach the
sheet, save the reply verbatim to build/copilot_batchN_response.csv, and run
ingest_response.py to merge it into the control sheet. Nothing about that step
is automatable here -- there is no API.

Usage:
    python scripts/build_sheet.py <blank_crf.pdf> [--build-dir build]
        [--precedent build/corpus_lookup.csv] [--copilot-batches]
        [--max-rows-per-batch N]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import (  # noqa: E402
    control_sheet,
    grouping,
    layout,
    prompt,
    rows as rows_mod,
    stamp,
)
from pipeline.corpus_precedent import match_precedent, read_corpus_lookup_csv  # noqa: E402
from pipeline.models import AnnotationSet  # noqa: E402
from pipeline.msg import apply_colors  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", type=Path, help="blank CRF PDF")
    parser.add_argument("--build-dir", type=Path, default=Path("build"))
    parser.add_argument(
        "--precedent",
        type=Path,
        default=None,
        help="corpus lookup CSV from scripts/mine_corpus.py, used to pre-populate",
    )
    parser.add_argument(
        "--copilot-batches",
        action="store_true",
        help="also write Copilot batch materials for the rows precedent left blank",
    )
    parser.add_argument(
        "--max-rows-per-batch",
        type=int,
        default=prompt.DEFAULT_MAX_ROWS_PER_BATCH,
        help=f"rows per Copilot batch (default {prompt.DEFAULT_MAX_ROWS_PER_BATCH}; "
        "a guess, not a measured limit)",
    )
    parser.add_argument(
        "--no-preview", action="store_true", help="skip build/qc_preview.pdf"
    )
    parser.add_argument(
        "--no-group-repeats",
        action="store_true",
        help=(
            "pre-populate every row of a repeating block separately instead of "
            "folding identical runs into one grouped annotation"
        ),
    )
    args = parser.parse_args(argv)

    if not args.pdf.exists():
        parser.error(f"{args.pdf} does not exist")
    args.build_dir.mkdir(parents=True, exist_ok=True)

    rows = rows_mod.extract_rows(args.pdf)
    rows_path = args.build_dir / "rows.json"
    rows_path.write_text(rows.model_dump_json(indent=2), encoding="utf-8")

    single = [p.page_index + 1 for p in rows.pages if p.gutter_x is None]
    print(f"{len(rows.rows)} rows across {len(rows.pages)} page(s) -> {rows_path}")
    for page in rows.pages:
        where = "single-column" if page.gutter_x is None else f"gutter x={page.gutter_x:.1f}"
        print(f"  page {page.page_index + 1:>3}  {len(rows.for_page(page.page_index)):>3} rows  {where}")
    if single:
        print(
            f"  note: page(s) {single} read as single-column, so every row on them is "
            "all question and no response column. Check preview_extraction.py if that "
            "looks wrong for this CRF."
        )

    # Pre-populate from mined precedent, which plays the metadata-repository role.
    lookup = read_corpus_lookup_csv(args.precedent) if args.precedent else []
    if args.precedent and not lookup:
        print(f"warning: {args.precedent} has no rows -- nothing to pre-populate from")
    proposals = (
        match_precedent(rows, lookup)
        if lookup
        else AnnotationSet(source_pdf=str(args.pdf), pages=rows.pages, annotations=[])
    )
    # Before placement: precedent pre-populates each row of a repeating block
    # separately, which is a column of identical boxes unless the run is folded
    # into one group first. The reviewer sees the result as a group key in the
    # sheet and can undo it by clearing the cells.
    if not args.no_group_repeats:
        proposals = grouping.collapse_repeats(proposals, rows)
    proposals = apply_colors(proposals, rows)
    proposals = layout.place_annotations(
        proposals, rows, obstacles=layout.text_obstacles(args.pdf)
    )

    proposals_path = args.build_dir / "proposals.json"
    proposals_path.write_text(proposals.model_dump_json(indent=2), encoding="utf-8")

    matched = {r for a in proposals.annotations for r in a.covered_row_ids}
    print(
        f"\n{len(matched)} of {len(rows.rows)} rows pre-populated from precedent "
        f"-> {proposals_path}"
    )
    n_groups, n_grouped_rows = grouping.summarize(proposals)
    if n_groups:
        print(
            f"{n_grouped_rows} of them carry repeating annotations, folded into "
            f"{n_groups} group(s) -- see the sheet's `group` column"
        )

    sheet_path = control_sheet.write_control_sheet(
        rows, args.build_dir / "control_sheet.xlsx", proposals
    )
    print(f"control sheet -> {sheet_path}   (grey cells = suggested, need review)")

    if not args.no_preview:
        preview = stamp.stamp_annotations(
            args.pdf, proposals, args.build_dir / "qc_preview.pdf", rows=rows
        )
        print(f"QC preview   -> {preview}   (NOT a deliverable)")

    if args.copilot_batches:
        remaining = prompt.rows_needing_annotation(rows, proposals)
        manifest = prompt.write_batches(
            remaining,
            args.build_dir,
            max_rows_per_batch=args.max_rows_per_batch,
            precedent_rows=lookup or None,
        )
        batches_path = args.build_dir / "batches.json"
        batches_path.write_text(
            json.dumps(
                {"source_pdf": str(args.pdf), "rows": str(rows_path), "batches": manifest},
                indent=2,
            ),
            encoding="utf-8",
        )
        if manifest:
            print(f"\n{len(manifest)} Copilot batch(es) for {len(remaining.rows)} unmatched rows:")
            for entry in manifest:
                pages = [p + 1 for p in entry["pages"]]
                print(f"  batch {entry['batch']}: pages {pages}, {entry['rows']} rows")
                print(f"    instructions {entry['instructions']}")
                print(f"    sheet        {entry['sheet']}")
                print(f"    save reply   {entry['expected_response']}")
        else:
            print("\nno Copilot batches needed -- precedent covered every row")

    print(f"\nNext: open {sheet_path}, complete it, then run scripts/annotate.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
