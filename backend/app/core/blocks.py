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

PROVENANCE NOW EXISTS, AND THIS MODULE STILL IGNORES IT ON PURPOSE (c279, migration
0030). user_blocks.source records whether a block was made by name or through an
anonymous chirp, and the READ filters use it: feed posts, comments and counts hide only
on 'named', so a by-chirp block no longer moves a named surface and the feed diff that
used to name the anonymous author has nothing to show.

CONTACT ENFORCEMENT DELIBERATELY DOES NOT MAKE THAT DISTINCTION. blockers_of matches on
the pair alone, so both kinds still refuse contact, and that is load-bearing: the whole
point of by-chirp blocking is a student shutting out someone who is harassing them
anonymously. If a by-chirp block stopped refusing DMs, the harasser could keep messaging
the person who blocked them, which is the exact failure this module was written to end.

It is safe to keep enforcing both here for the reason the direction note above already
gives: the asymmetry means the blocker's OWN outbound is never filtered by their own
blocks, so a blocker cannot use contact refusal as a probe at all. Provenance would only
matter here if the rule were symmetric, and it is not. The migration this docstring used
to call deferred has landed; the deferral it described is resolved, not still open.
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
