"""c268: campus verification sends are limited per TARGET mailbox, not just per caller.

The existing limit is keyed campus_verify_send:{user.id}. It bounds what one account can
send and says nothing about what one INBOX can receive, so N accounts converging on a
single student's address were unbounded — mailbox-bombing a specific person. The cost
does not land on the attacker either: it lands on the shared Resend quota (c240, the free
tier is 100/DAY), so a campaign against one victim degrades verification for everyone.

Any authenticated user can request a code for any recognised .edu address — resolve_campus
resolves the campus from the DOMAIN and never checks that the caller belongs to it, which
is deliberate (the address is the thing being proved) and is exactly what makes the
bombing vector reachable. test_n_accounts_converging_on_one_inbox_are_stopped is the
whole point of this card.

The second half matters as much: a real student re-requesting their own code must never
meet this. Their per-caller budget (3 per 15 minutes) paces them long before the target
ceiling, and test_a_real_owner_chasing_a_missing_code_never_hits_the_target_limit walks
that through with the caller's window cleared between bursts, the way real time would.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.core.rate_limits import CAMPUS_VERIFY_TARGET_LIMIT
from app.services import campus_verification, rate_limit
from tests.conftest import MakeCampus, MakeUser

DOMAIN = "uncg.edu"
VICTIM = f"victim@{DOMAIN}"

TARGET_MAX, _TARGET_WINDOW = CAMPUS_VERIFY_TARGET_LIMIT
PER_CALLER = campus_verification.SEND_MAX_PER_WINDOW


async def _set_domains(campus_id: str, domains: list[str]) -> None:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE campuses SET email_domains = :domains WHERE id = :id"),
            {"domains": domains, "id": campus_id},
        )
        await session.commit()


@pytest.fixture
async def campus(make_campus: MakeCampus) -> str:
    campus_id = await make_campus()
    await _set_domains(campus_id, [DOMAIN])
    return campus_id


def _clear_caller_window(user_id: str) -> None:
    """Drop ONE caller's send window, leaving every other budget untouched.

    Stands in for the passage of fifteen minutes. _reset_all() would also wipe the
    target counter, which is the thing under test — so this reaches for the single key
    instead. White-box on purpose: the alternative is a test that cannot distinguish
    "the target limit is generous" from "the target limit was reset".
    """
    rate_limit._WINDOWS.pop(f"campus_verify_send:{user_id}", None)


async def _send(client: AsyncClient, user, address: str = VICTIM):
    return await client.post(
        "/auth/campus-verification", json={"edu_email": address}, headers=user.headers
    )


# ---------------------------------------------------------------------------
# the vector this card exists for
# ---------------------------------------------------------------------------


async def test_n_accounts_converging_on_one_inbox_are_stopped(
    client: AsyncClient, make_user: MakeUser, campus: str
) -> None:
    """Every attacker stays inside their OWN budget; the victim's inbox still stops.

    This is the shape the per-caller limit could never see. Each account sends only its
    permitted 3, so nothing here is individually abusive — the abuse is only visible
    from the target's side.
    """
    senders = TARGET_MAX // PER_CALLER
    assert senders * PER_CALLER == TARGET_MAX, "test assumes the ceiling divides evenly"

    for i in range(senders):
        attacker = await make_user(f"Attacker {i}")
        for j in range(PER_CALLER):
            response = await _send(client, attacker)
            assert response.status_code == 202, f"sender {i} send {j}: {response.text}"

    fresh = await make_user("One More Attacker")
    refused = await _send(client, fresh)
    assert refused.status_code == 429, refused.text
    assert refused.json()["detail"] == "verification_rate_limited"


async def test_plus_addressing_cannot_mint_a_fresh_budget(
    client: AsyncClient, make_user: MakeUser, campus: str
) -> None:
    """victim+1@ and victim+2@ land in victim@'s inbox, so they must share its budget.

    Keying on the address as typed would leave the whole fix trivially bypassable: an
    attacker appends a counter and gets an unlimited supply of new ceilings against the
    same student.
    """
    tagged_sends = 0
    sender_index = 0
    while tagged_sends < TARGET_MAX:
        attacker = await make_user(f"Tagger {sender_index}")
        sender_index += 1
        for _ in range(PER_CALLER):
            if tagged_sends >= TARGET_MAX:
                break
            response = await _send(client, attacker, f"victim+{tagged_sends}@{DOMAIN}")
            assert response.status_code == 202, response.text
            tagged_sends += 1

    # The plain address is now out of budget, because every tagged variant spent it.
    victim_owner = await make_user("Actual Victim")
    refused = await _send(client, victim_owner, VICTIM)
    assert refused.status_code == 429, refused.text


def test_mailbox_key_folds_case_and_tags_without_help_from_its_caller() -> None:
    """_mailbox_key must normalize on its own, not trust the caller to have done it.

    THIS IS THE TEST THAT ACTUALLY PINS THE FOLDING, and it is a unit test on purpose.
    The end-to-end version below passes either way — normalize_email lowercases before
    the key is ever built, so removing the fold inside _mailbox_key changes nothing that
    a route-level test can see. That made the route test look like proof when it was
    only proof of the CALLER's behaviour. The hole it leaves is a second caller that
    passes a raw address, at which point Jose@ and jose@ become two budgets against one
    inbox, and no route test in the file would notice.
    """
    expected = f"victim@{DOMAIN}"
    for raw in (
        f"VICTIM@{DOMAIN.upper()}",
        f"Victim+Tag@{DOMAIN}",
        f"  vIcTiM+a+b@{DOMAIN.title()}  ",
        expected,
    ):
        assert campus_verification._mailbox_key(raw) == expected, raw


async def test_case_and_tag_variants_all_share_one_budget(
    client: AsyncClient, make_user: MakeUser, campus: str
) -> None:
    """Jose+tag@UNCG.EDU and jose@uncg.edu are one mailbox, so they are one budget.

    Case folding is the same bypass as plus-addressing one rung down, and until this
    test existed nothing pinned it: the key was lowercase only because normalize_email
    happened to run first, which a second caller could quietly stop doing.
    """
    spent = 0
    index = 0
    variants = [
        f"VICTIM@{DOMAIN.upper()}",
        f"Victim+Tag@{DOMAIN}",
        f"vIcTiM@{DOMAIN.title()}",
        VICTIM,
    ]
    while spent < TARGET_MAX:
        sender = await make_user(f"Variant Sender {index}")
        index += 1
        for _ in range(PER_CALLER):
            if spent >= TARGET_MAX:
                break
            response = await _send(client, sender, variants[spent % len(variants)])
            assert response.status_code == 202, response.text
            spent += 1

    exhausted = await make_user("After The Variants")
    refused = await _send(client, exhausted, VICTIM)
    assert refused.status_code == 429, (
        "case and tag variants minted separate budgets instead of sharing one: "
        f"{refused.status_code}"
    )


# ---------------------------------------------------------------------------
# and the half that matters more: it must not fire on real use
# ---------------------------------------------------------------------------


async def test_a_real_owner_chasing_a_missing_code_never_hits_the_target_limit(
    client: AsyncClient, make_user: MakeUser, campus: str
) -> None:
    """A student whose code is not arriving retries, waits, retries again.

    Fifteen sends across five windows is a thoroughly frustrated person — well past
    where anyone gives up and mails support — and it must sail through. If this ever
    fails, the target ceiling has been tightened into the path of a real student, which
    is the failure this project keeps refusing to ship.
    """
    frustrated_real_world_sends = 15
    assert frustrated_real_world_sends * 2 <= TARGET_MAX, (
        "the target ceiling is no longer comfortably clear of a real owner's retrying"
    )

    owner = await make_user("Owner")
    for i in range(frustrated_real_world_sends):
        if i and i % PER_CALLER == 0:
            _clear_caller_window(owner.id)  # fifteen minutes pass
        response = await _send(client, owner)
        assert response.status_code == 202, f"send {i + 1}: {response.text}"


async def test_another_students_inbox_has_its_own_budget(
    client: AsyncClient, make_user: MakeUser, campus: str
) -> None:
    """The limit is per target, not global.

    A global ceiling would mean one bombing campaign stops verification for the entire
    product — strictly worse than the bug being fixed, and the kind of mitigation that
    becomes the outage.
    """
    senders = TARGET_MAX // PER_CALLER
    for i in range(senders):
        attacker = await make_user(f"Attacker {i}")
        for _ in range(PER_CALLER):
            assert (await _send(client, attacker)).status_code == 202

    assert (await _send(client, await make_user("Blocked"))).status_code == 429

    bystander = await make_user("Unrelated Student")
    response = await _send(client, bystander, f"someone.else@{DOMAIN}")
    assert response.status_code == 202, response.text


async def test_a_caller_over_their_own_limit_does_not_spend_the_targets_budget(
    client: AsyncClient, make_user: MakeUser, campus: str
) -> None:
    """Ordering property, and it is not cosmetic.

    enforce_limit increments even when it raises, so checking the target FIRST would let
    one already-blocked spammer keep burning the victim's ceiling down — turning a
    limiter meant to protect that student into the thing that stops them verifying. The
    caller check therefore runs first and short-circuits.
    """
    spammer = await make_user("Spammer")
    for _ in range(PER_CALLER):
        assert (await _send(client, spammer)).status_code == 202

    over = await _send(client, spammer)
    assert over.status_code == 429, over.text

    spent = len(rate_limit._WINDOWS.get(f"campus_verify_target:{VICTIM}", ()))
    assert spent == PER_CALLER, (
        f"the refused call still spent the target's budget: {spent} of {TARGET_MAX} "
        f"used after only {PER_CALLER} accepted sends"
    )
