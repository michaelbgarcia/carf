"""Recovery from a pasted/attached chat reply to a spec sheet.

.. warning::
   These cases are constructed from known chat-UI behaviours, NOT from an
   observed Copilot 365 reply. The build instructions are explicit that the
   parser should be exercised against a real response, because its actual
   quirks are the thing needing coverage, and that round trip has not happened
   yet. Treat this file as necessary but not sufficient: when a real reply
   arrives, add it verbatim as a fixture and expect to find quirks nobody
   guessed -- markdown-table reformatting is the predicted big one, but it is
   still a prediction.
"""

import csv
import io

import pytest

from pipeline.extract import extract_fields
from pipeline.models import ReviewStatus
from pipeline.parse_response import (
    IncompleteResponseError,
    ResponseParseError,
    SOURCE_MODEL,
    attach_geometry,
    ingest_response_file,
    parse_proposals,
    parse_sheet_rows,
)
from pipeline.prompt import SHEET_COLUMNS

CLEAN_HEADER = ",".join(SHEET_COLUMNS)
CLEAN = (
    f"{CLEAN_HEADER}\n"
    "DM_SITEID,1,Site ID,section: DEMOGRAPHICS,DM_SITEID,variable,DM,SITEID,,,"
    "Collected,0.9,Site identifier in the page header.\n"
)


@pytest.fixture(scope="module")
def fieldset(crfs):
    return extract_fields(crfs["acroform"])


def _full_batch_reply(fieldset, page_indexes=(0,)) -> str:
    """A syntactically valid CSV reply covering every field in a batch."""
    fields = [f for f in fieldset.fields if f.page_index in set(page_indexes)]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=SHEET_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for f in fields:
        writer.writerow(
            {
                "field_id": f.field_id,
                "page": f.page_index + 1,
                "label": f.label,
                "context": f.context,
                "acroform_name": f.acroform_name or "",
                "kind": "variable",
                "domain": "DM",
                "variable": "SITEID",
                "condition": "",
                "codelist": "",
                "origin": "Collected",
                "confidence": 0.8,
                "rationale": "x",
            }
        )
    return buf.getvalue()


def _as_markdown_table(csv_text: str) -> str:
    rows = list(csv.reader(io.StringIO(csv_text)))
    header, data = rows[0], rows[1:]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for row in data:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


# --- the mangling a chat UI introduces --------------------------------------


def test_parses_a_clean_csv():
    assert parse_proposals(CLEAN)[0].variable == "SITEID"


def test_parses_a_markdown_table_reformatting():
    """The predicted primary failure mode: chat UIs love turning tabular
    replies into markdown tables even when told to return CSV."""
    md = _as_markdown_table(CLEAN)
    assert parse_proposals(md)[0].variable == "SITEID"


def test_strips_a_code_fence_around_csv():
    fenced = f"```csv\n{CLEAN}\n```"
    assert parse_proposals(fenced)[0].field_id == "DM_SITEID"


def test_strips_a_code_fence_around_a_markdown_table():
    fenced = f"```\n{_as_markdown_table(CLEAN)}\n```"
    assert parse_proposals(fenced)[0].field_id == "DM_SITEID"


def test_ignores_conversational_wrapping():
    wrapped = (
        "Sure! Here's the completed spec sheet:\n\n"
        f"{CLEAN}\n\n"
        "Let me know if you'd like me to adjust any of these mappings."
    )
    assert len(parse_proposals(wrapped)) == 1


def test_normalizes_smart_quotes_in_a_quoted_csv_field():
    """CSV quoting depends on literal double quotes, so a smart-quoted field
    breaks the csv module's own quote matching -- this matters more here
    than it did for JSON."""
    quoted = CLEAN.replace(
        "Site identifier in the page header.",
        '"Site identifier, in the page header."',
    )
    smart = quoted.replace('"', "“", 1).replace('"', "”", 1)
    assert parse_proposals(smart)[0].field_id == "DM_SITEID"


def test_treats_the_models_stand_ins_for_empty_as_null():
    text = CLEAN.replace(",,,", ",N/A,None,")
    p = parse_proposals(text)[0]
    assert p.condition is None and p.codelist is None


def test_ignores_extra_columns_the_model_volunteers():
    text = CLEAN_HEADER + ",sdtm_class\n" + CLEAN.splitlines()[1] + ",Special Purpose\n"
    assert parse_proposals(text)[0].field_id == "DM_SITEID"


def test_column_headers_are_matched_case_and_space_insensitively_in_markdown():
    md = _as_markdown_table(CLEAN).replace("field_id", "Field Id")
    assert parse_proposals(md)[0].field_id == "DM_SITEID"


def test_accepts_lowercase_origin_spellings():
    text = CLEAN.replace("Collected", "collected")
    assert parse_proposals(text)[0].origin.value == "Collected"


def test_tab_separated_reply_still_parses():
    """Guards against the exact CSV-vs-TSV fragility flagged in the design:
    if a chat UI's whitespace handling ends up producing tabs instead of
    commas, the sniffer should still recover it."""
    tsv = CLEAN.replace(",", "\t")
    assert parse_proposals(tsv)[0].field_id == "DM_SITEID"


# --- failing loudly ----------------------------------------------------------


def test_empty_response_raises_rather_than_returning_nothing():
    with pytest.raises(ResponseParseError, match="empty"):
        parse_sheet_rows("   \n  ")


def test_prose_only_response_raises():
    with pytest.raises(ResponseParseError):
        parse_proposals("I'm sorry, I can't help with that request.")


