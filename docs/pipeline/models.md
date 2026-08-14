# `pipeline/models.py`

## Role in the pipeline

This is the data model for the entire project — every other file passes
these objects around instead of raw dicts or tuples. It's built on
[**pydantic**](https://docs.pydantic.dev/), a library that turns plain Python
class definitions into runtime-validated, JSON-serializable records. If a
`CRFField` or an `SdtmAnnotation` exists anywhere in this codebase, pydantic
has already checked that its fields have the right types and satisfy the
model's validation rules — malformed data simply cannot be constructed.

Two ideas from the project's domain shape almost everything here:
1. **Coordinates always travel in "PDF user space"** (bottom-left origin,
   y increasing upward) once they leave `extract.py`. See
   [geometry.md](geometry.md) for why that matters and where the conversion
   happens.
2. **Every annotation is provisional until a human accepts it.** Because
   there's no programmatic LLM access (see the repo root README), a person
   pastes annotation proposals out of Microsoft Copilot chat by hand. For a
   regulated (GxP / 21 CFR Part 11) submission, the audit trail needs to prove
   a human was in the loop — so fields like `review_status`, `reviewed_by`,
   and `source_model` aren't decorative, they're enforced by a validator (see
   `SdtmAnnotation._check_review_provenance` below).

## Python concepts you'll see here

**`from __future__ import annotations`.** This line, present at the top of
almost every file in this project, changes how Python evaluates type hints:
instead of evaluating `list[CRFField]` or `"BBox"` immediately when the class
body runs, Python treats every annotation as a *string*, evaluated lazily (or
never, if nothing asks for it). Two practical effects you'll see used here:
you can write `list[CRFField]` even in Python versions where that generic
syntax wasn't allowed pre-3.9 for annotations, and you can reference a class
in its own methods before the class is fully defined (`-> "BBox"` inside
`BBox` itself, for instance).

**`pydantic.BaseModel`.** Subclassing `BaseModel` (instead of plain classes or
`dataclasses`) gets you, for free: constructor argument validation (wrong type
→ `ValidationError`, not a silent wrong value), `.model_dump_json()` /
`.model_validate_json()` for serialization, and a declarative way to say "this
field is required," "this field defaults to X," or "this field must satisfy
this rule." Every class in this file except the plain functions is a
`BaseModel` subclass.

**Declaring fields as class-body annotations.**
```python
class BBox(BaseModel):
    x0: float
    y0: float
```
This looks like it's just annotating variables, but pydantic (like
`dataclasses`) inspects the class body at class-definition time and turns each
annotated name into a model field. There's no `__init__` written anywhere in
this file — pydantic generates one from these annotations.

**`Field(...)` for field metadata.** `Field(default=..., description=...,
ge=0.0, le=1.0)` attaches extra behavior/documentation to a single field:
`ge=0.0, le=1.0` (used on `confidence`) means "greater-or-equal to 0, less-or-
equal to 1" and pydantic will reject any value outside that range at
construction time. `default_factory=_utcnow` means "call this function to
produce the default" rather than sharing one fixed default value across every
instance — necessary for anything mutable or time-dependent (a plain
`default=datetime.now()` would freeze at import time, computed once).

**Validators: `@field_validator` and `@model_validator`.** These decorators
mark a method as a pydantic validation hook.
- `@field_validator("domain", "variable", mode="before")` runs *before*
  pydantic's own type coercion, on one or more named fields, and can
  transform or reject the raw input.
- `@model_validator(mode="after")` runs once the whole model has been built
  from its individual fields, so it can check relationships *between* fields
  (e.g. "if `review_status` isn't `PROPOSED`, `reviewed_by` must be set").

Both are used as `@classmethod`s (see below) because they run before/without a
particular instance existing yet, or need to construct/return one.

**`@classmethod`.** A method bound to the *class*, not an instance — called as
`Origin.coerce(value)` rather than `some_instance.coerce(value)`, and receives
`cls` (the class itself) instead of `self`. Used here both for pydantic
validators (which pydantic calls without an instance in hand yet) and for
`Origin.coerce`, an alternate constructor that does case-insensitive lookup.

