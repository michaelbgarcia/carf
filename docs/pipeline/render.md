# `pipeline/render.py`

## Role in the pipeline

The smallest file in `pipeline/`, and deliberately so. Both `stamp.py` (the
pre-review QC preview) and `xfdf_to_pdf.py` (the actual submission-ready
renderer) need to do the same low-level thing: draw one piece of annotation
text onto a PDF page using PyMuPDF's `add_freetext_annot` API. Rather than
each implementing that independently — and risking the two drifting apart, or
one picking up a bug the other doesn't — this module holds the **one, shared**
implementation. The two callers stay separate *entry points* on purpose (the
docstring is explicit about why: "conflating them is how a stale preview gets
mistaken for the deliverable"), but the actual pixel-drawing code is not
duplicated.

A deliberate design choice worth noting: `draw_annotation` takes **text**, a
plain string — not an `SdtmAnnotation` object. The docstring explains why
this matters: `stamp.py` derives its text from in-memory records (fine,
nothing has been reviewed yet), but `xfdf_to_pdf.py` must render whatever
`<contents>` a human actually wrote in the XFDF file, verbatim — because a
reviewer may have hand-edited it in Acrobat, and *that* text, not a
freshly-recomputed one, is authoritative by then. If this function accepted a
record and derived the text itself, `xfdf_to_pdf.py` would have no way to
avoid silently discarding a human's edit.

## Python concepts you'll see here

**Documenting an experiment's result in the docstring, not just the
conclusion.** The "Borders on freetext annotations" section of the module
docstring is worth reading closely as an example of good technical writing:
it doesn't just say "use `border_width`," it documents *three* things that
were tried against a specific PyMuPDF version (1.28.2), what happened with
each (including one that raises an exception and one that silently drops
required data), and which was chosen and why. This is the kind of "why not
the other options" context that's easy to lose once code just shows the
final choice — and it directly answers a concern flagged in
`COWORK_INSTRUCTIONS.md` (that a *different* PDF library, `pypdf`, has a known
bug where a border silently drops FreeText content) by confirming PyMuPDF
doesn't share that bug, for the specific configuration actually used here.

**Keyword-only arguments (the bare `*` in a signature).**
```python
def draw_annotation(
    page: pymupdf.Page,
    bbox: BBox,
    text: str,
    *,
    boxed: bool = False,
    muted: bool = False,
    fontsize: float = FONT_SIZE,
) -> pymupdf.Annot:
```
The lone `*` in the parameter list means everything after it (`boxed`,
`muted`, `fontsize`) **must** be passed by keyword — `draw_annotation(page,
bbox, text, True)` would be a `TypeError`; it has to be `draw_annotation(page,
bbox, text, boxed=True)`. This is a deliberate readability choice for
booleans especially: `draw_annotation(page, bbox, text, True, False)` at a
call site tells a reader nothing about which flag is which without checking
the function signature; `boxed=True, muted=False` is self-documenting at the
call site itself.

**Tuple constants for RGB color.** `ANNOT_COLOR = (0.80, 0.05, 0.05)` — PDF
graphics (and PyMuPDF's API) express color as three floats from 0 to 1, not
the more familiar 0–255 integers or hex codes — a small but easy-to-trip-on
domain detail worth knowing if you're used to CSS/web color conventions.

## Functions, in file order

### `draw_annotation(page, bbox, text, *, boxed=False, muted=False, fontsize=FONT_SIZE) -> pymupdf.Annot`
Converts the incoming `bbox` (PDF user space) to a fitz rectangle via
`geometry.bbox_to_fitz_rect` (see [geometry.md](geometry.md) — this is one of
only two places in the codebase that call this specific conversion function,
the other being `xfdf_to_pdf.py`), then calls
`page.add_freetext_annot(rect, text, fontsize=..., fontname=FONT,
text_color=..., border_width=...)`. `text_color` switches between the muted
gray (`NOTE_COLOR`) and the standard red (`ANNOT_COLOR`) based on the `muted`
flag; `border_width` is `DOMAIN_BORDER` (a thin visible border) when `boxed`
is true — used for page-level domain markers like a boxed "DM" — and `0`
otherwise. Calls `annot.update()` after creation, which is required by
PyMuPDF for a newly-added annotation's appearance to actually be generated
and rendered (an easy step to forget, since the annotation *exists* in the
document either way — it just won't display correctly without this call).
Returns the created `pymupdf.Annot` object, letting a caller (like
`xfdf_to_pdf.py`'s `overflowing_annotations` check) inspect it further if
needed, though the current callers don't use the return value themselves.

### `save_with_annotations(doc, out_path) -> None`
A one-line wrapper: `doc.save(out_path, garbage=4, deflate=True)`. `garbage=4`
tells PyMuPDF to run its most aggressive garbage collection pass over unused
PDF objects when saving (shrinking file size); `deflate=True` compresses
streams. The docstring is emphatic about what this function must **never**
do: call `doc.bake()` (PyMuPDF's method for flattening annotations into
static page content, turning them into pixels rather than PDF markup). Doing
so would look identical on screen but destroy the property FDA review tooling
depends on — that annotations remain **searchable and separable** from the
underlying form. This function exists specifically so that guarantee is
enforced in exactly one place, called by both `stamp.py` and `xfdf_to_pdf.py`,
rather than each caller needing to independently remember not to flatten.
</content>
