#!/usr/bin/env python3
"""Step 2 of 3: filled-in Copilot sheets -> validated annotations -> XFDF + QC PDF.

Reads:
    build/fields.json                  geometry, to reattach to each reply
    build/batches.json                 manifest from extract_and_prompt.py:
                                        which pages are in which batch, and
                                        where each batch's response should be
    build/copilot_batchN_response.csv  what the human pasted/attached back

Writes:
    build/proposals.json    annotation records, all review_status=proposed
    build/blankcrf.xfdf     the artifact the human reviews and may hand-edit
    build/qc_preview.pdf    pre-review visual check -- NOT a deliverable

Everything written here is a proposal. The human then reviews
build/blankcrf.xfdf (Acrobat, or the Dash review UI later) and that is where
review_status moves off `proposed`. Nothing in this script may set any other
status.

A parse failure on any batch is fatal and prints the raw pasted text: better
to have the human re-paste one batch than to ship a silently short annotation
set. Completeness is checked per batch (does this batch's reply cover every
field extract.py found on its pages) and then again across the whole document,
so a batch silently missing from the manifest is caught rather than producing
a quietly incomplete submission artifact.

Usage:
    python scripts/ingest_response.py [--build-dir build] [--pdf fixtures/sample_crf.pdf]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import layout, parse_response, stamp, xfdf  # noqa: E402
from pipeline.models import AnnotationSet, FieldSet  # noqa: E402
from pipeline.parse_response import ResponseParseError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--build-dir", type=Path, default=Path("build"))
    parser.add_argument(
        "--pdf", type=Path, default=None, help="blank CRF, for the QC preview"
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="accept a reply that does not cover every field in its batch (off "
        "by default: a silently short annotation set has nothing downstream "
        "to flag it)",
    )
    args = parser.parse_args(argv)

    fieldset = FieldSet.model_validate_json(
        (args.build_dir / "fields.json").read_text(encoding="utf-8")
    )
    pdf = args.pdf or Path(fieldset.source_pdf)

    manifest_path = args.build_dir / "batches.json"
    if not manifest_path.exists():
        raise SystemExit(f"missing {manifest_path} -- run extract_and_prompt.py first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    collected: list = []
    covered_field_ids: set[str] = set()
    for entry in manifest:
        response_file = Path(entry["expected_response"])
        if not response_file.exists():
            raise SystemExit(
                f"missing {response_file} -- paste/attach the Copilot reply for "
                f"batch {entry['batch']} (pages "
                f"{entry['pages'][0] + 1}-{entry['pages'][-1] + 1}) before ingesting"
            )
        try:
            result = parse_response.ingest_response_file(
                response_file, fieldset, entry["pages"], allow_partial=args.allow_partial
            )
        except ResponseParseError as exc:
            # Show the human what came back so they can fix it and re-paste,
            # rather than leaving a silent gap in the annotation set.
            raise SystemExit(exc.report()) from exc
        collected.extend(result.annotations)
        covered_field_ids.update(a.field_id for a in result.annotations if a.field_id)

    # Completeness across the whole document, not just within each batch --
    # catches a batch that is missing from the manifest entirely.
    missing_overall = {f.field_id for f in fieldset.fields} - covered_field_ids
    if missing_overall and not args.allow_partial:
        raise SystemExit(
            f"{len(missing_overall)} field(s) are not covered by any ingested batch: "
            f"{sorted(missing_overall)}\nCheck build/batches.json against what was "
            "actually pasted, or pass --allow-partial to proceed anyway."
        )

    annotations = AnnotationSet(
        source_pdf=fieldset.source_pdf, pages=fieldset.pages, annotations=collected
    )
    # Move each annotation off the field it annotates, or the text renders on
    # top of the box a site writes in. Feed the form's printed text in as
    # obstacles too, so annotations do not land on captions.
    obstacles = layout.text_obstacles(pdf) if pdf.exists() else None
    annotations = layout.place_annotations(annotations, fieldset, obstacles=obstacles)

    proposals_json = args.build_dir / "proposals.json"
    proposals_json.write_text(annotations.model_dump_json(indent=2), encoding="utf-8")
    print(f"wrote {proposals_json} ({len(collected)} proposed annotations)")

    xfdf_path = xfdf.write_xfdf(annotations, args.build_dir / "blankcrf.xfdf")
    print(f"wrote {xfdf_path}")

    if pdf.exists():
        qc = stamp.stamp_annotations(pdf, annotations, args.build_dir / "qc_preview.pdf")
        print(f"wrote {qc}  (pre-review preview only, not a submission artifact)")
    else:
        print(f"skipped QC preview: {pdf} not found (pass --pdf)")

    print("\nNext: review/edit build/blankcrf.xfdf, then run render_final.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
