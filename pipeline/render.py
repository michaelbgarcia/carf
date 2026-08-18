"""Shared drawing helper for the two renderers.

``stamp.py`` and ``xfdf_to_pdf.py`` both put annotation text on a page with
PyMuPDF, so the drawing itself lives here once. They stay separate entry points
on purpose -- one is a pre-review preview, the other is the submission
artifact, and conflating them is how a stale preview gets mistaken for the
deliverable.

Note the signature: this takes **text**, not an ``SdtmAnnotation``. That is
deliberate. ``stamp.py`` derives the text from in-memory records, while
``xfdf_to_pdf.py`` must render the ``<contents>`` of the XFDF verbatim, because
a human may have edited it and the file is authoritative by then. Taking a
record here would invite re-deriving the text and silently discarding that
edit -- the same rule ``SdtmAnnotation.text`` encodes at the model level.

Metadata Submission Guidelines v2.0 styling
-------------------------------------------
Three visual distinctions, all of them load-bearing for a reviewer:

* a **variable** annotation is black text on a domain-coloured fill with a thin
  solid border;
* a **note** has no fill and a **dashed** border, which is how MSG marks
  commentary rather than a mapping;
* a **domain legend banner** is the same coloured fill as the variables it
  keys, sitting at the top of the page.

Colours arrive as 0-255 triples and convert through ``pipeline.msg.to_pdf_color``,
so the palette is stated once, in guideline units, in one module.

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

Re-checked against the same version now that fills and dashes are involved:
``fill_color`` and ``dashes`` both go on the ``add_freetext_annot`` call, both
take effect, and ``contents`` stays populated through either. ``border_color``
still raises, so the dashed note border is drawn via ``dashes`` on a
``border_width`` border rather than by colouring one. "Does not break
``contents``" is an assumption with a compliance consequence, so
``tests/test_render.py`` asserts it rather than trusting this note.

Two more things measured rather than assumed, both of which surprise on first
contact:

* ``fill_color`` is written to the annotation's ``/C`` entry, so PyMuPDF reports
  it back as ``annot.colors["stroke"]`` and leaves ``colors["fill"]`` empty. On
  a FreeText annot ``/C`` *is* the background -- the guidelines' own example
  labels ``/C [0.75 1 1]`` "set background color" -- so this is correct, just
  confusingly named on the way back out.
* The stored ``/Rect`` is inflated by half the border width (a box asked for at
  x0=260 comes back at 259.5), because the stroke is centred on the path. Tests
  comparing a requested bbox against ``annot.rect`` need that tolerance.
"""

from __future__ import annotations

from typing import Optional

import pymupdf

from pipeline.geometry import bbox_to_fitz_rect
from pipeline.models import BBox
from pipeline.msg import TEXT_COLOR, to_pdf_color

FONT = "helv"
FONT_SIZE = 7.0
LEGEND_FONT_SIZE = 8.0

#: Fallback ink for an annotation with no domain colour -- a note, or a mapping
#: whose domain never made it into the palette.
UNFILLED_TEXT_COLOR = (0.80, 0.05, 0.05)
NOTE_TEXT_COLOR = (0.20, 0.20, 0.20)

BORDER_WIDTH = 1.0
#: PDF border-style dash pattern for a note: /BS << /W 1 /S /D /D [3] >>.
DASH_PATTERN = (3,)


def _drop_callout(annot: pymupdf.Annot) -> None:
    """Remove the ``/CL`` callout line PyMuPDF writes onto every freetext annot.

    Measured on 1.28.2: ``add_freetext_annot`` writes a ``/CL`` array running
    from a page corner to the annotation -- ``/CL [0 792 316.3 712]`` -- with no
    way to suppress it through the call (``callout=None`` and ``line_end=0``
    both leave it in place).

    It is *inert as generated*: the appearance stream contains only the fill
    rect, the border rect and the text, and there is no ``/IT
    /FreeTextCallout`` to make ``/CL`` meaningful, so a conforming viewer
    ignores it. But an annotation in a submission artifact should not carry a
    key that says "draw a leader line from the corner of the page" and rely on
    every reader declining to. A viewer that regenerates appearance streams --
    which Acrobat does after some edits -- is within its rights to draw it.
    """
    annot.parent.parent.xref_set_key(annot.xref, "CL", "null")


