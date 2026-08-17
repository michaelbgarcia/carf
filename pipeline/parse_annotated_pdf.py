"""Recovers structured SDTM mappings from an already-annotated CRF PDF.

Not a step in the forward pipeline (extract -> prompt -> parse_response ->
layout -> xfdf -> xfdf_to_pdf). This runs the opposite direction: given a
*finished* aCRF -- this pipeline's own output from a prior study, or a third
party's, as long as it follows the same FDA aCRF convention of small FreeText
markup positioned near the field it describes -- recover a label ->
domain/variable/condition table. See COWORK_INSTRUCTIONS.md-adjacent
discussion: this is what makes a corpus of historical aCRFs usable as
Copilot-prompt precedent or as a QC cross-check, rather than just a pile of
PDFs nobody can query.

Two things make this lossy in a way the forward pipeline is not:

* **No join key survives to the final PDF.** ``render.draw_annotation`` (via
  ``xfdf_to_pdf.render_annotations``) draws only a bbox and a text string --
  the ``field_id`` that ties an annotation to the field it describes lives in
  XFDF's ``<carf:meta>`` only (see ``xfdf.py``), and XFDF is never part of a
  finished aCRF. So a mark has to be re-matched to a field by *position* --
  the same right/below/left placement ``layout.py`` used, inferred in
  reverse from geometry alone. A wrong match is possible, not just a missing
  one; every match here is reported with the distance it was found at so a
  caller can decide how much to trust it.
* **Origin (Define-XML) never reaches the page.** ``display_text()`` renders
  only domain/variable/condition -- never origin -- so a recovered mapping
  can say what a field was called, never how it was collected. Don't invent
  an origin here; leave it to the caller (e.g. defaulting to Collected as a
  starting guess for human review, never as a fact).

Visual convention assumed below -- this pipeline's own, and the general FDA
aCRF convention it mirrors: variable annotations in plain, usually red text
near the field; domain banners boxed; not-submitted fields in a muted/gray
color. A source that draws its aCRFs a different way needs the color/border
heuristics re-tuned; the matching logic underneath does not change.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

import pymupdf

from pipeline import layout
from pipeline.extract import extract_fields
from pipeline.geometry import fitz_rect_to_bbox
from pipeline.models import BBox, CRFField, FieldSet

NOT_SUBMITTED_TEXT = "[Not Submitted]"
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
    boxed: bool  # non-zero border width -- this pipeline's domain-banner marker
    muted: bool  # grayscale text color -- this pipeline's not-submitted marker


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
    :func:`match_marks_to_fields`.
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
                out.append(
                    RawMark(
                        page_index=page_index,
                        bbox=fitz_rect_to_bbox(annot.rect, height),
                        text=text,
                        boxed=annot.border.get("width", 0.0) > 0.0,
                        muted=_is_muted(da),
                    )
                )
        return out
    finally:
        doc.close()


_LEGEND_RE = re.compile(r"^\s*(?P<code>[A-Za-z]{2,8})\s*=\s*(?P<name>[A-Za-z][A-Za-z0-9 /&\-]*)\s*$")


def split_legend_marks(
    marks: list[RawMark], fieldset: FieldSet
) -> tuple[dict[int, dict[str, str]], list[RawMark]]:
    """Pull page-level domain legends (e.g. ``DS=Disposition``) out of the marks.

    A legend sits in a page's header/margin, above every capture field on
    that page, and reads as a bare domain code -> full name -- the same
    ``CODE = phrase`` shape a fixed-value assignment mark can have (e.g.
    ``DSCAT = PROTOCOL MILESTONE``, see :func:`parse_mapping_text`). Text
    shape alone cannot tell the two apart; position can, and must: a legend
    is never handed to ``_mark_to_mapping``/``attribute_domains``/
    ``match_marks_to_fields`` -- it describes the page, not a field, and
    parsing it as one would invent a nonsense variable named after the
    domain's own full name.

    Returns ``({page_index: {code: name}}, marks_with_legends_removed)``. A
    page with no detected fields yields no legends from it -- nothing to
    compare a mark's position against, and no fields to make the legend
    useful to anyway.
    """
    field_top: dict[int, float] = {}
    for f in fieldset.fields:
        field_top[f.page_index] = max(field_top.get(f.page_index, f.bbox.y1), f.bbox.y1)

    legends: dict[int, dict[str, str]] = {}
    remaining: list[RawMark] = []
    for m in marks:
        top = field_top.get(m.page_index)
        match = _LEGEND_RE.match(m.text) if top is not None else None
        if match and m.bbox.y0 >= top:
            legends.setdefault(m.page_index, {})[match.group("code").upper()] = match.group(
                "name"
            ).strip()
            continue
        remaining.append(m)
    return legends, remaining


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
    if not text or text == NOT_SUBMITTED_TEXT:
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
    domain_inferred: bool = False  # True if `domain` came from a nearby banner, not this mark's own text
    domain_inference_source: Optional[str] = None  # "banner" | "builtin" | "precedent"
    field_id: Optional[str] = None
    label: Optional[str] = None
    context: Optional[str] = None
    match_distance: Optional[float] = None  # points, centre-to-centre; None if unmatched
    synthesized: bool = False  # True only for summarize_page_domains' derived page-domain rows
    legend_name: Optional[str] = None  # cross-check: this domain's name in the page's own legend, if any
    source_pdf: Optional[str] = None  # filename, set by pipeline.corpus_precedent when mining a corpus


def _center(bbox: BBox) -> tuple[float, float]:
    return (bbox.x0 + bbox.x1) / 2.0, (bbox.y0 + bbox.y1) / 2.0


def _distance(a: BBox, b: BBox) -> float:
    ax, ay = _center(a)
    bx, by = _center(b)
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def _mark_to_mapping(mark: RawMark) -> RecoveredMapping:
    domain, variable, condition, fixed_value = parse_mapping_text(mark.text)
    if mark.boxed:
        # A banner's text is its domain code alone -- what the regex above,
        # lacking a dot to key on, puts in `variable` for lack of anywhere
        # else to put it.
        domain, variable = (domain or variable), None
        kind = "domain"
    elif mark.muted:
        kind = "note"
    else:
        kind = "variable"
    return RecoveredMapping(
        page_index=mark.page_index,
        bbox=mark.bbox,
        text=mark.text,
        kind=kind,
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
    mappings: list[RecoveredMapping], precedent: dict[str, str] | None = None
) -> list[RecoveredMapping]:
    """Fill in `domain` on a variable mark, weakest evidence last.

    Four tiers, in order: the mark's own explicit domain (nothing to do);
    the nearest boxed banner above it on the page (mirrors ``extract.py``'s
    ``_section_heading`` -- a domain banner governs everything below it,
    proximity is vertical-only, not bounded by horizontal overlap the way
    caption lookup is, since a banner is usually left-aligned while the
    fields under it are indented); CDISC-standardized built-in constants
    (:func:`_builtin_domain`); and finally ``precedent`` -- a variable ->
    domain table mined from a historical corpus (see
    ``pipeline/corpus_precedent.py``), the weakest tier since it reflects
    what other documents did, not what this one says.

    ``domain_inference_source`` records which tier fired ("banner" |
    "builtin" | "precedent"), so a caller mining this output as further
    precedent can require the strongest evidence and avoid amplifying its
    own guesses (see ``build_variable_domain_precedent``).
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


