# `pipeline/__init__.py`

## Role in the pipeline

This is the file that makes the `pipeline/` directory a **Python package**
(anything importable as `import pipeline` or `from pipeline import x`). It does
no real work itself — its only job is to declare the package's version and
re-export the model classes from `models.py` so callers can write:

```python
from pipeline import BBox, CRFField, SdtmAnnotation
```

instead of the more verbose:

```python
from pipeline.models import BBox, CRFField, SdtmAnnotation
```

Every other file in this repo imports from `pipeline.models` directly (e.g.
`from pipeline.models import BBox`) rather than from `pipeline` itself — so in
practice this re-export is a convenience for external code or a REPL session,
not something the pipeline depends on internally.

## Python concepts you'll see here

**`__init__.py` and packages.** In Python, a directory becomes an *importable
package* by containing an `__init__.py` file (this rule is technically relaxed
by "namespace packages" in modern Python, but explicit `__init__.py` files are
still the normal, explicit way to do it — and it's what this project uses).
Code in `__init__.py` runs once, the first time anything imports from that
package. Here, that means `from pipeline.models import (...)` executes once,
pulling those names into the `pipeline` namespace itself.

**`__version__`.** A very common convention: expose the package's version as a
plain string attribute so other code (or a `pip show`-style tool) can check
`pipeline.__version__`. It's just a module-level variable — nothing special
about the name except that tooling *conventionally* looks for it.

**`__all__`.** This list controls what `from pipeline import *` pulls in, and
signals to readers (and some linters/IDEs) which names are the package's
public API versus internal implementation detail. It's advisory, not
enforced — you can still do `from pipeline import _private_thing` if such a
name existed and wasn't excluded some other way — but it's a clear, greppable
statement of intent.

**Multi-line imports with parentheses.** `from pipeline.models import (a, b,
c, ...)` — wrapping the imported names in parentheses lets the list span
multiple lines without needing a trailing backslash (`\`) for line
continuation. This is the standard, `black`/`ruff`-friendly way to write a
long import.

## Contents, top to bottom

There are no functions in this file — just three things:

1. **Module docstring** (lines 1–9) — a one-paragraph summary of the entire
   pipeline's data flow, from blank PDF to annotated PDF, plus the reminder
   that the "Copilot step" is a human action, not a function call. Worth
   reading before anything else in the codebase, since it's the shortest
   complete description of what this project does.

2. `__version__ = "0.1.0"` — the package version. Not currently read by any
   other code in the repo; it exists for external tooling / future packaging.

3. The re-export block:
   ```python
   from pipeline.models import (
       AnnotationKind, AnnotationSet, BBox, CopilotProposal, CRFField,
       FieldSet, FieldSource, Origin, PageGeometry, ReviewStatus, SdtmAnnotation,
   )
   __all__ = [ ... ]
   ```
   Every class defined in `pipeline/models.py` (see
   [models.md](models.md)) is imported here and listed in `__all__`. Note it
   deliberately does **not** re-export anything from `geometry.py`,
   `extract.py`, `xfdf.py`, etc. — those stay reachable only via their full
   module path (`pipeline.extract.extract_fields(...)`), which keeps the
   top-level `pipeline` namespace limited to *data*, not *behavior*.
</content>
