# `pipeline/layout.py`

## Role in the pipeline

Every annotation coming out of `parse_response.py` starts out positioned at
the exact bounding box of the field it describes — because that's the only
geometry `attach_geometry` has to give it (see [parse_response.md](parse_response.md)).
Rendered as-is, that would print `"DM.SITEID"` directly on top of the blank
box a clinical site is supposed to write an actual site ID into. This module
fixes that: it moves each annotation to a nearby, empty spot before anything
gets drawn.

The placement strategy, per the module docstring, is intentionally modest: try
a small number of candidate positions around the field (right, then below,
then left), reject any that leave the page or collide with an obstacle, and
if all three are blocked, nudge downward repeatedly until something's clear.
Obstacles are every capture field on the page, every annotation already
placed, and — optionally — the form's own printed text (so an annotation
doesn't get placed on top of a caption like "Sitting / Supine / Standing").
The module docstring is explicit about what this is **not**: it doesn't
reflow into margins, shrink/abbreviate text, or draw leader lines connecting a
displaced annotation back to its field. That's called out as later,
unbuilt work ("collision layout for dense pages").

## Python concepts you'll see here

**`@dataclass` for a small internal helper type.** `_Box` is a plain,
mutable dataclass (no `frozen=True` this time, since `_on_page` and the
nudging loop build new `_Box` instances rather than mutating one in place
anyway) — used purely as internal scratch geometry during placement, never
returned to a caller. Its `to_bbox()` method converts it to the "real"
`BBox` model once a good position is found.

**Order-dependent fallback logic via a list of candidates.** `_candidates`
returns a *list* of positions in priority order (right, below, left); the
caller (`place_one`) simply iterates that list and returns the first
acceptable one:
```python
for box in _candidates(field, w, h):
    if _on_page(box, page) and not any(box.overlaps(o) for o in obstacles):
        return box.to_bbox()
```
This is the same "ordered fallback" shape seen in `extract.py`'s
`associate_label`, applied to a different problem — a recurring, reusable
pattern worth recognizing: *build a prioritized list of candidates, return the
first one that satisfies a predicate.*

**A bounded retry loop as a last resort.** When none of the three candidates
work, `place_one` starts over at the preferred position and walks straight
down the page in fixed steps, giving up after `MAX_NUDGES` attempts:
```python
box = _candidates(field, w, h)[0]
for _ in range(MAX_NUDGES):
    if _on_page(box, page) and not any(box.overlaps(o) for o in obstacles):
        return box.to_bbox()
    box = _Box(box.x0, box.y0 - NUDGE, box.x1, box.y1 - NUDGE)
return _candidates(field, w, h)[0].to_bbox()
```
`for _ in range(MAX_NUDGES)` — using `_` as the loop variable name is Python
convention for "I need to repeat this N times but don't care about the
counter's value." Note the deliberate final fallback: if nothing is ever
clear, the function returns the *original preferred position* anyway rather
than raising or returning nothing — the docstring's reasoning is blunt: "a
visible collision is reviewable, a missing annotation is not." A dropped
annotation is a worse outcome (silently incomplete submission) than an
overlapping one (visibly wrong, and any human reviewer will notice and fix
it).

