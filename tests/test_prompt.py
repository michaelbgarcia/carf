"""The generated Copilot batch materials: instructions text + CSV spec sheet.

The contract these tests defend: a human pastes the instructions, attaches or
pastes the sheet, and does nothing else before sending. Anything that forces
them to add context, explain the task, or edit before sending is a defect --
so is anything that would teach Copilot to produce output the parser cannot
read (an unparseable worked example, an ambiguous column header).

Batching is the point of this redesign -- see README "Batches, not pages" --
so batch_pages() itself is tested as carefully as the text generation.
"""

import csv
import io
from pathlib import Path

import pytest

from pipeline.rows import extract_rows
from pipeline.prompt import (
    DEFAULT_MAX_ROWS_PER_BATCH,
    FILL_COLUMNS,
    SHEET_COLUMNS,
    batch_pages,
    build_batch_instructions,
    build_precedent_appendix,
    build_spec_sheet,
    write_batches,
)


@pytest.fixture(scope="module")
def rowset(crfs):
    return extract_rows(crfs["acroform"])


@pytest.fixture(scope="module")
def one_batch(rowset):
    """The synthetic CRF is small, so by default it collapses to one batch --
    which is itself the thing this redesign is supposed to demonstrate."""
    return batch_pages(rowset)


# --- batching ---------------------------------------------------------------


def test_small_document_collapses_to_one_batch(rowset, one_batch):
    """The whole point: round-trip count should not scale with page count
    until a real ceiling is hit."""
    assert len(one_batch) == 1
    assert one_batch[0] == [p.page_index for p in rowset.pages]


def test_batches_cover_every_page_exactly_once(rowset):
    batches = batch_pages(rowset, max_rows_per_batch=10)
    covered = [p for b in batches for p in b]
    assert sorted(covered) == sorted({r.page_index for r in rowset.rows})
    assert len(covered) == len(set(covered))


def test_a_page_is_never_split_across_batches(rowset):
    """Section context in a page's captions is only meaningful together."""
    for ceiling in (1, 5, 10, 16, 20):
        for batch in batch_pages(rowset, max_rows_per_batch=ceiling):
            # every page in a batch is a single contiguous unit; nothing here
            # asserts partial-page membership because the API can't express it
            assert all(isinstance(p, int) for p in batch)


def test_an_oversized_page_still_becomes_its_own_batch(rowset):
    """A page over the ceiling is not dropped or split -- it just goes alone."""
    batches = batch_pages(rowset, max_rows_per_batch=1)
    assert len(batches) == len(rowset.pages)
    assert all(len(b) == 1 for b in batches)


def test_default_ceiling_is_generous_enough_for_the_fixture(rowset):
    assert DEFAULT_MAX_ROWS_PER_BATCH > len(rowset.rows)


# --- spec sheet ---------------------------------------------------------------


def test_sheet_is_valid_csv_with_the_declared_columns(rowset, one_batch):
    sheet = build_spec_sheet(rowset, one_batch[0])
    rows = list(csv.DictReader(io.StringIO(sheet)))
    assert list(rows[0].keys()) == SHEET_COLUMNS
    assert len(rows) == len(rowset.rows)


def test_readonly_columns_are_filled_and_fill_columns_start_empty(rowset, one_batch):
    sheet = build_spec_sheet(rowset, one_batch[0])
    rows = list(csv.DictReader(io.StringIO(sheet)))
    for row in rows:
        assert row["row_id"]
        # Either column half may legitimately be empty, but both must be present
        # as columns, and at least one must carry text.
        assert row["text_1"] or row["text_2"]
        for col in FILL_COLUMNS:
            assert row[col] == ""


def test_row_id_in_the_sheet_matches_extraction_exactly(rowset, one_batch):
    sheet = build_spec_sheet(rowset, one_batch[0])
    ids_in_sheet = {r["row_id"] for r in csv.DictReader(io.StringIO(sheet))}
    assert ids_in_sheet == {r.row_id for r in rowset.rows}


def test_page_column_is_one_based_for_humans_only(rowset, one_batch):
    sheet = build_spec_sheet(rowset, one_batch[0])
    rows = {r["row_id"]: r for r in csv.DictReader(io.StringIO(sheet))}
    zero_based = {r.row_id: r.page_index for r in rowset.rows}
    for row_id, page_str in ((k, v["page"]) for k, v in rows.items()):
        assert int(page_str) == zero_based[row_id] + 1


def test_sheet_rows_are_in_reading_order(rowset, one_batch):
    sheet = build_spec_sheet(rowset, [0])
    rows = list(csv.DictReader(io.StringIO(sheet)))
    pages = [int(r["page"]) for r in rows]
    assert pages == sorted(pages)


