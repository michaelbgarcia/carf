"""A stand-in for a Copilot reply, for testing plumbing only.

.. danger::
   This is NOT a Copilot response and must never be treated as one. The build
   instructions are explicit: ``parse_response.py`` has to be exercised against
   a real reply, because Copilot's actual formatting quirks are the thing that
   needs coverage, and a hand-written sample only ever exercises the quirks
   somebody already thought of. That is doubly true now that the format is a
   spec-sheet round trip rather than a JSON array -- markdown-table
   reformatting, CSV quoting under a chat paste, and attachment-vs-paste
   behaviour are all real unknowns this file cannot resolve.

   What this file is for is the *other* thing tests need: an end-to-end run of
   extract -> batch -> ingest -> XFDF -> PDF that does not require a human at
   the keyboard. It proves the pipeline is wired together. It proves nothing
   about the parser's tolerance of real chat output.

   When a real reply arrives, save it verbatim as a fixture and add cases from
   whatever it actually did.

The mappings below are plausible SDTM for the synthetic CRF, hand-written by
the same process that produced ``scripts/illustrative_target.py``.
"""

from __future__ import annotations

import csv
import io

from pipeline.models import FieldSet
from pipeline.prompt import SHEET_COLUMNS

# acroform_name prefix -> (kind, domain, variable, condition, codelist, origin)
_MAPPINGS = {
    "DM_SITEID": ("variable", "DM", "SITEID", None, None, "Collected"),
    "DM_USUBJID": ("variable", "DM", "SUBJID", None, None, "Collected"),
    "DM_BRTHDTC": ("variable", "DM", "BRTHDTC", None, None, "Collected"),
    "DM_AGE": ("variable", "DM", "AGE", None, None, "Collected"),
    "DM_SEX": ("variable", "DM", "SEX", None, "C66731", "Collected"),
    "DM_RACE": ("variable", "DM", "RACE", None, "C74457", "Collected"),
    "DM_ETHNIC": ("variable", "DM", "ETHNIC", None, "C66790", "Collected"),
    "DM_COUNTRY": ("variable", "DM", "COUNTRY", None, None, "Collected"),
    "DM_INVINIT": ("note", None, None, None, None, "NotSubmitted"),
    "VS_VISITDAT": ("variable", "VS", "VSDTC", None, None, "Collected"),
    "VS_POS": ("variable", "VS", "VSPOS", None, "C71148", "Collected"),
    "VS_ASSINIT": ("note", None, None, None, None, "NotSubmitted"),
}

_VS_TESTS = {"SYSBP", "DIABP", "PULSE", "TEMP", "RESP"}


def _map_field(acroform_name: str):
    if acroform_name.startswith("VS_"):
        parts = acroform_name.split("_")
        if len(parts) == 3 and parts[1] in _VS_TESTS:
            testcd, suffix = parts[1], parts[2]
            condition = f"VSTESTCD = {testcd}"
            if suffix == "RES":
                return ("variable", "VS", "VSORRES", condition, None, "Collected")
            return ("variable", "VS", "VSSTAT", condition, None, "Collected")
    for prefix, mapping in _MAPPINGS.items():
        if acroform_name.startswith(prefix):
            return mapping
    return ("variable", None, None, None, None, "Collected")


def build_response(
    fieldset: FieldSet,
    page_indexes: list[int],
    *,
    as_markdown_table: bool = False,
    chatty: bool = False,
    smart_quotes: bool = False,
    drop: tuple[str, ...] = (),
) -> str:
    """Render a stand-in filled-in sheet, optionally mangled the way a chat UI would."""
    wanted = set(page_indexes)
    fields = sorted(
        (f for f in fieldset.fields if f.page_index in wanted),
        key=lambda f: (f.page_index, -f.bbox.y1, f.bbox.x0),
    )

    rows = []
    for f in fields:
        if f.field_id in drop:
            continue
        kind, domain, variable, condition, codelist, origin = _map_field(
            f.acroform_name or ""
        )
        rows.append(
            {
                "field_id": f.field_id,
                "page": f.page_index + 1,
                "label": f.label,
                "context": f.context,
                "acroform_name": f.acroform_name or "",
                "kind": kind,
                "domain": domain or "",
                "variable": variable or "",
                "condition": condition or "",
                "codelist": codelist or "",
                "origin": origin,
                "confidence": 0.9 if domain else 0.95,
                "rationale": f"Mapped from the form caption {f.label!r}.",
            }
        )

    if as_markdown_table:
        header = "| " + " | ".join(SHEET_COLUMNS) + " |"
        sep = "|" + "|".join(["---"] * len(SHEET_COLUMNS)) + "|"
        lines = [header, sep]
        for r in rows:
            lines.append("| " + " | ".join(str(r[c]) for c in SHEET_COLUMNS) + " |")
        body = "\n".join(lines)
    else:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=SHEET_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        body = buf.getvalue()

    if smart_quotes and rows:
        # Simulate a chat renderer swapping straight quotes for curly ones in
        # the first quoted rationale it encounters.
        body = body.replace('"', "“", 1).replace('"', "”", 1)
    if chatty:
        body = (
            "Sure! Here's the completed spec sheet with SDTM annotations:\n\n"
            f"{body}\n\n"
            "Let me know if you'd like me to revisit any of these mappings."
        )
    return body