**Deferred imports to avoid a circular dependency.** Inside `text_obstacles`:
```python
def text_obstacles(pdf_path) -> dict[int, list[BBox]]:
    from pipeline.extract import text_runs
    from pipeline.geometry import fitz_rect_to_bbox
    ...
```
Importing `text_runs` from `extract.py` *inside the function body*, rather
than at the top of the file with the other imports, is a way to sidestep a
circular import: if `extract.py` ever imported something from `layout.py`
at module level (it doesn't currently, but the comment `# imported here to
avoid a cycle` documents this as a deliberate defensive choice, not an
oversight), a top-level import here would fail at module load time. A
function-local import only runs the first time that function is actually
*called*, by which point both modules have already finished loading.

**Dict-of-lists accumulation with `setdefault`.**
```python
fields_by_page.setdefault(f.page_index, []).append(f.bbox)
```
`dict.setdefault(key, default)` returns the existing value for `key` if
present, or inserts `default` and returns *that* if not — in one call. This
is the standard idiom for "build up a dict mapping each key to a list of
things," avoiding a more verbose `if key not in d: d[key] = []` followed by
`d[key].append(...)`.

## Constants

`FONT`, `FONT_SIZE`, `GAP`, `PAD`, `MARGIN`, `NUDGE`, `MAX_NUDGES`,
`MIN_OVERLAP` — module-level tunables (same pattern as `extract.py`'s
constants block) controlling annotation sizing and placement tolerances.

## Functions, in file order

### `text_width(text, size=FONT_SIZE) -> float`
Thin wrapper around `pymupdf.get_text_length` — measures how wide a string
would render at a given font/size, in points, without actually drawing
anything. This is what lets the module size an annotation's box to its text
*before* placing it.

### `annotation_size(text, size=FONT_SIZE) -> tuple[float, float]`
Adds padding (`PAD`) on both sides of the measured text width and height:
`(text_width(text, size) + 2 * PAD, size + 2 * PAD)`.

### `_Box` (dataclass)
Plain `x0, y0, x1, y1` rectangle used only internally.
- **`overlaps(other) -> bool`** — true if the intersection area in *both*
  axes exceeds `MIN_OVERLAP` (a small tolerance so grazing/touching edges
  don't count as a real overlap).
- **`to_bbox() -> BBox`** — converts to the real, validated model type once
  placement is decided.

### `_on_page(box, page) -> bool`
True if `box` sits entirely within the page, inset by `MARGIN` on every side.

### `_candidates(field, w, h) -> list[_Box]`
Builds the three candidate positions, in priority order — right of the
field, below it, left of it — each sized `w` × `h`.

### `place_one(field, text, page, obstacles) -> BBox`
The per-annotation placement algorithm described above: try the three
candidates in order, then fall back to the downward nudge loop, then give up
gracefully and return the preferred (possibly-colliding) position.

### `text_obstacles(pdf_path) -> dict[int, list[BBox]]`
Opens the PDF fresh (independently of any already-open `pymupdf.Document`
elsewhere in the call chain) and, for every page, converts every detected
text run (reusing `extract.text_runs` — see [extract.md](extract.md), so
"what counts as printed text" is defined in exactly one place) into a `BBox`
via the shared geometry flip. Returns a dict keyed by page index — this is
what `place_annotations` merges in as extra obstacles when the caller opts
in.

### `place_annotations(annotations, fieldset, size=FONT_SIZE, obstacles=None) -> AnnotationSet`
**The module's entry point**, called once per full document from
`scripts/ingest_response.py`. Builds a combined obstacle map — every field's
own bbox, plus (if supplied) every text run's bbox — then processes
annotations **in page-then-top-to-bottom order**:
```python
for a in sorted(annotations.annotations, key=lambda a: (a.page_index, -a.bbox.y1, a.bbox.x0)):
```
(the same "negate y1 to sort top-first" trick used in `prompt.py`'s
`build_spec_sheet` — see [prompt.md](prompt.md)). Processing order matters
here specifically because each newly-placed annotation is added to that
page's obstacle set (`taken.setdefault(...).append(box)`) before the *next*
annotation is placed — so placement order determines what counts as "already
occupied," and the docstring notes this keeps displacement predictable for a
reviewer (annotations don't jump around unpredictably based on iteration
order). Annotations with no page match or no display text (e.g. a
`display_text()` of `""` — see [models.md](models.md)) are left untouched and
passed through unchanged. Returns a **new** `AnnotationSet` via
`.model_copy(update={"annotations": placed})` rather than mutating the input
— consistent with the "treat these models as values" convention seen
throughout the codebase.
</content>
