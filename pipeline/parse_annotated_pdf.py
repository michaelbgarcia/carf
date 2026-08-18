"""Recovers structured SDTM mappings from an already-annotated CRF PDF.

Not a step in the forward pipeline (rows -> precedent -> control sheet ->
layout -> annotated PDF). This runs the opposite direction: given a *finished*
aCRF -- this pipeline's own output from a prior study, or a third party's, as
long as it follows the same FDA aCRF convention of small FreeText markup
positioned near what it describes -- recover a text -> domain/variable/condition
table. That table is what makes a corpus of historical aCRFs usable as the
pre-population source for new ones (see ``pipeline.corpus_precedent``), rather
than a pile of PDFs nobody can query.

Two things make this lossy in a way the forward pipeline is not:

* **No join key survives to the final PDF.** ``render.draw_annotation`` draws
  only a bbox and a text string -- the ``row_id`` that ties an annotation to the
  row it describes lives in XFDF's ``<carf:meta>`` and in the control sheet, and
  neither is part of a finished aCRF. So a mark has to be re-matched by
  *position*.

  The two-column row model makes that materially more reliable than it was.
  Marks are matched against **rows**, and an annotation's position relative to
  its row is nearly unambiguous: ``layout.place_row`` puts it at
  ``bbox_1.x1 + GAP`` on the row's own baseline, so a mark sharing a row's
  vertical extent and starting just past its question text is that row's, full
  stop. The previous design matched against *detected capture fields*, where a
  dense grid of same-sized widgets a few points apart meant nearest-centroid
  regularly picked the neighbouring row. A wrong match is still possible for
  third-party markup that was never positioned by this codebase; every match is
  reported with the distance it was found at so a caller can weigh it.
* **Origin (Define-XML) never reaches the page.** ``display_text()`` renders
  only domain/variable/condition -- never origin -- so a recovered mapping
  can say what a field was called, never how it was collected. Don't invent
  an origin here; leave it to the caller (e.g. defaulting to Collected as a
  starting guess for human review, never as a fact).

Two visual conventions are read, because a corpus spans both -- see
:func:`classify_mark` for the rules and why their order matters. In short:
Metadata Submission Guidelines v2.0 output (what this pipeline now writes) marks
a mapping with a domain-coloured background and a comment with a dashed border,
while older output marks a mapping with red text, a note with grey text and a
domain banner with a border. Under MSG every mark is bordered and black, so the
older signals have to be checked last or they invert the classification
completely. A source drawing its aCRFs some third way needs those heuristics
re-tuned; the position matching underneath does not change.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

import pymupdf

from pipeline import layout
from pipeline.geometry import fitz_rect_to_bbox
from pipeline.models import NOT_SUBMITTED_TEXT, BBox, CRFRow, RowSet
from pipeline.rows import extract_rows

DEFAULT_MAX_MATCH_DISTANCE = 200.0  # points; layout.py's own moves are well under this
_GRAYSCALE_TOLERANCE = 0.08  # max channel spread that still counts as "gray"
_GRAY_CEILING = 0.9  # near-white text would also read as "gray"; exclude it

_DA_COLOR_RE = re.compile(r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+rg")
_MAPPING_RE = re.compile(
    r"^\s*(?:(?P<domain>[A-Za-z]{2,8})\.(?P<var_with_domain>[A-Za-z0-9_]+)"
    r"|(?P<var_alone>[A-Za-z0-9_]+))"
    r"(?:\s+when\s+(?P<condition>.+?)"
    r"|\s*=\s*(?P<fixed_value>\"[^\"]*\"|.+?))?\s*$"
)


# --------------------------------------------------------------------------
# Reading the PDF's own annotation layer
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RawMark:
    """One FreeText/Square annotation, read straight off the page -- no interpretation yet."""

    page_index: int
    bbox: BBox  # PDF user space
    text: str
    boxed: bool  # non-zero border width
    muted: bool  # grayscale text color
    dashed: bool = False  # dashed border -- MSG's marker for a comment note
    fill: Optional[tuple[float, float, float]] = None  # /C background, 0..1 per channel


def _is_muted(da: Optional[tuple]) -> bool:
    """Grayscale text color reads as a muted/not-submitted mark.

    Reads the raw ``/DA`` (default appearance) string via ``xref_get_key``
    rather than ``Annot.colors`` -- PyMuPDF's high-level ``colors`` covers a
    FreeText annot's border/interior, not its text color, which lives only in
    DA. Grayscale rather than an exact hex match against this codebase's own
    ``#7A7A7A``, so a source that picked a different but still-neutral note
    color is still recognised.
    """
    if not da or da[0] != "string":
        return False
    m = _DA_COLOR_RE.search(da[1])
    if not m:
        return False
    r, g, b = (float(v) for v in m.groups())
    return max(r, g, b) - min(r, g, b) <= _GRAYSCALE_TOLERANCE and max(r, g, b) < _GRAY_CEILING


def read_marks(pdf_path: str | Path) -> list[RawMark]:
    """Every non-empty FreeText/Square annotation on the PDF's own annotation layer.

    The PDF-native counterpart to ``xfdf_to_pdf.parse_xfdf`` reading an XFDF
    file -- same idea, different source. Deliberately ignores any join-key
    metadata (there isn't any, see module docstring) and does no field
    matching; that is a separate, uncertain step handled by
    :func:`match_marks_to_rows`.
    """
    doc = pymupdf.open(pdf_path)
    try:
        out: list[RawMark] = []
        for page_index, page in enumerate(doc):
            height = page.rect.height
            for annot in page.annots():
                if annot.type[1] not in ("FreeText", "Square"):
                    continue
                text = (annot.info.get("content") or "").strip()
                if not text:
                    continue
                da = doc.xref_get_key(annot.xref, "DA")
                border = annot.border
                # On a FreeText annot the background lives in /C, which PyMuPDF
                # reports back under "stroke" -- see render.py's docstring.
                fill = annot.colors.get("stroke") or None
                out.append(
                    RawMark(
                        page_index=page_index,
                        bbox=fitz_rect_to_bbox(annot.rect, height),
                        text=text,
                        boxed=border.get("width", 0.0) > 0.0,
                        muted=_is_muted(da),
                        dashed=bool(border.get("dashes")),
                        fill=tuple(fill) if fill else None,
                    )
                )
        return out
    finally:
        doc.close()


#: A legend entry, in either shape it gets written. MSG v2.0 uses
#: ``DM (Demographics)``; this codebase's older output and some third-party
#: aCRFs use ``DM = Demographics``. Both are a bare domain code plus its full
#: name, and both collide in shape with a fixed-value mark, so position -- not
#: text -- is what finally decides (see :func:`split_legend_marks`).
_LEGEND_RE = re.compile(
    r"^\s*(?P<code>[A-Za-z]{2,8})\s*(?:=\s*(?P<eq_name>[A-Za-z][A-Za-z0-9 /&\-]*)"
    r"|\(\s*(?P<paren_name>[A-Za-z][A-Za-z0-9 /&\-]*?)\s*\))\s*$"
)


def _legend_name(match: re.Match) -> str:
    return (match.group("eq_name") or match.group("paren_name") or "").strip()


def split_legend_marks(
    marks: list[RawMark], rows: RowSet
) -> tuple[dict[int, dict[str, str]], list[RawMark]]:
    """Pull page-level domain legends (e.g. ``DS=Disposition``) out of the marks.

    A legend sits in a page's header/margin, above every row on that page, and
    reads as a bare domain code -> full name -- the same ``CODE = phrase`` shape
    a fixed-value assignment mark can have (e.g. ``DSCAT = PROTOCOL MILESTONE``,
    see :func:`parse_mapping_text`). Text shape alone cannot tell the two apart;
    position can, and must: a legend is never handed to
    ``_mark_to_mapping``/``attribute_domains``/:func:`match_marks_to_rows` -- it
    describes the page, not a row, and parsing it as one would invent a nonsense
    variable named after the domain's own full name.

    Returns ``({page_index: {code: name}}, marks_with_legends_removed)``. A page
    with no extracted rows yields no legends from it -- nothing to compare a
    mark's position against, and no rows to make the legend useful to anyway.

    See :func:`legend_color_map` for the other thing a legend carries.
    """
    row_top: dict[int, float] = {}
    for row in rows.rows:
        top = row.anchor.y1
        row_top[row.page_index] = max(row_top.get(row.page_index, top), top)

    legends: dict[int, dict[str, str]] = {}
    remaining: list[RawMark] = []
    for m in marks:
        top = row_top.get(m.page_index)
        match = _LEGEND_RE.match(m.text) if top is not None else None
        if match and m.bbox.y0 >= top:
            legends.setdefault(m.page_index, {})[match.group("code").upper()] = _legend_name(match)
            continue
        remaining.append(m)
    return legends, remaining


def _color_key(fill: Optional[tuple[float, float, float]]) -> Optional[tuple[int, int, int]]:
    """Quantise a fill to 0-255 ints, so PDF float rounding cannot split a colour.

    A colour written as ``0.75 1 1`` and read back as ``0.7490196`` must compare
    equal, or every annotation would appear to be its own domain.
    """
    if fill is None:
        return None
    return tuple(int(round(c * 255)) for c in fill)  # type: ignore[return-value]


def legend_color_map(
    marks: list[RawMark], rows: RowSet
) -> dict[int, dict[tuple[int, int, int], str]]:
    """``{page_index: {fill colour: domain code}}``, from the page's own legend.

    The attribution tier that MSG styling makes necessary. Under the guidelines a
    variable annotation reads ``BRTHDTC``, not ``DM.BRTHDTC`` -- the domain is
    carried by the box's *colour*, keyed by the legend at the top of the page. So
    a mark's own text no longer names its domain, and without reading the legend
    the whole document would come back unattributed.

    That is not a guess. The legend is an explicit assertion, printed on the page,
    that this colour means this domain -- which is why the caller ranks it
    immediately after a boxed banner and well ahead of the built-in constants and
    mined precedent tiers.

    Runs on the *unsplit* mark list, since ``split_legend_marks`` removes exactly
    the marks this needs.
    """
    legends, _ = split_legend_marks(marks, rows)
    out: dict[int, dict[tuple[int, int, int], str]] = {}
    for m in marks:
        match = _LEGEND_RE.match(m.text)
        if not match:
            continue
        code = match.group("code").upper()
        if code not in legends.get(m.page_index, {}):
            continue  # matched the shape but was not accepted as a legend
        key = _color_key(m.fill)
        if key is not None:
            out.setdefault(m.page_index, {})[key] = code
    return out


# --------------------------------------------------------------------------
# Text -> structure
# --------------------------------------------------------------------------


def _strip_quotes(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def parse_mapping_text(
    text: str,
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Inverse of ``SdtmAnnotation.label_text()``/``display_text()`` -- best effort.

    Returns ``(domain, variable, condition, fixed_value)``. A domain banner's
    own text is just its code with no dot ("VS"), which this regex cannot
    tell apart from a bare variable ("SEX") -- that disambiguation needs the
    mark's `boxed` flag, which lives in the caller (:func:`_mark_to_mapping`),
    not in the text shape alone.

    `condition` and `fixed_value` are mutually exclusive and answer different
    questions: `condition` (introduced by the literal word "when") selects
    *which row* of a findings-class domain a mapping applies to; `fixed_value`
    (introduced by "=") asserts what a constant/qualifier variable's value
    *always is*, e.g. ``DSCAT = PROTOCOL MILESTONE``. Quotes around a fixed
    value are stripped; case is otherwise preserved, same as `condition`.
    """
    text = text.strip()
    # Case-insensitive: the house spelling is upper case per the guidelines, but
    # historical aCRFs predate any house convention and use both.
    if not text or text.upper() == NOT_SUBMITTED_TEXT.upper():
        return None, None, None, None
    m = _MAPPING_RE.match(text)
    if not m:
        return None, None, None, None
    condition = m.group("condition")
    fixed_value = _strip_quotes(m.group("fixed_value"))
    if m.group("domain"):
        return m.group("domain").upper(), m.group("var_with_domain").upper(), condition, fixed_value
    return None, m.group("var_alone").upper(), condition, fixed_value


# --------------------------------------------------------------------------
# Recovered record + geometry helpers
# --------------------------------------------------------------------------


@dataclass
class RecoveredMapping:
    """Best-effort reconstruction of one mark's SDTM mapping.

    Deliberately not an ``SdtmAnnotation`` -- that model validates strictly
    (an ``Origin`` that was never on the page, a ``review_status`` state
    machine that doesn't apply here) and this data is inherently uncertain.
    Keeping it a separate, permissive record is the same call ``XfdfAnnotation``
    made in ``xfdf_to_pdf.py``, for the same reason.
    """

    page_index: int
    bbox: BBox  # the mark's own bbox, not the matched field's
    text: str  # raw annotation text, verbatim
    kind: str  # "domain" | "variable" | "note"
    domain: Optional[str] = None
    variable: Optional[str] = None
    condition: Optional[str] = None
    fixed_value: Optional[str] = None  # e.g. "PROTOCOL MILESTONE" from "DSCAT = PROTOCOL MILESTONE"
    fill: Optional[tuple[float, float, float]] = None  # the mark's own background, for the legend-colour tier
    domain_inferred: bool = False  # True if `domain` did not come from this mark's own text
    domain_inference_source: Optional[str] = None  # "banner" | "legend" | "builtin" | "precedent"
    row_id: Optional[str] = None
    #: Every row the one drawn box covered, when it was a grouped annotation.
    #: A grouped mark is expanded into one of these records *per member*, each
    #: with its own `row_id` and `label`, so precedent mining learns the mapping
    #: for all five rows of a block rather than for whichever one sat nearest.
    #: This field is what still says they came from a single box.
    member_row_ids: list[str] = field(default_factory=list)
    label: Optional[str] = None  # the matched row's text_1 -- the lookup key
    context: Optional[str] = None  # form and response column, for disambiguation
    match_distance: Optional[float] = None  # points, centre-to-centre; None if unmatched
    synthesized: bool = False  # True only for summarize_page_domains' derived page-domain rows
    legend_name: Optional[str] = None  # cross-check: this domain's name in the page's own legend, if any
    source_pdf: Optional[str] = None  # filename, set by pipeline.corpus_precedent when mining a corpus


def _center(bbox: BBox) -> tuple[float, float]:
    return (bbox.x0 + bbox.x1) / 2.0, (bbox.y0 + bbox.y1) / 2.0


def _distance(a: BBox, b: BBox) -> float:
    """Centre-to-centre distance, reported as ``match_distance``."""
    ax, ay = _center(a)
    bx, by = _center(b)
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def _proximity(mark: BBox, anchor: BBox) -> float:
    """Distance from the mark's centre to the nearest point of ``anchor``.

    Used for *ranking* tier-2 candidates, where centre-to-centre is actively
    misleading. A row spanning the full page width -- a header, a footer, any row
    on a single-column page -- has its centroid in the middle of a very wide box,
    so an annotation sitting immediately to its right measures as hundreds of
    points away and loses to a row it has nothing to do with, or falls outside
    ``max_distance`` entirely and goes unmatched.

    Clamping to the box makes "just outside the right-hand edge" the small
    distance it visually is. ``match_distance`` still reports centre-to-centre,
    so what a reviewer sees in the report is unchanged.
    """
    cx, cy = _center(mark)
    nx = min(max(cx, anchor.x0), anchor.x1)
    ny = min(max(cy, anchor.y0), anchor.y1)
    return ((cx - nx) ** 2 + (cy - ny) ** 2) ** 0.5


def classify_mark(mark: RawMark) -> str:
    """``"domain"`` | ``"variable"`` | ``"note"`` from a mark's styling.

    Two conventions have to be read here, and they conflict on every signal --
    which is why this is a function with its rules written down rather than two
    inline ``elif``s.

    **MSG v2.0** (what this pipeline now writes, and what a compliant aCRF from
    anywhere looks like): every annotation has a solid 1pt border and black text,
    a mapping has a domain-coloured background, and a comment note is
    distinguished by a *dashed* border. So under MSG, "has a border" and "has
    black text" carry no information at all -- they are universal.

    **The convention this codebase used to write** (and which older aCRFs in a
    corpus follow): red text with no fill for a mapping, grey text for a note,
    and a *boxed* mark for a domain banner.

    Read in the wrong order these invert completely: MSG output is boxed
    everywhere, which the old rule calls a domain banner, and its text is black,
    which the old grey-text test calls a note. So the MSG signals are checked
    first and the legacy ones only reached when a mark carries no MSG styling at
    all.
    """
    if mark.dashed:
        return "note"  # MSG: dashed border is the comment marker
    if mark.fill is not None:
        # MSG: a coloured background means a mapping. A domain legend banner is
        # also filled, but never reaches here -- split_legend_marks removes it
        # first, on position.
        return "variable"
    if mark.muted:
        return "note"  # legacy: grey text
    if mark.boxed:
        return "domain"  # legacy: boxed banner, unfilled
    return "variable"


def _mark_to_mapping(mark: RawMark) -> RecoveredMapping:
    domain, variable, condition, fixed_value = parse_mapping_text(mark.text)
    kind = classify_mark(mark)
    if kind == "domain":
        # A banner's text is its domain code alone -- what the regex above,
        # lacking a dot to key on, puts in `variable` for lack of anywhere
        # else to put it.
        domain, variable = (domain or variable), None
    return RecoveredMapping(
        page_index=mark.page_index,
        bbox=mark.bbox,
        text=mark.text,
        kind=kind,
        fill=mark.fill,
        domain=domain,
        variable=variable,
        condition=condition,
        fixed_value=fixed_value,
    )


# --------------------------------------------------------------------------
# Domain banner attribution
# --------------------------------------------------------------------------

# CDISC SDTMIG-standardized variables whose home domain isn't guessable from
# the variable's own name (RFICDTC lives on DM, not "RF"). These are facts
# about the standard, not study-specific guesses, so they belong in code
# rather than in mined corpus data -- the weakest, mined "precedent" tier
# below is for everything this table doesn't cover.
BUILTIN_DOMAIN_PRECEDENT: dict[str, str] = {
    "RFICDTC": "DM",
    "RFSTDTC": "DM",
    "RFENDTC": "DM",
    "RFXSTDTC": "DM",
    "RFXENDTC": "DM",
    "RFPENDTC": "DM",
    "DTHDTC": "DM",
    "SUBJID": "DM",
    "USUBJID": "DM",
}

# Two-letter SDTM domain codes recognised as a variable-name prefix (e.g.
# DSSTDTC -> DS) -- the general SDTMIG convention, not an exhaustive list.
_KNOWN_DOMAIN_PREFIXES = {
    "DM", "DS", "AE", "CM", "VS", "LB", "EG", "MH", "PE", "SU", "EX", "CE",
    "DV", "IE", "DA", "FA", "MB", "MI", "RS", "SC", "SS", "TU", "TR", "QS",
}


def _builtin_domain(variable: Optional[str]) -> Optional[str]:
    """CDISC-standardized domain fallback -- not a mined guess.

    Tries the explicit table first (for variables like RFICDTC where the
    domain isn't in the variable's own name), then the general SDTMIG
    convention that a domain-prefixed variable name's first two letters name
    its own domain.
    """
    if not variable:
        return None
    if variable in BUILTIN_DOMAIN_PRECEDENT:
        return BUILTIN_DOMAIN_PRECEDENT[variable]
    prefix = variable[:2]
    if prefix in _KNOWN_DOMAIN_PREFIXES and variable != prefix:
        return prefix
    return None


def attribute_domains(
    mappings: list[RecoveredMapping],
    precedent: dict[str, str] | None = None,
    legend_colors: dict[int, dict[tuple[int, int, int], str]] | None = None,
) -> list[RecoveredMapping]:
    """Fill in `domain` on a variable mark, weakest evidence last.

    Five tiers, in order:

    1. the mark's own explicit domain (nothing to do);
    2. the nearest boxed banner above it on the page -- a domain banner governs
       everything below it, so proximity is vertical-only rather than bounded by
       horizontal overlap, since a banner is usually left-aligned while the rows
       under it are indented;
    3. the **page legend's colour key** (:func:`legend_color_map`), which is what
       MSG-styled output leaves to read: its annotations say ``BRTHDTC``, not
       ``DM.BRTHDTC``, and the domain lives in the box's colour;
    4. CDISC-standardized built-in constants (:func:`_builtin_domain`);
    5. ``precedent`` -- a variable -> domain table mined from a historical corpus
       (see ``pipeline/corpus_precedent.py``), the weakest tier since it reflects
       what other documents did, not what this one says.

    Tier 3 sits above the built-ins deliberately. A legend is an explicit
    assertion printed on the page being read; a built-in constant is a fact about
    SDTMIG that may not match what this particular document did.

    ``domain_inference_source`` records which tier fired ("banner" | "legend" |
    "builtin" | "precedent"), so a caller mining this output as further precedent
    can require the strongest evidence and avoid amplifying its own guesses (see
    ``build_variable_domain_precedent``).
    """
    banners_by_page: dict[int, list[RecoveredMapping]] = {}
    for m in mappings:
        if m.kind == "domain" and m.domain:
            banners_by_page.setdefault(m.page_index, []).append(m)

    out: list[RecoveredMapping] = []
    for m in mappings:
        if m.kind != "variable" or m.domain:
            out.append(m)
            continue

        best: Optional[tuple[float, RecoveredMapping]] = None
        for b in banners_by_page.get(m.page_index, []):
            if b.bbox.y0 < m.bbox.y1:  # banner must sit at or above the mark
                continue
            d = b.bbox.y0 - m.bbox.y1
            if best is None or d < best[0]:
                best = (d, b)
        if best is not None:
            out.append(
                replace(
                    m, domain=best[1].domain, domain_inferred=True, domain_inference_source="banner"
                )
            )
            continue

        by_color = (legend_colors or {}).get(m.page_index, {})
        keyed = by_color.get(_color_key(m.fill)) if by_color else None
        if keyed is not None:
            out.append(
                replace(m, domain=keyed, domain_inferred=True, domain_inference_source="legend")
            )
            continue

        builtin = _builtin_domain(m.variable)
        if builtin is not None:
            out.append(
                replace(m, domain=builtin, domain_inferred=True, domain_inference_source="builtin")
            )
            continue

        mined = precedent.get(m.variable) if precedent and m.variable else None
        if mined is not None:
            out.append(
                replace(m, domain=mined, domain_inferred=True, domain_inference_source="precedent")
            )
            continue

        out.append(m)
    return out


# --------------------------------------------------------------------------
# Spatial join back to a field
# --------------------------------------------------------------------------


#: Tolerance on an exact reverse-layout alignment. Has to cover two things:
#: XFDF's 3-decimal-place ``@rect`` rounding, and -- the one that bites -- the
#: half-border-width inflation PyMuPDF applies to a bordered annotation's stored
#: ``/Rect`` (a box placed at x0=260 is read back at 259.5, see ``render.py``).
#: Under MSG styling *every* annotation is bordered, so a tolerance tighter than
#: half a border width makes tier-1 matching silently unreachable and drops the
#: whole corpus onto the weaker centroid fallback without any error.
_ALIGN_TOL = layout.BORDER_INFLATION + 0.05
_STEP_TOL = 1e-3  # tolerance on (delta / LINE_STEP) being an integer
#: Keeps any distance fallback ranked behind every exact alignment.
_CENTROID_OFFSET = layout.MAX_WRAPS + 10.0


def _reverse_layout_score(mark_bbox: BBox, row: CRFRow) -> Optional[float]:
    """Score of 0..~6 if `mark_bbox` is exactly where ``layout.place_row`` would
    have put an annotation for `row`; ``None`` otherwise.

    Reconstructing that placement is nearly trivial, which is the payoff of
    anchoring annotations to text instead of searching for a free spot. There are
    only two shapes to check:

    1. **On the row's baseline**, starting at ``anchor.x1 + GAP`` -- slot 1 -- or
       further right for slot 2, which lands at an offset that depends on slot
       1's rendered width and so is matched by "at or past the slot-1 x" rather
       than an exact x.
    2. **Wrapped below**, at the question's own indent ``anchor.x0``, some whole
       number of ``LINE_STEP`` units down.

    The old field-based version had four shapes to reconstruct (right, below,
    left, then a forty-step downward walk) because placement was a search. A
    lower score is a better match, used only to break ties; any exact alignment
    outranks every nearest-centroid fallback (see ``_CENTROID_OFFSET``).
    """
    anchor = row.anchor
    on_baseline_x0 = anchor.x1 + layout.GAP

    if abs(mark_bbox.y0 - anchor.y0) <= _ALIGN_TOL:
        if abs(mark_bbox.x0 - on_baseline_x0) <= _ALIGN_TOL:
            return 0.0
        # Slot 2 follows slot 1 on the same baseline. Its exact x depends on the
        # width of text this function cannot see, so the test is "on this row's
        # line, somewhere to the right of where slot 1 starts".
        if mark_bbox.x0 > on_baseline_x0:
            return 0.5

    # Wrapped: same indent as the question, a whole number of lines down.
    if abs(mark_bbox.x0 - anchor.x0) <= _ALIGN_TOL:
        delta = anchor.y0 - mark_bbox.y0
        if delta > 0:
            k = delta / layout.LINE_STEP
            if abs(k - round(k)) <= _STEP_TOL and 1 <= round(k) <= layout.MAX_WRAPS:
                return float(round(k))
    return None


#: Ranks a reconstructed group between an exact slot-1 hit (0.0) and the
#: "somewhere right of slot 1 on this baseline" slot-2 guess (0.5). A genuine
#: single-row annotation still wins its own row outright; a genuine grouped one
#: wins against the slot-2 guess, which is the only single-row shape it can
#: collide with.
_GROUP_SCORE = 0.25
#: A grouped box is centred on its block, so its centre lands mid-block rather
#: than on any row's baseline. The slack is a full line step: the block's own
#: extent is measured from glyph metrics, and one row of leading either way is
#: the difference between "centred on these five rows" and "not this block".
_GROUP_CENTRE_TOL = layout.LINE_STEP / 2.0


def _reverse_group_score(
    mark_bbox: BBox, page_rows: list[CRFRow]
) -> Optional[tuple[float, list[str]]]:
    """The block ``layout.place_group`` would have centred this mark on, if any.

    The grouped counterpart of :func:`_reverse_layout_score`, and it matters for
    the same reason: without it, one box covering five rows comes back attached
    to whichever single row its centre happens to sit nearest, and the corpus
    learns that mapping for one row's text while forgetting it for the other
    four. That is worse than not recovering it -- it is a wrong answer with a
    plausible ``match_distance``.

    Reconstructing it is the same arithmetic run backwards. ``place_group`` puts
    the box at ``block.x1 + GAP``, vertically centred on the block, so a run of
    consecutive rows is the answer when its block's right edge lands at the
    mark's left edge and its centre lands at the mark's centre.

    **Inverting a set is under-determined, and the tie-break is deliberate.**
    Several runs can satisfy both tests: a longer run symmetric about the same
    centre, containing the same widest row, produces the same two numbers. The
    smallest matching run is taken, on the bias this codebase already applies to
    the same shape of choice in ``rows._merge_wrapped`` -- under-claiming is
    recoverable (a row keeps its own precedent entry, or gets none), while
    over-claiming attributes a mapping to rows that never carried it, and
    nothing downstream can tell that it happened.

    Returns ``(score, member_row_ids)``, or ``None``. Runs of one are never
    returned: that shape is exactly the single-row case, which
    :func:`_reverse_layout_score` already answers more precisely.

    One configuration is genuinely ambiguous and is resolved by policy rather
    than by geometry. When a block has an odd number of uniform-height rows and
    its **widest row is the middle one**, ``place_group``'s centred box lands on
    that row's own baseline to within the border inflation -- the two functions
    produce the same rect, so no tolerance can separate them. This is ranked
    behind an exact single-row hit (see ``_GROUP_SCORE``) so that case reads as
    the single-row annotation, which claims less. The shape this exists for --
    a question row followed by narrower option rows -- is not affected: the
    question row is the widest and sits at the top of the block, so no single
    row's baseline is anywhere near the box.
    """
    _, mark_cy = _center(mark_bbox)

    for length in range(2, len(page_rows) + 1):  # smallest run first
        for i in range(0, len(page_rows) - length + 1):
            block = page_rows[i : i + length]
            # Through `block_anchor` rather than re-deriving the union here, so
            # the reconstruction cannot drift from the placement it inverts --
            # which half of a row the block's right edge comes from is exactly
            # the kind of rule that would drift.
            span = layout.block_anchor(block)
            if abs(mark_bbox.x0 - (span.x1 + layout.GAP)) > _ALIGN_TOL:
                continue
            if abs(mark_cy - (span.y0 + span.y1) / 2.0) > _GROUP_CENTRE_TOL:
                continue
            return (_GROUP_SCORE, [r.row_id for r in block])
    return None


def match_marks_to_rows(
    mappings: list[RecoveredMapping],
    rows: RowSet,
    max_distance: float = DEFAULT_MAX_MATCH_DISTANCE,
) -> list[RecoveredMapping]:
    """Re-attach each variable/note mark to the row it most likely annotates.

    A domain banner covers a whole page, not one row, so it is never matched.
    Two tiers, per (mark, row) candidate pair on the same page:

    1. **Exact reverse-layout alignment** (:func:`_reverse_layout_score`) --
       reconstructs ``layout.place_row``'s own arithmetic and asks "is this mark
       exactly where that function would have put it for this row?". For this
       pipeline's own output the answer is unambiguous.
    1b. **Grouped alignment** (:func:`_reverse_group_score`) -- the same
       question asked of ``layout.place_group``: is this mark centred on a run
       of consecutive rows, just past the widest of them? A mark that matches is
       expanded into one mapping *per member row*, so every row of a repeating
       block contributes its own precedent entry.
    2. **Nearest-row fallback** (:func:`_proximity`) -- for anything with no
       exact alignment: a third-party aCRF that was never positioned by this
       codebase, or one a reviewer moved by hand in Acrobat. Ranked behind every
       tier-1 match via ``_CENTROID_OFFSET``.

    Each mark keeps only its own single lowest-rank candidate. A row is *not*
    retired once matched: one CRF row legitimately carries more than one SDTM
    variable (``AGE`` and ``AGEU``, and a single collected date populating both
    ``DM.RFICDTC`` and ``DS.DSSTDTC``), so a row can end up matched by 0, 1, or
    several marks -- which is exactly the ``anno1``/``anno2`` shape the control
    sheet already has room for.

    The tier-2 ambiguity that remains is narrower than it was. Rows are printed
    lines, so candidates on a page are separated by a full line of leading rather
    than by the few points that separated adjacent widgets in a grid; the case
    where centroid distance picks the neighbour is correspondingly rarer.
    ``match_distance`` still makes a bad tier-2 guess visible to whoever reviews
    the report.
    """
    rows_by_page: dict[int, list[CRFRow]] = {}
    for row in rows.rows:
        rows_by_page.setdefault(row.page_index, []).append(row)

    # (rank, mark_index, row_id, reported_distance, members) -- `rank` decides
    # match order (tier 1 always ahead of tier 2) and is a wrap count, a group
    # score or a clamped proximity; `reported_distance` is always plain
    # centre-to-centre, kept separately so ``match_distance`` stays a single
    # comparable number in the report whichever tier produced the match.
    # `members` is non-empty only for a grouped match.
    candidates: list[tuple[float, int, str, float, tuple[str, ...]]] = []
    for i, m in enumerate(mappings):
        if m.kind == "domain":
            continue
        page_rows = rows_by_page.get(m.page_index, [])
        for row in page_rows:
            d = _distance(m.bbox, row.anchor)
            aligned = _reverse_layout_score(m.bbox, row)
            if aligned is not None:
                candidates.append((aligned, i, row.row_id, d, ()))
            else:
                near = _proximity(m.bbox, row.anchor)
                if near <= max_distance:
                    candidates.append((near + _CENTROID_OFFSET, i, row.row_id, d, ()))

        grouped = _reverse_group_score(m.bbox, page_rows)
        if grouped is not None:
            score, members = grouped
            anchor = rows.by_id(members[0])
            assert anchor is not None  # members come from this page's own rows
            candidates.append(
                (score, i, members[0], _distance(m.bbox, anchor.anchor), tuple(members))
            )
    candidates.sort(key=lambda t: t[0])

    chosen: dict[int, tuple[str, float, tuple[str, ...]]] = {}
    for _rank, i, row_id, d, members in candidates:
        if i in chosen:
            continue
        chosen[i] = (row_id, d, members)

    out: list[RecoveredMapping] = []
    for i, m in enumerate(mappings):
        if i not in chosen:
            out.append(m)
            continue
        row_id, dist, members = chosen[i]
        # One box covering five rows becomes five records sharing one bbox. Each
        # carries its own row's label, which is what the lookup table is keyed
        # on, and `member_row_ids` so a consumer can still tell they were drawn
        # once. Collapsing them back into one box is `grouping.collapse_repeats`'
        # job on the way out, and it will, because the text is identical.
        for member_id in members or (row_id,):
            row = rows.by_id(member_id)
            assert row is not None
            out.append(
                replace(
                    m,
                    row_id=member_id,
                    member_row_ids=list(members),
                    label=row.text_1 or row.text_2,
                    context=row_context(row),
                    match_distance=dist if member_id == row_id else _distance(m.bbox, row.anchor),
                )
            )
    return out


def row_context(row: CRFRow) -> str:
    """Disambiguating context for a matched row, for the lookup table.

    Under the old design this was assembled from a section-heading search and a
    same-line text scan, because a field's own geometry said nothing about what
    it meant. A row already carries it: the form it belongs to, and its response
    column, which is what separates the ``Result`` cell of the Systolic row from
    the ``Result`` cell of the Pulse row.

    Joined with '/' and ';', never '|'. This string travels as a CSV cell and, on
    the markdown-table reply path, as a table cell, where a bare '|' silently
    shifts every column after it.
    """
    parts = []
    if row.form:
        parts.append(f"form: {row.form}")
    if row.text_2:
        parts.append(f"response: {row.text_2}")
    return "; ".join(parts)


# --------------------------------------------------------------------------
# Page-level domain summary
# --------------------------------------------------------------------------


def summarize_page_domains(
    mappings: list[RecoveredMapping], legend_by_page: dict[int, dict[str, str]] | None = None
) -> list[RecoveredMapping]:
    """Append one derived "domain present" row per (page, domain).

    Must run *last*, after :func:`attribute_domains` and
    :func:`match_marks_to_rows`, so it reflects each mark's final resolved
    domain rather than raw annotation text -- this is what makes it a
    summary *derived from* what was actually found on the page, not a
    re-parsing of the page's own legend (see :func:`split_legend_marks`).
    Field-matching success is irrelevant here: a mark still establishes "this
    domain is present on this page" whether or not a field was found for it.

    Reuses the existing ``kind="domain"`` value rather than inventing a new
    one, so a synthesized row is automatically excluded from
    :func:`to_lookup_rows` and would be excluded from ever re-entering
    ``attribute_domains``/``match_marks_to_rows`` if this output were
    accidentally re-run through them. ``legend_name`` is populated only when
    the page's own legend also named that domain code -- a cross-check for a
    reviewer, never the source of the domain itself.
    """
    legend_by_page = legend_by_page or {}
    representative: dict[tuple[int, str], RecoveredMapping] = {}
    for m in mappings:
        if m.kind != "variable" or not m.domain:
            continue
        key = (m.page_index, m.domain)
        representative.setdefault(key, m)

    summaries = [
        RecoveredMapping(
            page_index=page_index,
            bbox=rep.bbox,
            text=domain,
            kind="domain",
            domain=domain,
            synthesized=True,
            legend_name=legend_by_page.get(page_index, {}).get(domain),
        )
        for (page_index, domain), rep in sorted(representative.items())
    ]
    return mappings + summaries


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def parse_annotated_pdf(
    pdf_path: str | Path,
    blank_pdf_path: str | Path | None = None,
    max_match_distance: float = DEFAULT_MAX_MATCH_DISTANCE,
    precedent: dict[str, str] | None = None,
) -> list[RecoveredMapping]:
    """An already-annotated CRF -> best-effort recovered mappings.

    Pass ``blank_pdf_path`` (the un-annotated counterpart) when you have it --
    row extraction on a clean form cannot confuse annotation text for form text.
    Without it, rows are extracted from ``pdf_path`` itself.

    That second case is worth being precise about, because it changed with the
    row model. Annotations are FreeText *markup*, not page content, so
    ``get_text("words")`` does not see them and the extracted rows are the
    form's own text either way -- as long as nothing flattened the markup into
    the page. This pipeline never flattens (see
    ``render.save_with_annotations``); a third-party aCRF is not guaranteed to
    make the same choice, and a flattened one will produce rows that include the
    annotations, which then get matched against themselves.

    Pass ``precedent`` (a variable -> domain table, typically from
    ``pipeline.corpus_precedent.build_variable_domain_precedent``) to give
    ``attribute_domains`` a fourth, weakest fallback tier for variables a
    boxed banner and the built-in CDISC constants both leave unattributed.
    """
    marks = read_marks(pdf_path)
    rows = extract_rows(blank_pdf_path or pdf_path)
    # Built before the split, since split_legend_marks removes the very marks the
    # colour key is read from.
    legend_colors = legend_color_map(marks, rows)
    legend_by_page, marks = split_legend_marks(marks, rows)
    mappings = [_mark_to_mapping(m) for m in marks]
    mappings = attribute_domains(mappings, precedent, legend_colors)
    mappings = match_marks_to_rows(mappings, rows, max_match_distance)
    mappings = summarize_page_domains(mappings, legend_by_page)
    return mappings


# --------------------------------------------------------------------------
# Reference-table export
# --------------------------------------------------------------------------

LOOKUP_COLUMNS = [
    "page", "label", "context", "domain", "variable", "condition", "fixed_value",
    "domain_inferred", "match_distance", "text",
]


def to_lookup_rows(mappings: list[RecoveredMapping]) -> list[dict]:
    """Flatten matched variable mappings into rows for a label-keyed reference table.

    Only matched variable-kind mappings are useful as Copilot-prompt
    precedent or QC precedent -- an unmatched mark has no ``label`` to key a
    lookup on, and a domain banner has no single field of its own either,
    even though its own text is one of the more reliable reads here (no
    field match needed to get it right).
    """
    rows = []
    for m in mappings:
        if m.kind != "variable" or m.row_id is None:
            continue
        rows.append(
            {
                "page": m.page_index + 1,
                "label": m.label or "",
                "context": m.context or "",
                "domain": m.domain or "",
                "variable": m.variable or "",
                "condition": m.condition or "",
                "fixed_value": m.fixed_value or "",
                "domain_inferred": m.domain_inferred,
                "match_distance": round(m.match_distance, 1) if m.match_distance is not None else "",
                "text": m.text,
            }
        )
    return rows


def write_lookup_csv(mappings: list[RecoveredMapping], out_path: str | Path) -> Path:
    """Write the reference table CSV -- see :func:`to_lookup_rows`."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=LOOKUP_COLUMNS)
        writer.writeheader()
        writer.writerows(to_lookup_rows(mappings))
    return out_path


# --------------------------------------------------------------------------
# Full diagnostic export -- every mark, matched or not
# --------------------------------------------------------------------------

REPORT_COLUMNS = [
    "page", "kind", "text", "domain", "variable", "condition", "fixed_value",
    "domain_inferred", "domain_inference_source", "row_id", "label", "context",
    "match_distance", "synthesized", "legend_name",
]


def to_report_rows(mappings: list[RecoveredMapping]) -> list[dict]:
    """Every recovered mark, one row each, matched or not.

    Unlike :func:`to_lookup_rows` -- which keeps only what's reusable as
    Copilot-prompt precedent -- this is the full audit view: domain banners,
    not-submitted notes, and anything that found no field within
    ``max_match_distance`` all get a row, with ``row_id`` blank wherever a
    match failed. That blank is the signal to look at: a page-by-page count
    of it is the actual answer to "how much of this resolved cleanly".
    """
    rows = []
    for m in mappings:
        rows.append(
            {
                "page": m.page_index + 1,
                "kind": m.kind,
                "text": m.text,
                "domain": m.domain or "",
                "variable": m.variable or "",
                "condition": m.condition or "",
                "fixed_value": m.fixed_value or "",
                "domain_inferred": m.domain_inferred,
                "domain_inference_source": m.domain_inference_source or "",
                "row_id": m.row_id or "",
                "label": m.label or "",
                "context": m.context or "",
                "match_distance": round(m.match_distance, 1) if m.match_distance is not None else "",
                "synthesized": m.synthesized,
                "legend_name": m.legend_name or "",
            }
        )
    return rows


def write_report_csv(mappings: list[RecoveredMapping], out_path: str | Path) -> Path:
    """Write the full diagnostic CSV -- see :func:`to_report_rows`."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(to_report_rows(mappings))
    return out_path


__all__ = [
    "BUILTIN_DOMAIN_PRECEDENT",
    "DEFAULT_MAX_MATCH_DISTANCE",
    "LOOKUP_COLUMNS",
    "NOT_SUBMITTED_TEXT",
    "REPORT_COLUMNS",
    "RawMark",
    "RecoveredMapping",
    "attribute_domains",
    "classify_mark",
    "legend_color_map",
    "match_marks_to_rows",
    "parse_annotated_pdf",
    "row_context",
    "parse_mapping_text",
    "read_marks",
    "split_legend_marks",
    "summarize_page_domains",
    "to_lookup_rows",
    "to_report_rows",
    "write_lookup_csv",
    "write_report_csv",
]
