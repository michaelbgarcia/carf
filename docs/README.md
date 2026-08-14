# carf — Code Documentation & Python Field Guide

This `docs/` folder documents **every source file in the repository**, function by
function, in plain language — plus notes on the Python language features each
file demonstrates, since this project doubles as a decent tour of "real world"
Python (type hints, `pydantic`, `dataclasses`, `enum`, `pathlib`, XML handling,
`pytest`, and more).

It mirrors the source layout:

```
docs/
  README.md                 <- you are here
  pipeline/                 <- docs for pipeline/*.py  (the library code)
  scripts/                  <- docs for scripts/*.py   (the CLI entry points)
  tests/                    <- docs for tests/*.py     (the test suite)
```

Each doc file covers **one** source file and has the same shape:
1. **Role in the pipeline** — why this file exists, what calls it, what it calls.
2. **Python concepts** — language features used here, explained for someone
   learning Python, with a pointer to where they show up in the file.
3. **Function-by-function walkthrough** — every `def`, in file order, with its
   signature, what it does, and why it does it that way.

## What this codebase actually does

`carf` (an**C**RF, "annotated Case Report Form") takes a **blank clinical trial
form PDF** and produces an **annotated PDF** where every fillable field is
labeled with the SDTM (clinical data standard) variable it maps to — the kind
of document pharmaceutical companies submit to the FDA. Read
[../README.md](../README.md) and [../COWORK_INSTRUCTIONS.md](../COWORK_INSTRUCTIONS.md)
in the repo root first — they explain the *why* behind every design decision
referenced in these docs. This folder explains the *how*, at the function level.

The one unusual constraint that shapes the whole codebase: there is **no API
access to an LLM**. The only available model is a human typing into Microsoft
Copilot 365's chat window. So the "AI step" isn't a function call anywhere in
this code — it's a human copy/pasting between two files. Three scripts exist
specifically to bracket that human hop:

```
scripts/extract_and_prompt.py   scripts/ingest_response.py     scripts/render_final.py
       (before the human)     (after the human pastes a reply)   (after human review)
```

## Pipeline data flow

```
blank_crf.pdf
     │
     │  pipeline/extract.py   (PyMuPDF: find every fillable field + its caption)
     ▼
FieldSet (build/fields.json)
     │
     │  pipeline/prompt.py    (build Copilot instructions + CSV "spec sheet")
     ▼
copilot_batchN_instructions.txt  +  copilot_batchN_sheet.csv
     │
     │  ═══ HUMAN: paste into Copilot 365 chat, save the reply ═══
     ▼
copilot_batchN_response.csv
     │
     │  pipeline/parse_response.py  (CSV/markdown-table -> validated proposals)
     ▼
list[SdtmAnnotation]  (all review_status = "proposed")
     │
     │  pipeline/layout.py    (move each annotation off the field it describes)
     ▼
placed annotations
     │
     │  pipeline/xfdf.py      (serialize to XFDF, the Acrobat annotation format)
     ▼
build/blankcrf.xfdf
     │
     │  ═══ HUMAN: review/edit in Acrobat, mark accepted/edited/rejected ═══
     ▼
final.xfdf  (now authoritative)
     │
     │  pipeline/xfdf_to_pdf.py  (XFDF -> real PDF FreeText annotations)
     ▼
build/annotated_crf.pdf   <-- the FDA-submission-ready deliverable
```

Two side branches that never touch the deliverable:
- `pipeline/stamp.py` — a quick, pre-review "does this look right" preview,
  rendered straight from in-memory records rather than the XFDF file.
- `pipeline/render.py` — the low-level PyMuPDF drawing routine `stamp.py` and
  `xfdf_to_pdf.py` both call, so there's exactly one implementation of "draw a
  FreeText annotation on a page."

## Reading order, if you want to learn this codebase (and Python) top to bottom

1. **[pipeline/models.md](pipeline/models.md)** — the data model. Start here: every
   other file passes these objects around. Best introduction to `pydantic` in
   the repo.
2. **[pipeline/geometry.md](pipeline/geometry.md)** — tiny file, one concept (a
   coordinate flip), but it's the single most important piece of math in the
   project and a good example of writing an isolated, well-tested pure function.
