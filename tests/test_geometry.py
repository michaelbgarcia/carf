"""Coordinate-flip arithmetic.

Pure math against the one implementation in ``pipeline.geometry`` -- no
PyMuPDF needed, so these run before any PDF code exists. The two tests that
need a real page (the visual stamp check and the XFDF round trip) live in
``test_roundtrip.py``.
"""

import pytest

from pipeline.geometry import (
    bbox_to_fitz_rect,
    fitz_rect_to_bbox,
    format_xfdf_rect,
    parse_xfdf_rect,
)
from pipeline.models import BBox

PAGE_HEIGHT = 792.0  # US Letter


def test_flip_moves_a_top_of_page_rect_to_the_top_in_pdf_space():
    # 50pt below the top edge in fitz coords -> 50pt below the top in PDF coords
    bbox = fitz_rect_to_bbox((72.0, 50.0, 300.0, 70.0), PAGE_HEIGHT)
    assert bbox.y0 == pytest.approx(722.0)
    assert bbox.y1 == pytest.approx(742.0)
    assert (bbox.x0, bbox.x1) == (72.0, 300.0)


def test_flip_reorders_y_rather_than_subtracting_in_place():
    """The bug this whole module exists to prevent: y0/y1 left swapped."""
    bbox = fitz_rect_to_bbox((0.0, 100.0, 10.0, 200.0), PAGE_HEIGHT)
    assert bbox.y0 < bbox.y1
    assert bbox.height == pytest.approx(100.0)


def test_round_trip_is_the_identity():
    original = BBox(x0=72.0, y0=600.0, x1=300.0, y1=620.0)
    there_and_back = fitz_rect_to_bbox(
        bbox_to_fitz_rect(original, PAGE_HEIGHT), PAGE_HEIGHT
    )
    assert there_and_back.as_tuple() == pytest.approx(original.as_tuple())


def test_xfdf_rect_serialisation_round_trips():
    original = BBox(x0=72.5, y0=600.25, x1=300.0, y1=620.125)
    assert parse_xfdf_rect(format_xfdf_rect(original)).as_tuple() == pytest.approx(
        original.as_tuple()
    )


def test_bbox_rejects_inverted_y():
    with pytest.raises(ValueError):
        BBox(x0=0.0, y0=100.0, x1=10.0, y1=50.0)
