"""Model invariants: the Part 11 guard and the two lenient input boundaries."""

import pytest
from pydantic import ValidationError

from pipeline.models import (
    AnnotationKind,
    BBox,
    CopilotProposal,
    Origin,
    ReviewStatus,
    SdtmAnnotation,
)


def _annot(**overrides):
    kwargs = dict(
        annot_id="a1",
        page_index=0,
        bbox=BBox(x0=72, y0=700, x1=200, y1=715),
        kind=AnnotationKind.VARIABLE,
        domain="VS",
        variable="VSORRES",
    )
    kwargs.update(overrides)
    return SdtmAnnotation(**kwargs)


# --- provenance / 21 CFR Part 11 ------------------------------------------


def test_annotations_start_as_proposals():
    assert _annot().review_status is ReviewStatus.PROPOSED


def test_leaving_proposed_requires_naming_the_reviewer():
    with pytest.raises(ValidationError, match="reviewed_by"):
        _annot(review_status=ReviewStatus.ACCEPTED)


def test_reviewed_annotation_is_valid_once_attributed():
    a = _annot(review_status=ReviewStatus.ACCEPTED, reviewed_by="mgarcia")
    assert a.review_status is ReviewStatus.ACCEPTED


# --- lenient input, canonical storage -------------------------------------


@pytest.mark.parametrize("spelling", ["Collected", "collected", "COLLECTED"])
def test_origin_accepts_chat_and_hand_typed_spellings(spelling):
    """Copilot's reply and a hand-edited XFDF are both human-authored input."""
    assert CopilotProposal(field_id="DM_USUBJID", origin=spelling).origin is Origin.COLLECTED
    assert _annot(origin=spelling).origin is Origin.COLLECTED


@pytest.mark.parametrize("spelling", ["NotSubmitted", "not submitted", "not_submitted"])
def test_origin_tolerates_separators_in_multiword_values(spelling):
    assert Origin.coerce(spelling) is Origin.NOT_SUBMITTED


def test_origin_still_rejects_values_outside_define_xml():
    with pytest.raises(ValueError, match="Define-XML"):
        Origin.coerce("Guessed")


def test_domain_and_variable_are_normalised_to_uppercase():
    assert _annot(domain="vs", variable="vsorres").variable == "VSORRES"
    p = CopilotProposal(field_id="VS_SYSBP_RES", domain=" vs ", variable="vsorres")
    assert (p.domain, p.variable) == ("VS", "VSORRES")


# --- parse boundary -------------------------------------------------------


def test_proposal_ignores_extra_keys_copilot_invents():
    p = CopilotProposal.model_validate(
        {"field_id": "DM_AGE", "variable": "AGE", "commentary": "looks like a DM field"}
    )
    assert p.field_id == "DM_AGE" and p.variable == "AGE"


def test_proposal_rejects_a_missing_field_id():
    """Without it there is no way back to a bbox, so this must not pass silently."""
    with pytest.raises(ValidationError):
        CopilotProposal.model_validate({"variable": "AGE"})


# --- label rendering ------------------------------------------------------


def test_label_text_renders_the_conditional_pattern():
    a = _annot(domain="VS", variable="VSORRES", condition="VSTESTCD = SYSBP")
    assert a.label_text() == "VS.VSORRES when VSTESTCD = SYSBP"


def test_display_page_is_one_based_only_for_humans():
    a = _annot(page_index=0)
    assert (a.page_index, a.display_page) == (0, 1)
