# `tests/test_models.py`

## Role in the pipeline

Tests the invariants pydantic enforces on the data model in
[../pipeline/models.md](../pipeline/models.md) — split, per the file's own
docstring, into "the Part 11 guard" (the audit-trail rule that any review
status transition must name a human) and "the two lenient input boundaries"
(the forgiving `Origin` parsing and uppercase normalization that both
`CopilotProposal` and `SdtmAnnotation` apply, because both models have a
human-authored entry point — a Copilot reply and a hand-edited XFDF file,
respectively).

## Python concepts you'll see here

**A local helper function with keyword-argument overrides, for reducing test
boilerplate.**
```python
def _annot(**overrides):
    kwargs = dict(
        annot_id="a1", page_index=0, bbox=BBox(x0=72, y0=700, x1=200, y1=715),
        kind=AnnotationKind.VARIABLE, domain="VS", variable="VSORRES",
    )
    kwargs.update(overrides)
    return SdtmAnnotation(**kwargs)
```
`**overrides` collects any keyword arguments the caller passes into a dict;
`kwargs.update(overrides)` merges them over a set of sensible defaults
(anything in `overrides` replaces the matching default; anything not
mentioned keeps its default); `SdtmAnnotation(**kwargs)` spreads the merged
dict back out as keyword arguments to the constructor. The result: every test
in this file that needs an `SdtmAnnotation` can write just
`_annot(review_status=ReviewStatus.ACCEPTED)` and get a fully-valid object
with only the *one* field relevant to that test overridden — a very common
Python testing pattern for reducing repetition when many tests need "mostly
the same" object.

**`pytest.raises(..., match=...)`.** `pytest.raises(ValidationError,
match="reviewed_by")` doesn't just assert *that* an exception was raised — it
also asserts the exception's string representation contains a match for the
given regular expression. This is a stronger test than a bare `pytest.raises`
with no `match`: it confirms the error is about the thing the test expects
(a missing `reviewed_by`), not merely *some* `ValidationError` that happened
to occur for an unrelated reason.

**`pytest.mark.parametrize`.**
```python
@pytest.mark.parametrize("spelling", ["Collected", "collected", "COLLECTED"])
def test_origin_accepts_chat_and_hand_typed_spellings(spelling):
    ...
```
This decorator runs the same test function **three separate times**, once
per value in the list, each showing up as its own named test result
(`test_origin_accepts_chat_and_hand_typed_spellings[Collected]`, etc.) — a
concise way to test the same logic against multiple inputs without
copy-pasting the test body three times, and each case is individually
reportable if it fails.

## Tests, in file order

### Provenance / 21 CFR Part 11 (`_annot` helper defined first)
- **`test_annotations_start_as_proposals`** — a freshly constructed
  `SdtmAnnotation` defaults to `ReviewStatus.PROPOSED`.
- **`test_leaving_proposed_requires_naming_the_reviewer`** — constructing one
  with `review_status=ACCEPTED` and no `reviewed_by` raises
  `ValidationError` matching `"reviewed_by"` — this is the direct test of
  `SdtmAnnotation._check_review_provenance` (see
  [../pipeline/models.md](../pipeline/models.md)).
- **`test_reviewed_annotation_is_valid_once_attributed`** — the same
  transition succeeds once `reviewed_by` is supplied.

### Lenient input, canonical storage
- **`test_origin_accepts_chat_and_hand_typed_spellings`** — parametrized
  over `"Collected"`/`"collected"`/`"COLLECTED"`, checked against **both**
  `CopilotProposal` and `SdtmAnnotation` — confirming the same leniency is
  applied at both human-authored entry points.
- **`test_origin_tolerates_separators_in_multiword_values`** — parametrized
  over `"NotSubmitted"`/`"not submitted"`/`"not_submitted"`, testing
  `Origin.coerce` directly rather than through a model.
- **`test_origin_still_rejects_values_outside_define_xml`** — `Origin.coerce
  ("Guessed")` still raises `ValueError` — confirming the leniency has a
  boundary; it accepts spelling/spacing variance, not made-up values outside
  the actual Define-XML v2.1 vocabulary.
- **`test_domain_and_variable_are_normalised_to_uppercase`** — lowercase
  input to either model comes back uppercase, confirming the `_upper`
  validators run.

### Parse boundary
- **`test_proposal_ignores_extra_keys_copilot_invents`** — a dict with an
  unrecognized key (`"commentary"`) still validates successfully against
  `CopilotProposal`, confirming `extra="ignore"` behaves as intended (see
  [../pipeline/models.md](../pipeline/models.md)).
- **`test_proposal_rejects_a_missing_field_id`** — without `field_id`,
  construction raises `ValidationError` — since without it there's no way
  back to a `bbox`, this must fail loudly rather than silently producing an
  unusable proposal.

### Label rendering
- **`test_label_text_renders_the_conditional_pattern`** — confirms
  `label_text()` produces `"VS.VSORRES when VSTESTCD = SYSBP"` for a
  domain+variable+condition combination — the exact findings-class pattern
  central to the whole project's SDTM mapping.
- **`test_display_page_is_one_based_only_for_humans`** — confirms
  `page_index` stays `0` while `display_page` reports `1` — the one place
  1-based numbering is allowed to exist, per [../pipeline/models.md](../pipeline/models.md).
</content>
