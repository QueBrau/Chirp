"""GET /chapters/{chapter_id}/overview (board card c171).

The President dashboard's one call. Most of these tests exist to hold lines that a
reasonable-looking rewrite would cross without failing anything else:

  * the gate is president-only, not e-board (test_a_treasurer_cannot_read_the_overview)
  * paid + outstanding is spined on the CURRENT roster, so it can never disagree with
    the roster panel on the same screen (test_paid_plus_outstanding_always_equals_active)
  * collected_cents is deliberately NOT spined that way, because money that came in is
    not a headcount (test_collected_cents_keeps_money_from_a_member_who_left)
  * corrections net out of both, because ledger_entries is append-only and a refund is
    a new row (test_a_full_correction_puts_the_member_back_in_outstanding)
  * attendance never counts a dual-chapter member's absences from their other chapter
    (test_a_dual_chapter_members_absence_elsewhere_is_not_counted_here)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from httpx import AsyncClient

from tests.conftest import ApiUser, ChapterSetup, MakeChapterWith, MakeUser

SPRING = datetime(2026, 3, 1, 18, 0, tzinfo=timezone.utc)


async def _overview(
    client: AsyncClient, setup: ChapterSetup, **params: str
) -> dict:
    response = await client.get(
        f"/chapters/{setup.chapter_id}/overview",
        params=params or None,
        headers=setup.president.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _join(
    client: AsyncClient, setup: ChapterSetup, user: ApiUser, role: str = "member"
) -> None:
    """Add an existing user to a chapter through the real invite/join flow."""
    invite = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": role},
        headers=setup.president.headers,
    )
    assert invite.status_code == 201, invite.text
    joined = await client.post(
        "/chapters/join", json={"code": invite.json()["code"]}, headers=user.headers
    )
    assert joined.status_code == 201, joined.text


async def _open_cycle(
    client: AsyncClient, setup: ChapterSetup, name: str = "Spring 2026", cents: int = 25_000
) -> str:
    response = await client.post(
        f"/chapters/{setup.chapter_id}/dues-cycles",
        json={"name": name, "amount_cents": cents, "due_date": date(2026, 5, 1).isoformat()},
        headers=setup.president.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _pay(
    client: AsyncClient, setup: ChapterSetup, cycle_id: str, user_id: str, cents: int = 25_000
) -> str:
    response = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={
            "entry_type": "dues_payment",
            "amount_cents": cents,
            "related_user_id": user_id,
            "dues_cycle_id": cycle_id,
        },
        headers=setup.president.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _correct(
    client: AsyncClient, setup: ChapterSetup, entry_id: str, cents: int
) -> None:
    response = await client.post(
        f"/chapters/{setup.chapter_id}/ledger",
        json={
            "entry_type": "correction",
            "amount_cents": cents,
            "corrects_entry_id": entry_id,
        },
        headers=setup.president.headers,
    )
    assert response.status_code == 201, response.text


async def _set_status(
    client: AsyncClient, setup: ChapterSetup, user_id: str, status: str
) -> None:
    response = await client.patch(
        f"/chapters/{setup.chapter_id}/members",
        json={"user_id": user_id, "status": status},
        headers=setup.president.headers,
    )
    assert response.status_code == 200, response.text


async def _create_meeting(
    client: AsyncClient, setup: ChapterSetup, title: str = "Chapter meeting",
    when: datetime | None = None,
) -> str:
    created = await client.post(
        f"/chapters/{setup.chapter_id}/meetings",
        json={"title": title, "meeting_date": (when or SPRING).isoformat()},
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def _mark(
    client: AsyncClient, setup: ChapterSetup, meeting_id: str,
    entries: list[tuple[str, str]],
) -> None:
    response = await client.put(
        f"/chapters/{setup.chapter_id}/meetings/{meeting_id}/attendance",
        json={"entries": [{"user_id": uid, "status": s} for uid, s in entries]},
        headers=setup.president.headers,
    )
    assert response.status_code == 200, response.text


# ---- the gate ----


async def test_a_treasurer_cannot_read_the_overview(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Tightest honest gate, not a convenient one.

    A treasurer holds dues_admin and would be a tempting addition here, since most of
    the payload is money. But the same response carries attendance, which no route
    lets a treasurer read on its own - gating this on DUES_ADMIN would hand them the
    Secretary's data through the back door.
    """
    setup = await make_chapter_with("treasurer")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/overview", headers=setup.member.headers
    )
    assert response.status_code == 403, response.text


