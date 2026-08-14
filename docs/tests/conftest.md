# `tests/conftest.py`

## Role in the pipeline

Not part of the pipeline itself — this is `pytest`'s special "shared setup"
file. Any file named exactly `conftest.py` inside (or above) a test directory
is loaded automatically by `pytest`, before any test runs, and anything it
defines (especially **fixtures**) becomes available to every test file in that
directory **without an explicit import**. This particular `conftest.py`
defines the two fixtures nearly every other test file in the suite depends
on: `crfs` (the generated synthetic CRF PDFs) and `truth` (their ground-truth
field data).

## Python concepts you'll see here

**`pytest` fixtures.** A function decorated `@pytest.fixture` becomes
something a test can *request* simply by naming it as a parameter:
```python
@pytest.fixture(scope="session")
def crfs(tmp_path_factory) -> dict:
    return gen.make_sample_crf(tmp_path_factory.mktemp("crf"))
```
Any test function that declares a parameter named `crfs` (e.g. `def
test_something(crfs): ...`) automatically receives whatever this function
returns — `pytest` handles calling it, matching it up by name, and (per the
`scope` argument) deciding how often to re-run it. This is Python's
"dependency injection" pattern, specific to `pytest`: tests declare what they
need by parameter name, and the framework supplies it.

**`scope="session"`.** Normally a fixture re-runs fresh for *every* test that
requests it. `scope="session"` changes that to "run once for the entire test
run, and hand every requesting test the same cached result." This matters a
lot here for performance: `gen.make_sample_crf(...)` draws three PDFs from
scratch using PyMuPDF — not free — and dozens of tests across the whole suite
need the result. Running it once per *session* instead of once per *test*
is the difference between doing that work once versus dozens of times.

**Fixtures that request other fixtures.** `crfs`'s own parameter,
`tmp_path_factory`, is itself a **built-in `pytest` fixture** (not defined
anywhere in this project) that provides a way to create fresh temporary
directories. `tmp_path_factory.mktemp("crf")` creates one, uniquely named,
that gets cleaned up automatically by `pytest`'s own temp-directory
management. Fixtures composing other fixtures this way — built-in or
project-defined — is completely normal and is how `pytest`'s fixture system
scales to complex setups.

**`sys.path` manipulation for a non-package script.** The same
`sys.path.insert(0, str(p))` idiom seen throughout `scripts/*.py` (see
[../scripts/make_sample_crf.md](../scripts/make_sample_crf.md)) appears here
too, but with an extra entry:
```python
ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
import make_sample_crf as gen
```
Both the repo root (so `import pipeline...` works from test files) **and**
`scripts/` (so `import make_sample_crf` works directly, since that file isn't
part of the `pipeline` package) get added. The `if str(p) not in sys.path`
guard avoids inserting the same path twice if `conftest.py` somehow got
imported more than once in a session. `import make_sample_crf as gen` aliases
the whole module to a short name (`gen`, for "generator") — used throughout
the test suite wherever `scripts/make_sample_crf.py`'s functions or constants
are needed (e.g. `gen.PAGE_H`, `gen.layout()`, `gen.BANNER`).

## Fixtures, in file order

### `crfs(tmp_path_factory) -> dict`
Regenerates the three synthetic CRF PDF variants into a fresh temporary
directory once per test session, and returns the dict `make_sample_crf`
returns (`{"acroform": Path, "flat": Path, "ruled": Path, "truth": Path}`).
The docstring explains why this generates fresh PDFs rather than reading
committed files: **the PDFs themselves are never committed to the repo** —
only `fixtures/sample_crf_truth.json` is (see
[../scripts/make_sample_crf.md](../scripts/make_sample_crf.md) for why) — so
there's nothing to read from disk; every test run has to build them.

### `truth() -> FieldSet`
Returns `gen.build_truth(ACROFORM_NAME)` — the ground-truth field data
computed directly from the layout specification (not from running any
extraction code), in PDF user space. `ACROFORM_NAME` is a module-level
constant (`"SYNTHETIC_sample_crf_acroform.pdf"`) used as the `source_pdf`
value in the resulting `FieldSet`. Notably, this fixture has **no declared
scope**, meaning it defaults to `scope="function"` (fresh for every test) —
but since `build_truth` is cheap (pure Python object construction from
already-in-memory layout data, no PDF drawing involved), that's an
inexpensive default rather than something that needed the same `session`
optimization `crfs` needed.
</content>
