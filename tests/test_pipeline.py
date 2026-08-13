"""Layout, QC stamping, and the whole loop wired together.

The end-to-end test uses ``standin_response.py``, which is NOT a Copilot
reply -- see that module's warning. It proves the pipeline is connected; it
proves nothing about the parser's tolerance of real chat output.

The synthetic CRF is two pages, which collapses to *one* batch under the
default ceiling -- that collapse is itself the thing this redesign exists to
demonstrate: one round trip covering both pages, not two.
"""

import pymupdf
import pytest

from pipeline import layout, stamp, xfdf
from pipeline.extract import extract_fields
from pipeline.models import AnnotationSet, ReviewStatus
from pipeline.parse_response import ingest_response_file
from pipeline.prompt import batch_pages, write_batches
from pipeline.xfdf_to_pdf import xfdf_to_pdf
from standin_response import build_response

from tests.test_roundtrip import read_annots  # noqa: F401  (shared helper)


@pytest.fixture(scope="module")
def fieldset(crfs):
    return extract_fields(crfs["acroform"])


@pytest.fixture(scope="module")
def placed(crfs, fieldset, tmp_path_factory):
    """A full annotation set, placed, as ingest_response.py would produce."""
    tmp = tmp_path_factory.mktemp("ingest")
    collected = []
    for pages in batch_pages(fieldset):
        path = tmp / f"batch-{pages[0]}.csv"
        path.write_text(build_response(fieldset, pages), encoding="utf-8")
        collected.extend(ingest_response_file(path, fieldset, pages).annotations)
    annots = AnnotationSet(
        source_pdf=fieldset.source_pdf, pages=fieldset.pages, annotations=collected
    )
    obstacles = layout.text_obstacles(crfs["acroform"])
    return layout.place_annotations(annots, fieldset, obstacles=obstacles)


# --- layout ---------------------------------------------------------------


def test_placement_moves_annotations_off_their_own_field(placed, fieldset):
    """Otherwise the text prints inside the box a site writes in."""
    fields = {f.field_id: f.bbox for f in fieldset.fields}
    for a in placed.annotations:
        field = fields[a.field_id]
        overlap_x = min(a.bbox.x1, field.x1) - max(a.bbox.x0, field.x0)
        overlap_y = min(a.bbox.y1, field.y1) - max(a.bbox.y0, field.y0)
        assert overlap_x <= 1.0 or overlap_y <= 1.0, f"{a.annot_id} covers its field"


def test_placed_annotations_do_not_overlap_each_other(placed):
    by_page: dict[int, list] = {}
    for a in placed.annotations:
        by_page.setdefault(a.page_index, []).append(a)
    for annots in by_page.values():
        for i, a in enumerate(annots):
            for b in annots[i + 1 :]:
                ox = min(a.bbox.x1, b.bbox.x1) - max(a.bbox.x0, b.bbox.x0)
                oy = min(a.bbox.y1, b.bbox.y1) - max(a.bbox.y0, b.bbox.y0)
                assert ox <= 1.0 or oy <= 1.0, f"{a.annot_id} overlaps {b.annot_id}"


def test_placed_annotations_stay_on_the_page(placed, fieldset):
    pages = {p.page_index: p for p in fieldset.pages}
    for a in placed.annotations:
        page = pages[a.page_index]
        assert 0 <= a.bbox.x0 and a.bbox.x1 <= page.width
        assert 0 <= a.bbox.y0 and a.bbox.y1 <= page.height


def test_text_obstacles_keep_annotations_off_printed_captions(crfs, fieldset, placed):
    """Without this the position annotations printed over Sitting/Supine/Standing."""
    obstacles = layout.text_obstacles(crfs["acroform"])
    hits = 0
    for a in placed.annotations:
        for o in obstacles[a.page_index]:
            ox = min(a.bbox.x1, o.x1) - max(a.bbox.x0, o.x0)
            oy = min(a.bbox.y1, o.y1) - max(a.bbox.y0, o.y0)
            if ox > 1.0 and oy > 1.0:
                hits += 1
    assert hits == 0


def test_layout_without_obstacles_still_produces_valid_boxes(placed, fieldset):
    bare = layout.place_annotations(placed, fieldset)
    assert len(bare.annotations) == len(placed.annotations)


# --- QC stamping ----------------------------------------------------------


