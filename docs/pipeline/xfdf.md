# `pipeline/xfdf.py`

## Role in the pipeline

This module writes **XFDF** (XML Forms Data Format), the file format Adobe
Acrobat uses to represent PDF annotations independent of the PDF file itself.
It's the pipeline's hand-off point from "code" to "human": once
`build/blankcrf.xfdf` is written, it becomes — per the module docstring — *the
authoritative record*, meant to be opened in Acrobat, reviewed, and possibly
hand-edited (accept/reject/retype an annotation) before the final PDF is
produced. `xfdf_to_pdf.py` (see [xfdf_to_pdf.md](xfdf_to_pdf.md)) deliberately
reads this file back **without** cross-checking it against anything upstream
(`fields.json`, `proposals.json`) — once written, this file's contents are the
truth, not a cache of some other truth.

Two details the docstring calls out as easy to get wrong:
- `@rect` must be written in **PDF user space** (bottom-left origin) — since a
  `BBox` already *is* that, this module does zero coordinate arithmetic; the
  y-flip lives entirely in [geometry.py](geometry.md), applied only at
  extraction and at final render time, never here.
- `@page` is **0-based**, matching `page_index` directly — never
  `display_page` (the 1-based, human-facing number defined on
  `SdtmAnnotation`, see [models.md](models.md)).

Rejected annotations (`review_status == REJECTED`) are excluded entirely from
the output — a rejection means, as far as the file this module writes is
concerned, the annotation doesn't exist.

## Python concepts you'll see here

**`xml.etree.ElementTree` — building XML from Python.** This is the standard
library's XML toolkit, imported here as `ET` (`import xml.etree.ElementTree
as ET`, a common aliasing convention for a long module name used repeatedly).
`ET.Element(tag)` creates a new XML element; `ET.SubElement(parent, tag)`
creates one and appends it as a child of `parent` in a single call;
`element.set(name, value)` sets an XML attribute; `element.text = "..."` sets
an element's text content. This module builds up an entire XML tree this way,
in memory, before ever converting it to a string.

**XML namespaces in ElementTree: the `{uri}tag` syntax.** XFDF documents use
an XML namespace (`http://ns.adobe.com/xfdf/`) so their tags don't collide
with some other XML vocabulary if embedded elsewhere. ElementTree represents
a namespaced tag as a Python string in **Clark notation**:
`f"{{{XFDF_NS}}}freetext"` evaluates to the literal string
`"{http://ns.adobe.com/xfdf/}freetext"` — note the *four* braces in the
f-string: the outer `{...}` is the f-string's interpolation syntax, and the
inner `{XFDF_NS}` are literal curly braces that survive into the output
string (an f-string that wants a literal `{` writes `{{`, so `{{{XFDF_NS}}}`
is "literal `{`" + "interpolated `XFDF_NS`" + "literal `}`"). The comment in
`annotation_to_element` explains *why* every element is constructed with this
explicit namespace rather than a bare tag name: a bare tag would still
serialize correctly (inheriting the root's default namespace), but the
**in-memory tree** and a **re-parsed** version of the same document would then
disagree about each element's fully-qualified tag — a trap for any code that
walks the tree afterward and compares tag names (as `xfdf_to_pdf.py` does).

**`ET.register_namespace`.** Called once, before building the document, to
tell ElementTree's serializer which short prefix (or none, for the default
namespace) to use when writing out namespaced tags — without it, ElementTree
falls back to auto-generated prefixes like `ns0:`, which is technically valid
XML but not what Acrobat's XFDF reader expects. Registering `""` (empty
string) as the prefix for `XFDF_NS` is what makes the output use plain,
unprefixed tags like `<freetext>` under a default `xmlns="..."` declaration —
verified directly by
`tests/test_xfdf.py::test_serialized_form_declares_xfdf_as_the_default_namespace`.

**`ET.indent`.** A convenience function (added in Python 3.9) that
pretty-prints an ElementTree in place, adding whitespace/newlines for human
readability — used here purely for readability of the saved `.xfdf` file
someone might open in a text editor, with no effect on what the XML *means*.

