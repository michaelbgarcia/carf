"""A stand-in for a Copilot reply, for testing plumbing only.

.. danger::
   This is NOT a Copilot response and must never be treated as one.
   ``parse_response.py`` still has to be exercised against a real reply, because
   Copilot's actual formatting quirks are the thing that needs coverage, and a
   hand-written sample only ever exercises the quirks somebody already thought
   of. Markdown-table reformatting, CSV quoting under a chat paste, and
   attachment-vs-paste behaviour are all real unknowns this file cannot resolve.

   What this file is for is the *other* thing tests need: an end-to-end run of
   rows -> batch -> ingest -> sheet -> PDF that does not require a human at the
   keyboard. It proves the pipeline is wired together. It proves nothing about
   the parser's tolerance of real chat output.

   When a real reply arrives, save it verbatim as a fixture and add cases from
   whatever it actually did.

The mappings below are plausible SDTM for the synthetic CRF, invented by hand --
the same status as ``scripts/worked_example.py``'s.
"""

from __future__ import annotations

import csv
import io

from pipeline.models import RowSet
from pipeline.prompt import SHEET_COLUMNS

# Question text -> (kind, domain, variable, variable2, condition, codelist, origin)
_MAPPINGS: dict[str, tuple] = {
    "Site Identifier": ("variable", "DM", "SITEID", "", "", "", "Collected"),
    "Year of Birth (yyyy)": ("variable", "DM", "BRTHDTC", "", "", "", "Collected"),
    # The two-variables-on-one-line case, which exercises the variable2 column.
    "Age (years) at time of consent": ("variable", "DM", "AGE", "AGEU", "", "", "Collected"),
    "Sex": ("variable", "DM", "SEX", "", "", "C66731", "Collected"),
    "Race (check all that apply)": ("variable", "DM", "RACE", "", "", "C74457", "Collected"),
    "Ethnicity": ("variable", "DM", "ETHNIC", "", "", "C66790", "Collected"),
    "Country of Enrollment": ("variable", "DM", "COUNTRY", "", "", "", "Collected"),
    "Visit Date": ("variable", "VS", "VSDTC", "", "", "", "Collected"),
    "Position": ("variable", "VS", "VSPOS", "", "", "C71148", "Collected"),
}

# Findings-class rows: one result variable selected by a --TESTCD condition.
_VS_TESTS = {
    "Systolic Blood Pressure": "SYSBP",
    "Diastolic Blood Pressure": "DIABP",
    "Pulse Rate": "PULSE",
    "Body Temperature": "TEMP",
    "Respiratory Rate": "RESP",
}

_NOT_SUBMITTED_MARKERS = (
    "SYNTHETIC TEST DATA",
    "Protocol SYNTH-001",
    "Form:",
    "Form Demographics",
    "Form Vital Signs",
    "Form Instructions",
    "Investigator Initials",
    "Assessor Initials",
    "INSTRUCTIONS",
)


def _map_row(text_1: str, text_2: str) -> tuple:
    key = text_1 or text_2
    if key in _MAPPINGS:
        return _MAPPINGS[key]
    if key in _VS_TESTS:
        return ("variable", "VS", "VSORRES", "", f"VSTESTCD = {_VS_TESTS[key]}", "", "Collected")
    if key.startswith(_NOT_SUBMITTED_MARKERS):
        return ("note", "", "", "", "", "", "NotSubmitted")
    return ("variable", "", "", "", "", "", "Collected")


def build_response(
    rows: RowSet,
    page_indexes: list[int],
    *,
    as_markdown_table: bool = False,
    chatty: bool = False,
    smart_quotes: bool = False,
    drop: tuple[str, ...] = (),
) -> str:
    """Render a stand-in filled-in sheet, optionally mangled the way a chat UI would."""
    wanted = set(page_indexes)
    selected = sorted(
        (r for r in rows.rows if r.page_index in wanted),
        key=lambda r: (r.page_index, -r.anchor.y1, r.anchor.x0),
    )

    out = []
    for row in selected:
        if row.row_id in drop:
            continue
        kind, domain, variable, variable2, condition, codelist, origin = _map_row(
            row.text_1, row.text_2
        )
        out.append(
            {
                "row_id": row.row_id,
                "page": row.display_page,
                "form": row.form,
                "text_1": row.text_1,
                "text_2": row.text_2,
                "kind": kind,
                "domain": domain,
                "variable": variable,
                "variable2": variable2,
                "condition": condition,
                "codelist": codelist,
                "origin": origin,
                "confidence": 0.9 if domain else 0.95,
                "rationale": f"Mapped from the question text {row.text_1!r}.",
            }
        )

    if as_markdown_table:
        header = "| " + " | ".join(SHEET_COLUMNS) + " |"
        sep = "|" + "|".join(["---"] * len(SHEET_COLUMNS)) + "|"
        lines = [header, sep]
        for r in out:
            lines.append("| " + " | ".join(str(r[c]) for c in SHEET_COLUMNS) + " |")
        body = "\n".join(lines)
    else:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=SHEET_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(out)
        body = buf.getvalue()

    if smart_quotes and out:
        # Simulate a chat renderer swapping straight quotes for curly ones in the
        # first quoted rationale it encounters.
        body = body.replace('"', "“", 1).replace('"', "”", 1)
    if chatty:
        body = (
            "Sure! Here's the completed spec sheet with SDTM annotations:\n\n"
            f"{body}\n\n"
            "Let me know if you'd like me to revisit any of these mappings."
        )
    return body