def test_qc_preview_is_labelled_as_unreviewed(crfs, placed, tmp_path):
    out = stamp.stamp_annotations(crfs["acroform"], placed, tmp_path / "qc.pdf")
    doc = pymupdf.open(out)
    page = doc[0]
    assert "NOT FOR SUBMISSION" in page.get_text()


def test_qc_preview_keeps_annotations_as_markup(crfs, placed, tmp_path):
    out = stamp.stamp_annotations(crfs["acroform"], placed, tmp_path / "qc.pdf")
    assert len(read_annots(out)) > 0


def test_qc_preview_skips_rejected_annotations(crfs, placed, tmp_path):
    rejected = placed.model_copy(
        update={
            "annotations": [
                a.model_copy(
                    update={
                        "review_status": ReviewStatus.REJECTED,
                        "reviewed_by": "mgarcia",
                    }
                )
                for a in placed.annotations
            ]
        }
    )
    out = stamp.stamp_annotations(crfs["acroform"], rejected, tmp_path / "qc.pdf")
    assert read_annots(out) == []


# --- end to end -----------------------------------------------------------


def test_full_loop_from_pdf_to_annotated_pdf(crfs, fieldset, tmp_path):
    """extract -> batch -> (stand-in reply) -> ingest -> XFDF -> annotated PDF."""
    manifest = write_batches(fieldset, tmp_path)

    collected = []
    for entry in manifest:
        reply = tmp_path / f"resp-batch{entry['batch']}.csv"
        # Mangled the way a chat UI would mangle it.
        reply.write_text(
            build_response(fieldset, entry["pages"], as_markdown_table=True, chatty=True),
            encoding="utf-8",
        )
        collected.extend(
            ingest_response_file(reply, fieldset, entry["pages"]).annotations
        )

    annots = AnnotationSet(
        source_pdf=fieldset.source_pdf, pages=fieldset.pages, annotations=collected
    )
    annots = layout.place_annotations(
        annots, fieldset, obstacles=layout.text_obstacles(crfs["acroform"])
    )
    xfdf_path = xfdf.write_xfdf(annots, tmp_path / "blankcrf.xfdf")
    final = xfdf_to_pdf(crfs["acroform"], xfdf_path, tmp_path / "annotated.pdf")

    assert len(collected) == len(fieldset.fields)
    on_page_1 = read_annots(final, 0)
    on_page_2 = read_annots(final, 1)
    assert len(on_page_1) + len(on_page_2) == len(fieldset.fields)


def test_default_batching_needs_only_one_round_trip_for_the_fixture(fieldset, tmp_path):
    """The actual point of the redesign, made explicit as an assertion."""
    manifest = write_batches(fieldset, tmp_path)
    assert len(manifest) == 1
    assert manifest[0]["pages"] == [0, 1]


def test_final_pdf_is_not_flattened(crfs, fieldset, placed, tmp_path):
    """FDA review tools expect annotations to stay as searchable PDF markup."""
    xfdf_path = xfdf.write_xfdf(placed, tmp_path / "x.xfdf")
    final = xfdf_to_pdf(crfs["acroform"], xfdf_path, tmp_path / "final.pdf")

    doc = pymupdf.open(final)
    page = doc[1]
    types = {a.type[1] for a in page.annots()}
    assert types == {"FreeText"}
    # Searchable: the text is real text, not a rendered image.
    assert "VSORRES" in page.get_text()


def test_conditional_annotations_reach_the_final_pdf(crfs, placed, tmp_path):
    """The --TESTCD pattern is the point of the whole VS grid."""
    xfdf_path = xfdf.write_xfdf(placed, tmp_path / "x.xfdf")
    final = xfdf_to_pdf(crfs["acroform"], xfdf_path, tmp_path / "final.pdf")
    contents = {c for c, _ in read_annots(final, 1)}
    assert "VS.VSORRES when VSTESTCD = SYSBP" in contents
    assert "VS.VSORRES when VSTESTCD = RESP" in contents


def test_unmapped_fields_are_visibly_marked_in_the_final_pdf(crfs, placed, tmp_path):
    xfdf_path = xfdf.write_xfdf(placed, tmp_path / "x.xfdf")
    final = xfdf_to_pdf(crfs["acroform"], xfdf_path, tmp_path / "final.pdf")
    contents = {c for c, _ in read_annots(final, 0)}
    assert "[Not Submitted]" in contents
