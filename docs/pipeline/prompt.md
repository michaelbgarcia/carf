# `pipeline/prompt.py`

## Role in the pipeline

This is the file that turns extracted fields into something a human can paste
into Microsoft Copilot 365's chat window. Because there's no programmatic LLM
access anywhere in this project (see the repo root README's "human-in-the-loop
hop" section), the *prompt* isn't sent over an API — it's written to disk as
text and CSV files, and a person copies it by hand.

The design here reflects a real lesson learned during the project (documented
at length in the repo's README under "Batches, not pages"): the original
approach generated one self-contained prompt *per page*, with field data
embedded as numbered prose. That doesn't scale — a several-hundred-page CRF
would mean several hundred manual copy/paste round trips, and *round-trip
count*, not per-page prompt quality, turned out to be the actual bottleneck
when a human has to sit at the keyboard for every one. This file's current
design instead separates two things that used to be fused:

- **Instructions** (`build_batch_instructions`) — short, static text (framing,
  rules, output format) that does not grow no matter how many fields are in a
  batch.
- **Spec sheet** (`build_spec_sheet`) — a CSV with one row per field and empty
  columns for Copilot to fill in. This is what actually carries the bulk of
  the data, and because it can span many pages in one file, it's the lever
  that lets `batch_pages` group several pages into a single round trip.

## Python concepts you'll see here

**The `csv` module — writing.** `csv.DictWriter` writes rows from
dictionaries, matched to column names by key, handling quoting and escaping
of commas/newlines inside cell values automatically — the reason field
`context` strings (which can legitimately contain punctuation) don't need any
manual escaping here.

**`io.StringIO` as an in-memory file.** `csv.DictWriter` (like most of the
standard library's file-oriented tools) wants a "file-like" object with a
`.write()` method — it doesn't know or care whether that object is backed by
an actual file on disk. `io.StringIO()` is an in-memory buffer that behaves
like a text file, so `build_spec_sheet` can build a complete CSV string in
memory (`buf.getvalue()`) without ever touching the filesystem — useful both
for testability (no temp files needed) and because the caller (`write_batches`)
decides *where* to write it, keeping this function pure text-generation.

**Multi-line string constants with triple-quoted strings + `\` line-continuation.**
```python
_FRAMING = """\
You are annotating a blank Case Report Form (CRF) ...
"""
```
The backslash immediately after the opening `"""` suppresses the newline that
would otherwise appear as the string's first character — a common trick for
writing a long literal block of text that starts flush against the margin
without a leading blank line. `_FRAMING`, `_RULES`, `_FORMAT`, `_REMINDER`
are all module-level string constants built this way — effectively template
fragments assembled by `build_batch_instructions`.

**`str.format()` with named placeholders.** `_FRAMING.format(page_span=...)`
and `_FORMAT.format(n_columns=...)` substitute `{page_span}` /
`{n_columns}` placeholders embedded in the template strings. This is an
older sibling of f-strings — functionally similar, but useful here because
the *string itself* is a variable (`_FRAMING`) defined far from where its
placeholders get filled in, so an f-string (which needs the expression
inline at definition time) wouldn't apply.

**Greedy accumulation in a single pass.** `batch_pages`'s core loop is a
classic *greedy algorithm*: walk through items in order, add each one to the
current group unless doing so would break a constraint, in which case start a
new group. Worth recognizing as a pattern independent of this specific
codebase — the same shape shows up in bin-packing, log-batching, and pagination
code generally.

**`Path.name` vs. the full path.** `Path(fieldset.source_pdf).name` extracts
just the filename (`sample_crf.pdf`) from a possibly-long path, for display
in the instructions text without leaking the local filesystem layout.

## Functions, in file order

### `batch_pages(fieldset, max_fields_per_batch=DEFAULT_MAX_FIELDS_PER_BATCH) -> list[list[int]]`
Groups page indexes into batches, each capped at (roughly)
`max_fields_per_batch` fields, **without ever splitting a single page across
two batches** — the docstring explains why: a page's caption context ("this
row belongs to the DEMOGRAPHICS section") is only meaningful together, so
keeping a whole page in one batch is both simpler to reason about and more
correct than trying to split mid-page. The algorithm:
```python
counts = {p.page_index: len(fieldset.for_page(p.page_index)) for p in fieldset.pages}
batches: list[list[int]] = []
current: list[int] = []
current_count = 0

for page in sorted(counts):
    n = counts[page]
    if current and current_count + n > max_fields_per_batch:
        batches.append(current)
        current, current_count = [], 0
    current.append(page)
    current_count += n

if current:
    batches.append(current)
return batches
```
`counts` is a **dict comprehension** mapping each page index to its field
count. The loop is greedy: keep adding whole pages to `current` until the
*next* page would push the running total over the ceiling, at which point the
current batch is closed out and a new one starts. Note the guard `if current
and ...` — without checking `current` is non-empty first, a single
oversized page would immediately close out an empty batch and loop forever
appending nothing; as written, an oversized page simply becomes its own
(oversized) batch, which the docstring calls out explicitly: "not dropped or
split." The trailing `if current: batches.append(current)` flushes whatever's
left after the loop ends (the last batch never triggers the "would exceed"
check that closes earlier ones).

### `build_spec_sheet(fieldset, page_indexes) -> str`
Renders the CSV text for one batch. Sorts fields into a natural reading order
— top-to-bottom, then left-to-right — via a **sort key with a negated
value**:
```python
sorted(fieldset.for_pages(page_indexes), key=lambda f: (f.page_index, -f.bbox.y1, f.bbox.x0))
```
Recall `bbox.y1` is the *top* edge in PDF user space (y increases upward — see
[geometry.md](geometry.md)), so sorting by `-f.bbox.y1` ascending is the same
as sorting by `y1` descending — i.e., highest-on-the-page (largest y1) comes
first. Combined with `f.bbox.x0` as a tiebreaker, this produces the reading
order a human scans a form in: top to bottom, left to right.

For each field, writes a row combining the read-only columns
(`field_id`, human-facing 1-based `page`, `label`, `context`,
`acroform_name`) with every fill-in column left blank — a **dict unpacking
inside a dict literal**:
```python
writer.writerow({
    "field_id": f.field_id,
    ...
    **{col: "" for col in FILL_COLUMNS},
})
```
`{col: "" for col in FILL_COLUMNS}` is a dict comprehension producing
`{"kind": "", "domain": "", ...}`; the `**` inside the outer dict literal
*spreads* those key-value pairs into the surrounding dict, avoiding having to
write out all eight empty-string assignments by hand (and staying correct
automatically if `FILL_COLUMNS` ever changes).

### `build_batch_instructions(fieldset, page_indexes, batch_num, total_batches) -> str`
Assembles the static instructions text for one batch: a header naming the
batch number and page span, the framing paragraph (with `{page_span}`
filled in), a line naming the accompanying sheet's filename and how many
fields it covers, the rules block, the output-format block, and the one-line
reminder — joined with `"\n".join([...])`, a common idiom for building a
multi-part text block from a list of pieces without manually concatenating
`+` between each one.

### `write_batches(fieldset, out_dir, max_fields_per_batch=...) -> list[dict]`
The file's top-level entry point, called from
`scripts/extract_and_prompt.py`. Calls `batch_pages` to decide the grouping,
then for each batch writes `copilot_batchN_instructions.txt` and
`copilot_batchN_sheet.csv` to `out_dir`, and returns a **manifest** — a list
of plain dicts, one per batch, recording which pages it covers and where its
three associated files (instructions, sheet, expected response) live. This
manifest is written to `build/batches.json` by the calling script and read
back, unmodified, by `scripts/ingest_response.py` — so the batching decision
is made exactly once and never re-derived. `out_dir.mkdir(parents=True,
exist_ok=True)` creates the output directory (and any missing parents)
without raising if it already exists — the standard idiom for "make sure this
directory exists" in modern Python, replacing the older `os.makedirs` +
`try/except FileExistsError` pattern.
</content>
