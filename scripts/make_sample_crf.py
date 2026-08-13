#!/usr/bin/env python3
"""Generate the synthetic blank CRF used to test the pipeline.

Task order step 2.

This draws a fake CRF from nothing -- an invented protocol, invented fields,
no study data of any kind. It exists so extract.py and everything downstream
can be tested without a real CRF anywhere near the repo. Every page is stamped
SYNTHETIC so a stray copy identifies itself on sight.

Three variants, because extract.py has to cope with all of them:

  SYNTHETIC_sample_crf_acroform.pdf  real AcroForm widgets (the good case)
  SYNTHETIC_sample_crf_flat.pdf      the same file, widgets baked into page
                                     content: boxed fields, no AcroForm
  SYNTHETIC_sample_crf_ruled.pdf     the other flat morphology -- text fields
                                     drawn as fill-in-the-blank underlines
                                     rather than boxes

The flat variant is produced by ``bake()``-ing the AcroForm one rather than
being drawn separately, so the two are geometrically identical by construction
and "do both extraction paths agree?" becomes a real assertion. Two things
this depends on, both verified rather than assumed:

* A widget with no border bakes to *nothing*. Widgets must carry
  ``border_width=1`` or the flat variant is a blank page with labels on it.
* Baking insets coordinates by half the border width (a widget at x0=150.0
  bakes to a drawn rect at 150.5), because the stroke is centred on the path.
  Tests comparing AcroForm to flat need a ~1pt tolerance, not equality.

Layout is deliberately adversarial in specific ways:

* **Vertically asymmetric.** Each page has a field near the very top
  (y_fitz ~50) and one near the very bottom (y_fitz ~760). A y-flip bug lands
  a mid-page field roughly where it belongs and looks fine; it puts these two
  in each other's place, which is unmissable.
* **Non-square bboxes** throughout, so an x/y transposition shows up.
* **False-positive distractors.** Section rules and the footer rule are long
  thin horizontal lines that are *not* fields. Without them a naive "every
  horizontal line is a fill-in blank" detector passes and proves nothing.
* **Both label geometries.** Demographics labels sit to the left of their
  field; the Vital Signs grid puts them in a column header above. Those are
  two different association heuristics.
* **Non-mapped text** (page numbers, form version) for the ``NotSubmitted``
  origin case.
* **A --TESTCD grid** (SYSBP/DIABP/PULSE/TEMP/RESP), which is what exercises
  the ``VSORRES when VSTESTCD = SYSBP`` condition pattern.

Deliberately *not* included yet: a rotated page. Realistic, and brutal on
coordinate handling, but debugging rotation and the y-flip simultaneously is a
bad trade. Add it once the unrotated path is proven.

The truth file
--------------
``sample_crf_truth.json`` is a serialised ``FieldSet`` holding the exact bbox
and label of every field drawn, in PDF user space. The script knows all of it
at draw time, so discarding it would force the step-3 test to re-derive
expected coordinates by hand.

It is the only artifact here that gets committed. The PDFs never do -- they
are regenerated on demand. Committing the numbers breaks the circularity that
would otherwise make the step-3 test worthless: if this generator and
extract.py were both written against the same wrong assumption, they would
agree with each other and the test would pass. A committed truth file fails
loudly the moment the generated layout drifts from it.

Usage:
    python scripts/make_sample_crf.py [--out-dir fixtures]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402

from pipeline.geometry import fitz_rect_to_bbox  # noqa: E402
from pipeline.models import CRFField, FieldSet, FieldSource, PageGeometry  # noqa: E402

PAGE_W, PAGE_H = 612.0, 792.0
BANNER = "SYNTHETIC TEST DATA - NOT A REAL CRF"
PROTOCOL = "Protocol SYNTH-001   Study: Synthetic Demonstration   Subject: ________"

FONT, FONT_BOLD = "helv", "hebo"
INK = (0.0, 0.0, 0.0)
GRAY = (0.45, 0.45, 0.45)
WARN = (0.70, 0.15, 0.15)

# Fixed so the committed truth file is byte-stable across regenerations.
# This is a generation marker, not a real extraction time.
TRUTH_TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

RectT = tuple[float, float, float, float]


# --------------------------------------------------------------------------
# Layout description -- one source of truth for drawing *and* the truth file
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    name: str  # AcroForm field name, also used as field_id
    kind: str  # "text" | "checkbox"
    rect: RectT  # fitz coords: top-left origin, y down
    label: str
    context: str


@dataclass(frozen=True)
class Label:
    xy: tuple[float, float]  # text baseline origin, fitz coords
    text: str
    size: float = 9.0
    bold: bool = False
    color: tuple[float, float, float] = INK


@dataclass(frozen=True)
class Rule:
    """A drawn horizontal line that is NOT a field -- a detector distractor."""

    y: float
    x0: float = 72.0
    x1: float = 540.0


@dataclass(frozen=True)
class PageSpec:
    index: int
    labels: list[Label] = dc_field(default_factory=list)
    rules: list[Rule] = dc_field(default_factory=list)
    fields: list[Field] = dc_field(default_factory=list)


def _chrome(page_no: int, form: str) -> tuple[list[Label], list[Rule]]:
    """Banner, protocol line, footer -- present on every page."""
    return (
        [
            Label((72, 26), BANNER, size=8, bold=True, color=WARN),
            Label((72, 44), PROTOCOL, size=8, color=GRAY),
            # Non-mapped text: these should come back as NotSubmitted.
            Label((72, 775), f"Form {form} v1.0", size=8, color=GRAY),
            Label((500, 775), f"Page {page_no} of 2", size=8, color=GRAY),
        ],
        [Rule(730.0)],  # footer rule -- distractor
    )


def demographics_page() -> PageSpec:
    labels, rules = _chrome(1, "DM")
    labels += [
        Label((72, 92), "DEMOGRAPHICS", size=14, bold=True),
        Label((420, 62), "Site ID:"),
        Label((72, 134), "Subject Identifier:"),
        Label((72, 164), "Date of Birth (DD/MMM/YYYY):"),
        Label((72, 194), "Age at Consent:"),
        Label((262, 194), "years", color=GRAY),
        Label((72, 224), "Sex:"),
        Label((218, 222), "Male"),
        Label((296, 222), "Female"),
        Label((72, 254), "Race:"),
        Label((218, 252), "White"),
        Label((218, 272), "Black or African American"),
        Label((218, 292), "Asian"),
        Label((218, 312), "American Indian or Alaska Native"),
        Label((72, 344), "Ethnicity:"),
        Label((218, 342), "Hispanic or Latino"),
        Label((218, 362), "Not Hispanic or Latino"),
        Label((72, 394), "Country of Enrollment:"),
        Label((72, 757), "Investigator Initials:"),
    ]
    rules += [Rule(100.0)]  # section rule -- distractor

    ctx = "Demographics"
    fields = [
        # Top outlier -- pairs with the bottom outlier to expose a y-flip.
        Field("DM_SITEID", "text", (470, 50, 570, 66), "Site ID", f"{ctx}; page header"),
        Field("DM_USUBJID", "text", (200, 122, 400, 138), "Subject Identifier", ctx),
        # Three adjacent blanks sharing one label.
        Field("DM_BRTHDTC_DD", "text", (200, 152, 235, 168),
              "Date of Birth (DD/MMM/YYYY)", f"{ctx}; day component"),
        Field("DM_BRTHDTC_MMM", "text", (245, 152, 290, 168),
              "Date of Birth (DD/MMM/YYYY)", f"{ctx}; month component"),
        Field("DM_BRTHDTC_YYYY", "text", (300, 152, 355, 168),
              "Date of Birth (DD/MMM/YYYY)", f"{ctx}; year component"),
        Field("DM_AGE", "text", (200, 182, 250, 198), "Age at Consent",
              f"{ctx}; units printed as 'years'"),
        Field("DM_SEX_M", "checkbox", (200, 212, 212, 224), "Male", f"{ctx}; Sex"),
        Field("DM_SEX_F", "checkbox", (278, 212, 290, 224), "Female", f"{ctx}; Sex"),
        Field("DM_RACE_WHITE", "checkbox", (200, 242, 212, 254), "White", f"{ctx}; Race"),
        Field("DM_RACE_BLACK", "checkbox", (200, 262, 212, 274),
              "Black or African American", f"{ctx}; Race"),
        Field("DM_RACE_ASIAN", "checkbox", (200, 282, 212, 294), "Asian", f"{ctx}; Race"),
        Field("DM_RACE_AIAN", "checkbox", (200, 302, 212, 314),
              "American Indian or Alaska Native", f"{ctx}; Race"),
        Field("DM_ETHNIC_HL", "checkbox", (200, 332, 212, 344),
              "Hispanic or Latino", f"{ctx}; Ethnicity"),
        Field("DM_ETHNIC_NHL", "checkbox", (200, 352, 212, 364),
              "Not Hispanic or Latino", f"{ctx}; Ethnicity"),
        Field("DM_COUNTRY", "text", (200, 382, 360, 398),
              "Country of Enrollment", ctx),
        # Bottom outlier.
        Field("DM_INVINIT", "text", (200, 745, 300, 761), "Investigator Initials",
              f"{ctx}; page footer"),
    ]
    return PageSpec(index=0, labels=labels, rules=rules, fields=fields)


VS_ROWS = [
    ("SYSBP", "Systolic Blood Pressure", "mmHg"),
    ("DIABP", "Diastolic Blood Pressure", "mmHg"),
    ("PULSE", "Pulse Rate", "beats/min"),
    ("TEMP", "Body Temperature", "C"),
    ("RESP", "Respiratory Rate", "breaths/min"),
]


def vital_signs_page() -> PageSpec:
    labels, rules = _chrome(2, "VS")
    labels += [
        Label((72, 92), "VITAL SIGNS", size=14, bold=True),
        Label((398, 62), "Visit Date:"),
        # Column headers: label ABOVE the field, unlike page 1.
        Label((72, 140), "Assessment", bold=True),
        Label((250, 140), "Result", bold=True),
        Label((360, 140), "Unit", bold=True),
        Label((440, 140), "Not Done", bold=True),
        Label((72, 340), "Position:"),
        Label((218, 338), "Sitting"),
        Label((298, 338), "Supine"),
        Label((378, 338), "Standing"),
        Label((72, 757), "Assessor Initials:"),
    ]
    rules += [Rule(100.0), Rule(146.0)]  # section + header rules -- distractors

    ctx = "Vital Signs"
    fields = [
        # Top outlier.
        Field("VS_VISITDAT", "text", (470, 50, 570, 66),
              "Visit Date", f"{ctx}; page header"),
    ]

    for i, (testcd, test_label, unit) in enumerate(VS_ROWS):
        y0 = 155.0 + i * 30.0
        y1 = y0 + 16.0
        labels.append(Label((72, y0 + 12), test_label))
        labels.append(Label((360, y0 + 12), unit, color=GRAY))
        row_ctx = f"{ctx}; row '{test_label}' ({unit}); grid column 'Result'"
        fields.append(
            Field(f"VS_{testcd}_RES", "text", (250, y0, 340, y1), test_label, row_ctx)
        )
        fields.append(
            Field(
                f"VS_{testcd}_ND",
                "checkbox",
                (440, y0 + 2, 452, y0 + 14),
                "Not Done",
                f"{ctx}; row '{test_label}'; grid column 'Not Done'",
            )
        )

    fields += [
        Field("VS_POS_SIT", "checkbox", (200, 328, 212, 340), "Sitting", f"{ctx}; Position"),
        Field("VS_POS_SUP", "checkbox", (280, 328, 292, 340), "Supine", f"{ctx}; Position"),
        Field("VS_POS_STA", "checkbox", (360, 328, 372, 340), "Standing", f"{ctx}; Position"),
        # Bottom outlier.
        Field("VS_ASSINIT", "text", (200, 745, 300, 761), "Assessor Initials",
              f"{ctx}; page footer"),
    ]
    return PageSpec(index=1, labels=labels, rules=rules, fields=fields)


def layout() -> list[PageSpec]:
    return [demographics_page(), vital_signs_page()]


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------


def _draw_static(page: "pymupdf.Page", spec: PageSpec) -> None:
    """Labels and rules -- identical across all three variants."""
    for rule in spec.rules:
        page.draw_line(
            pymupdf.Point(rule.x0, rule.y),
            pymupdf.Point(rule.x1, rule.y),
            color=GRAY,
            width=0.75,
        )
    for lab in spec.labels:
        page.insert_text(
            pymupdf.Point(*lab.xy),
            lab.text,
            fontname=FONT_BOLD if lab.bold else FONT,
            fontsize=lab.size,
            color=lab.color,
        )


def _add_widget(page: "pymupdf.Page", f: Field) -> None:
    w = pymupdf.Widget()
    w.field_name = f.name
    w.rect = pymupdf.Rect(*f.rect)
    # Required: a border-less widget bakes to nothing, leaving the flat
    # variant with no detectable geometry at all.
    w.border_width = 1
    w.border_color = INK
    if f.kind == "checkbox":
        w.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
        w.field_value = False
    else:
        w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        w.field_value = ""
        w.text_fontsize = 9
    page.add_widget(w)


def _draw_ruled_field(page: "pymupdf.Page", f: Field) -> None:
    """The other flat morphology: fill-in blanks as underlines, not boxes."""
    x0, y0, x1, y1 = f.rect
    if f.kind == "checkbox":
        page.draw_rect(pymupdf.Rect(x0, y0, x1, y1), color=INK, width=0.9)
    else:
        page.draw_line(
            pymupdf.Point(x0, y1), pymupdf.Point(x1, y1), color=INK, width=0.9
        )


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
        _draw_static(page, spec)
        for f in spec.fields:
            _add_widget(page, f)
    return doc


def build_ruled_crf() -> "pymupdf.Document":
    doc = _new_doc()
    for spec in layout():
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        _draw_static(page, spec)
        for f in spec.fields:
            _draw_ruled_field(page, f)
    return doc


# --------------------------------------------------------------------------
# Truth
# --------------------------------------------------------------------------


def build_truth(source_pdf: str) -> FieldSet:
    """Exact geometry of every field drawn, in PDF user space."""
    specs = layout()
    return FieldSet(
        source_pdf=source_pdf,
        pages=[
            PageGeometry(page_index=s.index, width=PAGE_W, height=PAGE_H, rotation=0)
            for s in specs
        ],
        fields=[
            CRFField(
                field_id=f.name,
                page_index=s.index,
                bbox=fitz_rect_to_bbox(f.rect, PAGE_H),
                label=f.label,
                source=FieldSource.ACROFORM,
                context=f.context,
                acroform_name=f.name,
            )
            for s in specs
            for f in s.fields
        ],
        extracted_at=TRUTH_TIMESTAMP,
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def make_sample_crf(out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    acro_path = out_dir / "SYNTHETIC_sample_crf_acroform.pdf"
    doc = build_acroform_crf()
    doc.save(acro_path, garbage=4, deflate=True)
    written["acroform"] = acro_path

    # Flat = the same document with widgets baked into page content, so the
    # two variants are geometrically identical by construction.
    doc.bake(widgets=True)
    flat_path = out_dir / "SYNTHETIC_sample_crf_flat.pdf"
    doc.save(flat_path, garbage=4, deflate=True)
    written["flat"] = flat_path
    doc.close()

    ruled_path = out_dir / "SYNTHETIC_sample_crf_ruled.pdf"
    ruled = build_ruled_crf()
    ruled.save(ruled_path, garbage=4, deflate=True)
    ruled.close()
    written["ruled"] = ruled_path

    truth_path = out_dir / "sample_crf_truth.json"
    truth = build_truth(acro_path.name)
    truth_path.write_text(truth.model_dump_json(indent=2), encoding="utf-8")
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
    n_fields = sum(len(s.fields) for s in layout())
    for kind, path in written.items():
        print(f"{kind:9} {path}")
    print(f"\n{n_fields} fields across 2 pages")
    print("PDFs are regenerated, not committed; only sample_crf_truth.json is tracked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
