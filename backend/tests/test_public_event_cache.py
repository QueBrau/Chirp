"""GET /public/events/{id} carries Cache-Control; nothing else does (board card c218).

WHY ONLY THIS ROUTE. It is the only read in the app that is unauthenticated AND
viewer-independent: PublicEventOut carries no field that varies by caller, so a SHARED
cache is safe here in a way it is not anywhere else. It is also the only read designed
to be pasted into a group chat, which is what makes one popular party thousands of
requests for a single id - each one three Postgres round trips today.

THE TTL IS A SAFETY NUMBER. The staleness that could hurt someone is serving "the party
is on" after it was called off, which sends people to an address. So a live event caches
briefly and a canceled one may cache far longer, because cancellation is terminal and
the dangerous direction does not exist in reverse.

The last test is the important one: it asserts caching was NOT added to the
authenticated event read. Those responses are per-viewer, and a shared cache in front of
one would serve a member's view of a chapter event to someone else.
"""
from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import MakeChapterWith

from tests.test_events import _event_body


async def _public_event(client: AsyncClient, setup, **create_kwargs) -> str:
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(visibility="public", **create_kwargs),
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def test_a_live_public_event_is_cacheable_by_shared_caches(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """`public`, not `private` — letting shared caches serve it is the whole point.

    A `private` directive would restrict it to the one browser that asked, which is
    exactly the fan-out this route needs absorbed.
    """
    setup = await make_chapter_with("member")
    event_id = await _public_event(client, setup)

    response = await client.get(f"/public/events/{event_id}")
    assert response.status_code == 200, response.text

    cache_control = response.headers.get("cache-control")
    assert cache_control is not None, "the public event route carries no freshness info"
    assert "public" in cache_control, (
        f"expected a shared-cacheable response, got: {cache_control}"
    )
    assert "max-age=60" in cache_control, (
        f"expected the 60s live-event TTL (c218), got: {cache_control}"
    )


async def test_a_canceled_public_event_may_be_cached_longer(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Cancellation is terminal, so the direction that could hurt someone is gone.

    There is no transition back to "on", so a long TTL here cannot send anyone to a
    party that was called off — the reverse of the risk the 60s number exists to bound.
    """
    setup = await make_chapter_with("member")
    event_id = await _public_event(client, setup)

    canceled = await client.post(
        f"/events/{event_id}/cancel", headers=setup.president.headers
    )
    assert canceled.status_code == 200, canceled.text

    response = await client.get(f"/public/events/{event_id}")
    assert response.status_code == 200, response.text
    assert response.json()["canceled_at"] is not None
    assert "max-age=3600" in response.headers.get("cache-control", ""), (
        f"canceled events should cache longer, got: {response.headers.get('cache-control')}"
    )


async def test_a_404_is_not_cached(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A non-public event 404s, and that 404 must NOT be cacheable.

    Visibility is editable: a host can flip a chapter event to public later. Caching the
    404 would leave shared caches serving "no such event" to everyone who followed the
    link in the window before it was widened — a self-inflicted outage on the exact
    request the route exists to serve.

    This passes today because the header is set only on the success path, after the
    not_found raise. It is pinned here so that ordering stays deliberate rather than
    incidental.
    """
    setup = await make_chapter_with("member")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json=_event_body(),  # defaults to the narrowest tier, 'chapter'
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text

    response = await client.get(f"/public/events/{created.json()['id']}")
    assert response.status_code == 404
    cache_control = response.headers.get("cache-control")
    assert cache_control is None or "no-store" in cache_control, (
        f"a 404 here must not be cached — visibility can be widened later, got: {cache_control}"
    )


async def test_the_authenticated_event_read_is_not_cacheable(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The guard: caching was added to exactly one route, and this is not it.

    GET /events/{id} answers differently depending on who asks — visibility tier,
    membership, and invites all gate it. A shared cache in front of this would serve one
    member's view to someone who should have been refused. c218 deliberately left every
    authenticated read alone, and this test is what makes a later "let's cache the reads"
    change fail loudly instead of leaking.
    """
    setup = await make_chapter_with("member")
    event_id = await _public_event(client, setup)

    response = await client.get(f"/events/{event_id}", headers=setup.member.headers)
    assert response.status_code == 200, response.text
    cache_control = response.headers.get("cache-control")
    assert cache_control is None or "no-store" in cache_control or "private" in cache_control, (
        "the authenticated event read must not be shared-cacheable — it varies by "
        f"caller. Got: {cache_control}"
    )
