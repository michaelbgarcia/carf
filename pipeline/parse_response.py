"""Parses a pasted/attached Copilot 365 spec-sheet reply back into annotations.

Task order step 5 (redesigned; see README "Batches, not pages").

The input is a filled-in copy of the CSV spec sheet ``prompt.py`` generated,
returned either as pasted text in the chat or as a downloaded attachment --
either way, saved to a response file by the human. It is not a clean API
payload, so expect the same category of mangling as before, plus one new,
CSV-specific one:

* **Markdown-table reformatting.** Chat UIs have a strong habit of rendering
  tabular data back as a markdown table (``| field_id | domain | ... |`` with
  a ``|---|---|`` header-separator row) rather than literal CSV, because
  that's idiomatic for a chat reply. The instructions explicitly ask for CSV;
  this parser expects the markdown table anyway and treats it as the primary
  case, not a fallback, the same way the old JSON parser stripped code fences
  regardless of being told not to add them.
* Smart quotes substituted by the chat renderer -- relevant here because CSV
  quoting depends on literal ``"`` characters; a smart-quoted field breaks the
  csv module's quote matching.
* A conversational wrapper before/after the table despite the instruction not
  to add one.
* Tabs silently converted to spaces, which is why the sheet is CSV rather than
  TSV: a chat UI's whitespace handling is exactly what a tab-delimited format
  depends on and exactly what it is least reliable at preserving.

Two rules govern everything here, unchanged from the original design:

* **Fail loudly.** A batch that will not parse raises with the raw text
  attached so the human can fix it and re-paste. Never skip it silently.
* **Everything is a proposal.** This module sets ``review_status=PROPOSED``
  and nothing else, ever.

The join key is ``field_id``, not a row position -- see ``prompt.py``. Rows
are matched by id via ``FieldSet.by_id``, which means row reordering, missing
rows, and rows from the wrong batch are all detectable rather than silently
misattributed the way a positional join would be.

.. warning::
   Like the JSON design before it, this has not been exercised against a real
   Copilot 365 reply. The recovery steps are built from known chat-UI
   behaviours, not from an observed response -- see README "The one manual
   step". Treat the quirk list above as provisional.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pipeline.models import (
    AnnotationSet,
    CopilotProposal,
    FieldSet,
    ReviewStatus,
    SdtmAnnotation,
)

SOURCE_MODEL = "Copilot 365 chat, manual paste"

# Chat renderers substitute these for the ASCII originals. CSV quoting
# depends on literal double quotes, so this matters more here than it did for
# JSON -- a smart-quoted field breaks the csv module's quote matching outright
# rather than just looking wrong.
_SMART_QUOTES = {
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "″": '"', "′": "'",
}
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*$", re.MULTILINE)
_MD_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_MD_SEPARATOR = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
# Matches the header row in CSV, TSV, or semicolon-delimited form, optionally
# quoted -- used to locate the table inside surrounding chat prose, the same
# way the old JSON parser located the array by its first '[' and last ']'.
_HEADER_LINE = re.compile(r"^\s*[\"']?field_id[\"']?\s*[,\t;]", re.IGNORECASE)
_NULLISH = {"", "null", "none", "n/a", "na", "not applicable", "-"}


class ResponseParseError(ValueError):
    """Raised with the raw pasted text attached, so the human can re-paste."""

    def __init__(self, message: str, raw_text: str) -> None:
        super().__init__(message)
        self.raw_text = raw_text

    def report(self, limit: int = 2000) -> str:
        excerpt = self.raw_text.strip()
        if len(excerpt) > limit:
            excerpt = excerpt[:limit] + f"\n... [{len(self.raw_text) - limit} more chars]"
        return f"{self}\n\n--- raw pasted text ---\n{excerpt}\n--- end ---"


class IncompleteResponseError(ResponseParseError):
    """The reply parsed but does not cover every field it should have."""

    def __init__(self, message: str, raw_text: str, missing: list[str]) -> None:
        super().__init__(message, raw_text)
        self.missing = missing


def _strip_fences(text: str) -> str:
    return _FENCE.sub("", text)


def _normalize_quotes(text: str) -> str:
    for bad, good in _SMART_QUOTES.items():
        text = text.replace(bad, good)
    return text


def normalize_pasted_text(text: str) -> str:
    """Strip fences and normalise smart quotes."""
    return _normalize_quotes(_strip_fences(text))


def _find_markdown_table_block(lines: list[str]) -> Optional[list[str]]:
    """Locate a header+separator+rows block anywhere in the text.

    Scanning for the block rather than assuming the whole text is a table is
    what lets a chatty wrapper ("Sure! Here's the completed sheet:" ... "Let
    me know if...") sit around it without derailing the parse -- the same
    posture as finding the first ``[`` and last ``]`` for a JSON reply instead
    of assuming ``response.strip()`` is the whole payload.
    """
    for i in range(len(lines) - 1):
        if _MD_ROW.match(lines[i]) and _MD_SEPARATOR.match(lines[i + 1]):
            block = [lines[i], lines[i + 1]]
            j = i + 2
            while j < len(lines) and _MD_ROW.match(lines[j]):
                block.append(lines[j])
                j += 1
            return block
    return None


def _parse_markdown_table(block: list[str]) -> list[dict[str, str]]:
    """Parse an isolated ``| a | b |`` block into dicts keyed by the header row.

    The anticipated primary failure mode -- see module docstring. A second,
    subtler one sits behind it: an unescaped ``|`` *inside* a cell (a stray
    one in a rationale sentence, say) silently shifts every column after it
    -- the row still parses, just with the wrong values in the wrong fields,
    and pydantic validation on the shifted result does not reliably catch it
    (an enum field landing on a stray field_id-shaped string does; two
    strings swapping between two free-text columns does not). Checking the
    cell count against the header here converts that from a silent
    misattribution into a loud failure, regardless of whether the shifted
    values happen to individually look valid.
    """
    def cells(line: str) -> list[str]:
        inner = _MD_ROW.match(line).group(1)
        return [c.strip() for c in inner.split("|")]

    header = [h.lower().replace(" ", "_") for h in cells(block[0])]
    data_rows = [ln for ln in block[1:] if not _MD_SEPARATOR.match(ln)]
    if not data_rows:
        raise ValueError("markdown table has no data rows")

    parsed = []
    for ln in data_rows:
        row_cells = cells(ln)
        if len(row_cells) != len(header):
            raise ValueError(
                f"row has {len(row_cells)} cells but the header has {len(header)} -- "
                f"likely an unescaped '|' inside a cell value shifted the columns: {ln!r}"
            )
        parsed.append(dict(zip(header, row_cells)))
    return parsed


def _isolate_tabular_block(lines: list[str]) -> Optional[str]:
    """Find the header row (CSV, TSV, or semicolon-delimited) and take from
    there to the next blank line -- isolating the table from a trailing
    "Let me know if..." paragraph the same way a leading one is skipped by
    starting the search at the header rather than at line 0.
    """
    start = next((i for i, ln in enumerate(lines) if _HEADER_LINE.match(ln)), None)
    if start is None:
        return None
    block: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            break
        block.append(line)
    return "\n".join(block)


def _parse_csv(text: str) -> list[dict[str, str]]:
    # Sniff the dialect where possible; a chat paste is at least as likely to
    # normalise delimiters as to preserve whatever csv.Sniffer expects, so
    # fall back to the excel default rather than raising on a sniff failure.
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError("no header row found")
    return [
        {(k or "").strip().lower(): (v or "").strip() for k, v in row.items() if k}
        for row in reader
    ]


def parse_sheet_rows(text: str) -> list[dict[str, str]]:
    """Pasted/attached reply -> list of row dicts, keyed by lowercased header.

    Tries a markdown table first (the predicted common case for a chat
    reply), then CSV/TSV. Both are attempted regardless of which the
    instructions asked for, the same posture as stripping a code fence even
    though the prompt said not to add one. Both isolate their table from
    surrounding prose before parsing it, rather than assuming the whole reply
    is the table.
    """
    if not text or not text.strip():
        raise ResponseParseError("the response file is empty", text or "")

    cleaned = normalize_pasted_text(text)
    lines = cleaned.splitlines()

    md_block = _find_markdown_table_block(lines)
    if md_block is not None:
        # A header+separator pair was found, so this IS a markdown-table
        # reply -- a structural failure past this point (e.g. a stray '|'
        # inside a cell shifting the columns) is a definitive diagnosis, not
        # a sign it might actually be CSV. Raising it directly, rather than
        # falling through and losing it behind a generic "no table found"
        # from the CSV path, is what keeps the error message actionable.
        try:
            rows = _parse_markdown_table(md_block)
        except ValueError as exc:
            raise ResponseParseError(f"could not parse the markdown table: {exc}", text) from exc
        if rows:
            return rows

    csv_block = _isolate_tabular_block(lines)
    if csv_block is None:
        raise ResponseParseError(
            "no recognizable table found in the response -- expected either a "
            "markdown table or a CSV/TSV block with a field_id header column",
            text,
        )

    try:
        rows = _parse_csv(csv_block)
    except (csv.Error, ValueError) as exc:
        raise ResponseParseError(f"could not parse the response as CSV: {exc}", text) from exc

    if not rows:
        raise ResponseParseError(
            "parsed the response but found no data rows -- expected a CSV header "
            "row followed by one row per field",
            text,
        )
    return rows


def _scrub(row: dict[str, str]) -> dict[str, Optional[str]]:
    """Turn the model's stand-ins for empty/null into actual None."""
    return {
        k: (None if v.strip().lower() in _NULLISH else v.strip())
        for k, v in row.items()
    }


def parse_proposals(text: str) -> list[CopilotProposal]:
    """Pasted/attached reply -> validated proposals. Raises :class:`ResponseParseError`.

    Once ``parse_sheet_rows`` has isolated the table from surrounding prose,
    every row that reaches here is expected to be real data -- so any row
    that still fails to validate is a genuine defect (a mistyped confidence,
    a dropped field_id) and raises rather than being silently excluded. A
    reply that is mostly right except for one bad row should come back to the
    human for a fix, not ship 30 of 31 annotations with no signal that one
    went missing.
    """
    rows = parse_sheet_rows(text)

    proposals: list[CopilotProposal] = []
    errors: list[str] = []
    for i, row in enumerate(rows, start=1):
        scrubbed = _scrub(row)
        if not scrubbed.get("field_id"):
            errors.append(f"row {i}: missing field_id")
            continue
        try:
            proposals.append(CopilotProposal.model_validate(scrubbed))
        except Exception as exc:  # pydantic ValidationError
            errors.append(f"row {i} ({scrubbed.get('field_id')}): {exc}")

    if errors:
        raise ResponseParseError(
            f"{len(errors)} of {len(rows)} row(s) in the response failed to "
            "parse:\n" + "\n".join(errors),
            text,
        )
    return proposals


def attach_geometry(
    proposals: list[CopilotProposal],
    fieldset: FieldSet,
) -> list[SdtmAnnotation]:
    """Rejoin proposals to their source fields by field_id and fill in provenance."""
    now = datetime.now(timezone.utc)
    seen: dict[str, int] = {}
    out: list[SdtmAnnotation] = []

    for p in proposals:
        field = fieldset.by_id(p.field_id)
        if field is None:
            raise ResponseParseError(
                f"field_id {p.field_id!r} is not in this document's field set -- "
                f"the reply may have been pasted against the wrong batch, or a "
                f"row's field_id was altered in the reply",
                p.field_id,
            )
        seen[p.field_id] = seen.get(p.field_id, 0) + 1
        suffix = "" if seen[p.field_id] == 1 else f"-{seen[p.field_id]}"
        out.append(
            SdtmAnnotation(
                annot_id=f"{field.field_id}{suffix}",
                field_id=field.field_id,
                page_index=field.page_index,
                bbox=field.bbox,
                kind=p.kind,
                domain=p.domain,
                variable=p.variable,
                condition=p.condition,
                codelist=p.codelist,
                origin=p.origin,
                confidence=p.confidence,
                rationale=p.rationale,
                # Provenance. Everything Copilot touches enters as a proposal;
                # the GxP audit trail needs to show a human pasted it.
                source_model=SOURCE_MODEL,
                review_status=ReviewStatus.PROPOSED,
                reviewed_by=None,
                created_at=now,
            )
        )
    return out


def ingest_response_file(
    response_path: str | Path,
    fieldset: FieldSet,
    page_indexes: list[int],
    allow_partial: bool = False,
) -> AnnotationSet:
    """Read one batch's filled-in sheet and return its proposed annotations.

    ``page_indexes`` is the batch's own page list (from the manifest
    ``write_batches`` returned), used only to check completeness -- geometry
    itself comes from ``field_id``, not from the caller.
    """
    path = Path(response_path)
    raw = path.read_text(encoding="utf-8")
    try:
        proposals = parse_proposals(raw)
    except ResponseParseError as exc:
        raise ResponseParseError(f"{path}: {exc}", raw) from exc

    annotations = attach_geometry(proposals, fieldset)

    expected = {f.field_id for f in fieldset.for_pages(page_indexes)}
    missing = sorted(expected - {p.field_id for p in proposals})
    if missing and not allow_partial:
        raise IncompleteResponseError(
            f"{path}: the reply covers {len(expected) - len(missing)} of "
            f"{len(expected)} fields in this batch; missing field_id(s) "
            f"{missing}. Re-paste the instructions and sheet and ask for the "
            f"remaining rows, or pass allow_partial to accept a short reply.",
            raw,
            missing,
        )

    pages = [p for p in fieldset.pages if p.page_index in set(page_indexes)]
    return AnnotationSet(source_pdf=fieldset.source_pdf, pages=pages, annotations=annotations)


__all__ = [
    "IncompleteResponseError",
    "ResponseParseError",
    "SOURCE_MODEL",
    "attach_geometry",
    "ingest_response_file",
    "normalize_pasted_text",
    "parse_proposals",
    "parse_sheet_rows",
]