_ALIGN_TOL = 0.05  # generous over XFDF's 3-decimal-place @rect rounding
_NUDGE_TOL = 1e-3  # tolerance on (delta / NUDGE) being an integer
_CENTROID_OFFSET = layout.MAX_NUDGES + 10.0  # keeps any centroid fallback ranked behind every exact match


def _reverse_layout_score(mark_bbox: BBox, field_bbox: BBox) -> Optional[float]:
    """Score of 0..~40 if `mark_bbox` is exactly where ``layout.place_one``
    would have put an annotation for `field_bbox`; ``None`` otherwise.

    ``layout.place_one`` tries, in order, a box to the right of the field, then
    below it, then to the left -- and if all three are blocked, walks the
    *right* candidate straight down the page in fixed ``NUDGE`` steps. All
    four shapes are cheap to reconstruct exactly: the mark's own bbox is
    already the same width/height ``annotation_size(text)`` produced at
    placement time, and ``GAP``/``NUDGE`` are fixed constants -- nothing here
    is estimated. A lower score (fewer nudge steps) is a better match, used
    only to break ties between multiple exact alignments; any exact alignment
    still outranks every nearest-centroid fallback (see ``_CENTROID_OFFSET``).
    """
    w, h = mark_bbox.width, mark_bbox.height
    right_x0, right_y0 = field_bbox.x1 + layout.GAP, field_bbox.y0
    below_x0, below_y1 = field_bbox.x0, field_bbox.y0 - layout.GAP
    left_x1, left_y0 = field_bbox.x0 - layout.GAP, field_bbox.y0

    if abs(mark_bbox.x0 - right_x0) <= _ALIGN_TOL and abs(mark_bbox.y0 - right_y0) <= _ALIGN_TOL:
        return 0.0
    if abs(mark_bbox.x0 - below_x0) <= _ALIGN_TOL and abs(mark_bbox.y1 - below_y1) <= _ALIGN_TOL:
        return 0.0
    if abs(mark_bbox.x1 - left_x1) <= _ALIGN_TOL and abs(mark_bbox.y0 - left_y0) <= _ALIGN_TOL:
        return 0.0

    # Not a clean candidate -- check the nudged-right fallback: same x0 as
    # the right candidate, y0 stepped down by a whole number of NUDGE units.
    if abs(mark_bbox.x0 - right_x0) <= _ALIGN_TOL:
        delta = right_y0 - mark_bbox.y0
        if delta > 0:
            k = delta / layout.NUDGE
            if abs(k - round(k)) <= _NUDGE_TOL and 1 <= round(k) <= layout.MAX_NUDGES:
                return float(round(k))
    return None