def draw_annotation(
    page: pymupdf.Page,
    bbox: BBox,
    text: str,
    *,
    fill: Optional[tuple[int, int, int]] = None,
    dashed: bool = False,
    muted: bool = False,
    bordered: Optional[bool] = None,
    fontsize: float = FONT_SIZE,
) -> pymupdf.Annot:
    """Draw one annotation as a real PDF FreeText annot.

    ``bbox`` is PDF user space; the flip back to fitz coordinates happens here
    via the one shared implementation in ``pipeline.geometry``.

    ``fill`` is a 0-255 MSG colour. When it is set the text goes black, per the
    guidelines; without one there is no coloured ground to read black against,
    so the text carries the emphasis instead.

    ``muted`` draws grey text with no fill and no border. That is not MSG styling
    -- it is the convention this codebase used to write, and which older aCRFs in
    a corpus follow. It is kept so that convention can be *reproduced*, which is
    what ``parse_annotated_pdf`` needs in order to be tested against it.

    ``bordered`` defaults to "whatever the other arguments imply": a filled or
    dashed annotation gets a border, a plain one does not. That default matters
    for more than looks. ``parse_annotated_pdf.classify_mark`` reads a bordered,
    *unfilled* mark as a legacy domain banner, so defaulting ``bordered`` to True
    would make every plain annotation come back from a corpus as a banner.
    """
    rect = pymupdf.Rect(*bbox_to_fitz_rect(bbox, page.rect.height))

    if fill is not None:
        text_color = to_pdf_color(TEXT_COLOR)
    elif muted or dashed:
        text_color = NOTE_TEXT_COLOR
    else:
        text_color = UNFILLED_TEXT_COLOR

    if bordered is None:
        bordered = fill is not None or dashed

    annot = page.add_freetext_annot(
        rect,
        text,
        fontsize=fontsize,
        fontname=FONT,
        text_color=text_color,
        fill_color=to_pdf_color(fill) if fill is not None else None,
        border_width=BORDER_WIDTH if bordered else 0,
        dashes=list(DASH_PATTERN) if dashed else None,
    )
    annot.update()
    _drop_callout(annot)
    return annot


def legend_origin(
    page: pymupdf.Page,
    *,
    x: float = 36.0,
    box_height: float = 13.0,
    gap: float = 4.0,
    min_y: float = 2.0,
) -> tuple[float, float]:
    """Where the legend can sit without printing over the page's own header.

    A CRF page's top margin is whatever the form left, which is often only a few
    points -- and a legend dropped at a fixed y prints straight through the
    version line or the form title. This finds the topmost printed text and puts
    the legend above it, falling back to the very top of the page when there is
    not enough room to fit above anything.

    Returns fitz coordinates (top-left origin), matching :func:`draw_legend`.
    """
    from pipeline.text import text_runs

    runs = text_runs(page)
    if not runs:
        return (x, min_y)
    top = min(r.rect.y0 for r in runs)
    return (x, max(min_y, top - box_height - gap))


def draw_legend(
    page: pymupdf.Page,
    domains: list[tuple[str, tuple[int, int, int]]],
    *,
    origin: tuple[float, float] = (36.0, 18.0),
    box_height: float = 13.0,
    gap: float = 4.0,
    max_width: Optional[float] = None,
    fontsize: float = LEGEND_FONT_SIZE,
) -> list[pymupdf.Annot]:
    """Draw the page-top domain legend, wrapping onto further lines as needed.

    ``domains`` is ``[(text, rgb), ...]`` already in the page's colour order --
    picking the order here would duplicate ``pipeline.msg``'s encounter-order
    rule in a second place.

    Coordinates are fitz here (top-left origin), unlike the rest of this module,
    because the legend is positioned relative to the *top* of the page and
    expressing that in a y-up frame reads backwards.
    """
    if not domains:
        return []
    limit = (max_width or page.rect.width) - origin[0]
    x, y = origin
    out: list[pymupdf.Annot] = []

    for text, rgb in domains:
        width = pymupdf.get_text_length(text, fontname=FONT, fontsize=fontsize) + 2 * gap
        if x > origin[0] and x + width > limit:
            x = origin[0]
            y += box_height + gap
        rect = pymupdf.Rect(x, y, x + width, y + box_height)
        annot = page.add_freetext_annot(
            rect,
            text,
            fontsize=fontsize,
            fontname=FONT,
            text_color=to_pdf_color(TEXT_COLOR),
            fill_color=to_pdf_color(rgb),
            border_width=BORDER_WIDTH,
        )
        annot.update()
        _drop_callout(annot)
        out.append(annot)
        x += width + gap
    return out


def save_with_annotations(doc: pymupdf.Document, out_path) -> None:
    """Save without flattening.

    FDA submission review tools expect annotations to remain as PDF markup --
    searchable and separable from the base form. Baking them into page content
    would look identical on screen and be a regression. Never call
    ``doc.bake()`` on a submission artifact.
    """
    doc.save(out_path, garbage=4, deflate=True)
