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

from pipeline.rows import extract_rows
from pipeline.models import AnnotationKind, ReviewStatus
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
#: The row_id of the "Site Identifier" row on page 1 of the synthetic CRF.
SITE_ROW = "p1_r004"
CLEAN = (
    f"{CLEAN_HEADER}\n"
    f"{SITE_ROW},1,Demographics,Site Identifier,,variable,DM,SITEID,,,,"
    "Collected,0.9,Site identifier in the page header.\n"
)


@pytest.fixture(scope="module")
def rowset(crfs):
    return extract_rows(crfs["acroform"])


def _full_batch_reply(rowset, page_indexes=(0,), variable2: str = "") -> str:
    """A syntactically valid CSV reply covering every row in a batch."""
    rows = [r for r in rowset.rows if r.page_index in set(page_indexes)]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=SHEET_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "row_id": row.row_id,
                "page": row.display_page,
                "form": row.form,
                "text_1": row.text_1,
                "text_2": row.text_2,
                "kind": "variable",
                "domain": "DM",
                "variable": "SITEID",
                "variable2": variable2,
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


def test_blank_kind_cell_falls_back_to_variable():
    """The instructions only ever tell Copilot to fill "kind" for note rows, so
    a real reply leaves it empty on nearly every row. An empty cell scrubs to
    None, which used to defeat the field default and fail the whole batch on an
    enum error."""
    blank_kind = CLEAN.replace(",variable,DM,SITEID", ",,DM,SITEID")
    proposal = parse_proposals(blank_kind)[0]
    assert proposal.kind is AnnotationKind.VARIABLE
    assert proposal.variable == "SITEID"


@pytest.mark.parametrize("cell", ["Variable", "VARIABLE", " note ", "Domain"])
def test_kind_is_case_insensitive(cell):
    """Same leniency as origin: a chat reply capitalises this column about as
    often as not, and that is not worth a re-paste."""
    text = CLEAN.replace(",variable,DM,SITEID", f",{cell},DM,SITEID")
    assert parse_proposals(text)[0].kind is AnnotationKind.coerce(cell)


def test_unknown_kind_still_fails_loudly():
    text = CLEAN.replace(",variable,DM,SITEID", ",question,DM,SITEID")
    with pytest.raises(ResponseParseError, match="not an annotation kind"):
        parse_proposals(text)


def test_parses_a_markdown_table_reformatting():
    """The predicted primary failure mode: chat UIs love turning tabular
    replies into markdown tables even when told to return CSV."""
    md = _as_markdown_table(CLEAN)
    assert parse_proposals(md)[0].variable == "SITEID"


def test_strips_a_code_fence_around_csv():
    fenced = f"```csv\n{CLEAN}\n```"
    assert parse_proposals(fenced)[0].row_id == SITE_ROW


def test_strips_a_code_fence_around_a_markdown_table():
    fenced = f"```\n{_as_markdown_table(CLEAN)}\n```"
    assert parse_proposals(fenced)[0].row_id == SITE_ROW


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
    assert parse_proposals(smart)[0].row_id == SITE_ROW


def test_treats_the_models_stand_ins_for_empty_as_null():
    text = CLEAN.replace(",,,", ",N/A,None,")
    p = parse_proposals(text)[0]
    assert p.condition is None and p.codelist is None


def test_ignores_extra_columns_the_model_volunteers():
    text = CLEAN_HEADER + ",sdtm_class\n" + CLEAN.splitlines()[1] + ",Special Purpose\n"
    assert parse_proposals(text)[0].row_id == SITE_ROW


def test_column_headers_are_matched_case_and_space_insensitively_in_markdown():
    md = _as_markdown_table(CLEAN).replace("row_id", "Row Id")
    assert parse_proposals(md)[0].row_id == SITE_ROW


def test_accepts_lowercase_origin_spellings():
    text = CLEAN.replace("Collected", "collected")
    assert parse_proposals(text)[0].origin.value == "Collected"


def test_tab_separated_reply_still_parses():
    """Guards against the exact CSV-vs-TSV fragility flagged in the design:
    if a chat UI's whitespace handling ends up producing tabs instead of
    commas, the sniffer should still recover it."""
    tsv = CLEAN.replace(",", "\t")
    assert parse_proposals(tsv)[0].row_id == SITE_ROW


# --- failing loudly ----------------------------------------------------------


def test_empty_response_raises_rather_than_returning_nothing():
    with pytest.raises(ResponseParseError, match="empty"):
        parse_sheet_rows("   \n  ")


def test_prose_only_response_raises():
    with pytest.raises(ResponseParseError):
        parse_proposals("I'm sorry, I can't help with that request.")


def test_a_row_missing_row_id_is_rejected_not_silently_dropped():
    text = CLEAN_HEADER + "\n" + CLEAN.splitlines()[1].replace(SITE_ROW, "", 1)
    with pytest.raises(ResponseParseError, match="row_id"):
        parse_proposals(text)


def test_report_truncates_a_huge_paste_but_says_so():
    err = ResponseParseError("boom", "x" * 5000)
    report = err.report(limit=100)
    assert "more chars" in report and len(report) < 400


def test_a_short_reply_is_never_silently_accepted(rowset, tmp_path):
    path = tmp_path / "r.csv"
    path.write_text(CLEAN, encoding="utf-8")
    with pytest.raises(IncompleteResponseError) as exc:
        ingest_response_file(path, rowset, [0])
    # Every page-1 row except the one the reply covered.
    assert len(exc.value.missing) == len(rowset.for_page(0)) - 1
    assert SITE_ROW not in exc.value.missing
    assert "Re-paste" in str(exc.value)


