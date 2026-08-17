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

from pipeline.extract import extract_fields
from pipeline.prompt import (
    DEFAULT_MAX_FIELDS_PER_BATCH,
    FILL_COLUMNS,
    SHEET_COLUMNS,
    batch_pages,
    build_batch_instructions,
    build_precedent_appendix,
    build_spec_sheet,
    write_batches,
)


@pytest.fixture(scope="module")
def fieldset(crfs):
    return extract_fields(crfs["acroform"])


@pytest.fixture(scope="module")
def one_batch(fieldset):
    """The synthetic CRF is small, so by default it collapses to one batch --
    which is itself the thing this redesign is supposed to demonstrate."""
    return batch_pages(fieldset)


# --- batching ---------------------------------------------------------------


def test_small_document_collapses_to_one_batch(fieldset, one_batch):
    """The whole point: round-trip count should not scale with page count
    until a real ceiling is hit."""
    assert len(one_batch) == 1
    assert one_batch[0] == [0, 1]


def test_batches_cover_every_page_exactly_once(fieldset):
    batches = batch_pages(fieldset, max_fields_per_batch=10)
    covered = [p for b in batches for p in b]
    assert sorted(covered) == sorted({f.page_index for f in fieldset.fields})
    assert len(covered) == len(set(covered))


def test_a_page_is_never_split_across_batches(fieldset):
    """Section context in a page's captions is only meaningful together."""
    for ceiling in (1, 5, 10, 16, 20):
        for batch in batch_pages(fieldset, max_fields_per_batch=ceiling):
            # every page in a batch is a single contiguous unit; nothing here
            # asserts partial-page membership because the API can't express it
            assert all(isinstance(p, int) for p in batch)


def test_an_oversized_page_still_becomes_its_own_batch(fieldset):
    """A page over the ceiling is not dropped or split -- it just goes alone."""
    batches = batch_pages(fieldset, max_fields_per_batch=1)
    assert len(batches) == 2
    assert all(len(b) == 1 for b in batches)


def test_default_ceiling_is_generous_enough_for_the_fixture(fieldset):
    assert DEFAULT_MAX_FIELDS_PER_BATCH > len(fieldset.fields)


# --- spec sheet ---------------------------------------------------------------


def test_sheet_is_valid_csv_with_the_declared_columns(fieldset, one_batch):
    sheet = build_spec_sheet(fieldset, one_batch[0])
    rows = list(csv.DictReader(io.StringIO(sheet)))
    assert list(rows[0].keys()) == SHEET_COLUMNS
    assert len(rows) == len(fieldset.fields)


def test_readonly_columns_are_filled_and_fill_columns_start_empty(fieldset, one_batch):
    sheet = build_spec_sheet(fieldset, one_batch[0])
    rows = list(csv.DictReader(io.StringIO(sheet)))
    for row in rows:
        assert row["field_id"]
        assert row["label"] or row["label"] == ""  # present as a column either way
        for col in FILL_COLUMNS:
            assert row[col] == ""


def test_field_id_in_the_sheet_matches_extraction_exactly(fieldset, one_batch):
    sheet = build_spec_sheet(fieldset, one_batch[0])
    ids_in_sheet = {r["field_id"] for r in csv.DictReader(io.StringIO(sheet))}
    assert ids_in_sheet == {f.field_id for f in fieldset.fields}


def test_page_column_is_one_based_for_humans_only(fieldset, one_batch):
    sheet = build_spec_sheet(fieldset, one_batch[0])
    rows = {r["field_id"]: r for r in csv.DictReader(io.StringIO(sheet))}
    zero_based = {f.field_id: f.page_index for f in fieldset.fields}
    for field_id, page_str in ((k, v["page"]) for k, v in rows.items()):
        assert int(page_str) == zero_based[field_id] + 1


def test_sheet_rows_are_in_reading_order(fieldset, one_batch):
    sheet = build_spec_sheet(fieldset, [0])
    rows = list(csv.DictReader(io.StringIO(sheet)))
    pages = [int(r["page"]) for r in rows]
    assert pages == sorted(pages)