def test_question_text_survives_csv_quoting(rowset, one_batch):
    """Question text contains commas and parentheses; assert the round trip.

    A CRF question routinely contains a comma ("If Yes, please provide..."), so
    this is not hypothetical -- an unquoted sheet would shear every column after
    it, which is the same class of bug as an unescaped '|' in a markdown cell.
    """
    sheet = build_spec_sheet(rowset, one_batch[0])
    rows = {r["row_id"]: r for r in csv.DictReader(io.StringIO(sheet))}
    by_id = {r.row_id: r for r in rowset.rows}
    assert any("," in r.text_1 for r in rowset.rows), "no comma in any question text"
    for row_id, row in rows.items():
        assert row["text_1"] == by_id[row_id].text_1
        assert row["text_2"] == by_id[row_id].text_2


# --- instructions -------------------------------------------------------------


@pytest.fixture(scope="module")
def instructions(rowset, one_batch):
    return build_batch_instructions(rowset, one_batch[0], 1, 1)


def test_instructions_state_the_task_and_the_standard(instructions):
    assert "annotated CRF" in instructions
    assert "SDTMIG v3.4" in instructions


def test_instructions_carry_every_rule_the_build_requires(instructions):
    for token in ("DOMAIN", "TESTCD", "NotSubmitted", "ORIGIN", "CONFIDENCE"):
        assert token in instructions


def test_instructions_reference_the_attached_sheet_not_embed_the_rows(instructions, rowset):
    """Row data lives in the sheet, not in prose the human has to grow per page."""
    assert "sheet" in instructions.lower()
    for row in rowset.rows:
        if len(row.text_1) > 8:  # short strings collide with ordinary prose
            assert row.text_1 not in instructions


def test_instructions_never_leak_coordinates(instructions, rowset):
    for row in rowset.rows:
        for value in row.anchor.as_tuple():
            assert f"{value:.1f}" not in instructions
    assert "bbox" not in instructions and "page_index" not in instructions


def test_instructions_name_columns_by_the_sheets_own_header(instructions):
    for col in FILL_COLUMNS:
        assert col in instructions


def test_instructions_demand_csv_and_reject_markdown_tables(instructions):
    """The anticipated failure mode -- fight it at the source, same as the
    old design asked for 'no markdown fences' even though the parser also
    has to strip them regardless."""
    assert "CSV" in instructions
    assert "markdown table" in instructions.lower()


def test_instructions_end_with_the_one_line_reminder(instructions):
    assert instructions.rstrip().endswith(
        "Return only the CSV. No markdown table, no code fence, no prose before "
        "or after it."
    )


def test_instructions_name_their_batch_for_a_human(instructions):
    assert instructions.startswith("CRF annotation batch 1 of 1")


# --- historical precedent appendix ---------------------------------------------


def test_precedent_rows_default_produces_identical_instructions_to_before(rowset, one_batch):
    """No precedent_rows argument (the historical default) must be
    byte-identical to explicitly passing None -- and must include no trace
    of a precedent section at all."""
    baseline = build_batch_instructions(rowset, one_batch[0], 1, 1)
    explicit_none = build_batch_instructions(rowset, one_batch[0], 1, 1, precedent_rows=None)
    assert baseline == explicit_none
    assert "HISTORICAL PRECEDENT" not in baseline


def test_precedent_appendix_is_empty_with_no_rows(rowset, one_batch):
    assert build_precedent_appendix(None, rowset, one_batch[0]) == ""
    assert build_precedent_appendix([], rowset, one_batch[0]) == ""


def test_precedent_appendix_only_includes_batch_relevant_rows(rowset, one_batch):
    rows = [
        {
            "label": "Subject Identifier", "domain": "DM", "variable": "USUBJID",
            "condition": "", "fixed_value": "", "count": "5",
        },
        {
            "label": "Completely Unrelated Widget", "domain": "ZZ", "variable": "ZZFOO",
            "condition": "", "fixed_value": "", "count": "5",
        },
    ]
    appendix = build_precedent_appendix(rows, rowset, one_batch[0])
    assert "DM.USUBJID" in appendix
    assert "ZZ.ZZFOO" not in appendix


def test_instructions_include_relevant_precedent_when_given(rowset, one_batch):
    rows = [{
        "label": "Subject Identifier", "domain": "DM", "variable": "USUBJID",
        "condition": "", "fixed_value": "", "count": "5",
    }]
    with_precedent = build_batch_instructions(rowset, one_batch[0], 1, 1, precedent_rows=rows)
    assert "HISTORICAL PRECEDENT" in with_precedent
    assert "DM.USUBJID" in with_precedent


# --- file output ---------------------------------------------------------------


def test_write_batches_produces_one_instructions_and_one_sheet_per_batch(rowset, tmp_path):
    manifest = write_batches(rowset, tmp_path, max_rows_per_batch=10)
    assert len(manifest) == len(rowset.pages)
    for entry in manifest:
        assert Path(entry["instructions"]).exists()
        assert Path(entry["sheet"]).exists()
        assert Path(entry["instructions"]).read_text(encoding="utf-8").strip()


