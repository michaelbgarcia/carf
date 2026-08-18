import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# scripts/ is not a package, so it goes on the path directly for the tests
# that exercise the fixture generator.
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import make_sample_crf as gen  # noqa: E402

ACROFORM_NAME = "SYNTHETIC_sample_crf_twocol_acroform.pdf"

#: The committed truth file. Read from disk rather than regenerated, because
#: reading it is what makes it a check: a truth file rebuilt in-process from the
#: current layout spec would agree with the current layout spec by construction
#: and could never catch a drift.
COMMITTED_TRUTH = ROOT / "fixtures" / "sample_crf_rows_truth.json"


@pytest.fixture(scope="session")
def crfs(tmp_path_factory) -> dict:
    """The synthetic CRF, regenerated into a temp dir.

    Generated rather than read from fixtures/ because the PDFs are never
    committed -- only their truth file is.
    """
    return gen.make_sample_crf(tmp_path_factory.mktemp("crf"))


@pytest.fixture(scope="session")
def truth() -> dict:
    """Ground truth as committed to fixtures/, in fitz space (y down)."""
    return json.loads(COMMITTED_TRUTH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def live_truth() -> dict:
    """Ground truth rebuilt from the current layout spec.

    Only for tests that deliberately perturb the layout and need the matching
    expectation. Anything asserting the layout has *not* drifted must use
    ``truth`` instead.
    """
    return gen.build_truth(ACROFORM_NAME)
