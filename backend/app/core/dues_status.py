"""Shared net-dues-standing definition for one member in one dues cycle (board c172).

Lives in core rather than beside either router because two surfaces both answer "has
this member paid" for the same cycle: the President overview's per-member paid/
outstanding split (board c171) and the double-charge guard in payments.py's
create_dues_payment_intent. Before this module existed they answered it two different
ways — the overview NETTED corrections per member, the guard treated the mere
EXISTENCE of a dues_payment row as paid and ignored corrections entirely — so a member
refunded in full showed up on the President's chase list while their own pay button
told them they had already paid and refused to let them try again. Same reasoning as
core/windows.py's shared meeting window: two endpoints answering the same question
with different rules is a bug nobody thinks to look for, because each one is internally
consistent and only disagrees with the OTHER screen.

NETTING IS THE RIGHT DEFINITION because ledger_entries is append-only (SPEC 8.2): a
refund or a correction is never an update to the original row, it is a new
entry_type="correction" row pointing at it via corrects_entry_id. Reading only
entry_type="dues_payment" reports money the chapter gave back as money it is still
holding, and marks a fully-refunded member as someone who does not need to be chased.

The correction row's own related_user_id is not trusted for attribution: it is
nullable and nothing requires it to match the entry being corrected. The user comes
from the payment the correction points at, which is the only link the schema actually
guarantees.
"""
import uuid

from sqlalchemy import select, union_all
from sqlalchemy.sql import Subquery

from app import models


def dues_contributions_subquery(
    chapter_id: uuid.UUID, dues_cycle_id: uuid.UUID
) -> Subquery:
    """(user_id, amount_cents) rows for one cycle: one per dues_payment, one per
    correction of it.

    Summing this per user gives that member's NET standing for the cycle — positive
    means paid, zero or negative means outstanding, including a member refunded in
    full or over-refunded. A caller that wants one member's status sums after
    filtering on user_id (payments.py's guard); chapter_overview folds every member's
    rows at once inside its roster-spined join, which is why this returns the
    unaggregated rows rather than a pre-summed total.
    """
    payments = (
        select(
            models.LedgerEntry.id.label("entry_id"),
            models.LedgerEntry.related_user_id.label("user_id"),
            models.LedgerEntry.amount_cents.label("amount_cents"),
        )
        .where(
            models.LedgerEntry.chapter_id == chapter_id,
            models.LedgerEntry.entry_type == "dues_payment",
            models.LedgerEntry.dues_cycle_id == dues_cycle_id,
        )
        .subquery()
    )
    corrections = (
        select(
            payments.c.user_id.label("user_id"),
            models.LedgerEntry.amount_cents.label("amount_cents"),
        )
        .join(payments, models.LedgerEntry.corrects_entry_id == payments.c.entry_id)
        .where(
            models.LedgerEntry.chapter_id == chapter_id,
            models.LedgerEntry.entry_type == "correction",
        )
    )
    return union_all(
        select(payments.c.user_id, payments.c.amount_cents), corrections
    ).subquery()
