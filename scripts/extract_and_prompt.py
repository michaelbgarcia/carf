#!/usr/bin/env python3
"""Step 1 of 3: blank CRF PDF -> extracted fields -> Copilot batch materials.

Writes:
    build/fields.json                    extracted fields, needed again at
                                          ingest time to reattach bbox/page
    build/batches.json                   manifest: which pages are in which
                                          batch, and where each batch's files
                                          live -- ingest_response.py reads
                                          this back rather than re-deriving it
    build/copilot_batch1_instructions.txt   short, static per batch
    build/copilot_batch1_sheet.csv          the field data, one row per field
    ...

Then the human hop, once per batch (not once per page -- see README "Batches,
not pages"): paste the instructions text into Copilot 365 chat, attach (or
paste) the accompanying sheet, and save the reply verbatim to
build/copilot_batchN_response.csv. Nothing about that step is automatable
here -- there is no API.

Usage:
    python scripts/extract_and_prompt.py <blank_crf.pdf> [--build-dir build]
        [--max-fields-per-batch N]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import extract, prompt  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", type=Path, help="blank CRF PDF")
    parser.add_argument("--build-dir", type=Path, default=Path("build"))
    parser.add_argument(
        "--max-fields-per-batch",
        type=int,
        default=prompt.DEFAULT_MAX_FIELDS_PER_BATCH,
        help=(
            f"fields per Copilot batch (default {prompt.DEFAULT_MAX_FIELDS_PER_BATCH}, "
            "an unvalidated guess -- tune once a real Copilot session's practical "
            "attachment/context limit is known)"
        ),
    )
    args = parser.parse_args(argv)

    args.build_dir.mkdir(parents=True, exist_ok=True)

    fieldset = extract.extract_fields(args.pdf)

    fields_json = args.build_dir / "fields.json"
    fields_json.write_text(fieldset.model_dump_json(indent=2), encoding="utf-8")
    print(f"wrote {fields_json} ({len(fieldset.fields)} fields)")

    manifest = prompt.write_batches(
        fieldset, args.build_dir, max_fields_per_batch=args.max_fields_per_batch
    )
    manifest_path = args.build_dir / "batches.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"wrote {manifest_path} ({len(manifest)} batch(es))")

    for entry in manifest:
        pages = entry["pages"]
        span = f"page {pages[0] + 1}" if len(pages) == 1 else f"pages {pages[0] + 1}-{pages[-1] + 1}"
        print(f"  batch {entry['batch']}: {span}")
        print(f"    {entry['instructions']}")
        print(f"    {entry['sheet']}")

    print(
        "\nNext, per batch: paste the instructions into Copilot 365 chat, attach "
        "(or paste) the sheet, and save the reply verbatim to each batch's "
        "expected_response path in batches.json. Then run ingest_response.py."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
