#!/usr/bin/env python3
"""Optional: merge Copilot replies into the control sheet's blank cells.

Only needed when ``build_sheet.py --copilot-batches`` was used because mined
precedent left too many rows blank to type out by hand. The normal path skips
this entirely -- open the control sheet, fill it in, run annotate.py.

Reads:
    build/rows.json                    geometry, to reattach to each reply
    build/batches.json                 manifest from build_sheet.py: which pages
                                        are in which batch, and where each
                                        batch's response should be
    build/copilot_batchN_response.csv  what the human pasted/attached back
    build/proposals.json               precedent proposals, kept and merged with

Writes:
    build/proposals.json      precedent + Copilot proposals together
    build/control_sheet.xlsx  rewritten with the Copilot cells filled in
    build/qc_preview.pdf      pre-review visual check -- NOT a deliverable

Everything written here is a proposal, marked ``suggested`` so the control sheet
greys it. The human then completes the sheet, and that is where review_status
moves off ``proposed``. Nothing in this script may set any other status.

**This overwrites build/control_sheet.xlsx.** Run it before doing manual work in
the sheet, not after -- any hand-typed cells would be lost. It refuses to
overwrite a sheet whose review_status has already been moved off ``proposed``,
which is the cheap check that catches the mistake.

A parse failure on any batch is fatal and prints the raw pasted text: better to
have the human re-paste one batch than to ship a silently short annotation set.

Usage:
    python scripts/ingest_response.py [--build-dir build] [--pdf blank_crf.pdf]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import control_sheet, layout, parse_response, prompt, stamp  # noqa: E402
from pipeline.models import AnnotationSet, ReviewStatus, RowSet  # noqa: E402
from pipeline.msg import apply_colors  # noqa: E402
from pipeline.parse_response import ResponseParseError  # noqa: E402


def _refuse_if_reviewed(sheet: Path) -> None:
    """Stop rather than overwrite human review work."""
    if not sheet.exists():
        return
    try:
        reviewed = [
            r
            for r in control_sheet.read_control_sheet(sheet)
            if r.review_status and r.review_status != ReviewStatus.PROPOSED.value
        ]
    except control_sheet.ControlSheetError:
        return  # not a sheet we wrote; nothing of ours to protect
    if reviewed:
        raise SystemExit(
            f"{sheet} already has {len(reviewed)} reviewed row(s) "
            f"(first: {reviewed[0].row_id} at spreadsheet row {reviewed[0].excel_row}).\n"
            "This script rewrites the sheet and would discard that work. Ingest "
            "Copilot replies before reviewing, or merge them by hand."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--build-dir", type=Path, default=Path("build"))
    parser.add_argument(
        "--pdf", type=Path, default=None, help="blank CRF, for layout and the QC preview"
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="accept a reply that does not cover every row in its batch (off by "
        "default: a silently short annotation set has nothing downstream to flag it)",
    )
    args = parser.parse_args(argv)

    rows_path = args.build_dir / "rows.json"
    if not rows_path.exists():
        raise SystemExit(f"missing {rows_path} -- run build_sheet.py first")
    rows = RowSet.model_validate_json(rows_path.read_text(encoding="utf-8"))
    pdf = args.pdf or Path(rows.source_pdf)

    manifest_path = args.build_dir / "batches.json"
    if not manifest_path.exists():
        raise SystemExit(
            f"missing {manifest_path} -- run build_sheet.py --copilot-batches first"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest["batches"] if isinstance(manifest, dict) else manifest

    sheet_path = args.build_dir / "control_sheet.xlsx"
    _refuse_if_reviewed(sheet_path)

    # Precedent proposals are kept: Copilot was only asked about the rows
    # precedent could not answer, so discarding them would throw away the better
    # half of the annotations.
    proposals_path = args.build_dir / "proposals.json"
    existing = (
        AnnotationSet.model_validate_json(proposals_path.read_text(encoding="utf-8")).annotations
        if proposals_path.exists()
        else []
    )
    already = {a.row_id for a in existing if a.row_id}

    # The batches were built from the narrowed row set, so completeness has to be
    # checked against that same set -- the full one would report every
    # pre-populated row as missing from the reply.
    asked = prompt.rows_needing_annotation(rows, AnnotationSet(source_pdf=rows.source_pdf))
    asked = asked.model_copy(
        update={"rows": [r for r in rows.rows if r.row_id not in already]}
    )

    collected: list = []
    covered: set[str] = set()
    for entry in entries:
        response_file = Path(entry["expected_response"])
        if not response_file.exists():
            raise SystemExit(
                f"missing {response_file} -- paste/attach the Copilot reply for "
                f"batch {entry['batch']} (pages "
                f"{entry['pages'][0] + 1}-{entry['pages'][-1] + 1}) before ingesting"
            )
        try:
            result = parse_response.ingest_response_file(
                response_file, asked, entry["pages"], allow_partial=args.allow_partial
            )
        except ResponseParseError as exc:
            # Show the human what came back so they can fix it and re-paste,
            # rather than leaving a silent gap in the annotation set.
            raise SystemExit(exc.report()) from exc
        collected.extend(result.annotations)
        covered.update(a.row_id for a in result.annotations if a.row_id)

    # Completeness across the whole document, not just within each batch --
    # catches a batch missing from the manifest entirely.
    missing = {r.row_id for r in asked.rows} - covered
    if missing and not args.allow_partial:
        raise SystemExit(
            f"{len(missing)} row(s) are not covered by any ingested batch: "
            f"{sorted(missing)}\nCheck {manifest_path} against what was actually "
            "pasted, or pass --allow-partial to proceed anyway."
        )

    annotations = AnnotationSet(
        source_pdf=rows.source_pdf, pages=rows.pages, annotations=existing + collected
    )
    annotations = apply_colors(annotations, rows)
    obstacles = layout.text_obstacles(pdf) if pdf.exists() else None
    annotations = layout.place_annotations(annotations, rows, obstacles=obstacles)

    proposals_path.write_text(annotations.model_dump_json(indent=2), encoding="utf-8")
    print(
        f"wrote {proposals_path} "
        f"({len(existing)} from precedent + {len(collected)} from Copilot)"
    )

    written = control_sheet.write_control_sheet(rows, sheet_path, annotations)
    print(f"wrote {written}   (grey cells = suggested, need review)")

    if pdf.exists():
        qc = stamp.stamp_annotations(
            pdf, annotations, args.build_dir / "qc_preview.pdf", rows=rows
        )
        print(f"wrote {qc}  (pre-review preview only, not a submission artifact)")
    else:
        print(f"skipped QC preview: {pdf} not found (pass --pdf)")

    print(f"\nNext: complete {written}, then run scripts/annotate.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
