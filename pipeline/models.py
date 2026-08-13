"""Typed records for the aCRF annotation pipeline.

Coordinate convention
---------------------
Every ``BBox`` in this module is in **PDF user space**: bottom-left origin,
y increasing upward. PyMuPDF's native ``fitz.Rect`` is top-left origin with y
increasing downward, so a fitz rect is *not* a BBox and must never be handed
to one of these models without going through ``pipeline.geometry``. The
fitz-native rect exists only transiently inside ``extract.py``.

Provenance
----------
There is no API model behind these annotations -- a human pastes them out of
Copilot 365 chat. The provenance fields (``source_model``, ``review_status``,
``reviewed_by``, timestamps) are therefore load-bearing for the GxP / 21 CFR
Part 11 audit trail rather than decorative: they are the record that a human
was in the loop at the annotation step.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Iterable, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class AnnotationKind(str, Enum):
    """What an annotation is asserting about the form."""

    DOMAIN = "domain"
    VARIABLE = "variable"
    NOTE = "note"


class FieldSource(str, Enum):
    """How a :class:`CRFField` was detected in the blank CRF."""

    ACROFORM = "acroform"
    TEXT_LAYOUT = "text_layout"


class Origin(str, Enum):
    """Define-XML v2.1 origin types."""

    COLLECTED = "Collected"
    DERIVED = "Derived"
    ASSIGNED = "Assigned"
    PROTOCOL = "Protocol"
    EDT = "eDT"
    PREDECESSOR = "Predecessor"
    NOT_SUBMITTED = "NotSubmitted"

    @classmethod
    def coerce(cls, value: object) -> "Origin":
        """Case-insensitive lookup.

        Copilot is a chat UI, not an API with a constrained decoding grammar;
        it returns ``"collected"`` or ``"NOT SUBMITTED"`` about as often as the
        canonical spelling. Normalising here keeps that leniency in one place.
        """
        if isinstance(value, cls):
            return value
        key = str(value).strip().replace(" ", "").replace("_", "").replace("-", "").lower()
        for member in cls:
            if member.value.lower() == key:
                return member
        raise ValueError(
            f"{value!r} is not a Define-XML v2.1 origin "
            f"(expected one of: {', '.join(m.value for m in cls)})"
        )


class ReviewStatus(str, Enum):
    """Where an annotation sits in the human review workflow.

    Everything Copilot produces enters as ``PROPOSED``. Nothing reaches a
    submission artifact until a human moves it off that value.
    """

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    EDITED = "edited"
    REJECTED = "rejected"


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------


class BBox(BaseModel):
    """An axis-aligned rectangle in PDF user space (bottom-left origin).

    ``y0`` is the bottom edge and ``y1`` the top edge, i.e. ``y1 >= y0``.
    Constructing one directly from a ``fitz.Rect`` is a bug; use
    ``pipeline.geometry.fitz_rect_to_bbox``.
    """

    model_config = ConfigDict(frozen=True)

    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode="after")
    def _check_ordering(self) -> "BBox":
        if self.x1 < self.x0:
            raise ValueError(f"x1 ({self.x1}) must be >= x0 ({self.x0})")
        if self.y1 < self.y0:
            raise ValueError(
                f"y1 ({self.y1}) must be >= y0 ({self.y0}); a y-flip that subtracted "
                "in place without re-sorting the coordinates is the usual cause"
            )
        return self

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


class PageGeometry(BaseModel):
    """Page dimensions, kept so downstream steps can flip coordinates.

    ``height`` is what both directions of the y-flip are measured against.
    """

    page_index: int = Field(ge=0, description="0-based; add 1 only when showing a human")
    width: float
    height: float
    rotation: int = 0


# --------------------------------------------------------------------------
# Extraction output
# --------------------------------------------------------------------------


class CRFField(BaseModel):
    """One capture field detected on the blank CRF."""

    field_id: str = Field(description="Stable id, unique across the document")
    page_index: int = Field(ge=0, description="0-based")
    bbox: BBox
    label: str = Field(description="Nearest caption text for the field")
    source: FieldSource
    context: str = Field(
        default="",
        description="Surrounding text, given to Copilot for disambiguation",
    )
    acroform_name: Optional[str] = Field(
        default=None, description="AcroForm /T value when source is acroform"
    )


class FieldSet(BaseModel):
    """Everything ``extract.py`` writes to ``build/fields.json``.

    Persisted because ``parse_response.py`` needs the geometry back at ingest
    time -- the Copilot reply carries no coordinates.
    """

    source_pdf: str
    pages: list[PageGeometry] = Field(default_factory=list)
    fields: list[CRFField] = Field(default_factory=list)
    extracted_at: datetime = Field(default_factory=_utcnow)

    def for_page(self, page_index: int) -> list[CRFField]:
        return [f for f in self.fields if f.page_index == page_index]

    def for_pages(self, page_indexes: Iterable[int]) -> list[CRFField]:
        wanted = set(page_indexes)
        return [f for f in self.fields if f.page_index in wanted]

    def by_id(self, field_id: str) -> Optional[CRFField]:
        """Look up a field by its stable id -- the join key for a Copilot reply.

        ``field_id`` is already globally unique across the document (see
        ``extract.py``'s dedup pass), which is what makes it safe as a row
        identifier in a spec sheet that can span many pages: unlike a
        page-relative ordinal, it needs no page number alongside it to be
        unambiguous.
        """
        for f in self.fields:
            if f.field_id == field_id:
                return f
        return None


# --------------------------------------------------------------------------
# Parse boundary: what Copilot is asked to return
# --------------------------------------------------------------------------


class CopilotProposal(BaseModel):
    """One row as it comes back on the filled-in spec sheet.

    Mirrors the columns spelled out in the generated instructions: proposal
    columns only, no provenance and no coordinates. ``parse_response.py``
    validates against this first, then builds an :class:`SdtmAnnotation` by
    joining on ``field_id`` and filling provenance in itself.

    ``field_id`` -- not a row number -- is the join key. It travels in its own
    column on the sheet, already globally unique, so Copilot echoing it back
    unambiguously identifies the row even when a sheet spans many pages and
    even if rows get reordered in the reply.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    field_id: str = Field(min_length=1, description="Echoed back from the sheet's field_id column")
    kind: AnnotationKind = AnnotationKind.VARIABLE
    domain: Optional[str] = None
    variable: Optional[str] = None
    condition: Optional[str] = Field(
        default=None, description='e.g. "VSTESTCD = SYSBP"'
    )
    codelist: Optional[str] = None
    origin: Optional[Origin] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    rationale: Optional[str] = None

    @field_validator("domain", "variable", mode="before")
    @classmethod
    def _upper(cls, v: object) -> object:
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("origin", mode="before")
    @classmethod
    def _coerce_origin(cls, v: object) -> object:
        if v is None or v == "":
            return None
        return Origin.coerce(v)


# --------------------------------------------------------------------------
# Annotation record
# --------------------------------------------------------------------------


class SdtmAnnotation(BaseModel):
    """A proposed or reviewed SDTM annotation, positioned on the page.

    ``field_id`` is nullable so page-level domain banners (which belong to the
    page, not to any one capture field) can be represented here too.
    """

    annot_id: str
    field_id: Optional[str] = None
    page_index: int = Field(ge=0, description="0-based")
    bbox: BBox
    kind: AnnotationKind

    # Proposal content
    domain: Optional[str] = None
    variable: Optional[str] = None
    condition: Optional[str] = None
    codelist: Optional[str] = None
    origin: Optional[Origin] = None

    # Provenance / audit trail
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    rationale: Optional[str] = None
    source_model: str = Field(
        default="Copilot 365 chat, manual paste",
        description="Free text -- there is no API model_id in this pipeline",
    )
    review_status: ReviewStatus = ReviewStatus.PROPOSED
    reviewed_by: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    reviewed_at: Optional[datetime] = None

    # Same leniency as CopilotProposal, because this model has a second
    # human-authored entry point: xfdf_to_pdf.py reads XFDF that a person may
    # have hand-edited in Acrobat, and "collected" typed by hand should not be
    # a hard failure at the last step of the pipeline. The stored value is
    # still canonical -- only the input spelling is forgiving.
    @field_validator("domain", "variable", mode="before")
    @classmethod
    def _upper(cls, v: object) -> object:
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("origin", mode="before")
    @classmethod
    def _coerce_origin(cls, v: object) -> object:
        if v is None or v == "":
            return None
        return Origin.coerce(v)

    @model_validator(mode="after")
    def _check_review_provenance(self) -> "SdtmAnnotation":
        if self.review_status is not ReviewStatus.PROPOSED and not self.reviewed_by:
            raise ValueError(
                f"review_status={self.review_status.value!r} requires reviewed_by "
                "(Part 11: a status transition must name the human who made it)"
            )
        return self

    @property
    def display_page(self) -> int:
        """1-based page number, for humans only. Never write this to XFDF."""
        return self.page_index + 1

    def label_text(self) -> str:
        """The SDTM mapping as text, e.g. ``VSORRES when VSTESTCD = SYSBP``.

        Empty when the annotation maps to no variable, which is a real case --
        see :meth:`display_text`.
        """
        parts = [p for p in (self.domain, self.variable) if p]
        text = ".".join(parts) if len(parts) == 2 else (parts[0] if parts else "")
        if self.condition:
            text = f"{text} when {self.condition}" if text else self.condition
        return text

    def display_text(self) -> str:
        """What actually gets drawn on the page.

        Fields that map to nothing still need a visible mark: a reviewer has
        to be able to tell "deliberately not submitted" from "nobody looked at
        it". Falling back to label_text() alone would render them as blank and
        silently drop them from the artifact.
        """
        text = self.label_text()
        if text:
            return text
        if self.origin is Origin.NOT_SUBMITTED:
            return "[Not Submitted]"
        return ""


class AnnotationSet(BaseModel):
    """A document's worth of annotations, as written to ``build/proposals.json``."""

    source_pdf: str
    pages: list[PageGeometry] = Field(default_factory=list)
    annotations: list[SdtmAnnotation] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=_utcnow)

    def for_page(self, page_index: int) -> list[SdtmAnnotation]:
        return [a for a in self.annotations if a.page_index == page_index]

    def submittable(self) -> list["SdtmAnnotation"]:
        """Everything that is not rejected -- i.e. what XFDF should carry."""
        return [a for a in self.annotations if a.review_status is not ReviewStatus.REJECTED]