**`enum.Enum`, and specifically `class Foo(str, Enum)`.** An `Enum` restricts
a value to one of a fixed, named set of options — safer than a bare string,
because `AnnotationKind.DOMAIN` can't be mistyped the way `"domian"` could be.
Mixing in `str` (`class AnnotationKind(str, Enum)`) means each member *is
also* a `str` — so `AnnotationKind.DOMAIN == "domain"` is `True`, and
`AnnotationKind.DOMAIN` serializes to JSON as the plain string `"domain"`
rather than something pydantic/JSON wouldn't know how to encode. This is a
very common pattern for enums that need to round-trip through JSON.

**`ConfigDict(frozen=True)`.** Set on `BBox`, this makes instances immutable
after construction — attempting `bbox.x0 = 5` raises an error. It reflects
that a `BBox` is a value (like a tuple), not something meant to be mutated in
place; code that wants a "changed" bbox creates a new one (pydantic's
`.model_copy(update={...})`, used throughout the codebase, is built for
exactly this).

**`@property`.** A method that's *accessed* like an attribute, no parentheses:
`bbox.width`, not `bbox.width()`. Used here for values computed from other
fields (`width = x1 - x0`) that don't need to be stored themselves.

**`Optional[X]`.** Shorthand for "either an `X` or `None`" (equivalent to `X |
None`). Used throughout for fields that are sometimes absent — e.g.
`SdtmAnnotation.field_id` is `Optional[str]` because a page-level "domain"
annotation belongs to the whole page, not to one field.

## Classes and functions, in file order

### `_utcnow() -> datetime`
A one-line helper: `datetime.now(timezone.utc)`. Exists so every
`default_factory=_utcnow` in this file shares one implementation, and so
timestamps are always timezone-aware UTC rather than ambiguous "naive"
datetimes (a common source of bugs when comparing or serializing times).

### `AnnotationKind(str, Enum)`
What an annotation is *asserting*: `DOMAIN` (a page-level marker like "DM"),
`VARIABLE` (maps a field to an SDTM variable), or `NOTE` (informational, e.g.
"this field isn't submitted").

### `FieldSource(str, Enum)`
How a `CRFField` was detected: `ACROFORM` (a real fillable PDF form field) or
`TEXT_LAYOUT` (inferred from text position and drawn lines/boxes, when there's
no AcroForm). See [extract.md](extract.md) for how each path is chosen.

### `Origin(str, Enum)`
The Define-XML v2.1 vocabulary for where a data point came from:
`COLLECTED`, `DERIVED`, `ASSIGNED`, `PROTOCOL`, `EDT`, `PREDECESSOR`,
`NOT_SUBMITTED`. This is a real clinical-data-standards concept, not
project-specific.

#### `Origin.coerce(value) -> Origin` (classmethod)
A lenient, case-insensitive lookup: turns `"collected"`, `"COLLECTED"`, or
even `"NOT SUBMITTED"` into the correct enum member. Necessary because the
input isn't a strict API payload — it's either typed by Copilot into a chat
window or typed by a human editing XFDF in Acrobat, and both are inconsistent
about capitalization and spacing. The normalization strips spaces,
underscores, and hyphens and lowercases before comparing:
```python
key = str(value).strip().replace(" ", "").replace("_", "").replace("-", "").lower()
for member in cls:
    if member.value.lower() == key:
        return member
raise ValueError(...)
```
Iterating `for member in cls` over an `Enum` class yields each of its members
in definition order — a built-in feature of `Enum`, not something this code
implements. If nothing matches, it raises `ValueError` listing every valid
option, rather than silently accepting garbage — a recurring theme in this
codebase: fail loudly rather than let a chat-UI typo become a wrong
submission field ("fail loudly" is also central to
[parse_response.md](parse_response.md)).

### `ReviewStatus(str, Enum)`
Where an annotation sits in human review: `PROPOSED` → `ACCEPTED` /
`EDITED` / `REJECTED`. Everything Copilot produces enters as `PROPOSED`;
nothing else in the codebase is allowed to set any other value (enforced by
convention in code that constructs these, and by the validator described
below).

### `BBox(BaseModel)`
An axis-aligned rectangle, **always in PDF user space** (bottom-left origin,
y increasing upward — never a raw fitz rectangle; see
[geometry.md](geometry.md)). Fields: `x0, y0, x1, y1: float`. `frozen=True`
makes it immutable.

