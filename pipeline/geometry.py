"""The y-flip, in one place.

PyMuPDF uses a **top-left origin with y increasing downward** (``fitz.Rect``).
XFDF and the PDF spec's ``/Rect`` use a **bottom-left origin with y increasing
upward**. Converting between them means reflecting y about the page height::

    y_other = page_height - y_this

...which reverses the ordering of the two y coordinates, so ``y0`` and ``y1``
have to be swapped as well. Subtracting in place and leaving them in their
original slots produces a rect that is upside down about its own centre --
close enough to look plausible on a dense page, wrong everywhere.

The instructions ask for this conversion to live in exactly one place. Note
that it is called from two boundaries, not one:

* ``extract.py``  -- fitz rect in, ``BBox`` out (models are PDF user space).
* ``xfdf_to_pdf.py`` -- ``BBox`` in, fitz rect out, when rebuilding the PDF.

Both call the same pair of functions below, so there is still only one
implementation of the arithmetic. ``xfdf.py`` writes ``BBox`` values straight
into ``@rect`` without touching y at all, because by then they are already in
PDF user space.

The reflection is its own inverse, so ``bbox_to_fitz_rect`` and
``fitz_rect_to_bbox`` compose back to the identity -- which is what the
round-trip test in ``tests/`` asserts.

A note on the name: the import is ``pymupdf``, not ``fitz`` -- PyMuPDF 1.28
deprecated the ``fitz`` alias and warns it will be removed. "fitz" survives in
these function names because it is the established shorthand for *the
coordinate convention* (top-left origin, y down), which is what they convert,
and the build instructions use it throughout. Import the library as
``pymupdf`` everywhere.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Tuple

from pipeline.models import BBox

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    import pymupdf


RectTuple = Tuple[float, float, float, float]


def _flip(y0: float, y1: float, page_height: float) -> tuple[float, float]:
    """Reflect a y-interval about ``page_height``, returned low-to-high."""
    a = page_height - y0
    b = page_height - y1
    return (b, a) if b <= a else (a, b)


def fitz_rect_to_bbox(rect: "pymupdf.Rect | RectTuple", page_height: float) -> BBox:
    """fitz (top-left origin, y down) -> :class:`BBox` (PDF user space, y up).

    Accepts a ``pymupdf.Rect`` or any 4-tuple so this module stays importable
    without PyMuPDF loaded.
    """
    x0, y0, x1, y1 = (float(v) for v in tuple(rect))
    if x1 < x0:
        x0, x1 = x1, x0
    low, high = _flip(y0, y1, page_height)
    return BBox(x0=x0, y0=low, x1=x1, y1=high)


def bbox_to_fitz_rect(bbox: BBox, page_height: float) -> RectTuple:
    """:class:`BBox` (PDF user space, y up) -> fitz rect tuple (y down).

    Returned as a plain tuple; callers that need a ``pymupdf.Rect`` construct
    one with ``pymupdf.Rect(*result)``, keeping this module PyMuPDF-free.
    """
    low, high = _flip(bbox.y0, bbox.y1, page_height)
    return (bbox.x0, low, bbox.x1, high)


def format_xfdf_rect(bbox: BBox, precision: int = 3) -> str:
    """Render a :class:`BBox` as an XFDF ``@rect`` attribute value.

    XFDF wants ``x0,y0,x1,y1`` in PDF user space -- no flip here, a ``BBox``
    already is PDF user space.
    """
    return ",".join(f"{v:.{precision}f}" for v in bbox.as_tuple())


def parse_xfdf_rect(value: str) -> BBox:
    """Inverse of :func:`format_xfdf_rect`, tolerant of whitespace."""
    parts = [p for p in value.replace(",", " ").split() if p]
    if len(parts) != 4:
        raise ValueError(f"expected 4 numbers in XFDF rect, got {value!r}")
    x0, y0, x1, y1 = (float(p) for p in parts)
    return BBox(
        x0=min(x0, x1), y0=min(y0, y1), x1=max(x0, x1), y1=max(y0, y1)
    )
