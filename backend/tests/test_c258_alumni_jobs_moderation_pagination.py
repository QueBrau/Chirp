"""c258, final endpoints: alumni directory, job board, moderation queue.

All three grow with time, so all three take cursors rather than caps.

THE MODERATION ONE CARRIES THE REAL RISK and is why the filter moved server-side before
the cursor went on. The screen used to fetch every report and keep the open ones in the
client. Cursoring under that would have produced the worst outcome in this wave: a page
of already-resolved reports renders an EMPTY queue while real open reports sit on later
pages, so a moderator is told there is nothing to do while work is outstanding. Filtering
before paging means a page of open reports is a page of open reports.

Ties are MANUFACTURED here, never hoped for. Rows created over HTTP get distinct
microsecond timestamps, so a test that relies on the setup to produce a tie eventually
stops producing one - six times in this repo now. Construct the condition, assert it
exists, then rely on it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from httpx import AsyncClient
from sqlalchemy import text as sa_text

from tests.conftest import ApiUser, MakeChapterWith, MakeUser

TIED_AT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


async def _walk(client: AsyncClient, path: str, headers, page: int, key: str) -> list[str]:
    """Follow the cursor to exhaustion, collecting `key` from every row."""
    collected: list[str] = []
    cursor = ""
    joiner = "&" if "?" in path else "?"
    for _ in range(20):
        response = await client.get(
            f"{path}{joiner}limit={page}{cursor}", headers=headers
        )
        assert response.status_code == 200, response.text
        rows = response.json()
        if not rows:
            break
        collected += [r[key] for r in rows]
        cursor = f"&before={rows[-1]['created_at']}&before_id={rows[-1]['id']}"
        if len(rows) < page:
            break
    return collected


# ---------------------------------------------------------------------------
# moderation queue - the one with real consequences
# ---------------------------------------------------------------------------


async def _seed_reports(setup, *, resolved: int, open_: int) -> list[str]:
    """Mostly-resolved queue with a few open reports, all sharing one timestamp.

    The shape matters: with the old client-side filter and a cursor, page one would be
    all resolved rows and the moderator would see an empty queue.
    """
    from app.db import get_session_factory

    campus = (await _campus_of(setup))
    open_ids: list[str] = []
    async with get_session_factory()() as session:
        for i in range(resolved + open_):
            report_id = uuid.uuid4()
            is_open = i >= resolved
            await session.execute(
                sa_text(
                    "INSERT INTO content_reports (id, reporter_id, campus_id, target_type,"
                    " target_id, reason, status, created_at)"
                    " VALUES (:id, :rep, :campus, 'post', :target, :reason, :status, :at)"
                ),
                {
                    "id": report_id,
                    "rep": uuid.UUID(setup.president.id),
                    "campus": campus,
                    "target": uuid.uuid4(),
                    "reason": f"report-{i:03d}",
                    "status": "open" if is_open else "dismissed",
                    "at": TIED_AT,
                },
            )
            if is_open:
                open_ids.append(str(report_id))
        await session.commit()
    return open_ids


async def _campus_of(setup) -> uuid.UUID:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        row = await session.execute(
            sa_text("SELECT campus_id FROM chapters WHERE id = :cid"),
            {"cid": uuid.UUID(setup.chapter_id)},
        )
        return row.scalar_one()


async def test_an_open_queue_is_not_hidden_behind_resolved_reports(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """THE REGRESSION THIS ORDERING PREVENTS. 60 resolved reports, 3 open. A page of 50
    filtered server-side returns the OPEN ones; filtering in the client after paging
    would have returned 50 resolved rows and an empty-looking queue."""
    setup = await make_chapter_with("member")
    open_ids = await _seed_reports(setup, resolved=60, open_=3)

    page = await client.get(
        "/moderation/reports?status=open&limit=50", headers=setup.president.headers
    )
    assert page.status_code == 200, page.text
    rows = page.json()
    assert {r["id"] for r in rows} == set(open_ids), (
        "every open report must be on the first page when filtering server-side"
    )
    assert all(r["status"] == "open" for r in rows)


async def test_reports_page_across_a_shared_timestamp(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """All rows share one created_at, so a timestamp-only cursor drops or repeats the
    tied run at the boundary."""
    setup = await make_chapter_with("member")
    await _seed_reports(setup, resolved=0, open_=11)

    listed = await client.get("/moderation/reports", headers=setup.president.headers)
    assert len({r["created_at"] for r in listed.json()}) == 1, "the ties must be real"

    collected = await _walk(
        client, "/moderation/reports", setup.president.headers, 4, "reason"
    )
    assert len(collected) == 11, "no report may be skipped or repeated"
    assert len(set(collected)) == 11


# ---------------------------------------------------------------------------
# job board
# ---------------------------------------------------------------------------


async def test_jobs_page_across_a_shared_timestamp(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    from app.db import get_session_factory

    titles = [f"job-{i:03d}" for i in range(9)]
    async with get_session_factory()() as session:
        for title in titles:
            await session.execute(
                sa_text(
                    "INSERT INTO job_posts (id, chapter_id, posted_by, title, company,"
                    " description, created_at)"
                    " VALUES (:id, :cid, :by, :title, 'ACME', 'A role', :at)"
                ),
                {
                    "id": uuid.uuid4(), "cid": uuid.UUID(setup.chapter_id),
                    "by": uuid.UUID(setup.president.id), "title": title, "at": TIED_AT,
                },
            )
        await session.commit()

    listed = await client.get("/jobs", headers=setup.president.headers)
    assert listed.status_code == 200, listed.text
    assert len({r["created_at"] for r in listed.json()}) == 1, "the ties must be real"

    collected = await _walk(client, "/jobs", setup.president.headers, 3, "title")
    assert sorted(collected) == sorted(titles), "no posting may be skipped or repeated"


# ---------------------------------------------------------------------------
# alumni directory - the one that had no ordering at all
# ---------------------------------------------------------------------------


async def test_directory_pages_deterministically(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """This query had NO order by before c258, so a cursor over it was meaningless and a
    LIMIT would have truncated arbitrary rows.

    It also has no created_at to sort by - a profile is not an event - which my first
    attempt missed: I cursored on AlumniProfile.created_at, a column that does not
    exist, and only this test caught it. `create_app()` builds fine either way, because
    the query is assembled inside the handler at request time; "the app builds" is not
    "the endpoint works". The cursor is now the primary key alone, which is unique and
    therefore needs no tie-break."""
    setup = await make_chapter_with("member")
    from app.db import get_session_factory

    peers: list[ApiUser] = []
    for i in range(7):
        peer = await make_user(f"Alum {i}")
        invite = await client.post(
            f"/chapters/{setup.chapter_id}/invites", json={"role": "alumni"},
            headers=setup.president.headers,
        )
        joined = await client.post(
            "/chapters/join", json={"code": invite.json()["code"]}, headers=peer.headers
        )
        assert joined.status_code == 201, joined.text
        peers.append(peer)

    async with get_session_factory()() as session:
        for peer in peers:
            await session.execute(
                sa_text(
                    "INSERT INTO alumni_profiles (user_id, grad_year, open_to_mentoring)"
                    " VALUES (:u, 2025, false)"
                ),
                {"u": uuid.UUID(peer.id)},
            )
        await session.commit()

    full = await client.get("/alumni/directory", headers=setup.president.headers)
    assert full.status_code == 200, full.text
    expected = {r["user_id"] for r in full.json()}
    assert len(expected) == len(peers), "every peer profile must be visible unpaged"

    collected: list[str] = []
    cursor = ""
    for _ in range(20):
        response = await client.get(
            f"/alumni/directory?limit=3{cursor}", headers=setup.president.headers
        )
        assert response.status_code == 200, response.text
        rows = response.json()
        if not rows:
            break
        collected += [r["user_id"] for r in rows]
        # SINGLE-COLUMN cursor: alumni_profiles has no created_at, and user_id is the
        # primary key, so it is unique and needs no tie-break companion.
        cursor = f"&before_id={rows[-1]['user_id']}"
        if len(rows) < 3:
            break

    assert set(collected) == expected, "no profile may be skipped or repeated"
    assert len(collected) == len(expected)


async def test_limits_are_bounded(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    for path in ("/alumni/directory", "/jobs", "/moderation/reports"):
        for bad in (0, 5000):
            response = await client.get(
                f"{path}?limit={bad}", headers=setup.president.headers
            )
            assert response.status_code == 422, f"{path} limit={bad}: {response.text}"