**Tuple unpacking with a conditional expression.**
```python
r, g, b = (0.80, 0.05, 0.05) if _color_for(annot) == DEFAULT_COLOR else (0.48,) * 3
```
`(0.48,) * 3` — a **1-tuple multiplied by an integer** — repeats the tuple's
single element, producing `(0.48, 0.48, 0.48)`. Multiplying a tuple (or list)
by an int is a general Python idiom for repetition, here used to avoid
spelling out the same gray value three times.

**`strftime` for PDF-specific date formatting.** `dt.strftime("D:%Y%m%d%H%M%SZ")`
produces the exact date syntax the PDF spec requires for annotation
timestamps (e.g. `D:20260813120000Z`) — `strftime` format codes (`%Y` four-
digit year, `%m` month, etc.) are a standard-library feature worth knowing
generally, not specific to this project.

**Building an attribute dict conditionally, via a loop over tuples.**
```python
for key, value in (
    ("field_id", annot.field_id),
    ("domain", annot.domain),
    ...
):
    if value:
        meta.set(key, str(value))
```
Iterating a literal tuple-of-tuples rather than calling `.set(...)` ten
times separately means the "only set it if truthy" guard is written once and
applied uniformly — a small refactor that avoids ten nearly-identical `if
annot.x: meta.set("x", str(annot.x))` lines.

## Functions, in file order

### `_pdf_date(dt) -> str`
Formats a `datetime` (or empty string if `None`) as PDF date syntax, described
above.

### `_color_for(annot) -> str`
Picks between `DEFAULT_COLOR` (`"#CC0D0D"`, a red used for real SDTM
mappings) and `NOTE_COLOR` (`"#7A7A7A"`, gray for "note"-kind annotations like
`[Not Submitted]`) based on whether the annotation actually maps to something
(`annot.label_text()` non-empty — see [models.md](models.md)).

### `annotation_to_element(annot) -> ET.Element`
Builds one `<freetext>` XML element per `SdtmAnnotation`. Sets attributes
directly matching the model's fields (`page` from `page_index`, `rect` via
`geometry.format_xfdf_rect`, `color`, `flags="print"` so the annotation
appears when the PDF is printed, `name` from `annot_id`, `title` from
`source_model`, `subject` from the annotation kind, `date`), then appends
three child elements: `<contents>` (the actual visible text, from
`annot.display_text()`), `<defaultappearance>` (a PDF content-stream snippet
setting font and color: `/Helv 7 Tf 0.8 0.05 0.05 rg`), and a custom
`<carf:meta>` element carrying every provenance field (`field_id`, `domain`,
`variable`, `condition`, `codelist`, `origin`, `confidence`, `review_status`,
`reviewed_by`, `rationale`) as XML attributes. The docstring is explicit that
this `<carf:meta>` block is a **convenience for the review UI**, not part of
the rendering contract — Acrobat generally preserves unknown elements but
isn't *obliged* to, so nothing downstream may *require* it to survive; the
`<contents>` and `@rect` alone must be enough on their own to render
correctly.

### `build_xfdf(annotations, source_pdf) -> str`
Assembles the full document: registers namespaces, builds the root
`<xfdf>` element (with `xml:space="preserve"` so whitespace in `<contents>`
isn't collapsed by a reader), an `<f href="...">` element naming the source
PDF, and an `<annots>` element containing one `<freetext>` per **non-rejected**
annotation, sorted into the same top-to-bottom reading order used elsewhere
in the codebase:
```python
for annot in sorted(annotations.annotations, key=lambda a: (a.page_index, -a.bbox.y1, a.bbox.x0)):
    if annot.review_status is ReviewStatus.REJECTED:
        continue
    annots.append(annotation_to_element(annot))
```
Finally serializes the tree to a string with `ET.tostring(root,
encoding="unicode", xml_declaration=False)` (returning a Python `str`, not
`bytes`, because `encoding="unicode"` was requested — a slightly unusual but
documented `ElementTree` option), and manually prepends a standard XML
declaration line, since `xml_declaration=False` was needed to avoid
`ET.tostring` emitting one with a different encoding label than intended.

### `write_xfdf(annotations, out_path, source_pdf=None) -> Path`
The file-writing entry point, called from `scripts/ingest_response.py`.
Ensures the output directory exists, calls `build_xfdf`, writes the result as
UTF-8 text, and returns the `Path` written — a small, common pattern of
"return the path you were given/computed" so callers can immediately chain
further use of the result (e.g. printing it, or passing it straight to
`xfdf_to_pdf`).
</content>