async def test_a_secretary_cannot_read_the_overview(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The mirror of the treasurer case: minutes_admin must not reach dues."""
    setup = await make_chapter_with("secretary")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/overview", headers=setup.member.headers
    )
    assert response.status_code == 403, response.text


async def test_a_plain_member_cannot_read_the_overview(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    response = await client.get(
        f"/chapters/{setup.chapter_id}/overview", headers=setup.member.headers
    )
    assert response.status_code == 403, response.text


# ---- the empty chapter ----


async def test_a_brand_new_chapter_renders_zeroes_rather_than_nothing(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Every panel must be present and numeric on a chapter that has done nothing yet.

    This is c82's lesson restated: a roster-spined query returns no rows for an empty
    roster, so any count folded into one as a subquery disappears exactly when a new
    president opens this screen for the first time. Nulls or missing keys here would
    render as a broken dashboard on day one.
    """
    setup = await make_chapter_with("president")

    payload = await _overview(client, setup)

    assert payload["roster"]["active"] == 1  # the president themselves
    assert payload["roster"]["inactive"] == 0
    assert payload["dues"]["cycle_id"] is None
    assert payload["dues"]["paid_members"] == 0
    assert payload["dues"]["outstanding_members"] == 0
    assert payload["dues"]["collected_cents"] == 0
    assert payload["attendance"]["meetings_in_window"] == 0
    assert payload["attendance"]["members_with_absence"] == 0
    assert payload["lineage"]["unconfirmed_edges"] == 0
    assert payload["invites"]["live_codes"] == 0
    assert payload["invites"]["remaining_uses"] == 0


# ---- roster ----


async def test_by_role_counts_active_members_only_and_sums_to_active(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """A breakdown that quietly included inactive members would make the two numbers
    on screen disagree with no way to tell which one was wrong."""
    setup = await make_chapter_with("treasurer")
    leaver = await make_user("Departing Member")
    await _join(client, setup, leaver)
    await _set_status(client, setup, leaver.id, "inactive")

    payload = await _overview(client, setup)
    roster = payload["roster"]

    assert roster["active"] == 2  # president + treasurer
    assert roster["inactive"] == 1
    assert sum(entry["count"] for entry in roster["by_role"]) == roster["active"]
    assert {entry["role"] for entry in roster["by_role"]} == {"president", "treasurer"}


async def test_a_removed_member_is_on_neither_side_of_the_roster(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """"removed" is not a quieter "inactive" - it is off the roster entirely, and
    counting it as inactive would tell a president they have people to win back."""
    setup = await make_chapter_with("president")
    gone = await make_user("Removed Member")
    await _join(client, setup, gone)
    await _set_status(client, setup, gone.id, "removed")

    roster = (await _overview(client, setup))["roster"]

    assert roster["active"] == 1
    assert roster["inactive"] == 0


# ---- dues ----


async def test_paid_plus_outstanding_always_equals_active(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    setup = await make_chapter_with("member")
    third = await make_user("Third Member")
    await _join(client, setup, third)
    cycle = await _open_cycle(client, setup)
    await _pay(client, setup, cycle, setup.member.id)

    payload = await _overview(client, setup)

    assert payload["dues"]["cycle_name"] == "Spring 2026"
    assert payload["dues"]["paid_members"] == 1
    assert payload["dues"]["outstanding_members"] == 2
    assert (
        payload["dues"]["paid_members"] + payload["dues"]["outstanding_members"]
        == payload["roster"]["active"]
    )


async def test_a_payer_who_goes_inactive_cannot_push_outstanding_negative(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """FALSIFIES the obvious implementation.

    Counting DISTINCT payers and subtracting from the roster looks right and breaks
    here: the roster shrinks when a member goes inactive, the payer count does not, and
    outstanding goes negative. Spining both on the active roster is what makes that
    unrepresentable rather than merely unlikely.
    """
    setup = await make_chapter_with("member")
    cycle = await _open_cycle(client, setup)
    await _pay(client, setup, cycle, setup.member.id)
    await _pay(client, setup, cycle, setup.president.id)
    await _set_status(client, setup, setup.member.id, "inactive")

    payload = await _overview(client, setup)

    assert payload["roster"]["active"] == 1
    assert payload["dues"]["paid_members"] == 1
    assert payload["dues"]["outstanding_members"] == 0


async def test_collected_cents_keeps_money_from_a_member_who_left(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The counterpart to the test above, and the reason they are separate statements.

    "How many of my members still owe me" is a roster question. "How much came in for
    this cycle" is a bank question, and filtering it to the current roster to make the
    two agree would under-report real money.
    """
    setup = await make_chapter_with("member")
    cycle = await _open_cycle(client, setup)
    await _pay(client, setup, cycle, setup.member.id)
    await _set_status(client, setup, setup.member.id, "inactive")

    payload = await _overview(client, setup)

    assert payload["dues"]["paid_members"] == 0
    assert payload["dues"]["collected_cents"] == 25_000


async def test_a_full_correction_puts_the_member_back_in_outstanding(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """ledger_entries is append-only, so a refund is a NEW negative row pointing at the
    original. Reading only entry_type="dues_payment" would report money the chapter
    gave back as money it collected, and leave the member marked paid."""
    setup = await make_chapter_with("member")
    cycle = await _open_cycle(client, setup)
    entry = await _pay(client, setup, cycle, setup.member.id)
    await _correct(client, setup, entry, -25_000)

    payload = await _overview(client, setup)

    assert payload["dues"]["paid_members"] == 0
    assert payload["dues"]["outstanding_members"] == 2
    assert payload["dues"]["collected_cents"] == 0


async def test_a_partial_correction_leaves_the_member_paid(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Netting per member rather than "has any correction": someone refunded $10 of
    $250 has still paid, and should not reappear on a chase list."""
    setup = await make_chapter_with("member")
    cycle = await _open_cycle(client, setup)
    entry = await _pay(client, setup, cycle, setup.member.id)
    await _correct(client, setup, entry, -1_000)

    payload = await _overview(client, setup)

    assert payload["dues"]["paid_members"] == 1
    assert payload["dues"]["collected_cents"] == 24_000


async def test_the_current_cycle_is_the_most_recently_created_one(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Matches list_dues_cycles' ordering, and therefore treasurer.tsx's cycles[0].

    Picking the nearest due_date instead is defensible in isolation and would put a
    different cycle name on the President and Treasurer screens on the same day.
    """
    setup = await make_chapter_with("member")
    await _open_cycle(client, setup, "Fall 2025", 10_000)
    await _open_cycle(client, setup, "Spring 2026", 25_000)

    dues = (await _overview(client, setup))["dues"]

    assert dues["cycle_name"] == "Spring 2026"
    assert dues["amount_cents"] == 25_000


async def test_a_payment_against_an_older_cycle_is_not_counted(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    old = await _open_cycle(client, setup, "Fall 2025", 10_000)
    await _open_cycle(client, setup, "Spring 2026", 25_000)
    await _pay(client, setup, old, setup.member.id, 10_000)

    dues = (await _overview(client, setup))["dues"]

    assert dues["paid_members"] == 0
    assert dues["collected_cents"] == 0


# ---- attendance ----


async def test_members_with_absence_counts_people_not_absences(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    first = await _create_meeting(client, setup, "Week 1", SPRING)
    second = await _create_meeting(client, setup, "Week 2", SPRING + timedelta(days=7))
    await _mark(client, setup, first, [(setup.member.id, "absent")])
    await _mark(client, setup, second, [(setup.member.id, "absent")])

    attendance = (await _overview(client, setup))["attendance"]

    assert attendance["meetings_in_window"] == 2
    assert attendance["members_with_absence"] == 1


async def test_excused_is_not_an_absence(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The three statuses exist so that excused is not a euphemism for absent; folding
    them together would put members on a chase list they were told they were off."""
    setup = await make_chapter_with("member")
    meeting = await _create_meeting(client, setup)
    await _mark(client, setup, meeting, [(setup.member.id, "excused")])

    assert (await _overview(client, setup))["attendance"]["members_with_absence"] == 0


async def test_the_window_excludes_meetings_outside_it(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    old = await _create_meeting(client, setup, "Last year", SPRING - timedelta(days=400))
    await _mark(client, setup, old, [(setup.member.id, "absent")])
    await _create_meeting(client, setup, "This term", SPRING)

    payload = await _overview(
        client,
        setup,
        start=(SPRING - timedelta(days=30)).isoformat(),
        end=(SPRING + timedelta(days=30)).isoformat(),
    )

    assert payload["attendance"]["meetings_in_window"] == 1
    assert payload["attendance"]["members_with_absence"] == 0


async def test_a_dual_chapter_members_absence_elsewhere_is_not_counted_here(
    client: AsyncClient,
    make_chapter_with: MakeChapterWith,
    make_user: MakeUser,
) -> None:
    """THE LINE THIS ENDPOINT IS MOST LIKELY TO CROSS, and it passes every
    single-chapter test when it does.

    The natural-looking join - attendance by user, then out to `meetings` filtered on
    this chapter - leaves the meetings side NULL on a LEFT JOIN while `status` stays
    non-null, so the other chapter's absences survive the filter and get counted here.
    Joining attendance against meeting ids ALREADY filtered to this chapter is what
    makes that impossible. Same argument as attendance_summary's (c82).
    """
    home = await make_chapter_with("member")
    away = await make_chapter_with("member")
    traveller = home.member
    await _join(client, away, traveller)

    away_meeting = await _create_meeting(client, away, "Other chapter", SPRING)
    await _mark(client, away, away_meeting, [(traveller.id, "absent")])
    await _create_meeting(client, home, "Home chapter", SPRING)

    payload = await _overview(client, home)

    assert payload["attendance"]["meetings_in_window"] == 1
    assert payload["attendance"]["members_with_absence"] == 0


# ---- lineage ----


async def test_unconfirmed_edges_drop_when_the_little_confirms(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges",
        json={"big_user_id": setup.president.id, "little_user_id": setup.member.id},
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    assert (await _overview(client, setup))["lineage"]["unconfirmed_edges"] == 1

    confirmed = await client.post(
        f"/chapters/{setup.chapter_id}/lineage/edges/{created.json()['id']}/confirm",
        headers=setup.member.headers,
    )
    assert confirmed.status_code == 200, confirmed.text

    assert (await _overview(client, setup))["lineage"]["unconfirmed_edges"] == 0


# ---- invites ----


async def test_live_codes_counts_only_codes_that_could_still_be_redeemed(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """All three of c105's conditions together. A code that is merely unexpired is not
    live if it was revoked, and reporting it as live is how a president concludes a
    leaked code is still a problem after they already killed it."""
    setup = await make_chapter_with("president")
    live = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": "member", "max_uses": 5},
        headers=setup.president.headers,
    )
    assert live.status_code == 201, live.text
    doomed = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": "member", "max_uses": 3},
        headers=setup.president.headers,
    )
    assert doomed.status_code == 201, doomed.text
    revoked = await client.post(
        f"/chapters/{setup.chapter_id}/invites/revoke",
        json={"code": doomed.json()["code"]},
        headers=setup.president.headers,
    )
    assert revoked.status_code == 200, revoked.text

    invites = (await _overview(client, setup))["invites"]

    assert invites["live_codes"] == 1
    assert invites["remaining_uses"] == 5


async def test_remaining_uses_falls_as_a_code_is_redeemed(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """The number that decides whether something needs revoking is how many more
    people could still walk in, not how many codes exist."""
    setup = await make_chapter_with("president")
    invite = await client.post(
        f"/chapters/{setup.chapter_id}/invites",
        json={"role": "member", "max_uses": 2},
        headers=setup.president.headers,
    )
    assert invite.status_code == 201, invite.text
    joiner = await make_user("Joining Member")
    joined = await client.post(
        "/chapters/join", json={"code": invite.json()["code"]}, headers=joiner.headers
    )
    assert joined.status_code == 201, joined.text

    invites = (await _overview(client, setup))["invites"]

    assert invites["live_codes"] == 1
    assert invites["remaining_uses"] == 1
