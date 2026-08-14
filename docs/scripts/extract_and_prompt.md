# `scripts/extract_and_prompt.py`

## Role in the pipeline

**Step 1 of 3.** The first thing a human actually runs against a real CRF.
Takes a blank CRF PDF and produces everything needed to start the manual
Copilot round trip: the extracted field data (`build/fields.json`), a
batching manifest (`build/batches.json`), and, per batch, a short
instructions text file and a CSV spec sheet. It's a thin CLI wrapper — nearly
all the actual logic lives in `pipeline.extract` (see
[../pipeline/extract.md](../pipeline/extract.md)) and `pipeline.prompt` (see
[../pipeline/prompt.md](../pipeline/prompt.md)); this script's job is to wire
them together, write their outputs to disk, and print a human-readable
summary of what to do next.

## Python concepts you'll see here

**`argparse` with a computed default and a dynamically-built help string.**
```python
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
```
Rather than hard-coding `150` here and separately in `prompt.py`, the default
value is *imported* from `prompt.DEFAULT_MAX_FIELDS_PER_BATCH` — so the two
files can never silently drift apart, and the `--help` text documents the
actual value automatically via an f-string, no matter what it's later tuned
to. This is a small but genuinely good practice: single-source a constant,
and generate anything that describes it (like help text) from that same
source rather than restating it by hand.

**Writing JSON two different ways.** This script writes two JSON files with
two different techniques:
```python
fields_json.write_text(fieldset.model_dump_json(indent=2), encoding="utf-8")
...
manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
```
`fieldset` is a pydantic model, so it has its own `.model_dump_json()` method
that serializes it (handling nested models, enums, datetimes, etc.
correctly) directly to a JSON string. `manifest`, in contrast, is a **plain
list of plain dicts** returned by `prompt.write_batches` (not a pydantic
model) — so it goes through the standard library's `json.dumps` instead. Both
calls pass `indent=2` for human-readable (pretty-printed) output rather than
a single dense line — worth doing for any file a person might actually open
and read, as opposed to one only ever consumed by other code.

**`Path.write_text` vs. `open(...).write(...)`.** `fields_json.write_text(...,
encoding="utf-8")` is a `pathlib.Path` convenience method that opens the
file, writes the string, and closes it again, all in one call — shorter than
the equivalent `with open(fields_json, "w", encoding="utf-8") as f:
f.write(...)`, and used throughout this project wherever a whole file's
content is available as a single string upfront (as opposed to being streamed
incrementally, which is what `csv.DictWriter` in `prompt.py` needs `io.StringIO`
or an open file handle for instead).

## Walkthrough of `main(argv=None) -> int`

There's only one function besides the `if __name__ == "__main__"` guard. In
order:

1. **Argument parsing** — positional `pdf` argument (the blank CRF path),
   plus `--build-dir` (default `build/`) and `--max-fields-per-batch`.
2. **Ensure the build directory exists** — `args.build_dir.mkdir(parents=True,
   exist_ok=True)`.
3. **Extract** — `fieldset = extract.extract_fields(args.pdf)`, calling into
   [../pipeline/extract.md](../pipeline/extract.md).
4. **Write `fields.json`** and print how many fields were found.
5. **Batch and write the Copilot materials** — `prompt.write_batches(fieldset,
   args.build_dir, max_fields_per_batch=args.max_fields_per_batch)` (see
   [../pipeline/prompt.md](../pipeline/prompt.md)), returning the manifest
   list described there.
6. **Write `batches.json`** — the manifest, so `scripts/ingest_response.py`
   can read back exactly which pages are in which batch and where each
   batch's files live, without re-running the batching algorithm itself
   (which could, in principle, produce a different grouping if run again with
   different arguments — persisting the manifest avoids that entire class of
   inconsistency).
7. **Print a per-batch summary** — for each manifest entry, formats the page
   span as a human-readable string (`"page 3"` for a single page, `"pages
   3-5"` for a range) via a **conditional expression**:
   ```python
   span = f"page {pages[0] + 1}" if len(pages) == 1 else f"pages {pages[0] + 1}-{pages[-1] + 1}"
   ```
8. **Print next-step instructions** — a plain-English reminder of the manual
   hop: paste the instructions into Copilot 365 chat, attach/paste the sheet,
   save the reply to the path named in `batches.json`, then run
   `ingest_response.py`. This is the script's way of keeping the "what do I
   do next" burden on the tool rather than requiring the human operator to
   remember the whole multi-step workflow from documentation alone.
</content>
