"""The shared block lookup for CONTACT paths (board card c243).

Blocks existed since c76 but were enforced only on READ paths — routers/feed.py and
routers/chirps.py filter a blocked author's content out of the blocker's feed. Nothing
in messages.py, events.py or the push fan-out ever consulted user_blocks, so blocking
someone hid their chirps and stopped nothing else: they could still open a DM with you,
add you to a group, invite you to their event, and light up your phone. "Blocked" that
does not stop contact is worse than no block at all, because the user believes they are
protected. This module is the one place that answers "who here has blocked me", so the
next contact surface added to the app has a check to reuse instead of a check to forget
(same reasoning as core/campus_access.py, which exists because three routers each grew
their own copy of the campus test).

DIRECTION IS ASYMMETRIC, AND THAT IS A DELIBERATE CHOICE, NOT AN OVERSIGHT.
A block stops the BLOCKED user from reaching the BLOCKER. It does NOT stop the blocker
from reaching the person they blocked. The symmetric rule is the more usual product
behaviour and it is what we would otherwise want, but it is INCOMPATIBLE with the
anonymous-chirp guarantee (SPEC §8.3):

    POST /moderation/blocks/by-chirp/{id} lets someone block a chirp's author WITHOUT
    ever learning who that author is — the endpoint goes to real lengths to keep it that
    way (no response body, unconditional upsert so even the LATENCY is constant; see the
    comments on that handler). There is no endpoint that lists your own blocks.

    Under a symmetric rule the blocker could hand back the identity the by-chirp endpoint
    refuses to give them: block an anonymous chirp's author, then try to open a DM with
    each person on the roster in turn. The one that gets refused is the author. That is a
    clean deanonymisation oracle built out of the safety feature.

Enforcing only "the blocker is unreachable BY the blocked user" closes the harassment
hole the card is about while giving the blocker no observable signal at all — their own
blocks are never consulted when THEY are the one making contact. The residual gap is
that A, having blocked B, can still open a conversation with B; that is self-healing,
because B blocking back is exactly the case this module does enforce.

Recording WHY a block was created (by-chirp vs. an ordinary named block) would let the
symmetric rule apply safely to named blocks only, but user_blocks has no such column and
adding one is a migration — deliberately not taken here (see the c243 PR).
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models


async def blockers_of(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    candidate_ids: Iterable[uuid.UUID],
) -> set[uuid.UUID]:
    """Return the subset of `candidate_ids` who have blocked `subject_id`.

    Read it as "which of these people have shut `subject_id` out". `subject_id` is
    always the user attempting to make contact, so callers never pass the caller's own
    blocks through here — see the direction note in the module docstring.
    """
    candidates = {c for c in candidate_ids if c != subject_id}
    if not candidates:
        return set()
    result = await session.execute(
        select(models.UserBlock.blocker_id).where(
            models.UserBlock.blocked_id == subject_id,
            models.UserBlock.blocker_id.in_(candidates),
        )
    )
    return set(result.scalars().all())
