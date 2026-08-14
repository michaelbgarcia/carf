# `pipeline/parse_response.py`

## Role in the pipeline

This is the mirror image of `prompt.py`: instead of generating text for a
human to paste *out*, it recovers structured data from whatever a human
pastes *back in* after their Copilot 365 chat session. That input is
explicitly **not** a clean API response — it's whatever ends up in a saved
file after a person copies text out of (or downloads an attachment from) a
chat UI, which is free to reformat, reword, and mangle things despite being
told not to.

The module docstring lists the specific failure modes this is built to
recover from: chat UIs rendering a CSV reply as a **markdown table** instead
(treated here as the *primary* case, not a fallback, since it's the
overwhelmingly likely outcome); **smart quotes** substituted for straight
ones (which breaks CSV quoting, since the `csv` module matches on literal
`"`); a **conversational wrapper** ("Sure! Here's the completed sheet...")
around the actual data; and tabs silently normalized to spaces.

Two rules govern the whole file, stated explicitly in the docstring and worth
internalizing as a general parsing philosophy:
- **Fail loudly.** Anything that can't be recovered raises an exception
  carrying the raw pasted text, so a human can see exactly what went wrong
  and re-paste — never silently drop or skip a row.
- **Everything is a proposal.** This module only ever sets
  `review_status=PROPOSED`; nothing it produces can already be "accepted."

## Python concepts you'll see here

**Custom exception classes with extra state.**
```python
class ResponseParseError(ValueError):
    def __init__(self, message: str, raw_text: str) -> None:
        super().__init__(message)
        self.raw_text = raw_text
```
Subclassing a built-in exception (`ValueError`) rather than `Exception`
directly is good practice when the failure genuinely *is* a value problem —
callers that only know how to catch `ValueError` in general still catch this.
`super().__init__(message)` calls the parent class's constructor so the
message still behaves like a normal exception message (`str(exc)` works);
`self.raw_text = raw_text` attaches extra data beyond what a plain exception
carries, retrievable by anything that catches it specifically as a
`ResponseParseError`. `IncompleteResponseError` further subclasses
`ResponseParseError`, adding a `missing: list[str]` attribute — exceptions can
form their own small class hierarchy just like any other objects, and
`except ResponseParseError` will catch instances of the more specific
`IncompleteResponseError` too, since it *is* one (inheritance again).

**`re.compile` and precompiled regular expressions.** Every regex used more
than once is compiled once at module load time (`_FENCE = re.compile(...)`)
rather than passed as a raw string to `re.sub`/`re.match` each call — slightly
faster, and it gives each pattern a name that documents what it matches.
`re.MULTILINE` changes `^`/`$` to match at the start/end of *each line* rather
than only the start/end of the whole string — necessary here since these
patterns are matched line-by-line against a multi-line pasted reply.

**Dict translation via a loop, not `str.translate`.** `_normalize_quotes`
iterates a dict of bad→good character mappings and calls `.replace()`
repeatedly:
```python
for bad, good in _SMART_QUOTES.items():
    text = text.replace(bad, good)
return text
```
`str.translate()` (with `str.maketrans`) is the more "idiomatic" one-pass way
to do single-character substitution in Python, but it's harder to read for a
handful of specific mappings — this code trades a small amount of efficiency
for clarity, a reasonable choice given the input sizes involved (a pasted
chat reply, not gigabytes of text).

**`csv.Sniffer`.** A standard-library helper that guesses a CSV dialect
(delimiter, quoting style) by examining a text sample — used here, wrapped in
`try/except csv.Error`, to detect whether a reply actually came back
tab-separated or semicolon-separated despite being asked for commas, falling
back to the default "excel" dialect if sniffing itself fails. This is a good
example of *trying* something smarter first and falling back to something
simple and safe, rather than requiring the smarter path to succeed.

**Set literals for membership tests.** `_NULLISH = {"", "null", "none", "n/a",
"na", "not applicable", "-"}` — a `set` literal (curly braces, comma-
separated values, no colons) rather than a `list`, because the only operation
performed on it is `in` (membership testing), which is O(1) on a set versus
O(n) on a list.

**Dict/list comprehensions with tuple unpacking.**
```python
return {
    (k or "").strip().lower(): (v or "").strip()
    for k, v in row.items() if k
}
```
`row.items()` yields `(key, value)` pairs, unpacked directly into `k, v` in
the comprehension's `for` clause. `(k or "")` guards against `k` being `None`
(possible if a CSV row has a stray extra unnamed column, which `csv.DictReader`
represents with a `None` key) before calling `.strip()` on it.