3. **[pipeline/extract.md](pipeline/extract.md)** — the biggest, most
   algorithmic file: turning raw PDF geometry into structured fields.
4. **[pipeline/prompt.md](pipeline/prompt.md)** and
   **[pipeline/parse_response.md](pipeline/parse_response.md)** — building text
   for a human to paste, and recovering structure from what a human pastes back.
   Good examples of defensive text parsing.
5. **[pipeline/layout.md](pipeline/layout.md)**,
   **[pipeline/xfdf.md](pipeline/xfdf.md)**,
   **[pipeline/render.md](pipeline/render.md)**,
   **[pipeline/stamp.md](pipeline/stamp.md)**,
   **[pipeline/xfdf_to_pdf.md](pipeline/xfdf_to_pdf.md)** — turning annotations
   back into pixels/PDF markup.
6. **[scripts/](scripts)** — the CLI wrappers a human actually runs. Short,
   because nearly all logic lives in `pipeline/`.
7. **[tests/](tests)** — read these *after* the modules they test; they're the
   executable spec of what each function promises, and a good source of
   `pytest` idioms (fixtures, `parametrize`, property-based-style assertions).

## Python topics indexed across these docs

If you're using this project to learn Python, here's where to find each topic
explained in context (not in the abstract — tied to a real line of this code):