- **`_check_ordering` (`@model_validator(mode="after")`)** — rejects
  construction if `x1 < x0` or `y1 < y0`, with an error message that
  specifically calls out "a y-flip that subtracted in place without
  re-sorting" as the likely cause. This validator is what makes
  `tests/test_geometry.py::test_bbox_rejects_inverted_y` and
  `tests/test_roundtrip.py::test_bbox_is_never_constructed_from_a_raw_fitz_rect`
  possible — a coordinate bug becomes an immediate exception at construction
  time instead of a silently-wrong rectangle discovered later.
- **`width` / `height` (`@property`)** — `x1 - x0` and `y1 - y0`.
- **`as_tuple() -> tuple[float, float, float, float]`** — returns
  `(x0, y0, x1, y1)`. Used wherever code needs to compare or format the raw
  numbers (e.g. `format_xfdf_rect` in `geometry.py`, or test assertions).

### `PageGeometry(BaseModel)`
One page's dimensions: `page_index` (0-based; note `Field(ge=0, ...)` rejects
negative page numbers), `width`, `height`, `rotation`. Stored so later steps
can flip coordinates without re-opening the PDF — `height` is what every
y-flip in `geometry.py` is measured against.

### `CRFField(BaseModel)`
One detected capture field on the blank CRF: `field_id` (document-unique),
`page_index`, `bbox`, `label` (nearest caption text), `source`
(`FieldSource`), `context` (extra surrounding text for disambiguation — see
`build_context` in [extract.md](extract.md)), and `acroform_name` (the raw PDF
form-field name, if any). This is what `extract.py` produces and what
`prompt.py` turns into rows of the CSV spec sheet.

### `FieldSet(BaseModel)`
The complete output of one extraction run — what gets written to
`build/fields.json`. Fields: `source_pdf`, `pages: list[PageGeometry]`,
`fields: list[CRFField]`, `extracted_at` (defaults to now). Persisted (rather
than kept only in memory) because `parse_response.py` needs the geometry back
*after* a Copilot reply returns — Copilot's reply carries no coordinates at
all, only `field_id`s, so this file is how a `field_id` gets its `bbox` back.

- **`for_page(page_index) -> list[CRFField]`** — a one-line filter:
  `[f for f in self.fields if f.page_index == page_index]`. This is a **list
  comprehension** — build a new list by iterating `self.fields` and keeping
  only the elements matching the condition, all in one expression rather than
  a `for` loop with `.append()` calls.
- **`for_pages(page_indexes: Iterable[int]) -> list[CRFField]`** — like
  `for_page` but for multiple pages at once. Converts the input to a `set`
  first (`wanted = set(page_indexes)`) so the membership check
  `f.page_index in wanted` is O(1) per field rather than O(n) — a small but
  real optimization when checking many fields against many wanted pages.
  `Iterable[int]` (rather than `list[int]`) is a looser type hint meaning "any
  type I can loop over," which is the more honest constraint for a parameter
  that's immediately converted to a `set` anyway.