def _group_candidate_fields(fieldset: FieldSet) -> list[CRFField]:
    """One synthetic field per ``group_id``, spanning its members' union bbox.

    A mark describing a whole checkbox question (e.g. a Yes/No/Not Applicable
    row) is drawn once beside the row, not beside any one option -- so it
    naturally sits closer to the group's union bbox than to any single
    option's small bbox off to one side. Adding these into the same
    candidate pool :func:`match_marks_to_fields` scores against needs no
    change to the matching algorithm itself; the union bbox just competes on
    ordinary centroid distance like any other field. ``field_id`` carries a
    ``grp_`` prefix -- the only signal a recovered mapping matched a group
    rather than an individual option. Fields with no ``group_id`` (any
    ``FieldSet`` predating this, or a page with no checkbox groups) produce
    no candidates here, so this is a no-op unless grouping was detected.
    """
    members: dict[tuple[int, str], list[CRFField]] = {}
    for f in fieldset.fields:
        if f.group_id:
            members.setdefault((f.page_index, f.group_id), []).append(f)

    groups: list[CRFField] = []
    for (page_index, group_id), fields in members.items():
        groups.append(
            CRFField(
                field_id=f"grp_{group_id}",
                page_index=page_index,
                bbox=BBox(
                    x0=min(f.bbox.x0 for f in fields),
                    y0=min(f.bbox.y0 for f in fields),
                    x1=max(f.bbox.x1 for f in fields),
                    y1=max(f.bbox.y1 for f in fields),
                ),
                label=" / ".join(dict.fromkeys(f.label for f in fields if f.label)),
                source=fields[0].source,
                context=fields[0].context,
                group_id=group_id,
            )
        )
    return groups


def match_marks_to_fields(
    mappings: list[RecoveredMapping],
    fieldset: FieldSet,
    max_distance: float = DEFAULT_MAX_MATCH_DISTANCE,
) -> list[RecoveredMapping]:
    """Re-attach each variable/note mark to the field(s) it most likely annotates.

    A domain banner covers a whole section, not one field, so it is never
    matched. Candidate fields are every ``CRFField`` on the page plus one
    synthetic candidate per checkbox group (:func:`_group_candidate_fields`).
    Two tiers, per (mark, field) candidate pair on the same page:

    1. **Exact reverse-layout alignment** (:func:`_reverse_layout_score`) --
       reconstructs ``layout.place_one``'s own placement math and asks "is
       this mark exactly where that function would have put it for this
       field?". For this pipeline's own output the answer is unambiguous and
       essentially never coincidental, which is what makes a dense grid (many
       same-size fields a few points apart, where centroid distance alone
       regularly picks the neighbouring row) still resolve correctly.
    2. **Nearest-centroid fallback** -- for anything with no exact alignment:
       a third-party aCRF that was never positioned by this codebase's
       ``layout.py``, or a pipeline-generated one a reviewer moved by hand in
       Acrobat. Ranked behind every tier-1 match via ``_CENTROID_OFFSET`` so
       an exact reconstruction is always preferred when one exists.

    Each mark keeps only its own single lowest-rank candidate. A field is
    *not* retired once matched: one CRF field legitimately maps to more than
    one SDTM variable (e.g. a single collected date populating both
    ``DM.RFICDTC`` and ``DS.DSSTDTC``), so a field can end up matched by 0, 1,
    or several marks. This is safe for tier-1 alignment, which is collision-
    safe by construction -- two distinct fields cannot both satisfy
    ``_reverse_layout_score`` against the same mark bbox, since that would
    require identical field geometry. It is *not* defended for a tier-2-only
    collision between two distinct, nearby fields with no exact alignment
    evidence for either mark (a third-party aCRF never positioned by
    ``layout.py``) -- resolving that would require comparing mark *text*,
    which is not this function's job. ``match_distance`` makes a bad tier-2
    guess visible to whoever reviews the report.
    """
    all_fields = list(fieldset.fields) + _group_candidate_fields(fieldset)
    fields_by_page: dict[int, list] = {}
    for f in all_fields:
        fields_by_page.setdefault(f.page_index, []).append(f)

    # (rank, mark_index, field_id, reported_distance) -- `rank` decides match
    # order (tier 1 always ahead of tier 2), `reported_distance` is the plain
    # centroid distance in points, kept separately so an exact tier-1 match
    # still reports a meaningful ``match_distance`` rather than a nudge count.
    candidates: list[tuple[float, int, str, float]] = []
    for i, m in enumerate(mappings):
        if m.kind == "domain":
            continue
        for f in fields_by_page.get(m.page_index, []):
            d = _distance(m.bbox, f.bbox)
            aligned = _reverse_layout_score(m.bbox, f.bbox)
            if aligned is not None:
                candidates.append((aligned, i, f.field_id, d))
            elif d <= max_distance:
                candidates.append((d + _CENTROID_OFFSET, i, f.field_id, d))
    candidates.sort(key=lambda t: t[0])

    chosen: dict[int, tuple[str, float]] = {}
    for _rank, i, field_id, d in candidates:
        if i in chosen:
            continue
        chosen[i] = (field_id, d)

    fields_by_id = {f.field_id: f for f in all_fields}
    out: list[RecoveredMapping] = []
    for i, m in enumerate(mappings):
        if i not in chosen:
            out.append(m)
            continue
        field_id, dist = chosen[i]
        field = fields_by_id[field_id]
        out.append(
            replace(
                m,
                field_id=field_id,
                label=field.label,
                context=field.context,
                match_distance=dist,
            )
        )
    return out