| Topic | Where it's explained |
|---|---|
| Type hints, `Optional`, `list[...]`, `from __future__ import annotations` | [pipeline/models.md](pipeline/models.md) |
| `pydantic` models, validators, `model_config` | [pipeline/models.md](pipeline/models.md) |
| `enum.Enum` (esp. `str, Enum` mixins) | [pipeline/models.md](pipeline/models.md) |
| `dataclasses` (`@dataclass`, `frozen=True`) | [pipeline/geometry.md](pipeline/geometry.md), [pipeline/extract.md](pipeline/extract.md) |
| Pure functions & why they're easy to test | [pipeline/geometry.md](pipeline/geometry.md) |
| List/dict/set comprehensions | [pipeline/extract.md](pipeline/extract.md) |
| Closures & sort `key=` functions | [pipeline/extract.md](pipeline/extract.md), [pipeline/prompt.md](pipeline/prompt.md) |
| `try/finally` and resource cleanup (no `with` support in PyMuPDF's `Document`) | [pipeline/extract.md](pipeline/extract.md) |
| Regular expressions (`re.compile`, `re.MULTILINE`) | [pipeline/parse_response.md](pipeline/parse_response.md) |
| The `csv` module (`DictReader`, `DictWriter`, `Sniffer`) | [pipeline/prompt.md](pipeline/prompt.md), [pipeline/parse_response.md](pipeline/parse_response.md) |
| Custom exceptions with extra attributes | [pipeline/parse_response.md](pipeline/parse_response.md) |
| `xml.etree.ElementTree` (building & parsing XML) | [pipeline/xfdf.md](pipeline/xfdf.md), [pipeline/xfdf_to_pdf.md](pipeline/xfdf_to_pdf.md) |
| XML namespaces in ElementTree (`{uri}tag` syntax) | [pipeline/xfdf.md](pipeline/xfdf.md) |
| `pathlib.Path` | everywhere — see [scripts/extract_and_prompt.md](scripts/extract_and_prompt.md) |
| `argparse` | every file in [scripts/](scripts) |
| `@property` and computed attributes | [pipeline/models.md](pipeline/models.md) |
| `@classmethod` and alternate constructors | [pipeline/models.md](pipeline/models.md) |
| Module-level "constants" as configuration | [pipeline/extract.md](pipeline/extract.md), [pipeline/layout.md](pipeline/layout.md) |
| `pytest` fixtures & `scope=` | [tests/conftest.md](tests/conftest.md) |
| `pytest.mark.parametrize` | [tests/test_extract.md](tests/test_extract.md), [tests/test_models.md](tests/test_models.md) |
| `pytest.approx` for float comparisons | [tests/test_geometry.md](tests/test_geometry.md) |
| Docstring-driven design ("why", not "what") | every doc, but see [pipeline/layout.md](pipeline/layout.md) first |

## File index

### `pipeline/` — the library
| File | Docs | One-line role |
|---|---|---|
| `__init__.py` | [docs](pipeline/__init__.md) | Package entry point; re-exports the public model classes |
| `models.py` | [docs](pipeline/models.md) | Every typed record (`BBox`, `CRFField`, `SdtmAnnotation`, ...) |
| `geometry.py` | [docs](pipeline/geometry.md) | The PDF/PyMuPDF y-axis coordinate flip, in one place |
| `extract.py` | [docs](pipeline/extract.md) | Finds capture fields + captions in a blank CRF PDF |
| `prompt.py` | [docs](pipeline/prompt.md) | Builds the Copilot instructions + CSV spec sheet |
| `parse_response.py` | [docs](pipeline/parse_response.md) | Parses a pasted Copilot reply back into records |
| `layout.py` | [docs](pipeline/layout.md) | Moves each annotation off the field it describes |
| `xfdf.py` | [docs](pipeline/xfdf.md) | Writes the XFDF (Acrobat annotation) file |
| `render.py` | [docs](pipeline/render.md) | Shared PyMuPDF "draw one annotation" helper |
| `stamp.py` | [docs](pipeline/stamp.md) | Pre-review QC preview PDF |
| `xfdf_to_pdf.py` | [docs](pipeline/xfdf_to_pdf.md) | Reviewed XFDF -> final submission PDF |

### `scripts/` — the command-line entry points
| File | Docs | One-line role |
|---|---|---|
| `make_sample_crf.py` | [docs](scripts/make_sample_crf.md) | Generates the synthetic test CRF + truth file |
| `extract_and_prompt.py` | [docs](scripts/extract_and_prompt.md) | Step 1: PDF -> fields -> Copilot batch materials |
| `ingest_response.py` | [docs](scripts/ingest_response.md) | Step 2: Copilot reply -> XFDF + QC preview |
| `render_final.py` | [docs](scripts/render_final.md) | Step 3: reviewed XFDF -> annotated PDF |
| `preview_extraction.py` | [docs](scripts/preview_extraction.md) | Debug aid: visualize what `extract.py` found |
| `illustrative_target.py` | [docs](scripts/illustrative_target.md) | Hand-drawn mockup of the end goal (not pipeline output) |

### `tests/` — the test suite
| File | Docs | One-line role |
|---|---|---|
| `conftest.py` | [docs](tests/conftest.md) | Shared pytest fixtures (`crfs`, `truth`) |
| `standin_response.py` | [docs](tests/standin_response.md) | Fake-but-plausible Copilot reply generator, for plumbing tests |
| `test_extract.py` | [docs](tests/test_extract.md) | Field-detection correctness |
| `test_fixture.py` | [docs](tests/test_fixture.md) | The synthetic CRF generator itself, and its truth file |
| `test_geometry.py` | [docs](tests/test_geometry.md) | The coordinate-flip math |
| `test_models.py` | [docs](tests/test_models.md) | Pydantic model invariants (esp. the Part 11 audit rule) |
| `test_parse_response.py` | [docs](tests/test_parse_response.md) | Recovery from a mangled pasted reply |
| `test_pipeline.py` | [docs](tests/test_pipeline.md) | Layout, stamping, and the full end-to-end loop |
| `test_prompt.py` | [docs](tests/test_prompt.md) | The generated Copilot instructions + spec sheet |
| `test_roundtrip.py` | [docs](tests/test_roundtrip.md) | The two coordinate-flip "gate" tests |
| `test_xfdf.py` | [docs](tests/test_xfdf.md) | The XFDF writer and hand-written reader |

## Two files intentionally left undocumented here

`fixtures/sample_crf_truth.json` and `build/.gitkeep` / `fixtures/.gitkeep` are
data/placeholder files, not code — nothing to explain function-by-function.
`requirements.txt`, `requirements-dev.txt`, `.gitignore`, and `.claude/` are
tooling config, not part of the pipeline itself.
</content>
