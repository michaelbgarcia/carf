"""XFDF + blank CRF -> submission-ready annotated PDF.

Task order step 9. The last step in the pipeline.

**PyMuPDF has no XFDF importer.** There is no ``pymupdf.import_xfdf()``; XFDF
import/export is an Acrobat / pdf-lib ecosystem feature, not something the
PyMuPDF C core implements. This is a hand-written ``ElementTree`` walk feeding
PyMuPDF's native annotation API -- the intended design, not a workaround.

The XFDF is authoritative
-------------------------
By this point the file has been through human review and may have been edited
in Acrobat. Everything rendered comes from the XFDF: ``@rect`` for position,
``<contents>`` for text, verbatim. This module deliberately does **not**
consult ``fields.json`` or ``proposals.json`` -- reintroducing pre-review data
as a filter or as a source of text would quietly undo the review. If a human
retyped an annotation, that retyped text is what gets rendered.

Coordinates
-----------
``@rect`` is PDF user space; PyMuPDF wants fitz coordinates. The flip back is
``pipeline.geometry.bbox_to_fitz_rect`` -- the same implementation used on the
way in, called in the other direction, never transcribed a second time. A slip
here is invisible until someone opens the final PDF and finds everything
mirrored, which is what the round-trip test in ``tests/test_roundtrip.py``
exists to catch.

Annotations are never flattened -- see ``pipeline.render.save_with_annotations``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field as dc_field
from pathlib import Path

import pymupdf

from pipeline.geometry import parse_xfdf_rect
from pipeline.models import BBox
from pipeline.render import FONT, FONT_SIZE, draw_annotation, save_with_annotations
from pipeline.xfdf import CARF_NS, META_TAG, XFDF_NS

_NOTE_COLORS = {"#7a7a7a", "#7A7A7A"}
TEXT_PADDING = 2.0
# `format_xfdf_rect` rounds to 3 decimals, so a rect that layout.py sized to
# fit exactly comes back a thousandth of a point narrow. Without a tolerance
# every pipeline-generated annotation reports a 0pt overflow and the real ones
# are lost in the noise.
OVERFLOW_TOLERANCE = 0.5


@dataclass
class XfdfAnnotation:
    """One annotation as it appears in the XFDF file.

    Deliberately not an ``SdtmAnnotation``: that model derives its display
    text from ``domain``/``variable``, which would discard a hand-edited
    ``<contents>``. This record keeps the file's own text as the truth and
    carries the structured metadata alongside, when it survived the round
    trip through Acrobat.
    """

    annot_id: str
    page_index: int  # 0-based, matches @page directly
    bbox: BBox  # PDF user space
    text: str
    kind: str = "variable"
    muted: bool = False
    meta: dict[str, str] = dc_field(default_factory=dict)

    @property
    def review_status(self) -> str:
        return self.meta.get("review_status", "")


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text_of(el: ET.Element, name: str) -> str:
    for child in el:
        if _localname(child.tag) == name:
            return (child.text or "").strip()
    return ""


def parse_xfdf(xfdf_path: str | Path) -> list[XfdfAnnotation]:
    """Read an XFDF file into renderable records.

    Tolerates both namespaced and bare XFDF, since a file that has been
    through Acrobat may come back either way.
    """
    root = ET.parse(Path(xfdf_path)).getroot()
    if _localname(root.tag) != "xfdf":
        raise ValueError(f"{xfdf_path}: root element is <{root.tag}>, expected <xfdf>")

    out: list[XfdfAnnotation] = []
    for annots in root:
        if _localname(annots.tag) != "annots":
            continue
        for el in annots:
            tag = _localname(el.tag)
            if tag not in ("freetext", "square"):
                continue
            rect_attr = el.get("rect")
            if not rect_attr:
                raise ValueError(
                    f"{xfdf_path}: <{tag}> without a @rect "
                    f"(name={el.get('name', '?')})"
                )
            meta_el = el.find(META_TAG) if CARF_NS else None
            meta = dict(meta_el.attrib) if meta_el is not None else {}
            out.append(
                XfdfAnnotation(
                    annot_id=el.get("name", f"anon-{len(out) + 1}"),
                    page_index=int(el.get("page", 0)),
                    bbox=parse_xfdf_rect(rect_attr),
                    text=_text_of(el, "contents"),
                    kind=el.get("subject", "variable"),
                    muted=(el.get("color", "") in _NOTE_COLORS),
                    meta=meta,
                )
            )
    return out


def overflowing_annotations(
    annotations: list[XfdfAnnotation], fontsize: float = FONT_SIZE
) -> list[tuple[str, str, float]]:
    """Annotations whose text is wider than the rect it has to render in.

    PyMuPDF clips FreeText to its rect, so an oversized string is silently
    truncated in the finished PDF. The pipeline's own annotations always fit,
    because ``layout.py`` sizes each rect to its text -- but a human who
    retypes an annotation in Acrobat has no such guarantee, and a truncated
    annotation in a submission artifact is a data-integrity problem.

    This reports rather than corrects. Widening the rect would override a
    positioning decision a reviewer made deliberately, and the XFDF is
    authoritative by this point. Returns (annot_id, text, overflow_in_points).
    """
    out = []
    for a in annotations:
        if not a.text:
            continue
        needed = pymupdf.get_text_length(a.text, fontname=FONT, fontsize=fontsize)
        available = a.bbox.width - 2 * TEXT_PADDING
        if needed - available > OVERFLOW_TOLERANCE:
            out.append((a.annot_id, a.text, needed - available))
    return out


def render_annotations(
    pdf_path: str | Path,
    annotations: list[XfdfAnnotation],
    out_path: str | Path,
) -> Path:
    """Draw annotations onto a copy of the blank CRF and save it unflattened."""
    doc = pymupdf.open(pdf_path)
    try:
        for a in annotations:
            if a.page_index >= doc.page_count:
                raise ValueError(
                    f"annotation {a.annot_id} is on page {a.page_index + 1} but "
                    f"{Path(pdf_path).name} has {doc.page_count} pages"
                )
            if not a.text:
                continue
            draw_annotation(
                doc[a.page_index],
                a.bbox,
                a.text,
                boxed=(a.kind == "domain"),
                muted=a.muted,
            )
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        save_with_annotations(doc, out_path)
        return out_path
    finally:
        doc.close()


def xfdf_to_pdf(
    pdf_path: str | Path,
    xfdf_path: str | Path,
    out_path: str | Path,
) -> Path:
    """Blank CRF + reviewed XFDF -> annotated submission PDF."""
    annotations = parse_xfdf(xfdf_path)
    if not annotations:
        raise ValueError(f"{xfdf_path} contains no annotations")
    # Rejected annotations are already absent -- xfdf.py excludes them when
    # writing. Anything still present in a hand-edited file is there because a
    # human left it there, so it renders.
    return render_annotations(pdf_path, annotations, out_path)
