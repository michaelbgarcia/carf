"""Grouped (repeating) annotations -- one drawn box standing for several rows.

The case this exists for
------------------------
A CRF asks one question and offers a block of responses, or repeats the same
field down a log grid or across visits. Every one of those rows carries the
*same* SDTM mapping, so annotating each of them separately produces a column of
identical boxes::

    Please record            SUPPDS.QVAL when QNAM = "PROTVER"      Original    O
    protocol version         SUPPDS.QVAL when QNAM = "PROTVER"    Amendment 1   O
    on which subject         SUPPDS.QVAL when QNAM = "PROTVER"    Amendment 2   O
    is currently             SUPPDS.QVAL when QNAM = "PROTVER"    Amendment 3   O
    enrolled:                SUPPDS.QVAL when QNAM = "PROTVER"    Amendment 4   O

which is what an annotator avoids by hand, and what the guidelines expect the
finished artifact not to look like::

    Please record
    protocol version         SUPPDS.QVAL when QNAM = "PROTVER"      Original    O
    on which subject        |________________________________|    Amendment 1  O
    is currently                                                  Amendment 2  O
    enrolled:                                                     Amendment 3  O
                                                                  Amendment 4  O

Same assertion, five rows, one box. The annotation is still attributed to all
five rows in the data (``member_row_ids``), so nothing downstream has to infer
coverage from geometry -- which is the part that makes this safe to do.

Two ways a group comes into existence
-------------------------------------
**Declared.** A reviewer types the same key into the control sheet's ``group``
column on each member row and fills the annotation cell once. That is the
general mechanism, and the only one that can express a group the pipeline could
not have worked out for itself.

**Collapsed.** :func:`collapse_repeats` finds runs of *consecutive* rows already
carrying an identical annotation -- which is what pre-population from mined
precedent produces for exactly the block above -- and folds each run into one
group. This is deliberately the conservative half of the feature:

* **Identical text only.** Not "similar", not "same variable, different
  condition". Two boxes reading the same thing in the same place are a
  duplicate by definition; anything short of that is a judgement call and
  belongs to the reviewer.
* **Strictly adjacent rows only.** A gap means some row between the members
  carries no annotation, and a box centred across that gap would visually
  assert a mapping for a row nobody mapped. A human who wants that span can
  declare it; the machine will not assume it.

What a group is *not* allowed to be
-----------------------------------
A group must not straddle a row that carries a **different** annotation --
:func:`resolve_groups` raises rather than draw one box across a span that
visibly contradicts itself. A group whose members sit on several pages is fine
and is the repeating-form case; it renders as one box per page (see
:func:`page_runs`), because a page of a submission artifact has to be readable
on its own.

Ordering
--------
"Document order" throughout means position in ``RowSet.rows``, which is page
order then reading order down the page. The first member in that order is the
group's **anchor**: it keeps ``row_id``, so every join, colour rule and lookup
that already keys on ``row_id`` keeps working, and ``layout`` positions the box
against the whole block rather than against the anchor alone.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional

from pipeline.models import AnnotationSet, RowSet, SdtmAnnotation

#: Prefix on a group key this module assigned itself, as opposed to one a
#: reviewer typed. Visible in the control sheet's ``group`` column on purpose:
#: "the tool grouped these five rows" is exactly the kind of decision a reviewer
#: should be able to see and undo by clearing the cells.
AUTO_PREFIX = "g_"


class GroupingError(ValueError):
    """A declared group cannot be resolved into a single annotation.

    Carries the offending ``row_ids`` so a caller with a row-to-spreadsheet-line
    map -- ``control_sheet.to_annotations`` -- can point at the cells instead of
    at internal ids.
    """

    def __init__(self, message: str, row_ids: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.row_ids = list(row_ids)


def document_order(rows: RowSet) -> dict[str, int]:
    """``row_id -> position``. The one definition of "before" in this module."""
    return {row.row_id: i for i, row in enumerate(rows.rows)}


def _content_key(a: SdtmAnnotation) -> tuple:
    """What has to match for two annotations to be the same assertion.

    The drawn text is the substance, but not the whole of it: two rows can show
    the same text under different domains (so different colours), and a
    ``[NOT SUBMITTED]`` mark and a mapping that happens to read the same are not
    interchangeable. Comparing the rendered text alone would collapse those.
    """
    return (
        a.slot,
        a.kind,
        a.domain or "",
        a.origin.value if a.origin else "",
        a.display_text(),
    )


def page_runs(member_row_ids: list[str], rows: RowSet) -> list[list[str]]:
    """Split a group's members into one run per page, in document order.

    A group that repeats a form across visits legitimately spans pages, and it
    still has to be drawn once *per page*: a reviewer reads one page at a time,
    and a page whose fields are annotated only by a box on the previous page is
    unreadable on its own -- and, printed, unreviewable. So the group stays one
    record in the sheet and becomes one box per page on the artifact.
    """
    order = document_order(rows)
    by_page: dict[int, list[str]] = {}
    for row_id in sorted(member_row_ids, key=lambda r: order.get(r, 0)):
        row = rows.by_id(row_id)
        if row is None:
            continue
        by_page.setdefault(row.page_index, []).append(row_id)
    return [by_page[page] for page in sorted(by_page)]


def _interlopers(
    members: list[str],
    slot: int,
    rows: RowSet,
    annotated: Mapping[tuple[str, int], SdtmAnnotation],
) -> list[str]:
    """Rows inside the group's span that are not members and are annotated anyway.

    These are the case that cannot be drawn: one box covering rows 3-7 while row
    5 carries a different mapping asserts two contradictory things about row 5,
    and no amount of placement makes that readable. A non-member row inside the
    span with *no* annotation of its own is allowed through -- a reviewer who
    grouped across it made that call deliberately, and the box is the only claim
    on the page about that row.
    """
    order = document_order(rows)
    positions = [order[m] for m in members if m in order]
    if not positions:
        return []
    lo, hi = min(positions), max(positions)
    member_set = set(members)
    out = []
    for row in rows.rows[lo : hi + 1]:
        if row.row_id in member_set:
            continue
        other = annotated.get((row.row_id, slot))
        if other is not None and other.display_text():
            out.append(row.row_id)
    return out


def row_membership(
    keys: Mapping[str, str], slots: Iterable[int] = (1, 2)
) -> dict[tuple[str, int], str]:
    """A row-level ``group`` column expanded to the per-slot form.

    The control sheet has one ``group`` cell per row, not one per annotation
    cell, so a key typed there claims the row's ``anno1`` *and* ``anno2``. That
    is the right default for a hand-declared group -- a reviewer grouping five
    option rows means the whole line -- but it is not the only case, which is
    why :func:`resolve_groups` works in ``(row_id, slot)`` terms underneath.
    """
    return {
        (row_id, slot): key for row_id, key in keys.items() if key for slot in slots
    }


def resolve_groups(
    annotations: AnnotationSet,
    rows: RowSet,
    membership: Optional[Mapping[tuple[str, int], str]] = None,
) -> AnnotationSet:
    """Fold each declared group into one annotation per page it appears on.

    ``membership`` maps ``(row_id, slot) -> group key``, and it is passed
    separately rather than read off the annotations for one reason: **a member
    row usually has no annotation of its own.** The reviewer types the mapping
    once and the key on every row of the block, so four of the five members
    arrive here as rows with a group key and an empty annotation cell. Deriving
    membership from the annotations alone would see a group of one and silently
    drop the other four rows from its coverage. Use :func:`row_membership` to
    build it from a sheet's row-level ``group`` column.

    When ``membership`` is ``None`` the annotations' own ``group_id`` is used,
    which is the round-trip case: ``proposals.json`` re-read, or a set this
    module already collapsed.

    A ``(group, slot)`` with no text on any member is dropped rather than
    raising -- a block whose members fill only ``anno1`` produces an empty
    slot-2 group as a matter of course.
    """
    if membership is None:
        membership = {
            (row_id, a.slot): a.group_id
            for a in annotations.annotations
            if a.group_id
            for row_id in a.covered_row_ids
        }

    order = document_order(rows)
    by_row_slot = {
        (a.row_id, a.slot): a for a in annotations.annotations if a.row_id is not None
    }

    # Which rows each (key, slot) claims.
    members_of: dict[tuple[str, int], list[str]] = {}
    for (row_id, slot), key in membership.items():
        if not key:
            continue
        if rows.by_id(row_id) is None:
            raise GroupingError(
                f"group {key!r} names row {row_id!r}, which is not in the extracted rows",
                [row_id],
            )
        members_of.setdefault((key, slot), []).append(row_id)
    for member_ids in members_of.values():
        member_ids.sort(key=lambda r: order[r])

    claimed = {
        (row_id, slot) for (_key, slot), ids in members_of.items() for row_id in ids
    }
    out: list[SdtmAnnotation] = [
        a
        for a in annotations.annotations
        if a.row_id is None or (a.row_id, a.slot) not in claimed
    ]

    for (key, slot), member_ids in sorted(members_of.items(), key=lambda kv: kv[0]):
        content = [
            by_row_slot[(m, slot)]
            for m in member_ids
            if (m, slot) in by_row_slot and by_row_slot[(m, slot)].display_text()
        ]
        if not content:
            continue

        distinct = {_content_key(a) for a in content}
        if len(distinct) > 1:
            raise GroupingError(
                f"group {key!r} slot {slot} has {len(distinct)} different "
                "annotations on its member rows -- "
                + "; ".join(sorted({f"{a.row_id}: {a.display_text()!r}" for a in content}))
                + ". One group is one assertion: give the rows different group "
                "keys, or make the text identical.",
                [a.row_id for a in content if a.row_id],
            )

        blocked = _interlopers(member_ids, slot, rows, by_row_slot)
        if blocked:
            raise GroupingError(
                f"group {key!r} slot {slot} spans row(s) {blocked} that carry a "
                "different annotation and are not in the group. One box cannot "
                "cover them and mean something else on the way past -- narrow "
                "the group, or add those rows to it.",
                blocked,
            )

        template = content[0]
        for run in page_runs(member_ids, rows):
            anchor_row = rows.by_id(run[0])
            assert anchor_row is not None  # membership was validated above
            out.append(
                template.model_copy(
                    update={
                        "annot_id": f"{anchor_row.row_id}_a{slot}",
                        "row_id": anchor_row.row_id,
                        "page_index": anchor_row.page_index,
                        "bbox": anchor_row.anchor,
                        "group_id": key,
                        # A run of one is not a group: leaving members empty
                        # keeps `is_grouped` honest and keeps the annotation on
                        # the ordinary single-row placement path.
                        "member_row_ids": list(run) if len(run) > 1 else [],
                    }
                )
            )

    return annotations.model_copy(update={"annotations": _in_order(out, order)})


def _in_order(
    annotations: list[SdtmAnnotation], order: Mapping[str, int]
) -> list[SdtmAnnotation]:
    """Document order, so a resolved set reads the way the pages do."""
    return sorted(
        annotations,
        key=lambda a: (
            a.page_index,
            order.get(a.row_id or "", -1),
            a.slot,
        ),
    )


def collapse_repeats(annotations: AnnotationSet, rows: RowSet) -> AnnotationSet:
    """Fold runs of consecutive rows carrying an identical annotation into groups.

    This is the automatic half, and its two conditions -- identical content,
    strictly adjacent rows -- are what make it safe to run without asking. See
    the module docstring for why each one is drawn where it is.

    Annotations that already carry a ``group_id`` are left alone: a group a
    human declared is not a candidate for re-derivation, whatever the text
    happens to look like.
    """
    by_row_slot: dict[tuple[str, int], SdtmAnnotation] = {}
    for a in annotations.annotations:
        if a.row_id is not None and not a.group_id and a.display_text():
            by_row_slot[(a.row_id, a.slot)] = a

    assigned: dict[tuple[str, int], str] = {}

    def flush(run: list[SdtmAnnotation]) -> None:
        if len(run) < 2:
            return
        key = f"{AUTO_PREFIX}{run[0].row_id}"
        for a in run:
            assert a.row_id is not None  # by_row_slot only holds rows
            # Per slot, not per row: a run of identical `anno1` cells says
            # nothing about the `anno2` on one of those rows, and claiming it
            # for the group would attribute a variable to four rows that never
            # carried it.
            assigned[(a.row_id, a.slot)] = key

    for slot in sorted({slot for _, slot in by_row_slot}):
        # Walking `rows.rows` rather than the annotations is what enforces
        # adjacency: a row with no annotation of its own, or with a different
        # one, lands here as a `current` that does not continue the run.
        run: list[SdtmAnnotation] = []
        previous: Optional[SdtmAnnotation] = None
        for row in rows.rows:
            current = by_row_slot.get((row.row_id, slot))
            if (
                previous is not None
                and current is not None
                and _content_key(previous) == _content_key(current)
                and previous.page_index == current.page_index
                and _same_form(previous, current, rows)
            ):
                run.append(current)
            else:
                flush(run)
                run = [current] if current is not None else []
            previous = current
        flush(run)

    # Re-key through resolve_groups rather than assembling the merged records
    # here, so a collapsed group and a declared one go through exactly the same
    # validation and the same page-splitting -- including any group already
    # declared on the way in, which is left as it is but still resolved.
    membership = dict(assigned)
    for a in annotations.annotations:
        if a.group_id:
            for row_id in a.covered_row_ids:
                membership.setdefault((row_id, a.slot), a.group_id)
    if not membership:
        return annotations
    return resolve_groups(annotations, rows, membership)


def _same_form(a: SdtmAnnotation, b: SdtmAnnotation, rows: RowSet) -> bool:
    """Whether two annotations' rows belong to the same form.

    A form boundary can fall mid-page, and MSG colours are assigned per form --
    so two identical mappings either side of one are not the same box waiting to
    happen, they are two forms that each need their own.
    """
    row_a = rows.by_id(a.row_id or "")
    row_b = rows.by_id(b.row_id or "")
    return row_a is not None and row_b is not None and row_a.form == row_b.form


def coverage(annotations: AnnotationSet) -> dict[str, list[SdtmAnnotation]]:
    """``row_id -> the annotations covering it``, groups expanded to their members.

    The accessor for "is this row mapped", which a scan of ``row_id`` answers
    wrongly once groups exist -- it would report four of the five rows in a
    block as unmapped and batch them off to Copilot.
    """
    out: dict[str, list[SdtmAnnotation]] = {}
    for a in annotations.annotations:
        for row_id in a.covered_row_ids:
            out.setdefault(row_id, []).append(a)
    return out


def summarize(annotations: AnnotationSet) -> tuple[int, int]:
    """``(groups, rows covered by them)`` -- what the scripts print."""
    groups = [a for a in annotations.annotations if a.is_grouped]
    return len(groups), sum(len(a.member_row_ids) for a in groups)


__all__ = [
    "AUTO_PREFIX",
    "GroupingError",
    "collapse_repeats",
    "coverage",
    "document_order",
    "page_runs",
    "resolve_groups",
    "row_membership",
    "summarize",
]
