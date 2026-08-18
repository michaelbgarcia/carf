"""XFDF writer -- an export format, no longer the review surface.

**This is off the critical path.** The artifact a human touches is now the XLSX
control sheet (``pipeline/control_sheet.py``), which is where ``review_status``
moves off ``proposed`` and where the Part 11 argument about a human being in the
loop attaches. ``scripts/annotate.py`` renders the submission PDF from that
sheet directly.

XFDF stays for two reasons that are worth the module: it is the interchange
format for opening this pipeline's annotations *in Acrobat* alongside the PDF,
which is how a reviewer who prefers Acrobat to Excel can still comment; and it
round-trips through ``xfdf_to_pdf.py``, so annotations edited in Acrobat can be
rendered back out. When an XFDF *has* been hand-edited it is authoritative over
upstream records for the same reason the sheet is -- a human touched it -- and
``xfdf_to_pdf.py`` deliberately does not cross-check it against anything.

Two things to get right:

* ``@rect`` is PDF user space (bottom-left origin). A ``BBox`` already is, so
  this module does no coordinate arithmetic at all -- it calls
  ``geometry.format_xfdf_rect``. The y-flip lives in ``pipeline.geometry``
  and is applied at extraction and at render, not here.
* ``@page`` is **0-based**, which matches ``page_index`` directly. Never write
  ``display_page`` into XFDF.

Rejected annotations are excluded: a rejection means the annotation does not
exist as far as the submission is concerned.

Provenance survives the trip in a ``<carf:meta>`` child element in our own
namespace. Acrobat preserves unknown elements in most workflows but is not
obliged to, so nothing downstream may *depend* on it -- ``contents`` and
``rect`` alone are enough to render. The metadata is a convenience for the
review UI, not part of the rendering contract.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional

from pipeline.geometry import format_xfdf_rect
from pipeline.models import AnnotationKind, AnnotationSet, ReviewStatus, SdtmAnnotation
from pipeline.msg import TEXT_COLOR, to_pdf_color

XFDF_NS = "http://ns.adobe.com/xfdf/"
CARF_NS = "https://github.com/carf/annotations"
META_TAG = f"{{{CARF_NS}}}meta"

DEFAULT_COLOR = "#CC0D0D"
NOTE_COLOR = "#7A7A7A"
FONT_SIZE = 7.0


def _pdf_date(dt: Optional[datetime]) -> str:
    """PDF date syntax, e.g. D:20260813120000Z."""
    return dt.strftime("D:%Y%m%d%H%M%SZ") if dt else ""


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _color_for(annot: SdtmAnnotation) -> str:
    """XFDF ``@color`` -- the annotation's background, matching MSG.

    ``@color`` maps to the PDF ``/C`` entry, which on a FreeText annot is the
    background fill (see ``render.py``). So an annotation carrying an MSG colour
    exports with it, and Acrobat shows the same coloured box the rendered PDF
    has. Annotations with no domain colour keep the older red/grey text
    convention, since an uncoloured box would be invisible.
    """
    if annot.color is not None:
        return _hex(annot.color)
    return NOTE_COLOR if annot.kind is AnnotationKind.NOTE or not annot.label_text() else DEFAULT_COLOR


def _appearance(annot: SdtmAnnotation) -> str:
    """The ``<defaultappearance>`` string -- font plus *text* colour.

    Black on an MSG fill, per the guidelines; otherwise the red/grey text
    convention, which is the only thing carrying the distinction when there is no
    fill behind it.
    """
    if annot.color is not None:
        r, g, b = to_pdf_color(TEXT_COLOR)
    elif annot.kind is AnnotationKind.NOTE or not annot.label_text():
        r = g = b = 0.48
    else:
        r, g, b = (0.80, 0.05, 0.05)
    return f"/Helv {FONT_SIZE:g} Tf {r:g} {g:g} {b:g} rg"


def annotation_to_element(annot: SdtmAnnotation) -> ET.Element:
    """One annotation -> one ``<freetext>`` element.

    Structure is deliberately flat -- a single freetext per annotation, no
    separate ``<square>`` for boxed markers -- so ``xfdf_to_pdf.py`` has one
    element type to walk. Domain markers get a border via the appearance
    string rather than a second element.
    """
    # Elements carry the XFDF namespace explicitly. Creating them bare would
    # still serialise correctly (the default xmlns on the root covers them),
    # but the in-memory tree and a re-parsed one would then disagree about
    # every tag, which is a trap for anything that walks the tree.
    el = ET.Element(f"{{{XFDF_NS}}}freetext")
    el.set("page", str(annot.page_index))  # 0-based, matches page_index
    el.set("rect", format_xfdf_rect(annot.bbox))
    el.set("color", _color_for(annot))
    el.set("flags", "print")
    el.set("name", annot.annot_id)
    el.set("title", annot.source_model)
    el.set("subject", annot.kind.value)
    if annot.created_at:
        el.set("date", _pdf_date(annot.created_at))

    contents = ET.SubElement(el, f"{{{XFDF_NS}}}contents")
    contents.text = annot.display_text()

    appearance = ET.SubElement(el, f"{{{XFDF_NS}}}defaultappearance")
    appearance.text = _appearance(annot)

    meta = ET.SubElement(el, META_TAG)
    for key, value in (
        ("row_id", annot.row_id),
        ("slot", str(annot.slot)),
        # A grouped annotation is one box asserting a mapping for several rows.
        # The rect alone cannot say which rows those are, and Acrobat has no
        # concept to map it onto, so the membership rides in our own namespace
        # -- the one place a round trip can recover it from.
        ("group_id", annot.group_id),
        ("member_row_ids", " ".join(annot.member_row_ids)),
        ("domain", annot.domain),
        ("variable", annot.variable),
        ("condition", annot.condition),
        ("fixed_value", annot.fixed_value),
        ("codelist", annot.codelist),
        ("origin", annot.origin.value if annot.origin else None),
        ("confidence", f"{annot.confidence:g}" if annot.confidence is not None else None),
        ("suggested", "true" if annot.suggested else None),
        ("review_status", annot.review_status.value),
        ("reviewed_by", annot.reviewed_by),
        ("rationale", annot.rationale),
    ):
        if value:
            meta.set(key, str(value))
    return el


def build_xfdf(annotations: AnnotationSet, source_pdf: str) -> str:
    """Serialise an annotation set to XFDF, skipping rejected annotations."""
    ET.register_namespace("", XFDF_NS)
    ET.register_namespace("carf", CARF_NS)

    root = ET.Element(f"{{{XFDF_NS}}}xfdf")
    root.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")

    f = ET.SubElement(root, f"{{{XFDF_NS}}}f")
    f.set("href", Path(source_pdf).name)

    annots = ET.SubElement(root, f"{{{XFDF_NS}}}annots")
    for annot in sorted(
        annotations.annotations, key=lambda a: (a.page_index, -a.bbox.y1, a.bbox.x0)
    ):
        if annot.review_status is ReviewStatus.REJECTED:
            continue
        annots.append(annotation_to_element(annot))

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode", xml_declaration=False)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}\n'


def write_xfdf(
    annotations: AnnotationSet, out_path: str | Path, source_pdf: Optional[str] = None
) -> Path:
    """Write ``build/blankcrf.xfdf``."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        build_xfdf(annotations, source_pdf or annotations.source_pdf), encoding="utf-8"
    )
    return out_path
