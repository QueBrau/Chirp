"""The post-write routes must finalize media OFF the event loop (board card c223).

c211 established both the problem and the shape of the fix for the two SIGNING call
sites in routers/media.py: a synchronous function that crosses the network, called
directly from an async route, stalls the ONE event loop this process serves every
in-flight request on (the Dockerfile runs uvicorn with no --workers, at
containerConcurrency 80). That card did not touch finalize_media_object(), which is the
same shape - a GCS copy_blob followed by a delete - reached from all three post-write
routes through feed._finalize_media(). c221 wired the new avatar route correctly and
flagged this one rather than widening its own diff. This file is the evidence for
closing it.

WHY THESE TESTS EXIST AT ALL, since c223 changes no behaviour: every other test in the
suite passes identically before and after the fix, by construction. Same inputs, same
outputs, same call order - a green suite is a no-regression check and says nothing about
whether the work moved off the loop. These assert the property directly instead, on the
same reasoning (and in the same shape) as c211's own off-loop tests in
test_media_upload_url.py.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from httpx import AsyncClient

from app.routers import feed
from app.services import storage_service
from tests.conftest import MakeChapterWith

TEST_BUCKET = "chirps-prod-media"


def _configure_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", TEST_BUCKET)


def _permanent_url(user_id, suffix: str) -> str:
    """What the real finalize_media_object() would have returned for this move.

    The fakes below stand in for the whole function rather than for the GCS client, so
    they have to produce a url validate_media_urls() accepts - the route re-checks its
    OWN output against the permanent prefix before writing it to a post row.
    """
    return f"https://storage.googleapis.com/{TEST_BUCKET}/posts/{user_id}/{suffix}"


async def test_post_create_runs_finalize_off_the_event_loop_thread(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct proof of the to_thread wiring: the function the route calls must actually
    execute on a thread other than the one driving this test's event loop."""
    _configure_bucket(monkeypatch)
    setup = await make_chapter_with("member")
    test_thread = threading.current_thread()
    calling_threads: list[threading.Thread] = []

    def _fake_finalize(user_id: str, tmp_object_name: str, **kwargs) -> str:
        calling_threads.append(threading.current_thread())
        return _permanent_url(user_id, "abc123.jpg")

    monkeypatch.setattr(feed, "finalize_media_object", _fake_finalize)

    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={
            "body": "check out this pic",
            "media_object_names": [f"tmp/{setup.member.id}/abc123.jpg"],
            "post_type": "photo",
        },
        headers=setup.member.headers,
    )

    assert created.status_code == 201, created.text
    assert len(calling_threads) == 1
    assert calling_threads[0] is not test_thread