- **`by_id(field_id) -> Optional[CRFField]`** — linear search
  (`for f in self.fields: if f.field_id == field_id: return f`) returning
  `None` if nothing matches. This is the **join key** the whole
  batch/response redesign depends on: because `field_id` is globally unique
  (assigned by `extract.py`'s dedup pass), a Copilot reply can echo back a
  `field_id` for any row, in any order, missing rows and all, and still be
  unambiguously reattached to its original geometry via this method. Compare
  to a design keyed on row position, where a dropped or reordered row would
  silently misattribute data — that's the failure mode this method's
  docstring specifically calls out as avoided.

### `CopilotProposal(BaseModel)`
The shape of **one row as it comes back from Copilot**, before it's promoted
to a full `SdtmAnnotation`. Deliberately narrower than `SdtmAnnotation` — no
coordinates (Copilot never sees or reasons about them) and no provenance
fields (those get filled in afterward by `parse_response.py`, not trusted from
the reply).

- **`model_config = ConfigDict(extra="ignore", populate_by_name=True)`** —
  `extra="ignore"` means any column Copilot's reply has that isn't declared
  here (e.g. a volunteered `"notes"` or `"sdtm_class"` column) is silently
  dropped rather than raising a validation error. This matters because a chat
  model is prone to adding columns nobody asked for; the parser only cares
  about the columns it defined.
- **`field_id: str = Field(min_length=1, ...)`** — required, and must not be
  an empty string. Without a `field_id`, this row can never be rejoined to a
  `bbox`, so the model refuses to construct one — pushing that failure as
  early as possible.
- **`_upper` and `_coerce_origin` validators** — same normalization pattern as
  `SdtmAnnotation` below (uppercase domain/variable, lenient `Origin`
  parsing) — necessary because this model is the *first* validation boundary
  a chat reply hits.

### `SdtmAnnotation(BaseModel)`
The full annotation record — what actually gets positioned, rendered, and
written to XFDF. Combines proposal content (`domain`, `variable`,
`condition`, `codelist`, `origin`) with provenance/audit-trail fields
(`confidence`, `rationale`, `source_model`, `review_status`, `reviewed_by`,
`created_at`, `reviewed_at`).

- **`_upper` / `_coerce_origin` validators** — same as `CopilotProposal`, and
  the comment above them explains *why* this model needs the same leniency a
  second time: `xfdf_to_pdf.py` reads XFDF files that a human may have
  hand-typed a value like `"collected"` into via Acrobat, and that's a second,
  independent human-authored entry point into this model — so the same
  forgiveness Copilot's reply needs, a hand-edit needs too.
- **`_check_review_provenance` (`@model_validator(mode="after")`)** — the
  **Part 11 audit-trail guard**: if `review_status` is anything other than
  `PROPOSED`, `reviewed_by` must be set, or construction raises
  `ValidationError`. This is the mechanism that makes "every status
  transition names a human" a fact enforced by the type system, not just a
  convention someone has to remember to follow. See
  `tests/test_models.py::test_leaving_proposed_requires_naming_the_reviewer`.
- **`display_page` (`@property`) → int`** — `page_index + 1`. The *only*
  place page numbers convert from 0-based (used everywhere internally,
  matching XFDF's own 0-based `@page`) to 1-based (what a human expects to
  read). The docstring is explicit: "Never write this to XFDF."
- **`label_text() -> str`** — builds the human-readable mapping string, e.g.
  `"VS.VSORRES when VSTESTCD = SYSBP"`. Uses a list comprehension with a
  filter to drop `None`/empty parts:
  ```python
  parts = [p for p in (self.domain, self.variable) if p]
  text = ".".join(parts) if len(parts) == 2 else (parts[0] if parts else "")
  ```
  This is a **nested conditional expression** (`x if cond else y`, sometimes
  called a ternary) — read the outer one first ("if both domain and variable
  are present, join them with a dot; otherwise, pick the single part if there
  is one, else empty string"). Then, if a `condition` is set, it's appended
  with `" when "`. Returns empty string when the annotation maps to nothing —
  handled specially by the next method.
- **`display_text() -> str`** — what actually gets drawn on the page. Falls
  back from `label_text()` to `"[Not Submitted]"` when the origin is
  `NOT_SUBMITTED` and there's otherwise nothing to show, and to `""`
  otherwise. The docstring explains why this fallback exists: a reviewer
  needs to be able to visually distinguish "deliberately not submitted" from
  "nobody looked at this field yet" — rendering nothing for both would make
  them indistinguishable in the finished PDF.

### `AnnotationSet(BaseModel)`
A whole document's worth of annotations — what gets written to
`build/proposals.json`. Fields: `source_pdf`, `pages`, `annotations:
list[SdtmAnnotation]`, `generated_at`.

- **`for_page(page_index) -> list[SdtmAnnotation]`** — same pattern as
  `FieldSet.for_page`.
- **`submittable() -> list[SdtmAnnotation]`** — everything **not**
  `REJECTED`: `[a for a in self.annotations if a.review_status is not
  ReviewStatus.REJECTED]`. Note the `is not` (identity comparison) rather
  than `!=` — appropriate here because `ReviewStatus` members are singleton
  enum values, so identity and equality coincide, and `is`/`is not` is the
  idiomatic way to compare enum membership in Python. This is what XFDF
  writing (`xfdf.py`) is conceptually built around: a rejected annotation
  "does not exist as far as the submission is concerned."
</content>
