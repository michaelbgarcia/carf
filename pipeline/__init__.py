"""aCRF annotation pipeline.

Blank CRF PDF -> extracted fields -> a copy-paste prompt for Copilot 365 chat
-> pasted reply -> validated annotations -> XFDF -> human review -> annotated
submission PDF.

The Copilot step is a human hop, not a function call: there is no API access
to any LLM in this environment. See ``README.md``.
"""

__version__ = "0.1.0"

from pipeline.models import (
    AnnotationKind,
    AnnotationSet,
    BBox,
    CopilotProposal,
    CRFField,
    FieldSet,
    FieldSource,
    Origin,
    PageGeometry,
    ReviewStatus,
    SdtmAnnotation,
)

__all__ = [
    "AnnotationKind",
    "AnnotationSet",
    "BBox",
    "CRFField",
    "CopilotProposal",
    "FieldSet",
    "FieldSource",
    "Origin",
    "PageGeometry",
    "ReviewStatus",
    "SdtmAnnotation",
]
