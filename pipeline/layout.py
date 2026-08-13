"""Where an annotation's text goes on the page.

``parse_response.py`` gives every annotation the bbox of the field it
annotates, because that is the only geometry it has. Rendering that directly
would print ``DM.SITEID`` on top of the box a site is meant to write in. This
module moves each annotation off its field to a readable position.

Placement is tried in order -- right of the field, below it, left of it --
and rejected if it leaves the page or lands on an obstacle. Obstacles are
every capture field on the page plus every annotation already placed, so
annotations do not stack on each other or cover a field. If no candidate is
clean the annotation is nudged downward until it finds space.

Obstacles optionally include the form's own printed text, via
:func:`text_obstacles`. Without it, annotations avoid fields but happily land
on captions -- on the Vital Signs grid the position annotations printed
straight over "Sitting / Supine / Standing".

**What this is still not.** The full "collision layout for dense pages" the
build instructions defer to a later phase would also reflow into margins,
shrink or abbreviate text that cannot fit, and draw leader lines from a
displaced annotation back to its field. This does none of that: it only
searches three candidate positions and then walks downward. On a genuinely
dense page an annotation can end up far from the field it describes with
nothing connecting them visually.

Coordinates here are PDF user space (y up), like every ``BBox``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf

from pipeline.models import AnnotationSet, BBox, FieldSet, PageGeometry, SdtmAnnotation

FONT = "helv"
FONT_SIZE = 7.0
GAP = 4.0  # space between a field and its annotation
PAD = 2.0  # padding inside the annotation box
MARGIN = 6.0  # keep annotations this far from the page edge
NUDGE = 9.0  # vertical step when every candidate is blocked
MAX_NUDGES = 40
MIN_OVERLAP = 1.0  # ignore sub-point grazes


def text_width(text: str, size: float = FONT_SIZE) -> float:
    return pymupdf.get_text_length(text, fontname=FONT, fontsize=size)


def annotation_size(text: str, size: float = FONT_SIZE) -> tuple[float, float]:
    return text_width(text, size) + 2 * PAD, size + 2 * PAD


@dataclass
class _Box:
    x0: float
    y0: float
    x1: float
    y1: float

    def overlaps(self, other: BBox) -> bool:
        return (
            min(self.x1, other.x1) - max(self.x0, other.x0) > MIN_OVERLAP
            and min(self.y1, other.y1) - max(self.y0, other.y0) > MIN_OVERLAP
        )

    def to_bbox(self) -> BBox:
        return BBox(x0=self.x0, y0=self.y0, x1=self.x1, y1=self.y1)


def _on_page(box: _Box, page: PageGeometry) -> bool:
    return (
        box.x0 >= MARGIN
        and box.x1 <= page.width - MARGIN
        and box.y0 >= MARGIN
        and box.y1 <= page.height - MARGIN
    )


def _candidates(field: BBox, w: float, h: float) -> list[_Box]:
    """Right, then below, then left of the field."""
    return [
        _Box(field.x1 + GAP, field.y0, field.x1 + GAP + w, field.y0 + h),
        _Box(field.x0, field.y0 - GAP - h, field.x0 + w, field.y0 - GAP),
        _Box(field.x0 - GAP - w, field.y0, field.x0 - GAP, field.y0 + h),
    ]


def place_one(
    field: BBox, text: str, page: PageGeometry, obstacles: list[BBox]
) -> BBox:
    """Find a readable spot for one annotation, avoiding `obstacles`."""
    w, h = annotation_size(text)
    for box in _candidates(field, w, h):
        if _on_page(box, page) and not any(box.overlaps(o) for o in obstacles):
            return box.to_bbox()

    # Nothing clean: start at the preferred position and walk down the page.
    box = _candidates(field, w, h)[0]
    for _ in range(MAX_NUDGES):
        if _on_page(box, page) and not any(box.overlaps(o) for o in obstacles):
            return box.to_bbox()
        box = _Box(box.x0, box.y0 - NUDGE, box.x1, box.y1 - NUDGE)
    # Give up and keep the preferred position rather than dropping the
    # annotation -- a visible collision is reviewable, a missing annotation
    # is not.
    return _candidates(field, w, h)[0].to_bbox()


def text_obstacles(pdf_path) -> dict[int, list[BBox]]:
    """Bounding boxes of the form's own printed text, per page.

    Reuses ``extract.text_runs`` rather than re-deriving text grouping, so
    what layout treats as an obstacle is exactly what extraction treats as a
    caption.
    """
    from pipeline.extract import text_runs  # imported here to avoid a cycle
    from pipeline.geometry import fitz_rect_to_bbox

    doc = pymupdf.open(pdf_path)
    try:
        return {
            i: [fitz_rect_to_bbox(r.rect, page.rect.height) for r in text_runs(page)]
            for i, page in enumerate(doc)
        }
    finally:
        doc.close()


def place_annotations(
    annotations: AnnotationSet,
    fieldset: FieldSet,
    size: float = FONT_SIZE,
    obstacles: dict[int, list[BBox]] | None = None,
) -> AnnotationSet:
    """Reposition every annotation off the field it annotates.

    Annotations are placed top-to-bottom so the obstacle set grows in reading
    order, which keeps displacement predictable for a reviewer. Pass
    ``obstacles`` from :func:`text_obstacles` to also avoid printed text.
    """
    pages = {p.page_index: p for p in (annotations.pages or fieldset.pages)}
    fields_by_page: dict[int, list[BBox]] = {
        k: list(v) for k, v in (obstacles or {}).items()
    }
    for f in fieldset.fields:
        fields_by_page.setdefault(f.page_index, []).append(f.bbox)

    placed: list[SdtmAnnotation] = []
    taken: dict[int, list[BBox]] = {}

    for a in sorted(
        annotations.annotations, key=lambda a: (a.page_index, -a.bbox.y1, a.bbox.x0)
    ):
        page = pages.get(a.page_index)
        text = a.display_text()
        if page is None or not text:
            placed.append(a)
            continue
        obstacles = fields_by_page.get(a.page_index, []) + taken.get(a.page_index, [])
        box = place_one(a.bbox, text, page, obstacles)
        taken.setdefault(a.page_index, []).append(box)
        placed.append(a.model_copy(update={"bbox": box}))

    return annotations.model_copy(update={"annotations": placed})
