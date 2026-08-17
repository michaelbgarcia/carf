"""Mines a corpus of historical annotated CRF PDFs for reusable SDTM precedent.

Where ``pipeline/parse_annotated_pdf.py`` recovers mappings from *one* aCRF,
this module runs that recovery across a whole directory of them and
consolidates the results into a deduplicated label/context ->
domain/variable/condition reference table -- the actual point of having a
historical corpus at all, rather than a pile of PDFs nobody can query (see
that module's own docstring).

Two-pass bootstrap
------------------
``attribute_domains()`` tries, in order of trust: the mark's own explicit
domain, a boxed banner above it, built-in CDISC constants, and a mined
``precedent`` dict (see that function's docstring). The mined precedent this
module produces is exactly that fourth tier -- but producing it requires
running the parser over the corpus *first*, using only the first three
tiers, before any mined precedent exists to pass in. Mining is therefore two
passes, not one:

1. Run every PDF through ``parse_annotated_pdf()`` with no ``precedent``
   argument -- domains resolve only from explicit marks, boxed banners, and
   built-in constants.
2. Aggregate pass 1's results into a variable -> domain table
   (:func:`build_variable_domain_precedent`), counting only mappings whose
   domain came from an explicit mark or a real banner -- never from the
   built-in or (on a later re-run) precedent tiers themselves, so the miner
   can never amplify its own guesses into apparent corroboration.
3. Re-run every PDF through ``parse_annotated_pdf()`` with that table passed
   as ``precedent``. This is what resolves a page whose only clue to a bare
   variable's domain is what other pages in the corpus did with it: a
   ``RFICDTC``/``DSSTDTC`` mark with no banner nearby gets attributed not
   because *this* page has evidence, but because enough other pages did.

Pass 2's output is the authoritative mining result.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

from pipeline.parse_annotated_pdf import (
    DEFAULT_MAX_MATCH_DISTANCE,
    RecoveredMapping,
    parse_annotated_pdf,
)

DEFAULT_MIN_SUPPORT = 2

# domain_inference_source values counted as trustworthy evidence when mining:
# an explicit mark (source is None -- the mark stated its own domain) or a
# real boxed banner. Never "builtin" or "precedent" -- those are the miner's
# own inferences and must not be allowed to corroborate themselves.
_TRUSTED_SOURCES = (None, "banner")


def _blank_counterpart(pdf: Path, blank_pdf_dir: Path | None) -> Path | None:
    if blank_pdf_dir is None:
        return None
    candidate = blank_pdf_dir / pdf.name
    return candidate if candidate.exists() else None


def _run_corpus(
    pdf_dir: Path,
    blank_pdf_dir: Path | None,
    max_match_distance: float,
    precedent: dict[str, str] | None,
) -> list[RecoveredMapping]:
    out: list[RecoveredMapping] = []
    for pdf in sorted(pdf_dir.glob("*.pdf")):
        blank = _blank_counterpart(pdf, blank_pdf_dir)
        mappings = parse_annotated_pdf(pdf, blank, max_match_distance, precedent)
        out.extend(replace(m, source_pdf=pdf.name) for m in mappings)
    return out


def build_variable_domain_precedent(
    mappings: list[RecoveredMapping], min_support: int = DEFAULT_MIN_SUPPORT
) -> dict[str, str]:
    """Majority-vote variable -> domain table, built from trustworthy evidence only.

    Only counts a mapping toward a variable's tally when its domain came
    from the mark's own text or a real boxed banner
    (``domain_inference_source in (None, "banner")``) -- never from the
    built-in constants tier or a prior precedent pass, so re-running this
    over the miner's own output cannot manufacture false corroboration.
    ``min_support`` independent, corroborating occurrences are required
    before a variable enters the table, so one mislabeled historical page
    can't become precedent on its own.
    """
    votes: dict[str, Counter] = defaultdict(Counter)
    for m in mappings:
        if m.kind != "variable" or not m.variable or not m.domain:
            continue
        if m.domain_inference_source not in _TRUSTED_SOURCES:
            continue
        votes[m.variable][m.domain] += 1

    precedent: dict[str, str] = {}
    for variable, counter in votes.items():
        domain, count = counter.most_common(1)[0]
        if count >= min_support:
            precedent[variable] = domain
    return precedent


def mine_corpus(
    pdf_dir: str | Path,
    blank_pdf_dir: str | Path | None = None,
    max_match_distance: float = DEFAULT_MAX_MATCH_DISTANCE,
    min_support: int = DEFAULT_MIN_SUPPORT,
) -> list[RecoveredMapping]:
    """Mine every PDF in `pdf_dir` for SDTM precedent -- see module docstring.

    `blank_pdf_dir`, if given, is searched for a same-named counterpart to
    each annotated PDF (the un-annotated original, for more reliable field
    detection -- mirrors `parse_annotated_pdf`'s own `blank_pdf_path`).
    """
    pdf_dir = Path(pdf_dir)
    blank_dir = Path(blank_pdf_dir) if blank_pdf_dir else None

    pass1 = _run_corpus(pdf_dir, blank_dir, max_match_distance, precedent=None)
    precedent = build_variable_domain_precedent(pass1, min_support)
    return _run_corpus(pdf_dir, blank_dir, max_match_distance, precedent=precedent)


def _lookup_key(m: RecoveredMapping) -> tuple[str, str, str, str, str]:
    return (
        (m.label or "").strip().lower(),
        m.domain or "",
        m.variable or "",
        m.condition or "",
        m.fixed_value or "",
    )


def build_lookup_table(mappings: list[RecoveredMapping]) -> list[dict]:
    """Deduplicated corpus-wide reference table: label/context -> mapping precedent.

    Extends ``parse_annotated_pdf.to_lookup_rows``'s single-file shape with a
    ``count`` (independent corroborating occurrences across the corpus) and
    ``sample_pdfs`` (traceability -- which source files contributed) column.
    Only matched, variable-kind mappings are included, same restriction as
    the single-file lookup table and for the same reason: an unmatched mark
    has no label to key a lookup row on.
    """
    grouped: dict[tuple, dict] = {}
    for m in mappings:
        if m.kind != "variable" or m.field_id is None:
            continue
        key = _lookup_key(m)
        row = grouped.setdefault(
            key,
            {
                "label": m.label or "",
                "context": m.context or "",
                "domain": m.domain or "",
                "variable": m.variable or "",
                "condition": m.condition or "",
                "fixed_value": m.fixed_value or "",
                "count": 0,
                "sample_pdfs": [],
            },
        )
        row["count"] += 1
        if m.source_pdf and m.source_pdf not in row["sample_pdfs"]:
            row["sample_pdfs"].append(m.source_pdf)

    rows = sorted(grouped.values(), key=lambda r: (-r["count"], r["label"]))
    for row in rows:
        row["sample_pdfs"] = "; ".join(row["sample_pdfs"])
    return rows


CORPUS_LOOKUP_COLUMNS = [
    "label", "context", "domain", "variable", "condition", "fixed_value",
    "count", "sample_pdfs",
]


def write_corpus_lookup_csv(mappings: list[RecoveredMapping], out_path: str | Path) -> Path:
    """Write the corpus-wide reference table -- see :func:`build_lookup_table`."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CORPUS_LOOKUP_COLUMNS)
        writer.writeheader()
        writer.writerows(build_lookup_table(mappings))
    return out_path


def read_corpus_lookup_csv(path: str | Path) -> list[dict]:
    """Read a corpus lookup table back, e.g. for ``prompt.build_precedent_appendix``."""
    with Path(path).open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


__all__ = [
    "CORPUS_LOOKUP_COLUMNS",
    "DEFAULT_MIN_SUPPORT",
    "build_lookup_table",
    "build_variable_domain_precedent",
    "mine_corpus",
    "read_corpus_lookup_csv",
    "write_corpus_lookup_csv",
]
