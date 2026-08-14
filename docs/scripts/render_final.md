# `scripts/render_final.py`

## Role in the pipeline

**Step 3 of 3 — the last thing a human runs.** By the time this script is
invoked, a person has already reviewed (and possibly hand-edited) the XFDF
file in Acrobat, moving each annotation's `review_status` off `proposed`.
This script takes that reviewed XFDF, plus the original blank CRF PDF, and
produces `build/annotated_crf.pdf` — the actual FDA-submission-ready
deliverable. It is the shortest of the three top-level scripts, because
almost everything it needs is already implemented in
[../pipeline/xfdf_to_pdf.md](../pipeline/xfdf_to_pdf.md); this file is mostly
argument parsing plus a warning it prints on the way out.

Note the module docstring's header: *"Stub -- task order step 9."* — a
leftover label from the project's original build instructions numbering its
steps; by the time this file reached this state it's a fully working final
step, not literally an unfinished stub, but the comment is kept as an
accurate pointer back to where this step is defined in
`COWORK_INSTRUCTIONS.md`.

## Python concepts you'll see here

**Two positional CLI arguments plus one optional flag.**
```python
parser.add_argument("pdf", type=Path, help="blank CRF PDF")
parser.add_argument("xfdf", type=Path, help="reviewed XFDF")
parser.add_argument("-o", "--out", type=Path, default=Path("build/annotated_crf.pdf"))
```
`argparse` distinguishes **positional** arguments (`pdf`, `xfdf` — no leading
dashes, required, matched by position on the command line: `render_final.py
blank.pdf final.xfdf`) from **optional** ones (`-o`/`--out` — has a default,
can be given a short or long flag name, order doesn't matter). `type=Path`
tells `argparse` to convert each argument's string value to a
`pathlib.Path` object automatically, so `args.pdf`, `args.xfdf`, and
`args.out` are already `Path` instances by the time `main()` uses them — no
manual `Path(...)` wrapping needed later.

**Printing a formatted warning list from a list of tuples.**
```python
for annot_id, text, over in overflow:
    print(f"  {annot_id}: {text!r} overflows by {over:.0f}pt")
```
`{text!r}` in an f-string applies `repr()` to the value instead of `str()` —
for a string, that means it prints with quotes around it (and escapes any
special characters), which is generally clearer for showing exactly what a
piece of *text data* is versus embedding it unquoted in a sentence. `{over:.0f}`
formats a float with zero decimal places (rounded to the nearest whole
point).

## Walkthrough of `main(argv=None) -> int`

1. **Parse arguments** — `pdf`, `xfdf` (both required, positional), `-o/--out`
   (optional, defaults to `build/annotated_crf.pdf`).
2. **Ensure the output directory exists.**
3. **Parse the XFDF once, separately from rendering it** —
   `annotations = xfdf_to_pdf.parse_xfdf(args.xfdf)` — purely so the script
   can report *how many* annotations it found in the printed summary line,
   without needing `xfdf_to_pdf.xfdf_to_pdf` (the actual rendering function)
   to return that count itself.
4. **Render** — `out = xfdf_to_pdf.xfdf_to_pdf(args.pdf, args.xfdf, args.out)`
   (see [../pipeline/xfdf_to_pdf.md](../pipeline/xfdf_to_pdf.md)) — this
   re-parses the XFDF internally, a small duplication of work traded for
   keeping `xfdf_to_pdf` itself simple (parse-then-render, not
   parse-once-and-pass-the-result-around).
5. **Check for overflowing annotations** — calls
   `xfdf_to_pdf.overflowing_annotations(annotations)` and, if any text would
   be clipped by PyMuPDF's silent truncation (see
   [../pipeline/xfdf_to_pdf.md](../pipeline/xfdf_to_pdf.md)), prints a
   `WARNING:` block listing each offending annotation's id, its text, and how
   many points too wide it is — this is the *only* place in the pipeline that
   surfaces this specific failure mode to a human, since PyMuPDF itself gives
   no indication at all that anything was clipped.
</content>
