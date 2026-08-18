"""aCRF annotation pipeline.

Blank CRF PDF -> two-column rows -> annotations pre-populated from mined corpus
precedent -> XLSX control sheet -> human review -> MSG-styled annotated
submission PDF.

The extraction unit is a two-column *row*, not a detected capture field: a CRF
page is assumed to be a question column and a response column, and column
position carries what an earlier design tried to recover by searching for
whichever printed text seemed to caption each widget. See ``pipeline/rows.py``.

Rows the pre-population step could not map are the only ones routed to Copilot
365 chat, which is a human copy/paste hop rather than a function call -- there
is no API access to any LLM in this environment. See ``README.md``.
"""

__version__ = "0.2.0"

from pipeline.models import (
    NOT_SUBMITTED_TEXT,
    AnnotationKind,
    AnnotationSet,
    BBox,
    CopilotProposal,
    CRFRow,
    Origin,
    PageGeometry,
    ReviewStatus,
    RowSet,
    SdtmAnnotation,
)

__all__ = [
    "NOT_SUBMITTED_TEXT",
    "AnnotationKind",
    "AnnotationSet",
    "BBox",
    "CRFRow",
    "CopilotProposal",
    "Origin",
    "PageGeometry",
    "ReviewStatus",
    "RowSet",
    "SdtmAnnotation",
]
