"""Metadata Submission Guidelines v2.0 presentation rules.

MSG v2.0 constrains how an annotated CRF *looks*: annotation text sits in a
coloured box, one colour per SDTM domain, with a legend at the top of each page
naming the domains that appear on it. Getting this wrong is a compliance
finding, not a cosmetic one, which is why it lives in its own module with the
colour arithmetic in one place.

Colour is assigned by encounter order, not by domain
----------------------------------------------------
The rule that surprises people: **there is no fixed domain-to-colour mapping.**
Colours come off an ordered palette in the order domains are first encountered
within a *form*. So DM is not "the blue one" -- DM is whichever palette entry
comes up first on the form where it appears first. Two studies, or two forms in
one study, can legitimately colour the same domain differently.

That is why this module builds ``form -> [domain, ...]`` first and only then
distributes colours down to pages. Hardcoding ``{"DM": blue, "VS": orange}``
would look right on the one example everybody has seen and be wrong in general.

The palette is incomplete, deliberately
---------------------------------------
``MSG_PALETTE`` currently holds the four entries that can be read off the PHUSE
DH12 presentation. Those four are *that deck's* encounter-order assignment for
its own example forms, which is exactly why they cannot be treated as a
domain-to-colour table -- and why the remaining entries have to be transcribed
from the guidelines document rather than guessed. Cycling past the end of the
palette is handled (see :func:`palette_color`) so an incomplete palette
degrades into colour reuse rather than an exception, but a study with more than
four domains on one form will reuse colours until the constant is completed.
"""

from __future__ import annotations

from typing import Iterable, Optional

from pipeline.models import (
    NOT_SUBMITTED_TEXT,
    AnnotationKind,
    AnnotationSet,
    Origin,
    RowSet,
    SdtmAnnotation,
)

#: Ordered MSG v2.0 background fills, 0-255 per channel.
#:
#: TODO(incomplete): only the four entries visible in the PHUSE DH12 deck are
#: recorded. The rest must be transcribed from Metadata Submission Guidelines
#: v2.0 itself -- inventing plausible pastels would produce output that looks
#: compliant and is not, which is worse than output that visibly reuses a
#: colour. Extend this list in guideline order; nothing else needs to change.
MSG_PALETTE: list[tuple[int, int, int]] = [
    (191, 255, 255),  # pale cyan
    (255, 255, 150),  # pale yellow
    (150, 255, 150),  # pale green
    (255, 190, 155),  # pale orange
]

#: Rows deliberately excluded from SDTM. Grey rather than a palette entry: not
#: being submitted is not a domain, and giving it one would put it in the legend.
NOT_SUBMITTED_COLOR = (204, 204, 204)

#: MSG puts annotation text in black on the coloured fill -- not coloured text.
TEXT_COLOR = (0, 0, 0)

#: The pseudo-domain the guidelines use for a not-submitted mark.
NOT_SUBMITTED_DOMAIN = "XX"


