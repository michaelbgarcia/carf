#!/usr/bin/env python3
"""Draw a picture of the GOAL: what a finished annotated CRF should look like.

NOT PIPELINE OUTPUT. The SDTM annotations in here are hand-written by a human
to illustrate the target, not proposed by Copilot, not reviewed, and not
derived from anything. Nothing in this script may ever feed a submission
artifact. Output is named and bannered accordingly.

What is real and what is invented:

  real       field positions -- taken from extract.py against the synthetic
             CRF, so the placement logic is genuine and this doubles as a
             prototype of where stamp.py and xfdf_to_pdf.py should put text
  invented   every domain, variable, condition and codelist below

It exists to answer "what is this pipeline actually trying to produce?", which
the extraction overlay does not show: that overlay displays each field's
*caption as printed on the form* ("Subject Identifier"), whereas the finished
artifact shows its *SDTM annotation* ("DM.SUBJID"). Those are input and output
of the Copilot step respectively, and confusing them is easy.

Annotation conventions shown here, per CDISC SDTMIG v3.4:
  * a boxed domain marker per page
  * `DOMAIN.VARIABLE` beside each mapped capture field
  * the findings pattern `VSORRES when VSTESTCD = SYSBP` for test grids
  * controlled terminology codelist ids where they apply
  * `[Not Submitted]` for page furniture that maps to nothing

Usage:
    python scripts/illustrative_target.py [--out-dir build]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pymupdf  # noqa: E402

from pipeline.extract import extract_fields  # noqa: E402
from pipeline.geometry import bbox_to_fitz_rect, fitz_rect_to_bbox  # noqa: E402
from pipeline.models import (  # noqa: E402
    AnnotationKind,
    AnnotationSet,
    Origin,
    SdtmAnnotation,
)

ANNOT = (0.80, 0.05, 0.05)
BANNER = (0.75, 0.10, 0.10)
NOTICE_BG = (0.99, 0.94, 0.94)
SIZE = 6.8
FONT = "helv"
MARGIN = 8.0

MOCKUP_SOURCE = "ILLUSTRATIVE MOCKUP - hand-written by a human, not pipeline output"

# anchor, text, placement. `fixed` positions are chosen by hand to clear the
# form's own text, which is exactly the collision problem the real layout pass
# has to solve later (deferred: "collision layout for dense pages").
PAGE_ANNOTATIONS: dict[int, list[dict]] = {
    0: [
        dict(anchor="DM_SITEID", text="DM.SITEID", place="below"),
        dict(anchor="DM_USUBJID", text="DM.SUBJID", place="right"),
        dict(anchor="DM_BRTHDTC_YYYY", text="DM.BRTHDTC", place="right"),
        dict(anchor="DM_AGE", text="DM.AGE", place="fixed", at=(300, 196)),
        dict(anchor="DM_SEX_M", text="DM.SEX  (C66731)", place="fixed", at=(360, 223)),
        dict(anchor="DM_RACE_WHITE", text="DM.RACE  (C74457)", place="fixed", at=(400, 253)),
        dict(anchor="DM_ETHNIC_HL", text="DM.ETHNIC  (C66790)", place="fixed", at=(400, 343)),
        dict(anchor="DM_COUNTRY", text="DM.COUNTRY  (ISO 3166)", place="right"),
        dict(anchor="DM_INVINIT", text="[Not Submitted]", place="right"),
        dict(anchor=None, text="[Not Submitted]", place="fixed", at=(430, 778)),
    ],
    1: [
        dict(anchor="VS_VISITDAT", text="VS.VSDTC", place="below"),
        dict(anchor="VS_POS_SIT", text="VS.VSPOS  (C71148)", place="fixed", at=(440, 339)),
        dict(anchor="VS_ASSINIT", text="[Not Submitted]", place="right"),
        dict(anchor=None, text="[Not Submitted]", place="fixed", at=(430, 778)),
    ],
}

# The findings-class pattern: one variable, disambiguated by a condition.
VS_TESTS = [("SYSBP", "VS_SYSBP_RES"), ("DIABP", "VS_DIABP_RES"),
            ("PULSE", "VS_PULSE_RES"), ("TEMP", "VS_TEMP_RES"),
            ("RESP", "VS_RESP_RES")]
for testcd, anchor in VS_TESTS:
    PAGE_ANNOTATIONS[1].append(
        dict(anchor=anchor, text=f"VSORRES when VSTESTCD = {testcd}",
             place="fixed", at=(470, None))
    )

DOMAIN_MARKERS = {0: "DM", 1: "VS"}

LEGEND = [
    "VS.VSORRESU  -- units are pre-printed on the form  [Assigned]",
    "VS.VSSTAT = NOT DONE  when the Not Done box is ticked, per row VSTESTCD",
    "DM.USUBJID  -- [Derived] from SITEID + SUBJID, not collected on this form",
]


def _place(page, rect: pymupdf.Rect, text: str, spec: dict) -> pymupdf.Rect:
    """Work out where the annotation text goes, in fitz coordinates."""
    width = pymupdf.get_text_length(text, fontname=FONT, fontsize=SIZE)
    mode = spec["place"]
    if mode == "fixed":
        x, y = spec["at"]
        if y is None:  # inherit the anchor's baseline
            y = rect.y1 - 3
        return pymupdf.Rect(x, y - SIZE, x + width, y)
    if mode == "right" and rect.x1 + 4 + width < page.rect.width - MARGIN:
        return pymupdf.Rect(rect.x1 + 4, rect.y1 - SIZE - 3, rect.x1 + 4 + width, rect.y1 - 3)
    # below, or right with no room for it
    return pymupdf.Rect(rect.x0, rect.y1 + 2, rect.x0 + width, rect.y1 + 2 + SIZE)


def _parse_text(text: str) -> dict:
    """Split the display string back into model fields, for the record set."""
    if text.startswith("["):
        return dict(kind=AnnotationKind.NOTE, origin=Origin.NOT_SUBMITTED)
    body, _, condition = text.partition(" when ")
    body = body.split("  (")[0].split("  --")[0].strip()
    domain, _, variable = body.partition(".")
    if not variable:  # e.g. "VSORRES when ..."
        domain, variable = ("VS" if body.startswith("VS") else None), body
    return dict(
        kind=AnnotationKind.VARIABLE,
        domain=domain,
        variable=variable,
        condition=condition or None,
        origin=Origin.COLLECTED,
    )


def build(pdf_path: Path, out_dir: Path) -> tuple[Path, AnnotationSet]:
    fieldset = extract_fields(pdf_path)
    by_id = {f.field_id: f for f in fieldset.fields}
    doc = pymupdf.open(pdf_path)
    annotations: list[SdtmAnnotation] = []

    for page_index, page in enumerate(doc):
        h = page.rect.height

        # Boxed domain marker.
        marker = DOMAIN_MARKERS[page_index]
        box = pymupdf.Rect(250, 78, 292, 95)
        page.draw_rect(box, color=ANNOT, width=1.1)
        page.insert_text(pymupdf.Point(box.x0 + 13, box.y1 - 5), marker,
                         fontname="hebo", fontsize=10, color=ANNOT)
        annotations.append(
            SdtmAnnotation(
                annot_id=f"p{page_index + 1}-domain",
                field_id=None,
                page_index=page_index,
                bbox=fitz_rect_to_bbox(box, h),
                kind=AnnotationKind.DOMAIN,
                domain=marker,
                source_model=MOCKUP_SOURCE,
            )
        )

        for n, spec in enumerate(PAGE_ANNOTATIONS[page_index], start=1):
            anchor = spec["anchor"]
            if anchor is None:
                rect = pymupdf.Rect(spec["at"][0], spec["at"][1] - SIZE,
                                    spec["at"][0], spec["at"][1])
            else:
                rect = pymupdf.Rect(*bbox_to_fitz_rect(by_id[anchor].bbox, h))
            placed = _place(page, rect, spec["text"], spec)
            page.insert_text(pymupdf.Point(placed.x0, placed.y1),
                             spec["text"], fontname=FONT, fontsize=SIZE, color=ANNOT)
            annotations.append(
                SdtmAnnotation(
                    annot_id=f"p{page_index + 1}-{n:02d}",
                    field_id=anchor,
                    page_index=page_index,
                    bbox=fitz_rect_to_bbox(placed, h),
                    source_model=MOCKUP_SOURCE,
                    **_parse_text(spec["text"]),
                )
            )

        _draw_notice(page, page_index)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_pdf = out_dir / "ILLUSTRATIVE_target_annotated_crf.pdf"
    doc.set_metadata({
        "title": "ILLUSTRATIVE MOCKUP - target annotated CRF (not pipeline output)",
        "subject": MOCKUP_SOURCE,
        "keywords": "illustrative, mockup, not-a-deliverable, synthetic",
    })
    doc.save(out_pdf, garbage=4, deflate=True)

    for i, page in enumerate(doc):
        page.get_pixmap(dpi=110).save(out_dir / f"ILLUSTRATIVE_target_p{i + 1}.png")
    doc.close()

    return out_pdf, AnnotationSet(
        source_pdf=pdf_path.name, pages=fieldset.pages, annotations=annotations
    )


def _draw_notice(page, page_index: int) -> None:
    """The thing that stops this being mistaken for a deliverable."""
    box = pymupdf.Rect(72, 430, 540, 530) if page_index == 0 else pymupdf.Rect(72, 560, 540, 660)
    page.draw_rect(box, color=BANNER, fill=NOTICE_BG, width=1.2)
    page.insert_textbox(
        box + (10, 8, -10, -8),
        "ILLUSTRATIVE MOCKUP - NOT PIPELINE OUTPUT\n\n"
        "The SDTM annotations on this page were hand-written to show what a finished "
        "annotated CRF should look like. They were not proposed by Copilot, not reviewed, "
        "and not derived from any data. Field positions are real (from extract.py); every "
        "domain, variable and condition is invented.",
        fontname=FONT, fontsize=7.5, color=BANNER,
    )
    if page_index == 1:
        legend = pymupdf.Rect(72, 675, 540, 730)
        page.insert_textbox(
            legend,
            "Annotations a finished aCRF would also carry, omitted above for clarity:\n"
            + "\n".join(f"    {line}" for line in LEGEND),
            fontname=FONT, fontsize=7, color=(0.35, 0.35, 0.35),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", type=Path, default=Path("build"))
    parser.add_argument(
        "--pdf", type=Path,
        default=Path("fixtures/SYNTHETIC_sample_crf_acroform.pdf"),
    )
    args = parser.parse_args(argv)

    if not args.pdf.exists():
        from make_sample_crf import make_sample_crf

        make_sample_crf(args.pdf.parent)

    out_pdf, annots = build(args.pdf, args.out_dir)
    print(f"{out_pdf}   ({len(annots.annotations)} illustrative annotations)")
    for i in (1, 2):
        print(f"{args.out_dir}/ILLUSTRATIVE_target_p{i}.png")
    print("\nHand-written mockup of the goal. Not a deliverable, not pipeline output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
