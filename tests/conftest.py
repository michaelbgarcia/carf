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

ACROFORM_NAME = "SYNTHETIC_sample_crf_acroform.pdf"


@pytest.fixture(scope="session")
def crfs(tmp_path_factory) -> dict:
    """The synthetic CRF, regenerated into a temp dir.

    Generated rather than read from fixtures/ because the PDFs are never
    committed -- only their truth file is.
    """
    return gen.make_sample_crf(tmp_path_factory.mktemp("crf"))


@pytest.fixture(scope="session")
def truth():
    """Ground truth straight from the layout spec, in PDF user space."""
    return gen.build_truth(ACROFORM_NAME)
