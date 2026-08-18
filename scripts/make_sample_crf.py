#!/usr/bin/env python3
"""Generate the synthetic blank CRF used to test the pipeline.

This draws a fake CRF from nothing -- an invented protocol, invented questions,
no study data of any kind. It exists so ``pipeline/rows.py`` and everything
downstream can be tested without a real CRF anywhere near the repo. Every page
is stamped SYNTHETIC so a stray copy identifies itself on sight.

Two variants, and the point of having two has changed:

  SYNTHETIC_sample_crf_twocol_acroform.pdf   real AcroForm widgets
  SYNTHETIC_sample_crf_twocol_flat.pdf       the same file, widgets baked in

Under the old field-detection design these exercised two different detection
paths. Under the two-column row model they exercise a stronger claim: extraction
reads *text*, so the two variants must produce **identical rows**. Widgets and
their baked outlines contribute no text, and the assertion is that they are
ignored -- not coped with. The flat variant is produced by ``bake()``-ing the
AcroForm one, so any difference can only come from the widgets themselves.

Layout is deliberately adversarial in specific ways
---------------------------------------------------
* **Two-column morphology throughout**, which is the assumption under test:
  question text in the left column at ``COL1_X``, response text right-aligned
  to ``COL2_RIGHT``, response widgets to the right of that again.
* **Both option geometries.** Ethnicity puts its options in the *right* column
  ("Hispanic or Latino" right-aligned, radio beside it). Race puts them in the
  *left* column with only a checkbox on the right. Those assemble into rows
  differently and both are real.
* **A wrapped question** printed on two lines at the same indent, which must
  merge into one row -- annotating half a sentence is worse than useless.
* **Option-only continuation rows** ("No", "Female", "Not Reported",
  "Unknown") with an empty left column.
* **A full-width note crossing the gutter**, which must not erase the corridor
  it crosses and must still survive as a row -- a spanning note is frequently
  the thing that needs annotating.
* **Vertically asymmetric.** Real content sits near the very top (y_fitz ~60)
  and near the very bottom (y_fitz ~757). A y-flip bug lands a mid-page row
  roughly where it belongs and looks plausible; it puts these two in each
  other's place, which is unmissable.
* **A single-column page 3.** ``gutter_x`` must come back ``None`` there, and
  the document-median fallback must decline to shear it in half. Its page
  number is right-aligned on purpose, so passing requires
  ``rows.splits_into_columns`` actually working rather than the trivial
  no-text-on-the-right case.
* **A --TESTCD grid** on page 2 (SYSBP/DIABP/PULSE/TEMP/RESP), which is what
  exercises the ``VSORRES when VSTESTCD = SYSBP`` condition pattern.
* **Non-mapped page furniture** (page numbers, form version) for the
  ``NotSubmitted`` origin case.

Deliberately *not* included yet: a rotated page. Realistic, and brutal on
coordinate handling, but debugging rotation and the y-flip simultaneously is a
bad trade. Add it once the unrotated path is proven.

The truth file
--------------
``sample_crf_rows_truth.json`` commits the generator's **inputs**, not the
extractor's outputs: for every row, the text of each column, the form it
belongs to, whether it spans the gutter, and the exact x the text was inserted
at plus its baseline y. Per page it also commits the bounds the detected gutter
must fall between, measured from real font metrics at draw time.

It deliberately does *not* commit extracted bboxes. A glyph's vertical extent
comes from PyMuPDF's font metrics rather than from anything this script
chooses, so committing it would pin a library internal instead of a fact about
the layout. Committing the insertion anchors and asserting extraction recovers
them tests the same thing without that coupling.

This is the only artifact here that gets committed. The PDFs never do -- they
are regenerated on demand. Committing the numbers breaks the circularity that
would otherwise make the extraction tests worthless: if this generator and
``rows.py`` were both written against the same wrong assumption, they would
agree with each other and the tests would pass while proving nothing. A
committed truth file fails loudly the moment the generated layout drifts.

Usage:
    python scripts/make_sample_crf.py [--out-dir fixtures]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402

PAGE_W, PAGE_H = 612.0, 792.0
BANNER = "SYNTHETIC TEST DATA - NOT A REAL CRF"
PROTOCOL = "Protocol SYNTH-001   Subject: ________"

FONT, FONT_BOLD = "helv", "hebo"
INK = (0.0, 0.0, 0.0)
GRAY = (0.45, 0.45, 0.45)
WARN = (0.70, 0.15, 0.15)

BODY_SIZE = 9.0
SMALL_SIZE = 7.5
HEADING_SIZE = 11.5

# Column geometry -- the assumption made concrete.
COL1_X = 90.0  # question column, left-aligned
COL2_RIGHT = 470.0  # response column, right-aligned to this edge
CHECK_X0, CHECK_SIDE = 478.0, 10.0  # radio/checkbox, right of the response text
BLANK_X0, BLANK_X1 = 398.0, 470.0  # fill-in underline, inside column 2
TEXTBOX_H = 12.0

RectT = tuple[float, float, float, float]


def _width(text: str, size: float, bold: bool = False) -> float:
    """Advance width of ``text``, from the same font metrics PyMuPDF will draw."""
    return pymupdf.get_text_length(text, fontname=FONT_BOLD if bold else FONT, fontsize=size)


# --------------------------------------------------------------------------
# Layout description -- one source of truth for drawing *and* the truth file
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    """One printed line, described in two-column terms.

    ``col1`` is inserted at ``COL1_X`` (or ``indent`` when given); ``col2`` is
    right-aligned so its last glyph lands on ``COL2_RIGHT``, which is what makes
    the response column a column rather than a coincidence.
    """

    y: float  # text baseline, fitz coords (y down)
    col1: str = ""
    col2: str = ""
    size: float = BODY_SIZE
    bold: bool = False
    color: tuple[float, float, float] = INK
    indent: Optional[float] = None
    #: A checkbox/radio to the right of the response column.
    check: bool = False
    #: A fill-in underline inside the response column.
    blank: bool = False
    #: An AcroForm text box instead of an underline, same rect.
    widget_name: Optional[str] = None
    #: Expected to fold into the line above it as one wrapped question.
    merges_up: bool = False
    #: Expected to be flagged ``full_width`` -- crosses the gutter.
    spans: bool = False

    @property
    def x1_start(self) -> float:
        return self.indent if self.indent is not None else COL1_X

    @property
    def col1_x1(self) -> float:
        return self.x1_start + _width(self.col1, self.size, self.bold) if self.col1 else 0.0

    @property
    def x2_start(self) -> float:
        return COL2_RIGHT - _width(self.col2, self.size, self.bold)

    def widget_rect(self) -> Optional[tuple[RectT, str]]:
        """Rect and kind of the response control on this line, if any."""
        if self.check:
            top = self.y - CHECK_SIDE + 1.0
            return ((CHECK_X0, top, CHECK_X0 + CHECK_SIDE, top + CHECK_SIDE), "checkbox")
        if self.blank or self.widget_name:
            return ((BLANK_X0, self.y - TEXTBOX_H + 2.0, BLANK_X1, self.y + 2.0), "text")
        return None


@dataclass(frozen=True)
class Rule:
    """A drawn horizontal line that is not a field.

    Kept from the old fixture even though nothing detects lines any more: a
    section rule is exactly the kind of page furniture that used to be
    misread as a fill-in blank, and its continued presence is the standing
    check that nothing has quietly started looking at drawn geometry again.
    """

    y: float
    x0: float = COL1_X
    x1: float = 540.0


@dataclass
class PageSpec:
    index: int
    form: str
    lines: list[Line] = dc_field(default_factory=list)
    rules: list[Rule] = dc_field(default_factory=list)
    #: True when this page is expected to yield ``gutter_x is None``.
    single_column: bool = False


def _chrome(page_no: int, n_pages: int, form: str) -> list[Line]:
    """Banner, protocol line and footer -- present on every page.

    The footer is a genuine two-column line: form version at the left, page
    number right-aligned into the response column. That keeps page furniture
    from being a special case, and on the single-column page it is what makes
    the ``splits_into_columns`` guard load-bearing.
    """
    return [
        Line(y=30.0, col1=BANNER, size=8.0, bold=True, color=WARN),
        Line(y=44.0, col1=PROTOCOL, size=8.0, color=GRAY),
        Line(y=58.0, col1=f"Form: {form}", size=8.0, bold=True),
        Line(
            y=770.0,
            col1=f"Form {form} v1.0",
            col2=f"Page {page_no} of {n_pages}",
            size=8.0,
            color=GRAY,
        ),
    ]


def demographics_page() -> PageSpec:
    lines = _chrome(1, 3, "Demographics")
    lines += [
        # Top outlier -- pairs with the bottom outlier to expose a y-flip.
        Line(y=80.0, col1="Site Identifier", blank=True, widget_name="DM_SITEID"),
        Line(y=104.0, col1="Was this participant a prior screen failure?", col2="Yes", check=True),
        Line(y=118.0, col2="No", check=True),
        # A wrapped question: two lines, same indent, tight leading -> one row.
        Line(y=142.0, col1="If Yes, please provide the original", indent=95.0),
        Line(
            y=152.0,
            col1="participant number (xxxxx-xxxx)",
            indent=95.0,
            merges_up=True,
            blank=True,
            widget_name="DM_PSUBJID",
        ),
        Line(y=182.0, col1="DEMOGRAPHICS", size=HEADING_SIZE, bold=True),
        Line(y=206.0, col1="Year of Birth (yyyy)", blank=True, widget_name="DM_BRTHDTC"),
        Line(
            y=230.0,
            col1="Age (years) at time of consent",
            col2="Fixed Unit: years",
            blank=True,
            widget_name="DM_AGE",
        ),
        Line(y=254.0, col1="Sex", col2="Male", check=True),
        Line(y=268.0, col2="Female", check=True),
        Line(
            y=292.0,
            col1="Is the participant of childbearing potential?",
            col2="Yes",
            check=True,
        ),
        Line(y=306.0, col2="No", check=True),
        # Options in the RIGHT column, right-aligned.
        Line(y=330.0, col1="Ethnicity", col2="Hispanic or Latino", check=True),
        Line(y=344.0, col2="Not Hispanic or Latino", check=True),
        Line(y=358.0, col2="Not Reported", check=True),
        Line(y=372.0, col2="Unknown", check=True),
        Line(y=400.0, col1="Race (check all that apply)"),
        # Full-width note crossing the gutter: must not erase the corridor,
        # must still come back as a row.
        Line(
            y=414.0,
            col1=(
                "When multiple values are selected then RACE = MULTIPLE and individual "
                "responses are RACE1, RACE2, RACEn in SUPPDM"
            ),
            size=SMALL_SIZE,
            spans=True,
        ),
        # Options in the LEFT column, checkbox only on the right.
        Line(y=436.0, col1="American Indian or Alaska Native", indent=95.0, check=True),
        Line(y=450.0, col1="Asian", indent=95.0, check=True),
        Line(y=464.0, col1="Black or African American", indent=95.0, check=True),
        Line(y=478.0, col1="White", indent=95.0, check=True),
        Line(y=506.0, col1="Country of Enrollment", blank=True, widget_name="DM_COUNTRY"),
        # Bottom outlier.
        Line(y=757.0, col1="Investigator Initials", blank=True, widget_name="DM_INVINIT"),
    ]
    return PageSpec(
        index=0,
        form="Demographics",
        lines=lines,
        rules=[Rule(190.0), Rule(740.0)],
    )


VS_ROWS = [
    ("SYSBP", "Systolic Blood Pressure", "mmHg"),
    ("DIABP", "Diastolic Blood Pressure", "mmHg"),
    ("PULSE", "Pulse Rate", "beats/min"),
    ("TEMP", "Body Temperature", "C"),
    ("RESP", "Respiratory Rate", "breaths/min"),
]


def vital_signs_page() -> PageSpec:
    lines = _chrome(2, 3, "Vital Signs")
    lines += [
        Line(y=80.0, col1="Visit Date", blank=True, widget_name="VS_VISITDAT"),
        Line(y=110.0, col1="VITAL SIGNS", size=HEADING_SIZE, bold=True),
    ]
    for i, (testcd, test_label, unit) in enumerate(VS_ROWS):
        lines.append(
            Line(
                y=140.0 + i * 24.0,
                col1=test_label,
                col2=unit,
                blank=True,
                widget_name=f"VS_{testcd}_RES",
            )
        )
    lines += [
        Line(y=290.0, col1="Position", col2="Sitting", check=True),
        Line(y=304.0, col2="Supine", check=True),
        Line(y=318.0, col2="Standing", check=True),
        Line(y=757.0, col1="Assessor Initials", blank=True, widget_name="VS_ASSINIT"),
    ]
    return PageSpec(
        index=1, form="Vital Signs", lines=lines, rules=[Rule(120.0), Rule(740.0)]
    )


INSTRUCTION_PARAGRAPHS = [
    "Complete every page of this form in indelible black ink. Do not use correction fluid of",
    "any kind. If an entry requires correction, strike through the original value with a single",
    "line, enter the corrected value alongside it, and initial and date the change so that the",
    "original entry remains legible for the duration of the retention period.",
    "Where a measurement was not obtained, record the reason in the comment field rather",
    "than leaving the entry blank. A blank entry cannot be distinguished from an omission",
    "during data review and will be raised as a query against the site.",
]


def instructions_page() -> PageSpec:
    """A genuinely single-column page -- ``gutter_x`` must come back ``None``.

    Every paragraph line is drawn wide enough to exceed
    ``rows.FULL_WIDTH_FRACTION`` of the page, so gutter detection sets them
    aside, and the only remaining text on the right-hand side is the footer's
    page number. One run there is below ``rows.MIN_COLUMN_RUNS``, which is the
    guard this page exists to exercise: it must decline both its own detection
    and the borrowed document median.
    """
    lines = _chrome(3, 3, "Instructions")
    lines.append(Line(y=90.0, col1="INSTRUCTIONS FOR COMPLETION", size=HEADING_SIZE, bold=True))
    for i, para in enumerate(INSTRUCTION_PARAGRAPHS):
        lines.append(Line(y=120.0 + i * 16.0, col1=para, size=BODY_SIZE, spans=True))
    return PageSpec(
        index=2,
        form="Instructions",
        lines=lines,
        rules=[Rule(100.0), Rule(740.0)],
        single_column=True,
    )


def layout() -> list[PageSpec]:
    return [demographics_page(), vital_signs_page(), instructions_page()]


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------


def _draw_text(page: "pymupdf.Page", spec: PageSpec) -> None:
    for rule in spec.rules:
        page.draw_line(
            pymupdf.Point(rule.x0, rule.y),
            pymupdf.Point(rule.x1, rule.y),
            color=GRAY,
            width=0.75,
        )
    for line in spec.lines:
        font = FONT_BOLD if line.bold else FONT
        if line.col1:
            page.insert_text(
                pymupdf.Point(line.x1_start, line.y),
                line.col1,
                fontname=font,
                fontsize=line.size,
                color=line.color,
            )
        if line.col2:
            page.insert_text(
                pymupdf.Point(line.x2_start, line.y),
                line.col2,
                fontname=font,
                fontsize=line.size,
                color=line.color,
            )


def _add_widget(page: "pymupdf.Page", line: Line, name: str) -> None:
    control = line.widget_rect()
    if control is None:
        return
    rect, kind = control
    w = pymupdf.Widget()
    w.field_name = name
    w.rect = pymupdf.Rect(*rect)
    # Required: a border-less widget bakes to *nothing*, which would leave the
    # flat variant with no drawn geometry at all and make the two variants
    # trivially equal for the wrong reason.
    w.border_width = 1
    w.border_color = INK
    if kind == "checkbox":
        w.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
        w.field_value = False
    else:
        w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        w.field_value = ""
        w.text_fontsize = 9
    page.add_widget(w)


def _new_doc() -> "pymupdf.Document":
    doc = pymupdf.open()
    doc.set_metadata(
        {
            "title": "SYNTHETIC Sample CRF",
            "subject": "Synthetic test data - not a real CRF",
            "author": "carf scripts/make_sample_crf.py",
            "keywords": "synthetic, test, not-real-data",
            "creationDate": "",
            "modDate": "",
        }
    )
    return doc


def build_acroform_crf() -> "pymupdf.Document":
    doc = _new_doc()
    for spec in layout():
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        _draw_text(page, spec)
        for i, line in enumerate(spec.lines):
            if line.widget_rect() is None:
                continue
            name = line.widget_name or f"p{spec.index + 1}_ctl{i + 1:03d}"
            _add_widget(page, line, name)
    return doc


# --------------------------------------------------------------------------
# Truth
# --------------------------------------------------------------------------


def _norm(text: str) -> str:
    """Collapse whitespace, as extraction does.

    PyMuPDF returns words, not the source string, so runs of spaces in a label
    never survive round-tripping through a PDF. The truth file has to expect the
    collapsed form or it asserts something no extractor could satisfy.
    """
    return " ".join(text.split())


def expected_rows(spec: PageSpec) -> list[dict]:
    """The rows ``pipeline.rows`` should recover from this page.

    Built by applying the extractor's *rules* to the layout description, never
    by running the extractor -- that is what makes this a check rather than a
    tautology. Three rules matter:

    * a ``merges_up`` line folds into the one above it (one wrapped question);
    * an empty ``col1`` stays an empty left half (an option-only row);
    * on a **single-column** page there is no gutter, so ``col2`` is not a
      separate column -- it joins ``col1`` on the same line, and nothing can be
      ``full_width`` because there is no corridor to cross.

    That last rule is easy to get wrong in the other direction: expecting a
    two-column split on a page whose ``gutter_x`` is ``None`` would demand the
    extractor invent a gutter it correctly declined to find.
    """
    rows: list[dict] = []
    for line in sorted(spec.lines, key=lambda ln: ln.y):
        if line.merges_up and rows and rows[-1]["text_2"] == "":
            prev = rows[-1]
            prev["text_1"] = _norm(f"{prev['text_1']} {line.col1}")
            continue
        if spec.single_column:
            joined = _norm(f"{line.col1} {line.col2}")
            rows.append(
                {
                    "text_1": joined,
                    "text_2": "",
                    "anchor_x_1": line.x1_start if joined else None,
                    "anchor_x_2": None,
                    "baseline_y": line.y,
                    "full_width": False,
                }
            )
            continue
        rows.append(
            {
                "text_1": _norm(line.col1),
                "text_2": _norm(line.col2),
                "anchor_x_1": line.x1_start if line.col1 else None,
                "anchor_x_2": round(line.x2_start, 3) if line.col2 else None,
                "baseline_y": line.y,
                "full_width": line.spans,
            }
        )
    for n, row in enumerate(rows, start=1):
        row["row_id"] = f"p{spec.index + 1}_r{n:03d}"
        row["page_index"] = spec.index
        row["form"] = spec.form
    return rows


def gutter_bounds(spec: PageSpec) -> Optional[dict]:
    """The interval the detected gutter must fall inside, or ``None``.

    Measured from real font metrics at draw time: the right edge of the widest
    *non-spanning* left-column text, and the left edge of the leftmost
    right-column text. Any x between them separates the two columns; anything
    outside does not. Spanning lines are excluded for the same reason
    ``rows.detect_gutter`` excludes them -- they cross the corridor by design.
    """
    if spec.single_column:
        return None
    lo = max((ln.col1_x1 for ln in spec.lines if ln.col1 and not ln.spans), default=0.0)
    hi = min((ln.x2_start for ln in spec.lines if ln.col2), default=PAGE_W)
    if lo >= hi:
        raise AssertionError(
            f"page {spec.index}: column 1 reaches x={lo:.1f} but column 2 starts at "
            f"x={hi:.1f} -- the generated layout has no gutter to detect, so the "
            "fixture itself is wrong, not the extractor"
        )
    return {"lo": round(lo, 3), "hi": round(hi, 3)}


def build_truth(source_pdf: str) -> dict:
    specs = layout()
    return {
        "source_pdf": source_pdf,
        "note": (
            "Generator inputs, committed to break circularity -- see "
            "scripts/make_sample_crf.py. Coordinates are fitz space (top-left "
            "origin, y down), matching the insert_text call that drew them."
        ),
        "page_width": PAGE_W,
        "page_height": PAGE_H,
        "pages": [
            {
                "page_index": s.index,
                "form": s.form,
                "single_column": s.single_column,
                "gutter_bounds": gutter_bounds(s),
            }
            for s in specs
        ],
        "rows": [row for s in specs for row in expected_rows(s)],
    }


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def make_sample_crf(out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    acro_path = out_dir / "SYNTHETIC_sample_crf_twocol_acroform.pdf"
    doc = build_acroform_crf()
    doc.save(acro_path, garbage=4, deflate=True)
    written["acroform"] = acro_path

    # Flat = the same document with widgets baked into page content. Rows must
    # be identical across the two, since baking adds drawn outlines but no text.
    doc.bake(widgets=True)
    flat_path = out_dir / "SYNTHETIC_sample_crf_twocol_flat.pdf"
    doc.save(flat_path, garbage=4, deflate=True)
    written["flat"] = flat_path
    doc.close()

    truth_path = out_dir / "sample_crf_rows_truth.json"
    truth_path.write_text(
        json.dumps(build_truth(acro_path.name), indent=2) + "\n", encoding="utf-8"
    )
    written["truth"] = truth_path

    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("fixtures"),
        help="output directory (default: fixtures)",
    )
    args = parser.parse_args(argv)

    written = make_sample_crf(args.out_dir)
    specs = layout()
    n_rows = sum(len(expected_rows(s)) for s in specs)
    for kind, path in written.items():
        print(f"{kind:9} {path}")
    print(f"\n{n_rows} rows across {len(specs)} pages")
    for s in specs:
        bounds = gutter_bounds(s)
        where = "single-column" if bounds is None else f"gutter in ({bounds['lo']}, {bounds['hi']})"
        print(f"  page {s.index + 1}  {s.form:14} {where}")
    print("\nPDFs are regenerated, not committed; only sample_crf_rows_truth.json is tracked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