def test_a_row_missing_field_id_is_rejected_not_silently_dropped():
    text = CLEAN_HEADER + "\n" + CLEAN.splitlines()[1].replace("DM_SITEID", "", 1)
    with pytest.raises(ResponseParseError, match="field_id"):
        parse_proposals(text)


def test_report_truncates_a_huge_paste_but_says_so():
    err = ResponseParseError("boom", "x" * 5000)
    report = err.report(limit=100)
    assert "more chars" in report and len(report) < 400


def test_a_short_reply_is_never_silently_accepted(fieldset, tmp_path):
    path = tmp_path / "r.csv"
    path.write_text(CLEAN, encoding="utf-8")
    with pytest.raises(IncompleteResponseError) as exc:
        ingest_response_file(path, fieldset, [0])
    assert "DM_USUBJID" in exc.value.missing or len(exc.value.missing) > 0
    assert "Re-paste" in str(exc.value)


def test_a_short_reply_can_be_accepted_deliberately(fieldset, tmp_path):
    path = tmp_path / "r.csv"
    path.write_text(CLEAN, encoding="utf-8")
    got = ingest_response_file(path, fieldset, [0], allow_partial=True)
    assert len(got.annotations) == 1


def test_a_stray_pipe_inside_a_markdown_cell_fails_loudly_not_silently():
    """Regression guard for a real bug hit during development.

    A context value containing '|' (e.g. "line: Sex | Male | Female") shifts
    every column after it in a markdown-table row, landing wrong values in
    kind/origin/confidence with no exception in the naive case -- because a
    misaligned column doesn't always fail its own type check (two swapped
    free-text columns validate fine; only some misalignments happen to hit an
    enum field). The parser must catch this structurally, by cell count, not
    rely on pydantic to notice by luck.
    """
    header = "| " + " | ".join(SHEET_COLUMNS) + " |"
    sep = "|" + "|".join(["---"] * len(SHEET_COLUMNS)) + "|"
    # One extra '|' inside the context cell -- as if the field's context had
    # not been sanitised of pipes.
    bad_row = (
        "| DM_SITEID | 1 | Site ID | line: A | B | DM_SITEID | variable | DM | "
        "SITEID |  |  | Collected | 0.9 | x |"
    )
    with pytest.raises(ResponseParseError, match="shifted the columns|unescaped"):
        parse_proposals("\n".join([header, sep, bad_row]))


def test_a_row_naming_an_unknown_field_id_is_caught():
    """A row whose field_id does not exist anywhere in the document -- a typo
    or a row from an entirely different CRF."""
    bogus = CLEAN.replace("DM_SITEID", "NOT_A_REAL_FIELD")
    proposals = parse_proposals(bogus)
    from pipeline.models import FieldSet

    empty_fieldset = FieldSet(source_pdf="x.pdf")
    with pytest.raises(ResponseParseError, match="not in this document"):
        attach_geometry(proposals, empty_fieldset)


# --- rejoining geometry and provenance ---------------------------------------


def test_geometry_comes_from_the_fieldset_not_the_reply(fieldset):
    proposals = parse_proposals(CLEAN)
    annots = attach_geometry(proposals, fieldset)
    source = fieldset.by_id("DM_SITEID")
    assert annots[0].bbox == source.bbox
    assert annots[0].field_id == source.field_id
    assert annots[0].page_index == source.page_index


def test_join_survives_row_reordering(fieldset):
    """field_id, not row position, is the join key -- this is the whole
    point of moving off a positional index."""
    text = _full_batch_reply(fieldset, (0,))
    rows = text.splitlines()
    header, data = rows[0], rows[1:]
    reordered = "\n".join([header] + list(reversed(data)))
    annots = attach_geometry(parse_proposals(reordered), fieldset)
    assert {a.field_id for a in annots} == {f.field_id for f in fieldset.for_page(0)}


def test_everything_arrives_as_an_unreviewed_proposal(fieldset):
    annots = attach_geometry(parse_proposals(_full_batch_reply(fieldset, (0,))), fieldset)
    assert annots
    assert all(a.review_status is ReviewStatus.PROPOSED for a in annots)
    assert all(a.reviewed_by is None for a in annots)


def test_provenance_records_the_manual_paste(fieldset):
    annots = attach_geometry(parse_proposals(CLEAN), fieldset)
    assert annots[0].source_model == SOURCE_MODEL
    assert "Copilot" in annots[0].source_model
    assert annots[0].created_at is not None


def test_full_batch_reply_round_trips_every_field(fieldset, tmp_path):
    path = tmp_path / "r.csv"
    path.write_text(_full_batch_reply(fieldset, (0,)), encoding="utf-8")
    got = ingest_response_file(path, fieldset, [0])
    assert len(got.annotations) == len(fieldset.for_page(0))
    assert {a.field_id for a in got.annotations} == {
        f.field_id for f in fieldset.for_page(0)
    }


def test_a_batch_spanning_multiple_pages_round_trips(fieldset, tmp_path):
    """The actual point of the redesign: one reply, many pages."""
    path = tmp_path / "r.csv"
    path.write_text(_full_batch_reply(fieldset, (0, 1)), encoding="utf-8")
    got = ingest_response_file(path, fieldset, [0, 1])
    assert {a.field_id for a in got.annotations} == {f.field_id for f in fieldset.fields}
    assert {a.page_index for a in got.annotations} == {0, 1}
