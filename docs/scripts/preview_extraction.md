# `scripts/preview_extraction.py`

## Role in the pipeline

A **debugging aid**, not a pipeline step — it never appears in the
extract → prompt → ingest → render sequence at all. Its only job is to answer
one question visually: *"did `extract.py` find the right things, in the right
places?"* That question is genuinely hard to answer any other way in this
project, because — as the module docstring puts it — "a flipped rect still
lands on the page and still looks like a plausible box." A coordinate bug
here doesn't produce an obviously broken PDF; it produces a *plausible-looking
but wrong* one, which only an overlay comparison catches reliably.

It draws a red outline around every field `extract.py` detected, with its
associated caption printed beside it in blue — so a missing box means the
detector missed a field, and a box floating over nothing means the detector
invented one that isn't there.

The docstring is careful to distinguish this from two other, easily-confused
tools:
- `stamp.py` — previews **annotations** (SDTM mappings), *after* Copilot has
  proposed them.
- `xfdf_to_pdf.py` — produces the **actual submission artifact**, after human
  review.

This script previews neither of those — it previews what `extract.py` saw as
*input* (the field's printed caption, e.g. "Subject Identifier"), not what
the pipeline eventually produces as *output* (an SDTM annotation like
"DM.SUBJID"). `illustrative_target.py` (see
[illustrative_target.md](illustrative_target.md)) is the one that shows the
latter.

## Python concepts you'll see here

**A default-input helper with lazy, deferred generation.**
```python
def _default_inputs() -> list[Path]:
    fixtures = Path("fixtures")
    paths = [fixtures / f"SYNTHETIC_sample_crf_{v}.pdf" for v in DEFAULT_VARIANTS]
    if not all(p.exists() for p in paths):
        from make_sample_crf import make_sample_crf
        make_sample_crf(fixtures)
    return paths
```
`all(p.exists() for p in paths)` — `all()` applied to a generator expression
— checks whether **every** path in the list already exists on disk, short-
circuiting (stopping early) at the first missing one. If any are missing, it
imports `make_sample_crf` **at the point of use**, generates the fixtures,
and only then returns the (now-guaranteed-to-exist) paths. This is a
"generate on demand" pattern: running this script with no PDF specified
"just works" even on a completely fresh checkout, without requiring a
separate manual setup step first.

**`Path` division and `.stem` for building derived filenames.**
```python
stem = pdf_path.stem.replace("SYNTHETIC_sample_crf_", "")
out_pdf = out_dir / f"extraction_{stem}.pdf"
```
`Path.stem` is the filename without its extension (`"SYNTHETIC_sample_crf_
acroform"` for `.../SYNTHETIC_sample_crf_acroform.pdf`); `pdf_path.stem
.replace(...)` strips the common prefix down to just the variant name
(`"acroform"`); and `out_dir / f"..."` is `pathlib`'s overloaded `/` operator
for joining path components — used throughout this project instead of string
concatenation or `os.path.join`.

**`nargs="*"` for a variable-length positional argument.**
```python
parser.add_argument("pdf", nargs="*", type=Path, help="CRF PDFs (default: all variants)")
```
`nargs="*"` means this positional argument accepts **zero or more** values,
collected into a list (`args.pdf`) — so the script can be called as
`preview_extraction.py` (no PDFs, falls back to `_default_inputs()`),
`preview_extraction.py one.pdf`, or `preview_extraction.py one.pdf two.pdf
three.pdf`, all validly.

## Functions, in file order

### `preview(pdf_path, out_dir, dpi=110) -> tuple[Path, list[Path]]`
Runs `extract_fields(pdf_path)` (see
[../pipeline/extract.md](../pipeline/extract.md)), opens the PDF again
separately with PyMuPDF for drawing, and for every detected field, converts
its stored `BBox` back to a fitz rectangle (`bbox_to_fitz_rect` — see
[../pipeline/geometry.md](../pipeline/geometry.md)) and draws a red box
around it plus its caption text in blue just outside the box's corner. Saves
an annotated copy of the whole PDF, then renders **each page to a PNG image**
via `page.get_pixmap(dpi=dpi).save(png)` — useful for viewing the result
without opening a PDF viewer at all (e.g. embedding in a chat message or
viewing directly in a file browser). Returns both the annotated PDF's path
and the list of PNG paths.

### `_default_inputs() -> list[Path]`
Described above — the three synthetic variants, generating them first if
`fixtures/` doesn't already have them.

### `main(argv=None) -> int`
Parses arguments, then for each input PDF (explicit, or the default three
variants), runs `extract_fields` again (a second time, independently of the
call inside `preview()` — slightly redundant, but keeps this loop's summary
printing decoupled from `preview()`'s own internals) purely to print a field
count, calls `preview(...)`, and prints every output path produced.
</content>