**`isinstance` checks skipped in favor of duck typing.** Note how loosely
"row" data is typed throughout this file — `dict[str, str]`, built and rebuilt
by different code paths (CSV rows, markdown-table rows) that don't share a
class, only a *shape*. This is Python's usual style: code cares that
something behaves like a dict with string keys, not that it's literally one
specific class.

## Functions, in file order

### `ResponseParseError` / `IncompleteResponseError` (exception classes)
Described above. `ResponseParseError.report(limit=2000)` formats the error
plus a *truncated* excerpt of the raw pasted text (so a human sees enough to
diagnose the problem without a terminal being flooded by, say, a 50-page
pasted CSV) — string slicing (`excerpt[:limit]`) plus an f-string noting how
many characters were cut.

### `_strip_fences(text) -> str`
Removes any Markdown code-fence line (```` ``` ```` or ` ```csv `) via the
precompiled `_FENCE` regex — applied regardless of whether a fence is
actually present, the same defensive posture the module docstring calls out:
"strip a code fence even though the prompt said not to add one," because
what's asked for and what a chat UI actually returns aren't guaranteed to
match.

### `_normalize_quotes(text) -> str` / `normalize_pasted_text(text) -> str`
`_normalize_quotes` does the character substitution described above;
`normalize_pasted_text` composes it with `_strip_fences` — this is the first
processing step every parse path applies to raw pasted text.

### `_find_markdown_table_block(lines) -> Optional[list[str]]`
Scans line-by-line for the first place a markdown table header (`| a | b |`)
is immediately followed by a separator row (`|---|---|`), then greedily
collects every subsequent row that still matches the table-row pattern:
```python
for i in range(len(lines) - 1):
    if _MD_ROW.match(lines[i]) and _MD_SEPARATOR.match(lines[i + 1]):
        block = [lines[i], lines[i + 1]]
        j = i + 2
        while j < len(lines) and _MD_ROW.match(lines[j]):
            block.append(lines[j])
            j += 1
        return block
return None
```
This is what lets a chatty wrapper ("Sure! Here's the completed sheet:" ...
"Let me know if...") sit around the actual table without breaking the parse —
the function searches *for* the table rather than assuming the whole reply
*is* the table.

### `_parse_markdown_table(block) -> list[dict[str, str]]`
Given an isolated table block, splits each row on `|` and matches cells to
the header row. The critical defensive check here — and the fix for a real
bug hit during development, per the docstring — is validating that every data
row has exactly as many cells as the header:
```python
if len(row_cells) != len(header):
    raise ValueError(
        f"row has {len(row_cells)} cells but the header has {len(header)} -- "
        f"likely an unescaped '|' inside a cell value shifted the columns: {ln!r}"
    )
```
Why this matters: an unescaped `|` inside a free-text cell (e.g. a
`rationale` sentence containing a literal pipe character) silently shifts
every column after it — the row still "parses" syntactically, just with wrong
values landing in wrong columns, and — as the docstring notes — pydantic
validation on the shifted result doesn't reliably catch it, because two
swapped free-text string fields both still individually look like valid
strings. Checking cell *count* against the header turns that from a silent
misattribution into a loud, immediate failure. `tests/test_parse_response.py`
has a dedicated regression test for exactly this
(`test_a_stray_pipe_inside_a_markdown_cell_fails_loudly_not_silently`).

### `_isolate_tabular_block(lines) -> Optional[str]`
For the CSV/TSV path: finds the header line (matched via `_HEADER_LINE`,
which looks for `field_id` followed by a comma, tab, or semicolon) and
collects lines from there until the next blank line — isolating the table
from a trailing "Let me know if..." paragraph the same way the markdown path
does.

