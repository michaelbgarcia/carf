# `pipeline/xfdf_to_pdf.py`

## Role in the pipeline

This is the **last step of the pipeline** — the module that takes the blank
CRF plus a *reviewed* (possibly hand-edited-in-Acrobat) XFDF file and produces
the actual FDA-submission-ready annotated PDF. Everything upstream of this
module exists to arrive at a correct XFDF file; everything this module does
is turn that file, and only that file, into pixels and PDF markup.

The module docstring opens with an important fact about PyMuPDF: **it has no
built-in XFDF importer.** There's no `pymupdf.import_xfdf()` — reading XFDF
back into annotation objects is an Acrobat/pdf-lib-ecosystem feature that the
PyMuPDF C core simply doesn't implement. So this module is, deliberately, a
small hand-written `xml.etree.ElementTree` walk that feeds PyMuPDF's native
annotation-drawing API — described as "the intended design, not a
workaround," since XFDF here is simple XML this project fully controls (it's
the exact subset `xfdf.py` writes).

The single most important behavioral rule in this file, stated three separate
times across the docstring and code comments for emphasis: **the XFDF is
authoritative, and nothing here consults `fields.json` or `proposals.json`
again.** If a human retyped an annotation's text in Acrobat, that retyped text
— not anything a pipeline step originally proposed — is what gets rendered.
Reintroducing pre-review data as a filter or fallback here would silently
undo human review, which in a GxP/Part 11 context is exactly the failure mode
the whole review step exists to prevent.

## Python concepts you'll see here

**A dataclass deliberately *not* reusing an existing pydantic model.**
`XfdfAnnotation` could look, at a glance, like it overlaps heavily with
`SdtmAnnotation`. The docstring explains why it's a distinct type instead: an
`SdtmAnnotation` *derives* its display text from `domain`/`variable` fields
(via `label_text()`/`display_text()` — see [models.md](models.md)) — but
that would silently discard a hand-edited `<contents>` value that no longer
matches those fields at all. `XfdfAnnotation` instead stores the file's own
`text` directly as ground truth, with the structured metadata kept alongside
only as extra context (and only if it survived an Acrobat round trip). Using
`@dataclass` rather than `pydantic.BaseModel` here is a lighter-weight choice
appropriate for a value that only exists transiently, between reading the
XML and drawing it — it's never serialized back out or validated against
external rules the way `SdtmAnnotation` is.

**A `dataclasses.field(default_factory=...)` for a mutable default.**
```python
@dataclass
class XfdfAnnotation:
    ...
    meta: dict[str, str] = dc_field(default_factory=dict)
```
Note the import alias: `from dataclasses import dataclass, field as dc_field`
— renamed on import specifically to avoid shadowing `pipeline.models.Field`
(the pydantic one) or the `field` name used elsewhere, since this file also
imports things named similarly. This is the `dataclasses` module's equivalent
of pydantic's `Field(default_factory=...)` seen in `models.py`: a plain
`meta: dict = {}` default would share **one** dict across every instance of
the class (a classic Python gotcha with mutable default arguments), whereas
`default_factory=dict` calls `dict()` fresh for each new instance.

**Computing a property from a dict lookup with a default.**
```python
@property
def review_status(self) -> str:
    return self.meta.get("review_status", "")
```
`dict.get(key, default)` returns `default` instead of raising `KeyError` if
`key` is missing — appropriate here since `meta` may or may not have
survived an Acrobat round trip intact (per the `<carf:meta>` caveat discussed
in [xfdf.md](xfdf.md)).

**Stripping XML namespace prefixes for lenient parsing.**
```python
def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
```
Recall from [xfdf.md](xfdf.md) that ElementTree represents a namespaced tag
as `"{uri}localname"`. `tag.rsplit("}", 1)` splits from the *right*, at most
once (`maxsplit=1`), into `["{uri", "localname"]` if there's a namespace, or
leaves the whole string as a single-element list if there isn't (no `}`
present at all) — `[-1]` then grabs the local name either way. This is what
lets this module tolerate XFDF that "does not look like what we wrote," as
the docstring for `parse_xfdf` puts it — a file Acrobat has resaved may
present its tags with different namespace prefixes, no namespace at all
(bare `<xfdf>`), or the original ones, and comparing local names only sidesteps
all of that variation.

**`int(el.get("page", 0))`.** `Element.get(attr, default)` mirrors
`dict.get` for XML attributes — here providing a fallback of `0` if `@page`
is somehow missing, though in practice `@rect`'s absence (checked explicitly,
see below) is treated as the real failure case; `@page` quietly defaulting
avoids an unnecessary crash for a merely-cosmetic omission.

## Functions, in file order