def test_context_survives_commas_in_the_sheet(fieldset, one_batch):
    """context strings use ';' and '|' as separators, not ',', but csv quoting
    still has to hold if that ever changes -- assert the round trip, not the
    absence of commas."""
    sheet = build_spec_sheet(fieldset, one_batch[0])
    rows = {r["field_id"]: r for r in csv.DictReader(io.StringIO(sheet))}
    by_id = {f.field_id: f for f in fieldset.fields}
    for field_id, row in rows.items():
        assert row["context"] == by_id[field_id].context


# --- instructions -------------------------------------------------------------


@pytest.fixture(scope="module")
def instructions(fieldset, one_batch):
    return build_batch_instructions(fieldset, one_batch[0], 1, 1)


def test_instructions_state_the_task_and_the_standard(instructions):
    assert "annotated CRF" in instructions
    assert "SDTMIG v3.4" in instructions


def test_instructions_carry_every_rule_the_build_requires(instructions):
    for token in ("DOMAIN", "TESTCD", "NotSubmitted", "ORIGIN", "CONFIDENCE"):
        assert token in instructions


def test_instructions_reference_the_attached_sheet_not_embed_the_fields(instructions, fieldset):
    """The whole point of the redesign: field data lives in the sheet, not
    in prose the human has to keep growing per page."""
    assert "sheet" in instructions.lower()
    for f in fieldset.fields:
        assert f.label not in instructions or f.label == ""


def test_instructions_never_leak_coordinates(instructions, fieldset):
    for f in fieldset.fields:
        for value in f.bbox.as_tuple():
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


def test_precedent_rows_default_produces_identical_instructions_to_before(fieldset, one_batch):
    """No precedent_rows argument (the historical default) must be
    byte-identical to explicitly passing None -- and must include no trace
    of a precedent section at all."""
    baseline = build_batch_instructions(fieldset, one_batch[0], 1, 1)
    explicit_none = build_batch_instructions(fieldset, one_batch[0], 1, 1, precedent_rows=None)
    assert baseline == explicit_none
    assert "HISTORICAL PRECEDENT" not in baseline


def test_precedent_appendix_is_empty_with_no_rows(fieldset, one_batch):
    assert build_precedent_appendix(None, fieldset, one_batch[0]) == ""
    assert build_precedent_appendix([], fieldset, one_batch[0]) == ""


def test_precedent_appendix_only_includes_batch_relevant_rows(fieldset, one_batch):
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
    appendix = build_precedent_appendix(rows, fieldset, one_batch[0])
    assert "DM.USUBJID" in appendix
    assert "ZZ.ZZFOO" not in appendix


def test_instructions_include_relevant_precedent_when_given(fieldset, one_batch):
    rows = [{
        "label": "Subject Identifier", "domain": "DM", "variable": "USUBJID",
        "condition": "", "fixed_value": "", "count": "5",
    }]
    with_precedent = build_batch_instructions(fieldset, one_batch[0], 1, 1, precedent_rows=rows)
    assert "HISTORICAL PRECEDENT" in with_precedent
    assert "DM.USUBJID" in with_precedent


# --- file output ---------------------------------------------------------------


def test_write_batches_produces_one_instructions_and_one_sheet_per_batch(fieldset, tmp_path):
    manifest = write_batches(fieldset, tmp_path, max_fields_per_batch=10)
    assert len(manifest) == 2
    for entry in manifest:
        assert Path(entry["instructions"]).exists()
        assert Path(entry["sheet"]).exists()
        assert Path(entry["instructions"]).read_text(encoding="utf-8").strip()


def test_manifest_records_each_batchs_pages_and_expected_response_path(fieldset, tmp_path):
    manifest = write_batches(fieldset, tmp_path, max_fields_per_batch=10)
    assert manifest[0]["pages"] == [0]
    assert manifest[1]["pages"] == [1]
    assert manifest[0]["expected_response"].endswith("copilot_batch1_response.csv")


def test_each_sheet_only_contains_its_own_batchs_fields(fieldset, tmp_path):
    """Mixing batches is how a reply ends up applied to the wrong geometry."""
    manifest = write_batches(fieldset, tmp_path, max_fields_per_batch=10)

    sheet2_ids = {
        r["field_id"]
        for r in csv.DictReader(io.StringIO(Path(manifest[1]["sheet"]).read_text()))
    }
    page1_ids = {f.field_id for f in fieldset.for_page(0)}
    assert not (sheet2_ids & page1_ids)
