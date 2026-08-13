"""XFDF writer.

Task order step 6.

XFDF is the artifact a human touches: reviewed and possibly hand-edited in
Acrobat, or re-exported from the Dash review UI once that exists. From the
moment it is written it -- not ``proposals.json`` -- is the authoritative
record. ``xfdf_to_pdf.py`` reads it back and deliberately does not
cross-check against anything upstream.

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
from pipeline.models import AnnotationSet, ReviewStatus, SdtmAnnotation

XFDF_NS = "http://ns.adobe.com/xfdf/"
CARF_NS = "https://github.com/carf/annotations"
META_TAG = f"{{{CARF_NS}}}meta"

DEFAULT_COLOR = "#CC0D0D"
NOTE_COLOR = "#7A7A7A"
FONT_SIZE = 7.0


def _pdf_date(dt: Optional[datetime]) -> str:
    """PDF date syntax, e.g. D:20260813120000Z."""
    return dt.strftime("D:%Y%m%d%H%M%SZ") if dt else ""


def _color_for(annot: SdtmAnnotation) -> str:
    return NOTE_COLOR if not annot.label_text() else DEFAULT_COLOR


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
    r, g, b = (0.80, 0.05, 0.05) if _color_for(annot) == DEFAULT_COLOR else (0.48,) * 3
    appearance.text = f"/Helv {FONT_SIZE:g} Tf {r:g} {g:g} {b:g} rg"

    meta = ET.SubElement(el, META_TAG)
    for key, value in (
        ("field_id", annot.field_id),
        ("domain", annot.domain),
        ("variable", annot.variable),
        ("condition", annot.condition),
        ("codelist", annot.codelist),
        ("origin", annot.origin.value if annot.origin else None),
        ("confidence", f"{annot.confidence:g}" if annot.confidence is not None else None),
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
