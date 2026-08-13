"""Shared drawing helper for the two renderers.

``stamp.py`` and ``xfdf_to_pdf.py`` both put annotation text on a page with
PyMuPDF, so the drawing itself lives here once. They stay separate entry
points on purpose -- one is a pre-review preview, the other is the submission
artifact, and conflating them is how a stale preview gets mistaken for the
deliverable.

Note the signature: this takes **text**, not an ``SdtmAnnotation``. That is
deliberate. ``stamp.py`` derives the text from in-memory records, while
``xfdf_to_pdf.py`` must render the ``<contents>`` of the XFDF verbatim,
because a human may have edited it and the file is authoritative by then.
Taking a record here would invite re-deriving the text and silently
discarding that edit.

Borders on freetext annotations
-------------------------------
The build instructions flag that pypdf silently dropped freetext text when a
border was set, and ask this be confirmed before relying on it. Tested against
PyMuPDF 1.28.2:

* ``border_width`` alone -- text renders, the border takes the text colour,
  and ``contents`` stays populated. **This is what we use.**
* ``border_color`` -- raises ``ValueError: cannot set border_color if
  rich_text is False``. A hard failure, not a silent one.
* ``richtext=True`` with ``border_color`` -- renders, but leaves ``contents``
  *empty*, since the text lives in the rich-text stream instead. That breaks
  the searchability FDA review tools expect, so it is not used.
"""

from __future__ import annotations

import pymupdf

from pipeline.geometry import bbox_to_fitz_rect
from pipeline.models import BBox

FONT = "helv"
FONT_SIZE = 7.0
ANNOT_COLOR = (0.80, 0.05, 0.05)
NOTE_COLOR = (0.48, 0.48, 0.48)
DOMAIN_BORDER = 1.1


def draw_annotation(
    page: pymupdf.Page,
    bbox: BBox,
    text: str,
    *,
    boxed: bool = False,
    muted: bool = False,
    fontsize: float = FONT_SIZE,
) -> pymupdf.Annot:
    """Draw one annotation as a real PDF FreeText annot.

    ``bbox`` is PDF user space; the flip back to fitz coordinates happens here
    via the one shared implementation in ``pipeline.geometry``.
    """
    rect = pymupdf.Rect(*bbox_to_fitz_rect(bbox, page.rect.height))
    annot = page.add_freetext_annot(
        rect,
        text,
        fontsize=fontsize,
        fontname=FONT,
        text_color=NOTE_COLOR if muted else ANNOT_COLOR,
        border_width=DOMAIN_BORDER if boxed else 0,
    )
    annot.update()
    return annot


def save_with_annotations(doc: pymupdf.Document, out_path) -> None:
    """Save without flattening.

    FDA submission review tools expect annotations to remain as PDF markup --
    searchable and separable from the base form. Baking them into page content
    would look identical on screen and be a regression. Never call
    ``doc.bake()`` on a submission artifact.
    """
    doc.save(out_path, garbage=4, deflate=True)