### `_parse_csv(text) -> list[dict[str, str]]`
Sniffs the delimiter (see "Python concepts" above), reads rows with
`csv.DictReader`, and normalizes every key/value pair (lowercased, stripped
key; stripped value) while dropping any column with no header name at all
(`if k` in the comprehension's filter clause).

### `parse_sheet_rows(text) -> list[dict[str, str]]`
The orchestrator: raises immediately on empty/whitespace-only input, then
normalizes the text and tries the markdown-table path first — the *predicted*
common case — falling through to the CSV/TSV path only if no markdown table
was found (or the markdown table it found had zero data rows). Note that once
a markdown table *is* found, a subsequent parse failure inside it (e.g. the
column-shift check above) is raised **directly**, not caught and silently
retried as CSV:
```python
try:
    rows = _parse_markdown_table(md_block)
except ValueError as exc:
    raise ResponseParseError(f"could not parse the markdown table: {exc}", text) from exc
if rows:
    return rows
```
`raise ... from exc` is Python's **exception chaining** syntax — it preserves
the original exception as the new one's `__cause__`, so a traceback shows
*both* errors ("the following exception was the direct cause of..."),
instead of losing the low-level detail. The comment in the code explains why
this doesn't fall back to CSV here: once a header+separator pair is found,
that structurally *is* a markdown-table reply, so a failure past that point
is a definitive diagnosis, not a hint that maybe it's CSV after all — falling
through would bury the specific, actionable error behind a generic "no table
found."

### `_scrub(row) -> dict[str, Optional[str]]`
Converts the model's various stand-ins for "empty" (`""`, `"null"`, `"none"`,
`"n/a"`, `"-"`, etc. — case-insensitively, via `_NULLISH`) into actual
`None`, so downstream pydantic validation sees a real absence of a value
rather than a string that happens to say "none."

### `parse_proposals(text) -> list[CopilotProposal]`
Calls `parse_sheet_rows`, then validates each row against `CopilotProposal`
(see [models.md](models.md)), collecting *all* errors before raising rather
than stopping at the first one:
```python
for i, row in enumerate(rows, start=1):
    scrubbed = _scrub(row)
    if not scrubbed.get("field_id"):
        errors.append(f"row {i}: missing field_id")
        continue
    try:
        proposals.append(CopilotProposal.model_validate(scrubbed))
    except Exception as exc:
        errors.append(f"row {i} ({scrubbed.get('field_id')}): {exc}")
if errors:
    raise ResponseParseError(...)
```
`enumerate(rows, start=1)` numbers rows starting at 1 (human-friendly) rather
than 0. This "collect everything, then decide" pattern gives a human a
complete list of what's wrong in a single re-paste cycle, instead of a
whack-a-mole loop where each fix only reveals the next error.

### `attach_geometry(proposals, fieldset) -> list[SdtmAnnotation]`
Rejoins each `CopilotProposal` (which has no coordinates) to its source
`CRFField` (which does) by `field_id`, via `fieldset.by_id(...)` (see
[models.md](models.md)). Raises immediately if a `field_id` doesn't exist
anywhere in the document — a strong signal the reply was pasted against the
wrong batch, or a `field_id` was altered. Also handles a genuine edge case:
if the same `field_id` appears more than once in the reply (shouldn't
normally happen, but this isn't trusted input), it disambiguates the
resulting `annot_id` with a numeric suffix (`f"{field.field_id}{suffix}"`)
rather than silently overwriting one annotation with another. Builds each
`SdtmAnnotation` with the provenance fields hard-coded here — `source_model`,
`review_status=PROPOSED`, `reviewed_by=None` — never taken from the reply
itself, which is the concrete enforcement of "everything is a proposal."

### `ingest_response_file(response_path, fieldset, page_indexes, allow_partial=False) -> AnnotationSet`
**The module's top-level entry point**, called from
`scripts/ingest_response.py` once per batch. Reads the file, parses it
(wrapping any `ResponseParseError` to prepend the file path for context),
attaches geometry, then checks **completeness**: every `field_id` expected on
this batch's pages (per `fieldset.for_pages(page_indexes)`) must appear in
the reply, computed via **set difference**:
```python
expected = {f.field_id for f in fieldset.for_pages(page_indexes)}
missing = sorted(expected - {p.field_id for p in proposals})
```
`{... for ...}` here is a **set comprehension** (curly braces, like a dict
comprehension but with no `:` — so it produces a bare set of values, not
key-value pairs). `expected - other_set` is Python's set-subtraction
operator, yielding everything in `expected` that isn't also in the second
set — exactly the "what's missing" answer, computed in one expression instead
of a loop. If anything's missing and `allow_partial` wasn't passed, this
raises `IncompleteResponseError` (rather than silently returning a shorter
annotation set) — the caller can override this deliberately via
`allow_partial=True` for cases where a short reply is expected and accepted
on purpose.
</content>