### `XfdfAnnotation` (dataclass)
Described above: `annot_id`, `page_index`, `bbox`, `text`, `kind`, `muted`,
`meta`, plus the `review_status` property.

### `_localname(tag) -> str`
Strips any XML namespace prefix from a tag name, described above.

### `_text_of(el, name) -> str`
Finds the first direct child of `el` whose local name matches `name` (e.g.
`"contents"`) and returns its stripped text, or `""` if no such child exists:
```python
for child in el:
    if _localname(child.tag) == name:
        return (child.text or "").strip()
return ""
```
Iterating an `Element` directly (`for child in el`) yields its direct
children — a built-in `ElementTree` feature. `(child.text or "")` guards
against `child.text` being `None` (which ElementTree produces for an empty
element like `<contents></contents>` with no text node at all, as opposed to
one containing just whitespace).

### `parse_xfdf(xfdf_path) -> list[XfdfAnnotation]`
**Reads an entire XFDF file into a list of `XfdfAnnotation` records.** Parses
with `ET.parse(...).getroot()`, checks the root element's local name is
`"xfdf"` (raising `ValueError` immediately if not — a malformed or wrong file
should fail loudly rather than quietly returning zero annotations), then
walks direct children looking for the `<annots>` element, and within it,
every `<freetext>` or `<square>` child. For each, it requires a `@rect`
attribute to be present — raising `ValueError` naming the offending
annotation if not, since silently skipping an annotation with no position
would mean **losing an annotation from the submission** without any
indication that happened. Reads `<carf:meta>` if the `CARF_NS` constant
(imported from `xfdf.py`) is truthy — `dict(meta_el.attrib)` converts an
`ElementTree` element's attribute mapping into a plain Python `dict`. Builds
one `XfdfAnnotation` per entry, defaulting `annot_id` to a generated
`f"anon-{len(out) + 1}"` if `@name` is missing (another lenient default — an
annotation without a name is unusual but not fatal, unlike one without a
`@rect`).

### `overflowing_annotations(annotations, fontsize=FONT_SIZE) -> list[tuple[str, str, float]]`
Checks every annotation's text against the *actual* width of the rectangle
it will render into, using the same `pymupdf.get_text_length` measurement
`layout.py` uses to size annotations in the first place. The reasoning
matters here: PyMuPDF **silently clips** FreeText content that's wider than
its box — nothing about that failure is visible except a truncated string in
the finished PDF, which the docstring correctly identifies as "a
data-integrity problem" in a submission artifact. This function deliberately
only **reports** the problem rather than fixing it (e.g. by widening the
rect) — because by this point in the pipeline, a rect's position was chosen
deliberately (either by `layout.py`'s collision avoidance, or by a human
reviewer repositioning it in Acrobat), and silently overriding that decision
would be its own kind of "quietly undo human review" bug. Note the
`OVERFLOW_TOLERANCE = 0.5` constant and its comment: `format_xfdf_rect`
rounds to 3 decimal places when writing (see [xfdf.md](xfdf.md)), so a rect
`layout.py` sized to *exactly* fit its text comes back from a round trip
through the file a fraction of a point narrower than the text needs —
without this small tolerance, essentially every pipeline-generated
annotation would falsely report a near-zero overflow, burying any *real*
overflow (e.g. from a much longer hand-typed replacement string) in noise.

### `render_annotations(pdf_path, annotations, out_path) -> Path`
Opens the blank CRF, and for each `XfdfAnnotation`, first validates that its
`page_index` actually exists in this PDF (`if a.page_index >= doc.page_count:
raise ValueError(...)`) — a defensive check against an XFDF file that's
somehow mismatched with the PDF it's being applied to (e.g. wrong file, or a
page was removed) — then skips any annotation with empty text and otherwise
calls the shared `render.draw_annotation` (see [render.md](render.md)) with
`boxed=(a.kind == "domain")` and `muted=a.muted`. Saves via
`save_with_annotations` — never flattened, per the invariant established in
`render.py`.

### `xfdf_to_pdf(pdf_path, xfdf_path, out_path) -> Path`
**The module's top-level entry point**, called from `scripts/render_final.py`.
Parses the XFDF, raises `ValueError` immediately if it contains zero
annotations (rather than silently producing a "blank annotated PDF" that
looks like a completed deliverable but isn't one), and otherwise calls
`render_annotations`. The comment above the call is worth reading closely:
rejected annotations are **already absent** by the time this function runs,
because `xfdf.py` excludes them when *writing* the file in the first place —
so anything still present in a hand-edited XFDF by the time this reads it is
present because a human deliberately left it there, and this function has no
business second-guessing that by re-filtering on review status again.
</content>