def to_pdf_color(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """0-255 per channel -> PyMuPDF's 0.0-1.0 floats."""
    return tuple(c / 255.0 for c in rgb)  # type: ignore[return-value]


def palette_color(index: int) -> tuple[int, int, int]:
    """The ``index``-th palette entry, cycling when the palette runs out.

    Cycling rather than raising, because the palette is knowingly incomplete
    (see the module docstring) and refusing to render a fifth domain would be a
    worse failure than drawing it in a reused colour a reviewer can see.
    """
    return MSG_PALETTE[index % len(MSG_PALETTE)]


def _document_order(annotations: Iterable[SdtmAnnotation]) -> list[SdtmAnnotation]:
    """Reading order: page, then down the page, then across."""
    return sorted(annotations, key=lambda a: (a.page_index, -a.bbox.y1, a.bbox.x0))


def form_by_page(rows: RowSet) -> dict[int, str]:
    """Which form each page belongs to.

    Needed because a legend banner has no ``row_id`` -- it belongs to the page,
    not to any row -- so its form has to come from the page's rows.
    """
    out: dict[int, str] = {}
    for row in rows.rows:
        if row.form and row.page_index not in out:
            out[row.page_index] = row.form
    return out


def _form_of(annotation: SdtmAnnotation, rows: RowSet, by_page: dict[int, str]) -> str:
    if annotation.row_id:
        row = rows.by_id(annotation.row_id)
        if row is not None and row.form:
            return row.form
    return by_page.get(annotation.page_index, "")


def is_not_submitted(annotation: SdtmAnnotation) -> bool:
    """Whether this annotation marks a row as excluded from SDTM.

    Matched on the origin *or* the drawn text, because both entry points are
    real: the control sheet has an ``origin`` column, and a hand-authored
    ``anno1`` cell reading ``[NOT SUBMITTED]`` says the same thing without
    touching it.
    """
    if annotation.origin is Origin.NOT_SUBMITTED:
        return True
    text = annotation.display_text().strip().upper()
    return text == NOT_SUBMITTED_TEXT.upper() or annotation.domain == NOT_SUBMITTED_DOMAIN


def domain_order(annotations: AnnotationSet, rows: RowSet) -> dict[str, list[str]]:
    """``form -> domains in first-encountered order``.

    Encounter order is document reading order within the form. Legend banners
    are skipped: a banner exists *because* a domain was encountered, so counting
    it would let the legend decide its own colour ordering.
    """
    out: dict[str, list[str]] = {}
    by_page = form_by_page(rows)
    for a in _document_order(annotations.annotations):
        if a.kind is AnnotationKind.DOMAIN or not a.domain or is_not_submitted(a):
            continue
        seen = out.setdefault(_form_of(a, rows, by_page), [])
        if a.domain not in seen:
            seen.append(a.domain)
    return out


def color_map(annotations: AnnotationSet, rows: RowSet) -> dict[str, dict[str, tuple[int, int, int]]]:
    """``form -> {domain: rgb}``, from :func:`domain_order`."""
    return {
        form: {domain: palette_color(i) for i, domain in enumerate(domains)}
        for form, domains in domain_order(annotations, rows).items()
    }


def page_color_map(
    annotations: AnnotationSet, rows: RowSet
) -> dict[int, dict[str, tuple[int, int, int]]]:
    """``page_index -> {domain: rgb}`` -- what a page's legend needs to draw itself.

    Distributed down from the per-form map rather than computed per page, so a
    domain keeps one colour across every page of its form. Computing it per page
    would recolour a domain halfway through a form, which is exactly the
    inconsistency the guidelines exist to prevent.
    """
    by_form = color_map(annotations, rows)
    by_page: dict[int, dict[str, tuple[int, int, int]]] = {}
    form_of_page = form_by_page(rows)
    for a in _document_order(annotations.annotations):
        if a.kind is AnnotationKind.DOMAIN or not a.domain or is_not_submitted(a):
            continue
        form = _form_of(a, rows, form_of_page)
        color = by_form.get(form, {}).get(a.domain)
        if color is not None:
            by_page.setdefault(a.page_index, {})[a.domain] = color
    return by_page


def color_for(
    annotation: SdtmAnnotation, rows: RowSet, by_form: dict[str, dict[str, tuple[int, int, int]]]
) -> Optional[tuple[int, int, int]]:
    """The fill for one annotation, or ``None`` if it should have no fill.

    A note carries no domain and gets no fill -- MSG distinguishes it by a
    dashed border instead, which is ``render.py``'s job.
    """
    if is_not_submitted(annotation):
        return NOT_SUBMITTED_COLOR
    if annotation.kind is AnnotationKind.NOTE or not annotation.domain:
        return None
    form = _form_of(annotation, rows, form_by_page(rows))
    return by_form.get(form, {}).get(annotation.domain)


def apply_colors(annotations: AnnotationSet, rows: RowSet) -> AnnotationSet:
    """Return a copy with every annotation's ``color`` filled in.

    Domain legend banners take the colour of the domain they name, so the legend
    reads as a key to the boxes below it.
    """
    by_form = color_map(annotations, rows)
    form_of_page = form_by_page(rows)

    out: list[SdtmAnnotation] = []
    for a in annotations.annotations:
        if a.kind is AnnotationKind.DOMAIN and a.domain:
            form = _form_of(a, rows, form_of_page)
            color = by_form.get(form, {}).get(a.domain)
        else:
            color = color_for(a, rows, by_form)
        out.append(a.model_copy(update={"color": color}))
    return annotations.model_copy(update={"annotations": out})


__all__ = [
    "MSG_PALETTE",
    "NOT_SUBMITTED_COLOR",
    "NOT_SUBMITTED_DOMAIN",
    "TEXT_COLOR",
    "apply_colors",
    "color_for",
    "color_map",
    "domain_order",
    "form_by_page",
    "is_not_submitted",
    "page_color_map",
    "palette_color",
    "to_pdf_color",
]