async def test_a_slow_finalize_does_not_stall_an_unrelated_request(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim c223 actually makes, asserted rather than asserted-about.

    One post create is held inside a worker-thread finalizer. A completely unrelated feed
    read is issued only AFTER that finalizer is confirmed in flight, and must come back
    before the finalizer is released. Called inline, the finalizer is rejected immediately
    by its worker-versus-event-loop assertion instead of waiting for the reader.

    The reader's completion is asserted against the finalize call's OWN start/end marks,
    not against wall-clock elapsed time. The worker and pending request task are always
    released and drained on success or failure, so a broken implementation cannot strand
    a thread.
    """
    _configure_bucket(monkeypatch)
    setup = await make_chapter_with("member")
    in_finalize = threading.Event()
    release_finalize = threading.Event()
    finalize_finished = threading.Event()
    loop_thread_id = threading.get_ident()
    marks: dict[str, float] = {}

    def _slow_finalize(user_id: str, tmp_object_name: str, **kwargs) -> str:
        marks["finalize_start"] = time.monotonic()
        in_finalize.set()
        try:
            if threading.get_ident() == loop_thread_id:
                raise AssertionError("finalizer ran on the event-loop thread")
            if not release_finalize.wait(timeout=5):
                raise AssertionError("finalizer release deadline expired")
            marks["finalize_end"] = time.monotonic()
            return _permanent_url(user_id, "abc123.jpg")
        finally:
            finalize_finished.set()

    monkeypatch.setattr(feed, "finalize_media_object", _slow_finalize)

    async def _create_the_photo_post():
        return await client.post(
            f"/chapters/{setup.chapter_id}/posts",
            json={
                "body": "check out this pic",
                "media_object_names": [f"tmp/{setup.member.id}/abc123.jpg"],
                "post_type": "photo",
            },
            headers=setup.member.headers,
        )

    async def _read_the_feed_while_that_is_in_flight():
        # Event.wait runs in a worker so the test never blocks the event loop while it
        # waits for the finalizer.
        if not await asyncio.to_thread(in_finalize.wait, 5):
            raise AssertionError("finalizer did not start before reader deadline")
        try:
            response = await client.get(
                f"/chapters/{setup.chapter_id}/posts", headers=setup.member.headers
            )
            marks["read_done"] = time.monotonic()
            return response
        finally:
            release_finalize.set()

    create_task = asyncio.create_task(_create_the_photo_post())
    read_task = asyncio.create_task(_read_the_feed_while_that_is_in_flight())
    tasks = (create_task, read_task)
    try:
        created, read = await asyncio.wait_for(
            asyncio.gather(*tasks),
            timeout=10,
        )
    finally:
        finalizer_entered = in_finalize.is_set()
        release_finalize.set()
        if not finalizer_entered:
            # Wake a reader that is still waiting for the create request to enter the
            # finalizer, without later mistaking this synthetic signal for worker entry.
            in_finalize.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if finalizer_entered:
            assert await asyncio.to_thread(finalize_finished.wait, 5), (
                "finalizer worker did not drain before test cleanup"
            )

    assert created.status_code == 201, created.text
    assert read.status_code == 200, read.text
    assert marks["finalize_start"] < marks["read_done"] < marks["finalize_end"], marks


async def test_two_concurrent_photo_posts_overlap_instead_of_serializing(
    client: AsyncClient, make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two posts being created at once must not queue their copies behind each other.

    This is the per-request half of the same property: with the call inline, the second
    request's copy cannot start until the first has returned, because both run on the one
    loop. Both finalizers must reach a two-party barrier before either can return.
    This proves overlap without hoping CI schedules the second request within a fixed
    sleep window. A missing peer breaks the barrier after five seconds, so an inline
    or serialized regression fails instead of deadlocking the test.
    """
    _configure_bucket(monkeypatch)
    setup = await make_chapter_with("member")
    lock = threading.Lock()
    rendezvous = threading.Barrier(2, timeout=5)
    entries: list[tuple[str, int]] = []
    loop_thread_id = threading.get_ident()

    def _rendezvous_finalize(user_id: str, tmp_object_name: str, **kwargs) -> str:
        with lock:
            entries.append((user_id, threading.get_ident()))
        try:
            rendezvous.wait()
        except threading.BrokenBarrierError:
            raise AssertionError("Concurrent finalizers did not overlap at the barrier") from None
        return _permanent_url(user_id, "abc123.jpg")

    monkeypatch.setattr(feed, "finalize_media_object", _rendezvous_finalize)

    async def _create_as(member):
        return await client.post(
            f"/chapters/{setup.chapter_id}/posts",
            json={
                "body": "check out this pic",
                "media_object_names": [f"tmp/{member.id}/abc123.jpg"],
                "post_type": "photo",
            },
            headers=member.headers,
        )

    # Two DIFFERENT callers of the same chapter (the fixture's member and the president
    # who created it), because the tmp/ prefix gate is caller-scoped - one user cannot
    # name the other's upload, which is the whole point of validate_media_object_names.
    try:
        responses = await asyncio.wait_for(
            asyncio.gather(
                _create_as(setup.member), _create_as(setup.president),
                return_exceptions=True,
            ),
            timeout=15,
        )
    finally:
        # Release a worker if a request fails before its peer reaches the barrier.
        rendezvous.abort()

    for response in responses:
        assert not isinstance(response, BaseException), str(response)
        assert response.status_code == 201, response.text
    assert len(entries) == 2
    assert {user_id for user_id, _ in entries} == {setup.member.id, setup.president.id}
    assert len({thread_id for _, thread_id in entries}) == 2
    assert all(thread_id != loop_thread_id for _, thread_id in entries)
