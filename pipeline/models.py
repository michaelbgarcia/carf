"""Typed records for the aCRF annotation pipeline.

The unit of extraction
----------------------
A CRF page is treated as **two text columns**: the left column carries the
question prompt, the right column carries the response option, unit or fixed
value. The extraction unit is therefore a :class:`CRFRow` -- one printed line
(or wrapped block) with up to two pieces of text and up to two bboxes -- and
*not* a detected capture field.

That is a deliberate reversal of the earlier design, which located AcroForm
widgets, drawn boxes and fill-in-blank underlines and then searched left,
right and above for whichever printed text seemed to caption each one. Column
position carries the same information without the search, and it degrades
better: a text extractor cannot silently omit a line it failed to understand,
so a row nobody has mapped is visible as an unmapped row rather than absent.

Coordinate convention
---------------------
Every ``BBox`` in this module is in **PDF user space**: bottom-left origin,
y increasing upward. PyMuPDF's native ``pymupdf.Rect`` is top-left origin with
y increasing downward, so a fitz rect is *not* a BBox and must never be handed
to one of these models without going through ``pipeline.geometry``. The
fitz-native rect exists only transiently inside ``pipeline.text`` and
``pipeline.rows``.

Provenance
----------
There is no API model behind these annotations. They come from mined corpus
precedent, from a human typing in the control sheet, or from a person pasting
Copilot 365 chat output. The provenance fields (``source_model``,
``review_status``, ``reviewed_by``, timestamps) are therefore load-bearing for
the GxP / 21 CFR Part 11 audit trail rather than decorative: they are the
record that a human was in the loop at the annotation step.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Iterable, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: What a row deliberately excluded from SDTM is marked with. Upper case per
#: Metadata Submission Guidelines v2.0. ``parse_annotated_pdf`` matches it
#: case-insensitively when reading historical aCRFs, which predate any house
#: convention and use both spellings.
NOT_SUBMITTED_TEXT = "[NOT SUBMITTED]"


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class AnnotationKind(str, Enum):
    """What an annotation is asserting about the form.

    ``DOMAIN`` is the page-top legend banner ("DM (Demographics)"), ``VARIABLE``
    is a mapping drawn in the gutter beside a row, ``NOTE`` is a free-text
    comment. The three render differently under Metadata Submission Guidelines
    v2.0 -- see ``pipeline/render.py``.
    """

    DOMAIN = "domain"
    VARIABLE = "variable"
    NOTE = "note"

    @classmethod
    def coerce(cls, value: object) -> "AnnotationKind":
        """Case-insensitive lookup, the same leniency as :meth:`Origin.coerce`.

        This value arrives from the same human-typed sources -- a chat reply
        pasted out of Copilot returns ``"Variable"`` or ``"NOTE"`` about as
        often as the canonical spelling, and failing a whole batch over the
        capitalisation of a column whose only interesting value is ``note``
        costs a re-paste for nothing.
        """
        if isinstance(value, cls):
            return value
        key = str(value).strip().replace(" ", "").replace("_", "").replace("-", "").lower()
        for member in cls:
            if member.value == key:
                return member
        raise ValueError(
            f"{value!r} is not an annotation kind "
            f"(expected one of: {', '.join(m.value for m in cls)})"
        )


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

        Every entry point for this value is human-typed -- a spreadsheet cell,
        a hand-edited XFDF, a chat reply pasted out of Copilot. All of them
        produce ``"collected"`` or ``"NOT SUBMITTED"`` about as often as the
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

    Everything that was not typed by a human enters as ``PROPOSED``. Nothing
    reaches a submission artifact until a human moves it off that value, which
    now happens in the XLSX control sheet.
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
    Constructing one directly from a ``pymupdf.Rect`` is a bug; use
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
    """Page dimensions plus the detected column gutter.

    ``height`` is what both directions of the y-flip are measured against.

    ``gutter_x`` is the x coordinate separating the question column from the
    response column, in PDF user space (x is unaffected by the y-flip, so it
    needs no conversion). ``None`` means this page is single-column -- a title
    or instruction page -- which is a valid outcome and not an error. It is
    stored rather than recomputed so it is inspectable in ``rows.json`` and
    directly assertable in tests, instead of being an invisible intermediate
    that can only be debugged through its downstream effects.
    """

    page_index: int = Field(ge=0, description="0-based; add 1 only when showing a human")
    width: float
    height: float
    rotation: int = 0
    gutter_x: Optional[float] = None


# --------------------------------------------------------------------------
# Extraction output
# --------------------------------------------------------------------------


class CRFRow(BaseModel):
    """One printed line of a CRF, split across the two text columns.

    Mirrors the row shape the extraction step produces: page, form, ``text_1``
    with ``bbox_1``, ``text_2`` with ``bbox_2``.

    Both halves are optional, and each absence means something different:

    * ``text_2`` empty -- a question with a fill-in blank or a widget and no
      printed response text ("Year of Birth (yyyy)").
    * ``text_1`` empty -- a continuation option under the question above it
      ("Female" following "Sex ... Male"). Common, and the reason rows are not
      required to have a left half.

    ``full_width`` marks a run that crosses the gutter -- a page header, a
    footer, or a spanning instruction note. Those are excluded from gutter
    detection (they would erase the corridor they cross) but kept as rows,
    because a spanning note is frequently the thing that needs annotating.
    """

    model_config = ConfigDict(frozen=True)

    row_id: str = Field(description="Stable id, unique across the document")
    page_index: int = Field(ge=0, description="0-based")
    form: str = Field(default="", description="Form name from the page header, if any")
    text_1: str = Field(default="", description="Question column text")
    text_2: str = Field(default="", description="Response column text")
    bbox_1: Optional[BBox] = None
    bbox_2: Optional[BBox] = None
    full_width: bool = Field(
        default=False, description="Run crosses the gutter (header, footer, spanning note)"
    )

    @model_validator(mode="after")
    def _check_has_content(self) -> "CRFRow":
        if self.bbox_1 is None and self.bbox_2 is None:
            raise ValueError(
                f"row {self.row_id!r} has neither bbox_1 nor bbox_2; a row with no "
                "geometry cannot be annotated or reviewed and should not have been emitted"
            )
        if self.bbox_1 is not None and not self.text_1:
            raise ValueError(f"row {self.row_id!r} has bbox_1 but no text_1")
        if self.bbox_2 is not None and not self.text_2:
            raise ValueError(f"row {self.row_id!r} has bbox_2 but no text_2")
        return self

    @property
    def anchor(self) -> BBox:
        """The bbox an annotation is positioned against.

        ``bbox_1`` when there is a question, otherwise ``bbox_2`` -- an
        option-only row has nothing else to hang off.
        """
        return self.bbox_1 if self.bbox_1 is not None else self.bbox_2  # type: ignore[return-value]

    @property
    def display_page(self) -> int:
        """1-based page number, for humans only."""
        return self.page_index + 1

    def joined_text(self) -> str:
        """Both columns as one string, for precedent lookup and display."""
        parts = [p for p in (self.text_1, self.text_2) if p]
        return " / ".join(parts)


class RowSet(BaseModel):
    """Everything the extraction step writes to ``build/rows.json``.

    Persisted because every later step needs the geometry back, and neither a
    control sheet nor a Copilot reply carries coordinates.
    """

    source_pdf: str
    pages: list[PageGeometry] = Field(default_factory=list)
    rows: list[CRFRow] = Field(default_factory=list)
    extracted_at: datetime = Field(default_factory=_utcnow)

    def for_page(self, page_index: int) -> list[CRFRow]:
        return [r for r in self.rows if r.page_index == page_index]

    def for_pages(self, page_indexes: Iterable[int]) -> list[CRFRow]:
        wanted = set(page_indexes)
        return [r for r in self.rows if r.page_index in wanted]

    def by_id(self, row_id: str) -> Optional[CRFRow]:
        """Look up a row by its stable id -- the join key for every later step.

        ``row_id`` is globally unique across the document, which is what makes
        it safe as an identifier in a control sheet or spec sheet spanning many
        pages: unlike a page-relative ordinal, it needs no page number
        alongside it to be unambiguous, and a reordered or deleted row is still
        identifiable.
        """
        for r in self.rows:
            if r.row_id == row_id:
                return r
        return None

    def page(self, page_index: int) -> Optional[PageGeometry]:
        for p in self.pages:
            if p.page_index == page_index:
                return p
        return None

    def forms(self) -> list[str]:
        """Distinct form names in first-encountered order.

        Order matters: MSG colours are assigned per form by the order domains
        are first seen, so a stable form order makes the colouring stable.
        """
        seen: list[str] = []
        for r in self.rows:
            if r.form and r.form not in seen:
                seen.append(r.form)
        return seen


# --------------------------------------------------------------------------
# Parse boundary: what Copilot is asked to return
# --------------------------------------------------------------------------


class CopilotProposal(BaseModel):
    """One row as it comes back on the filled-in spec sheet.

    Mirrors the columns spelled out in the generated instructions: proposal
    columns only, no provenance and no coordinates. ``parse_response.py``
    validates against this first, then builds an :class:`SdtmAnnotation` by
    joining on ``(row_id, slot)`` and filling provenance in itself.

    ``row_id`` -- not a row number -- is the join key. It travels in its own
    column on the sheet, already globally unique, so Copilot echoing it back
    unambiguously identifies the row even when a sheet spans many pages and
    even if rows get reordered in the reply. ``slot`` distinguishes a second
    annotation on the same row (the ``AGE`` / ``AGEU`` case) from a duplicate.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    row_id: str = Field(min_length=1, description="Echoed back from the sheet's row_id column")
    slot: int = Field(default=1, ge=1, le=2)
    kind: AnnotationKind = AnnotationKind.VARIABLE
    domain: Optional[str] = None
    variable: Optional[str] = None
    condition: Optional[str] = Field(default=None, description='e.g. "VSTESTCD = SYSBP"')
    fixed_value: Optional[str] = Field(
        default=None, description='e.g. "PROTOCOL MILESTONE" from "DSCAT = PROTOCOL MILESTONE"'
    )
    codelist: Optional[str] = None
    origin: Optional[Origin] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    rationale: Optional[str] = None

    @field_validator("domain", "variable", mode="before")
    @classmethod
    def _upper(cls, v: object) -> object:
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("kind", mode="before")
    @classmethod
    def _coerce_kind(cls, v: object) -> object:
        """A blank ``kind`` cell means the ordinary case, not a validation error.

        ``parse_response._scrub`` turns every empty cell into ``None``, and an
        explicit ``None`` defeats the field default -- so a sheet that came back
        with this column left empty (the common reply, since only note rows were
        ever told to fill it in) failed *every* row on an enum error. Falling
        back to the default here is what makes "left it blank" mean ``variable``.
        """
        if v is None or (isinstance(v, str) and not v.strip()):
            return AnnotationKind.VARIABLE
        return AnnotationKind.coerce(v)

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

    ``row_id`` is nullable so page-level domain legend banners (which belong to
    the page, not to any one row) can be represented here too.

    One annotation can cover **several rows** -- see ``group_id`` and
    ``member_row_ids``, and ``pipeline/grouping.py`` for the rules. ``row_id``
    stays the anchor (the first member in document order) so every join,
    lookup and colour decision that already keys on it keeps working
    unchanged; the group is additional information, not a different shape.

    ``text`` and the structured fields both exist, and which one wins is a
    deliberate rule rather than an oversight: **``text`` is authoritative for
    rendering whenever it is set.** The control sheet's ``anno1`` / ``anno2``
    cells are free text, because a human annotator wants to type
    ``VSORRES when VSTESTCD = SYSBP`` in one cell rather than decompose it
    across three. That literal string is what gets drawn. The structured
    fields are parsed off it best-effort (via
    ``parse_annotated_pdf.parse_mapping_text``) and exist for querying,
    corpus mining and define.xml reconciliation -- re-deriving the drawn text
    from them would silently discard a human's exact wording, which is the
    same mistake ``render.py`` documents avoiding.
    """

    annot_id: str
    row_id: Optional[str] = None
    slot: int = Field(default=1, ge=1, le=2, description="anno1 or anno2 on the row")
    page_index: int = Field(ge=0, description="0-based")
    bbox: BBox
    kind: AnnotationKind

    # Proposal content
    text: str = Field(
        default="", description="Literal annotation text; authoritative for rendering"
    )
    domain: Optional[str] = None
    variable: Optional[str] = None
    condition: Optional[str] = None
    fixed_value: Optional[str] = None
    codelist: Optional[str] = None
    origin: Optional[Origin] = None

    # Grouping (one annotation, many rows)
    group_id: Optional[str] = Field(
        default=None,
        description=(
            "Key shared by every row this annotation covers. Typed by a reviewer "
            "in the control sheet's `group` column, or assigned by "
            "pipeline.grouping when identical annotations were collapsed."
        ),
    )
    member_row_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Every row covered, in document order, `row_id` first. Empty on an "
            "ordinary single-row annotation -- use `covered_row_ids`, which "
            "normalises the two cases."
        ),
    )

    # Presentation (Metadata Submission Guidelines v2.0)
    color: Optional[tuple[int, int, int]] = Field(
        default=None,
        description="MSG background fill, 0-255 per channel; assigned by pipeline.msg",
    )

    # Provenance / audit trail
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    rationale: Optional[str] = None
    source_model: str = Field(
        default="unknown",
        description="Free text -- e.g. 'mined precedent (n=7)', 'human, control sheet'",
    )
    suggested: bool = Field(
        default=False,
        description=(
            "True when this was pre-populated rather than authored by a human. "
            "Drives the grey fill in the control sheet, so a reviewer can see at "
            "a glance what still needs looking at."
        ),
    )
    review_status: ReviewStatus = ReviewStatus.PROPOSED
    reviewed_by: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    reviewed_at: Optional[datetime] = None

    # Same leniency as CopilotProposal, because this model has several
    # human-authored entry points: the XLSX control sheet, and XFDF that a
    # person may have hand-edited in Acrobat. "collected" typed by hand should
    # not be a hard failure at the last step of the pipeline. The stored value
    # is still canonical -- only the input spelling is forgiving.
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

    @model_validator(mode="after")
    def _check_group(self) -> "SdtmAnnotation":
        """The anchor leads its own membership list, and nothing repeats.

        Both rules exist because the anchor is what every other step keys on.
        A ``member_row_ids`` that does not start with ``row_id`` would mean the
        box is positioned against one row and attributed to another; a repeated
        member would double-count the same row in coverage reporting and mine
        the same precedent entry twice.
        """
        if not self.member_row_ids:
            return self
        if self.row_id is None:
            raise ValueError(
                f"annotation {self.annot_id!r} has member_row_ids but no row_id; "
                "a group is anchored on its first member, which must also be row_id"
            )
        if self.member_row_ids[0] != self.row_id:
            raise ValueError(
                f"annotation {self.annot_id!r}: member_row_ids[0] is "
                f"{self.member_row_ids[0]!r} but row_id is {self.row_id!r}; the "
                "anchor row must lead its own group, since placement uses row_id"
            )
        if len(set(self.member_row_ids)) != len(self.member_row_ids):
            raise ValueError(
                f"annotation {self.annot_id!r} lists a row twice in member_row_ids: "
                f"{self.member_row_ids}"
            )
        return self

    @property
    def display_page(self) -> int:
        """1-based page number, for humans only. Never write this to XFDF."""
        return self.page_index + 1

    @property
    def covered_row_ids(self) -> list[str]:
        """Every row this annotation asserts a mapping for, grouped or not.

        The one accessor callers should use when they mean "which rows does
        this cover" -- coverage reporting, precedent mining, "is this row still
        unmapped". Reading ``row_id`` alone answers that question wrongly for a
        group, silently, by under-counting the members.
        """
        if self.member_row_ids:
            return list(self.member_row_ids)
        return [self.row_id] if self.row_id else []

    @property
    def is_grouped(self) -> bool:
        """Whether one drawn box stands in for an annotation on several rows."""
        return len(self.member_row_ids) > 1

    def label_text(self) -> str:
        """The SDTM mapping composed from the structured fields.

        Empty when the annotation maps to no variable, which is a real case --
        see :meth:`display_text`. Note this is *not* what gets drawn when
        ``text`` is set; see the class docstring.
        """
        parts = [p for p in (self.domain, self.variable) if p]
        out = ".".join(parts) if len(parts) == 2 else (parts[0] if parts else "")
        if self.fixed_value:
            out = f"{out} = {self.fixed_value}" if out else self.fixed_value
        if self.condition:
            out = f"{out} when {self.condition}" if out else self.condition
        return out

    def variable_text(self) -> str:
        """The mapping *without* its domain prefix, e.g. ``VSORRES when ...``.

        This -- not :meth:`label_text` -- is what goes on the page and into the
        control sheet's ``anno1``/``anno2`` cells. Under Metadata Submission
        Guidelines v2.0 the domain is carried by the annotation's colour and by
        the page-top legend, so repeating it inside every box is redundant; the
        guidelines' own examples read ``BRTHDTC`` and ``RACE = ASIAN``, not
        ``DM.BRTHDTC``. The control sheet likewise has its own ``domain`` column,
        and duplicating it in the annotation cell invites the two disagreeing.
        """
        parts = [p for p in (self.variable,) if p]
        out = parts[0] if parts else ""
        if self.fixed_value:
            out = f"{out} = {self.fixed_value}" if out else self.fixed_value
        if self.condition:
            out = f"{out} when {self.condition}" if out else self.condition
        return out

    def display_text(self) -> str:
        """What actually gets drawn on the page.

        The literal ``text`` wins when present -- a reviewer's exact wording is
        never re-derived. Rows that map to nothing still need a visible mark: a
        reviewer has to be able to tell "deliberately not submitted" from
        "nobody looked at it". Falling back to an empty string would render them
        as blank and silently drop them from the artifact.
        """
        if self.text.strip():
            return self.text.strip()
        composed = self.variable_text()
        if composed:
            return composed
        if self.origin is Origin.NOT_SUBMITTED:
            return NOT_SUBMITTED_TEXT
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
        """Everything that is not rejected -- i.e. what the artifact carries."""
        return [a for a in self.annotations if a.review_status is not ReviewStatus.REJECTED]

    def domains_for_page(self, page_index: int) -> list[str]:
        """Distinct domains on a page, in first-encountered order.

        The page-top legend banner lists these, and ``pipeline.msg`` assigns
        colours in this order.
        """
        seen: list[str] = []
        for a in sorted(self.for_page(page_index), key=lambda a: (-a.bbox.y1, a.bbox.x0)):
            if a.domain and a.domain not in seen:
                seen.append(a.domain)
        return seen