def test_a_short_reply_can_be_accepted_deliberately(rowset, tmp_path):
    path = tmp_path / "r.csv"
    path.write_text(CLEAN, encoding="utf-8")
    got = ingest_response_file(path, rowset, [0], allow_partial=True)
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
    # One extra '|' inside the question-text cell, as if a CRF question
    # legitimately contained a pipe and nothing had escaped it.
    # Note the pipe has to add a cell the header does not have. "Sex | Male"
    # would not: text_1 and text_2 are legitimately two columns, so it lands
    # exactly on the header count and is indistinguishable from a correct row.
    # That is the point of counting cells rather than hunting for pipes.
    bad_row = (
        f"| {SITE_ROW} | 1 | Demographics | Sex | or gender | Male | variable | DM | "
        "SEX |  |  |  | Collected | 0.9 | x |"
    )
    with pytest.raises(ResponseParseError, match="shifted the columns|unescaped"):
        parse_proposals("\n".join([header, sep, bad_row]))


def test_a_row_naming_an_unknown_row_id_is_caught():
    """A row whose row_id does not exist anywhere in the document -- a typo
    or a row from an entirely different CRF."""
    bogus = CLEAN.replace(SITE_ROW, "p99_r999")
    proposals = parse_proposals(bogus)
    from pipeline.models import RowSet

    empty_rowset = RowSet(source_pdf="x.pdf")
    with pytest.raises(ResponseParseError, match="not in this document"):
        attach_geometry(proposals, empty_rowset)


# --- rejoining geometry and provenance ---------------------------------------


def test_geometry_comes_from_the_rowset_not_the_reply(rowset):
    proposals = parse_proposals(CLEAN)
    annots = attach_geometry(proposals, rowset)
    source = rowset.by_id(SITE_ROW)
    assert source is not None, f"{SITE_ROW} is no longer in the synthetic CRF"
    assert annots[0].bbox == source.anchor
    assert annots[0].row_id == source.row_id
    assert annots[0].page_index == source.page_index


def test_join_survives_row_reordering(rowset):
    """row_id, not row position, is the join key -- this is the whole
    point of moving off a positional index."""
    text = _full_batch_reply(rowset, (0,))
    rows = text.splitlines()
    header, data = rows[0], rows[1:]
    reordered = "\n".join([header] + list(reversed(data)))
    annots = attach_geometry(parse_proposals(reordered), rowset)
    assert {a.row_id for a in annots} == {r.row_id for r in rowset.for_page(0)}


def test_everything_arrives_as_an_unreviewed_proposal(rowset):
    annots = attach_geometry(parse_proposals(_full_batch_reply(rowset, (0,))), rowset)
    assert annots
    assert all(a.review_status is ReviewStatus.PROPOSED for a in annots)
    assert all(a.reviewed_by is None for a in annots)


def test_provenance_records_the_manual_paste(rowset):
    annots = attach_geometry(parse_proposals(CLEAN), rowset)
    assert annots[0].source_model == SOURCE_MODEL
    assert "Copilot" in annots[0].source_model
    assert annots[0].created_at is not None


def test_full_batch_reply_round_trips_every_field(rowset, tmp_path):
    path = tmp_path / "r.csv"
    path.write_text(_full_batch_reply(rowset, (0,)), encoding="utf-8")
    got = ingest_response_file(path, rowset, [0])
    assert len(got.annotations) == len(rowset.for_page(0))
    assert {a.row_id for a in got.annotations} == {
        r.row_id for r in rowset.for_page(0)
    }


def test_a_batch_spanning_multiple_pages_round_trips(rowset, tmp_path):
    """The actual point of the redesign: one reply, many pages."""
    path = tmp_path / "r.csv"
    path.write_text(_full_batch_reply(rowset, (0, 1)), encoding="utf-8")
    got = ingest_response_file(path, rowset, [0, 1])
    assert {a.row_id for a in got.annotations} == {
        r.row_id for r in rowset.for_pages([0, 1])
    }
    assert {a.page_index for a in got.annotations} == {0, 1}


def test_variable2_becomes_a_second_annotation_on_the_same_row(rowset):
    """The AGE / AGEU case: one printed line, two SDTM variables.

    Expanded from a column rather than a second sheet row, so "one row in, one
    row out" stays true of the reply -- which is what the row_id join relies on.
    """
    text = _full_batch_reply(rowset, (0,), variable2="AGEU")
    proposals = parse_proposals(text)
    by_slot = {}
    for p in proposals:
        by_slot.setdefault(p.row_id, {})[p.slot] = p

    first = next(iter(by_slot.values()))
    assert set(first) == {1, 2}
    assert first[1].variable == "SITEID"
    assert first[2].variable == "AGEU"

    annots = attach_geometry(proposals, rowset)
    slots = [a for a in annots if a.row_id == next(iter(by_slot))]
    assert {a.slot for a in slots} == {1, 2}
    assert len({a.annot_id for a in annots}) == len(annots), "annot_ids collided"


def test_a_second_variable_does_not_inherit_the_first_conditions(rowset):
    """AGEU is not "AGE when something" -- carrying the condition over would
    attach a where-clause nobody asked for."""
    header = ",".join(SHEET_COLUMNS)
    line = (
        f"{SITE_ROW},1,Demographics,Age (years) at time of consent,,variable,DM,"
        "AGE,AGEU,VSTESTCD = SYSBP,,Collected,0.9,x"
    )
    proposals = parse_proposals(f"{header}\n{line}\n")
    assert len(proposals) == 2
    assert proposals[0].condition == "VSTESTCD = SYSBP"
    assert proposals[1].variable == "AGEU"
    assert proposals[1].condition is None