# --------------------------------------------------------------------------
# Page-level domain summary
# --------------------------------------------------------------------------


def summarize_page_domains(
    mappings: list[RecoveredMapping], legend_by_page: dict[int, dict[str, str]] | None = None
) -> list[RecoveredMapping]:
    """Append one derived "domain present" row per (page, domain).

    Must run *last*, after :func:`attribute_domains` and
    :func:`match_marks_to_fields`, so it reflects each mark's final resolved
    domain rather than raw annotation text -- this is what makes it a
    summary *derived from* what was actually found on the page, not a
    re-parsing of the page's own legend (see :func:`split_legend_marks`).
    Field-matching success is irrelevant here: a mark still establishes "this
    domain is present on this page" whether or not a field was found for it.

    Reuses the existing ``kind="domain"`` value rather than inventing a new
    one, so a synthesized row is automatically excluded from
    :func:`to_lookup_rows` and would be excluded from ever re-entering
    ``attribute_domains``/``match_marks_to_fields`` if this output were
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
    field detection on a clean form is more reliable than detecting through
    markup. Without it, fields are detected on ``pdf_path`` itself, which
    works as long as the annotations were never flattened into page content
    (this pipeline never flattens; see ``render.save_with_annotations`` --
    but a third-party aCRF is not guaranteed to make the same choice).

    Pass ``precedent`` (a variable -> domain table, typically from
    ``pipeline.corpus_precedent.build_variable_domain_precedent``) to give
    ``attribute_domains`` a fourth, weakest fallback tier for variables a
    boxed banner and the built-in CDISC constants both leave unattributed.
    """
    marks = read_marks(pdf_path)
    fieldset = extract_fields(blank_pdf_path or pdf_path)
    legend_by_page, marks = split_legend_marks(marks, fieldset)
    mappings = [_mark_to_mapping(m) for m in marks]
    mappings = attribute_domains(mappings, precedent)
    mappings = match_marks_to_fields(mappings, fieldset, max_match_distance)
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
        if m.kind != "variable" or m.field_id is None:
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
    "domain_inferred", "domain_inference_source", "field_id", "label", "context",
    "match_distance", "synthesized", "legend_name",
]


def to_report_rows(mappings: list[RecoveredMapping]) -> list[dict]:
    """Every recovered mark, one row each, matched or not.

    Unlike :func:`to_lookup_rows` -- which keeps only what's reusable as
    Copilot-prompt precedent -- this is the full audit view: domain banners,
    not-submitted notes, and anything that found no field within
    ``max_match_distance`` all get a row, with ``field_id`` blank wherever a
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
                "field_id": m.field_id or "",
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
    "match_marks_to_fields",
    "parse_annotated_pdf",
    "parse_mapping_text",
    "read_marks",
    "split_legend_marks",
    "summarize_page_domains",
    "to_lookup_rows",
    "to_report_rows",
    "write_lookup_csv",
    "write_report_csv",
]
