# `scripts/ingest_response.py`

## Role in the pipeline

**Step 2 of 3**, and the busiest of the three top-level scripts. Runs after a
human has pasted every batch's instructions into Copilot 365 chat and saved
each reply to the path `batches.json` expects. This script reads all of that
back, parses and validates every batch's reply, checks that the *whole
document* is covered (not just each batch individually), positions every
annotation off the field it describes, and writes out the three artifacts
that follow: `proposals.json` (a record of every proposed annotation),
`blankcrf.xfdf` (the file a human reviews next, in Acrobat or eventually a
Dash UI), and `qc_preview.pdf` (an optional, non-authoritative quick look).

The module docstring states the rule this script exists to enforce plainly:
**"Everything written here is a proposal."** Nothing in this script is
permitted to set `review_status` to anything other than `proposed` — that
transition only happens later, when a human reviews `blankcrf.xfdf`.

## Python concepts you'll see here

**Reading a pydantic model back from JSON.** `FieldSet.model_validate_json(...)`
is the mirror image of `.model_dump_json()` used to write it in
`extract_and_prompt.py` — parses a JSON string and validates it against the
model's schema in one call, raising a `pydantic.ValidationError` if the file
doesn't match what `FieldSet` expects (e.g. someone hand-edited it into an
invalid shape).

**A CLI flag as a deliberate escape hatch, off by default.**
```python
parser.add_argument(
    "--allow-partial",
    action="store_true",
    help="accept a reply that does not cover every field in its batch (off "
    "by default: a silently short annotation set has nothing downstream "
    "to flag it)",
)
```
`action="store_true"` makes `--allow-partial` a boolean flag with no value to
supply — its mere presence on the command line sets `args.allow_partial =
True`; its absence leaves it `False`. The help text doubles as an explanation
of *why* the default is what it is, worth noting as a small writing habit:
documenting the reasoning behind a default, not just what the flag does.

**Two-level completeness checking with set accumulation.** This script checks
completeness *twice*, at two different scopes — described in detail below —
using a running `set` that accumulates across the whole loop:
```python
covered_field_ids: set[str] = set()
for entry in manifest:
    ...
    covered_field_ids.update(a.field_id for a in result.annotations if a.field_id)
```
`set.update(iterable)` adds every element of the iterable to the set in
place (as opposed to `set.add(x)`, which adds one element) — here fed a
generator expression filtering out any `None` field IDs (shouldn't normally
occur, but the `if a.field_id` guard is cheap insurance).

**Raising `SystemExit` with a message directly.** Rather than `print(...)`
followed by `return 1`, several failure paths do:
```python
raise SystemExit(f"missing {response_file} -- paste/attach the Copilot reply for ...")
```
`SystemExit`, when raised with a string argument (rather than an integer),
causes Python to print that string to stderr and exit with status code `1` —
a concise way to report a fatal CLI error and stop immediately, without a
separate `sys.exit(1)` call after printing.

## Walkthrough of `main(argv=None) -> int`

1. **Argument parsing** — `--build-dir` (default `build/`), `--pdf`
   (optional; needed only for the QC preview render), `--allow-partial`.
2. **Load `fields.json`** into a `FieldSet`, and resolve which PDF to use for
   the QC preview: the `--pdf` flag if given, else whatever path was recorded
   in `fieldset.source_pdf` at extraction time.
3. **Load `batches.json`**, failing fast with a clear message
   (`SystemExit(f"missing {manifest_path} -- run extract_and_prompt.py
   first")`) if step 1 was never run.
4. **Per-batch ingestion loop.** For each manifest entry:
   - Check the expected response file exists; if not, `SystemExit` naming
     exactly which batch and page range is still outstanding.
   - Call `parse_response.ingest_response_file(...)` (see
     [../pipeline/parse_response.md](../pipeline/parse_response.md)),
     catching `ResponseParseError` and re-raising as `SystemExit(exc.report())`
     — surfacing the raw pasted text (truncated) directly in the terminal so
     the human can see exactly what went wrong and fix it, rather than a
     bare stack trace.
   - Accumulate the resulting annotations into `collected`, and every
     covered `field_id` into `covered_field_ids`. Completeness *within* a
     batch is already checked inside `ingest_response_file` itself (per-batch
     `expected` vs. `missing`, see [../pipeline/parse_response.md](../pipeline/parse_response.md));
     this loop is building toward the **second**, document-wide check below.
5. **Document-wide completeness check** — computed via set subtraction, the
   same pattern used inside `parse_response.py` itself:
   ```python
   missing_overall = {f.field_id for f in fieldset.fields} - covered_field_ids
   ```
   This check exists to catch a failure mode the per-batch check *can't* see:
   an entire batch missing from the manifest, or a batch whose response was
   never processed for some reason — a gap that wouldn't show up as
   "incomplete" within any single batch, because from any one batch's
   perspective, everything it was asked about *was* answered.
6. **Build the `AnnotationSet`** from all collected annotations, then call
   `layout.place_annotations(...)` (see
   [../pipeline/layout.md](../pipeline/layout.md)) to move every annotation
   off the field it describes — passing `layout.text_obstacles(pdf)` as
   additional obstacles **only if the PDF file actually exists**
   (`if pdf.exists() else None`), since the QC PDF (and thus its printed text)
   is optional.
7. **Write `proposals.json`**, `blankcrf.xfdf` (via `xfdf.write_xfdf`), and —
   conditionally, if the source PDF is available — `qc_preview.pdf` (via
   `stamp.stamp_annotations`, see [../pipeline/stamp.md](../pipeline/stamp.md)),
   printing a warning and skipping the QC step gracefully rather than
   failing the whole run if the PDF isn't found.
8. **Print next-step instructions** — reminding the human to review/edit
   `blankcrf.xfdf` before running `render_final.py`.
</content>