def test_manifest_records_each_batchs_pages_and_expected_response_path(rowset, tmp_path):
    manifest = write_batches(rowset, tmp_path, max_rows_per_batch=10)
    assert manifest[0]["pages"] == [0]
    assert manifest[1]["pages"] == [1]
    assert manifest[0]["expected_response"].endswith("copilot_batch1_response.csv")


def test_each_sheet_only_contains_its_own_batchs_rows(rowset, tmp_path):
    """Mixing batches is how a reply ends up applied to the wrong geometry."""
    manifest = write_batches(rowset, tmp_path, max_rows_per_batch=10)

    sheet2_ids = {
        r["row_id"]
        for r in csv.DictReader(io.StringIO(Path(manifest[1]["sheet"]).read_text()))
    }
    page1_ids = {r.row_id for r in rowset.for_page(0)}
    assert not (sheet2_ids & page1_ids)


# --- Copilot as the gap filler, not the mapping source -------------------------


def test_rows_needing_annotation_drops_what_precedent_already_answered(rowset):
    """The narrowing that demotes Copilot off the critical path.

    Re-asking about a row mined precedent already answered would spend a round
    trip second-guessing corroborated historical evidence with a chat reply.
    """
    from pipeline.models import AnnotationKind, AnnotationSet, SdtmAnnotation
    from pipeline.prompt import rows_needing_annotation

    answered = rowset.rows[:5]
    proposals = AnnotationSet(
        source_pdf=rowset.source_pdf,
        pages=rowset.pages,
        annotations=[
            SdtmAnnotation(
                annot_id=f"{r.row_id}_a1",
                row_id=r.row_id,
                page_index=r.page_index,
                bbox=r.anchor,
                kind=AnnotationKind.VARIABLE,
                domain="DM",
                variable="SEX",
                suggested=True,
            )
            for r in answered
        ],
    )
    remaining = rows_needing_annotation(rowset, proposals)
    assert len(remaining.rows) == len(rowset.rows) - len(answered)
    assert not ({r.row_id for r in remaining.rows} & {r.row_id for r in answered})


def test_rows_needing_annotation_with_nothing_answered_is_everything(rowset):
    from pipeline.prompt import rows_needing_annotation

    assert len(rows_needing_annotation(rowset, None).rows) == len(rowset.rows)


def test_full_precedent_coverage_writes_no_batches_at_all(rowset, tmp_path):
    """The good outcome: nothing to ask, so no round trips."""
    from pipeline.prompt import rows_needing_annotation
    from pipeline.models import AnnotationKind, AnnotationSet, SdtmAnnotation

    proposals = AnnotationSet(
        source_pdf=rowset.source_pdf,
        pages=rowset.pages,
        annotations=[
            SdtmAnnotation(
                annot_id=f"{r.row_id}_a1", row_id=r.row_id, page_index=r.page_index,
                bbox=r.anchor, kind=AnnotationKind.VARIABLE, domain="DM", variable="X",
            )
            for r in rowset.rows
        ],
    )
    remaining = rows_needing_annotation(rowset, proposals)
    assert remaining.rows == []
    assert write_batches(remaining, tmp_path) == []
    assert list(tmp_path.iterdir()) == []


def test_a_narrowed_batch_only_sheets_the_rows_it_asked_about(rowset, tmp_path):
    """The completeness check downstream compares against the narrowed set.

    Sheeting a row that was already answered would make the reply look short when
    Copilot correctly left it out.
    """
    from pipeline.models import AnnotationKind, AnnotationSet, SdtmAnnotation
    from pipeline.prompt import rows_needing_annotation

    answered = {r.row_id for r in rowset.for_page(0)[:3]}
    proposals = AnnotationSet(
        source_pdf=rowset.source_pdf,
        pages=rowset.pages,
        annotations=[
            SdtmAnnotation(
                annot_id=f"{rid}_a1", row_id=rid, page_index=0,
                bbox=rowset.by_id(rid).anchor, kind=AnnotationKind.VARIABLE,
                domain="DM", variable="X",
            )
            for rid in answered
        ],
    )
    remaining = rows_needing_annotation(rowset, proposals)
    manifest = write_batches(remaining, tmp_path)

    sheeted = set()
    for entry in manifest:
        sheeted |= {
            r["row_id"]
            for r in csv.DictReader(io.StringIO(Path(entry["sheet"]).read_text()))
        }
    assert not (sheeted & answered)
    assert sheeted == {r.row_id for r in remaining.rows}


def test_the_sheet_offers_a_second_variable_column(rowset, one_batch):
    """The AGE / AGEU case, as a column rather than an extra row.

    Letting Copilot add rows would give up "one row in, one row out", which is
    the property the row_id join relies on.
    """
    assert "variable2" in SHEET_COLUMNS
    sheet = build_spec_sheet(rowset, one_batch[0])
    assert "variable2" in sheet.splitlines()[0]


def test_instructions_explain_the_two_column_layout(instructions):
    """Copilot never sees coordinates, so the column model has to be stated."""
    lowered = instructions.lower()
    assert "text_1" in instructions and "text_2" in instructions
    assert "two columns" in lowered or "question column" in lowered
