"""app.jobs.media_reconcile: reclaim posts/ objects nothing references (board c153).

The orphan these tests are about is CREATED by PATCH and pinned by two characterizing
tests in test_media_url_validation.py — read those first for why the route cannot
just delete the object itself (c132: the API's service account has no delete grant on
posts/, on purpose).

GCS is faked at the same boundary the other media tests fake it: storage_service's
lazy client. The job's own logic — the diff, the age floor, the abort guard, the page
walk, the 404 handling — all runs for real against that fake. What is NOT provable
here, on record rather than silently skipped: that the separate service account's
posts/-conditioned delete grant actually authorizes these deletes against the real
bucket. That needs the real bucket and the real SA, and it is the manager's live pass
after merge — the same split c132 used, where three real-bucket-only bugs got through
a green fake-backed suite.
"""
from __future__ import annotations

import argparse
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from google.api_core.exceptions import NotFound
from sqlalchemy import text

from app.db import get_session_factory
from app.jobs import media_reconcile
from app.jobs.media_reconcile import ReconcileAborted, reconcile_orphaned_media
from app.models.social import Post
from app.services import storage_service
from tests.conftest import MakeChapterWith

TEST_BUCKET = "chirps-prod-media"
OTHER_BUCKET = "some-other-bucket"
NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", ["0", "-1", "-24", "not-a-number"])
def test_cli_rejects_a_non_positive_or_invalid_age_floor(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        media_reconcile._positive_hours(value)


@pytest.mark.parametrize("value, expected", [("1", 1), ("24", 24), ("168", 168)])
def test_cli_accepts_a_positive_age_floor(value: str, expected: int) -> None:
    assert media_reconcile._positive_hours(value) == expected


def _url(object_name: str, bucket: str = TEST_BUCKET) -> str:
    return f"https://storage.googleapis.com/{bucket}/{object_name}"


class _FakeBlob:
    """Only the surface the job actually touches: name, time_created, delete()."""

    def __init__(self, name: str, time_created: datetime | None, captured: dict) -> None:
        self.name = name
        self.time_created = time_created
        self._captured = captured

    def delete(self) -> None:
        self._captured.setdefault("delete_attempts", []).append(self.name)
        if self.name in self._captured.get("delete_404_names", ()):
            raise NotFound(f"simulated already-deleted {self.name}")


class _FakePageIterator:
    """Stands in for the HTTPIterator client.list_blobs returns.

    Only `.pages` is implemented, deliberately: if the job ever stops walking pages
    explicitly and starts iterating the iterator directly, these tests fail loudly
    with a TypeError instead of quietly passing against a single page.
    """

    def __init__(self, pages: list[list[_FakeBlob]]) -> None:
        self._pages = pages

    @property
    def pages(self):
        return iter(self._pages)


def _install_fake_gcs(
    monkeypatch: pytest.MonkeyPatch, pages: list[list[_FakeBlob]], captured: dict
) -> None:
    def list_blobs(bucket_name, prefix=None):
        captured.setdefault("list_calls", []).append({"bucket": bucket_name, "prefix": prefix})
        return _FakePageIterator(pages)

    monkeypatch.setattr(
        storage_service, "_storage_client", lambda: SimpleNamespace(list_blobs=list_blobs)
    )


def _blobs(captured: dict, *specs: tuple[str, datetime | None]) -> list[_FakeBlob]:
    return [_FakeBlob(name, created, captured) for name, created in specs]


@pytest.fixture(autouse=True)
def _configure_bucket(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", TEST_BUCKET)
    storage_service._client = None
    yield
    storage_service._client = None


async def _insert_post(
    chapter_id: str,
    author_id: str,
    *,
    media_urls: list[str] | None,
    deleted_at: datetime | None = None,
) -> None:
    """Insert a post row carrying exactly the media_urls a case needs.

    Straight through the ORM rather than the API because the API only ever writes urls
    it just minted itself — these cases need urls naming another bucket, or naming an
    object that was already soft-deleted, which no API call can produce.
    """
    async with get_session_factory()() as session:
        campus_id = (
            await session.execute(
                text("SELECT campus_id FROM chapters WHERE id = :chapter_id"),
                {"chapter_id": chapter_id},
            )
        ).scalar_one()
        session.add(
            Post(
                chapter_id=uuid.UUID(chapter_id),
                campus_id=campus_id,
                author_id=uuid.UUID(author_id),
                body="c153 fixture",
                media_urls=media_urls,
                deleted_at=deleted_at,
            )
        )
        await session.commit()


async def _reconcile(**kwargs):
    async with get_session_factory()() as session:
        return await reconcile_orphaned_media(session, now=NOW, **kwargs)


async def test_a_referenced_object_is_never_eligible(
    make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = await make_chapter_with("member")
    name = f"posts/{setup.member.id}/keep-me.jpg"
    await _insert_post(setup.chapter_id, setup.member.id, media_urls=[_url(name)])

    captured: dict = {}
    _install_fake_gcs(monkeypatch, [_blobs(captured, (name, NOW - timedelta(days=30)))], captured)

    result = await _reconcile()

    assert result.scanned == 1
    assert result.referenced == 1
    assert result.eligible == ()
    assert captured.get("delete_attempts") is None
    # The listing is scoped to the permanent prefix, never the bucket root - tmp/ is
    # the lifecycle rule's job and must not be swept by this one.
    assert captured["list_calls"] == [{"bucket": TEST_BUCKET, "prefix": "posts/"}]


async def test_an_unreferenced_old_object_is_eligible_but_a_dry_run_deletes_nothing(
    make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry run is the default: it must produce the list and touch nothing."""
    setup = await make_chapter_with("member")
    orphan = f"posts/{setup.member.id}/orphaned.jpg"

    captured: dict = {}
    _install_fake_gcs(
        monkeypatch, [_blobs(captured, (orphan, NOW - timedelta(days=30)))], captured
    )

    result = await _reconcile()

    assert result.eligible == (orphan,)
    assert result.deleted == ()
    assert result.already_gone == ()
    assert captured.get("delete_attempts") is None


async def test_an_unreferenced_object_inside_the_age_floor_is_left_alone(
    make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race the age floor exists for: finalize_media_object() moves the object to
    posts/ BEFORE the row naming it is committed, so a legitimate object is genuinely
    unreferenced for a moment. Anything younger than the floor is presumed in flight."""
    await make_chapter_with("member")
    fresh = "posts/someone/just-uploaded.jpg"

    captured: dict = {}
    _install_fake_gcs(
        monkeypatch, [_blobs(captured, (fresh, NOW - timedelta(hours=1)))], captured
    )

    result = await _reconcile(delete=True)

    assert result.too_young == 1
    assert result.eligible == ()
    assert captured.get("delete_attempts") is None


async def test_an_object_with_no_readable_creation_time_is_never_deleted(
    make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """blob.time_created is None when the property was never loaded. An unknown age is
    not evidence of being old, so it counts as too young - forever, if need be."""
    await make_chapter_with("member")

    captured: dict = {}
    _install_fake_gcs(monkeypatch, [_blobs(captured, ("posts/x/ageless.jpg", None))], captured)

    result = await _reconcile(delete=True)

    assert result.too_young == 1
    assert result.eligible == ()
    assert captured.get("delete_attempts") is None


async def test_delete_removes_the_eligible_set_and_only_the_eligible_set(
    make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three populations in one bucket, so the assertion is about the boundary
    between them rather than about one case in isolation."""
    setup = await make_chapter_with("member")
    referenced = f"posts/{setup.member.id}/live.jpg"
    orphan = f"posts/{setup.member.id}/orphan.jpg"
    fresh = f"posts/{setup.member.id}/in-flight.jpg"
    await _insert_post(setup.chapter_id, setup.member.id, media_urls=[_url(referenced)])

    captured: dict = {}
    _install_fake_gcs(
        monkeypatch,
        [
            _blobs(
                captured,
                (referenced, NOW - timedelta(days=30)),
                (orphan, NOW - timedelta(days=30)),
                (fresh, NOW - timedelta(minutes=5)),
            )
        ],
        captured,
    )

    result = await _reconcile(delete=True)

    assert result.scanned == 3
    assert result.referenced == 1
    assert result.too_young == 1
    assert result.eligible == (orphan,)
    assert result.deleted == (orphan,)
    assert captured["delete_attempts"] == [orphan]


async def test_every_page_of_the_listing_is_walked(
    make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bucket bigger than one page is exactly where a wrong listing becomes a wrong
    delete set: objects on page two would look absent, not protected."""
    setup = await make_chapter_with("member")
    page_two_referenced = "posts/u/page2-live.jpg"
    await _insert_post(
        setup.chapter_id, setup.member.id, media_urls=[_url(page_two_referenced)]
    )

    old = NOW - timedelta(days=30)
    captured: dict = {}
    _install_fake_gcs(
        monkeypatch,
        [
            _blobs(captured, ("posts/u/page1-a.jpg", old), ("posts/u/page1-b.jpg", old)),
            _blobs(captured, (page_two_referenced, old), ("posts/u/page2-orphan.jpg", old)),
            _blobs(captured, ("posts/u/page3-orphan.jpg", old)),
        ],
        captured,
    )

    result = await _reconcile(delete=True)

    assert result.scanned == 5
    assert result.referenced == 1
    assert result.deleted == (
        "posts/u/page1-a.jpg",
        "posts/u/page1-b.jpg",
        "posts/u/page2-orphan.jpg",
        "posts/u/page3-orphan.jpg",
    )


async def test_a_delete_that_404s_counts_as_success_not_failure(
    make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Already gone is the state this job wanted. It must not abort the run or count
    as an error, and the objects after it in the batch must still be processed."""
    await make_chapter_with("member")
    gone = "posts/u/raced-away.jpg"
    still_there = "posts/u/really-here.jpg"

    old = NOW - timedelta(days=30)
    captured: dict = {"delete_404_names": {gone}}
    _install_fake_gcs(monkeypatch, [_blobs(captured, (gone, old), (still_there, old))], captured)

    result = await _reconcile(delete=True)

    assert result.eligible == (gone, still_there)
    assert result.already_gone == (gone,)
    assert result.deleted == (still_there,)
    assert captured["delete_attempts"] == [gone, still_there]


async def test_media_urls_that_all_name_a_different_bucket_abort_the_run(
    make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one input that turns this job into a catastrophe: point it at the wrong
    bucket and every stored reference stops resolving, so every object looks orphaned.
    Aborting is the correct answer, and the counts have to be in the log or the
    misconfiguration is not diagnosable from output alone."""
    setup = await make_chapter_with("member")
    await _insert_post(
        setup.chapter_id,
        setup.member.id,
        media_urls=[_url("posts/u/elsewhere.jpg", bucket=OTHER_BUCKET)],
    )

    captured: dict = {}
    _install_fake_gcs(
        monkeypatch,
        [_blobs(captured, ("posts/u/looks-orphaned.jpg", NOW - timedelta(days=30)))],
        captured,
    )

    with caplog.at_level(logging.ERROR, logger=media_reconcile.logger.name):
        with pytest.raises(ReconcileAborted):
            await _reconcile(delete=True)

    assert captured.get("delete_attempts") is None
    # The abort has to be diagnosable from job logs alone: both counts, the bucket it
    # was actually configured with, and a pointer to what to do next.
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "holds 1 url(s)" in logged
    assert "0 of them in a form this job could not parse" in logged
    assert "0 resolve to bucket" in logged
    assert TEST_BUCKET in logged
    assert "runbook" in logged
    assert "c153" in logged


async def test_one_row_naming_another_bucket_is_ignored_without_aborting(
    make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mixed case the all-rows abort guard does NOT cover: as long as something
    resolves, a row pointing somewhere else is simply not our business. It must not
    trip the guard, and it must not protect a same-named object in OUR bucket - the
    bucket is part of the identity, not decoration."""
    setup = await make_chapter_with("member")
    ours = f"posts/{setup.member.id}/ours.jpg"
    theirs = "posts/u/theirs.jpg"
    await _insert_post(setup.chapter_id, setup.member.id, media_urls=[_url(ours)])
    await _insert_post(
        setup.chapter_id, setup.member.id, media_urls=[_url(theirs, bucket=OTHER_BUCKET)]
    )

    old = NOW - timedelta(days=30)
    captured: dict = {}
    _install_fake_gcs(monkeypatch, [_blobs(captured, (ours, old), (theirs, old))], captured)

    result = await _reconcile(delete=True)

    assert result.referenced == 1
    assert result.deleted == (theirs,)


async def test_a_soft_deleted_post_still_protects_its_object(
    make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A soft-deleted post is restorable and still reviewable by moderation, so its
    photo is not garbage yet. It becomes collectable only once app.jobs.purge hard-
    deletes the row, on a later pass of this job."""
    setup = await make_chapter_with("member")
    name = f"posts/{setup.member.id}/soft-deleted.jpg"
    await _insert_post(
        setup.chapter_id,
        setup.member.id,
        media_urls=[_url(name)],
        deleted_at=NOW - timedelta(days=5),
    )

    captured: dict = {}
    _install_fake_gcs(monkeypatch, [_blobs(captured, (name, NOW - timedelta(days=30)))], captured)

    result = await _reconcile(delete=True)

    assert result.referenced == 1
    assert result.deleted == ()
    assert captured.get("delete_attempts") is None


async def test_an_empty_posts_table_does_not_trip_the_abort_guard(
    make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No post has a photo is a legitimate state, not a misconfiguration - the guard
    keys on urls that exist and fail to resolve, not on an empty reference set."""
    await make_chapter_with("member")
    orphan = "posts/u/nothing-references-me.jpg"

    captured: dict = {}
    _install_fake_gcs(
        monkeypatch, [_blobs(captured, (orphan, NOW - timedelta(days=30)))], captured
    )

    result = await _reconcile(delete=True)

    assert result.deleted == (orphan,)


async def test_an_unconfigured_bucket_aborts_before_touching_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", None)

    with pytest.raises(ReconcileAborted):
        await _reconcile(delete=True)


# ---------------------------------------------------------------------------
# Legacy url forms. posts.media_urls has three eras and only the newest one is a
# format guarantee: pre-c139 rows were never validated, c139..c132 rows were
# validated against the BUCKET ROOT only. A reference this job fails to parse is
# not a harmless miss - it is a live photo that looks unreferenced and gets
# deleted, so every one of these is a data-loss test, not a parsing test.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored_form",
    [
        "https://storage.cloud.google.com/{bucket}/{name}",
        "https://{bucket}.storage.googleapis.com/{name}",
        "gs://{bucket}/{name}",
        "https://storage.googleapis.com/{bucket}/{name}?generation=1700000000000000",
        # JSON API form. The object name is PERCENT-ENCODED here (posts%2Fu%2Fa.jpg),
        # so a resolver that returns the literal path segment protects nothing.
        "https://storage.googleapis.com/storage/v1/b/{bucket}/o/{encoded_name}",
        "https://storage.googleapis.com/download/storage/v1/b/{bucket}/o/{encoded_name}?alt=media",
    ],
    ids=[
        "authenticated-host",
        "virtual-hosted",
        "gs-uri",
        "with-query-string",
        "json-api-encoded",
        "json-api-download-encoded",
    ],
)
async def test_an_alternate_url_form_still_protects_its_object(
    make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch, stored_form: str
) -> None:
    """Each of these can name a real object in our bucket, and a client could have
    stored any of them before c132. None may be read as 'references nothing'."""
    setup = await make_chapter_with("member")
    name = f"posts/{setup.member.id}/legacy-form.jpg"
    await _insert_post(
        setup.chapter_id,
        setup.member.id,
        media_urls=[
            stored_form.format(
                bucket=TEST_BUCKET, name=name, encoded_name=quote(name, safe="")
            )
        ],
    )

    captured: dict = {}
    _install_fake_gcs(monkeypatch, [_blobs(captured, (name, NOW - timedelta(days=30)))], captured)

    result = await _reconcile(delete=True)

    assert result.deleted == ()
    assert captured.get("delete_attempts") is None


async def test_an_external_url_row_neither_crashes_nor_orphans_anything(
    make_chapter_with: MakeChapterWith, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-c139 row can hold an arbitrary external url. It references nothing in our
    bucket, so it protects nothing and endangers nothing - but it must be counted as
    unresolved rather than silently treated as a clean 'no reference'."""
    setup = await make_chapter_with("member")
    live = f"posts/{setup.member.id}/live.jpg"
    orphan = f"posts/{setup.member.id}/orphan.jpg"
    await _insert_post(setup.chapter_id, setup.member.id, media_urls=[_url(live)])
    await _insert_post(
        setup.chapter_id, setup.member.id, media_urls=["https://evil.example.com/tracker.png"]
    )

    old = NOW - timedelta(days=30)
    captured: dict = {}
    _install_fake_gcs(monkeypatch, [_blobs(captured, (live, old), (orphan, old))], captured)

    result = await _reconcile(delete=True)

    assert result.referenced == 1
    assert result.unresolved_values == 1
    assert result.deleted == (orphan,)


async def test_a_malformed_stored_value_is_logged_and_skipped_without_crashing(
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pre-c139 means literally anything, including strings that are not urls.

    list_eligible=True (board c414): the stored value only reaches the log when a
    human asked for it - see test_default_run_omits_object_names_and_stored_values_
    from_logs below for the default (scheduled) case, where it must NOT appear.
    """
    setup = await make_chapter_with("member")
    live = f"posts/{setup.member.id}/live.jpg"
    await _insert_post(setup.chapter_id, setup.member.id, media_urls=[_url(live)])
    await _insert_post(setup.chapter_id, setup.member.id, media_urls=["not a url at all"])

    captured: dict = {}
    _install_fake_gcs(monkeypatch, [_blobs(captured, (live, NOW - timedelta(days=30)))], captured)

    with caplog.at_level(logging.WARNING, logger=media_reconcile.logger.name):
        result = await _reconcile(delete=True, list_eligible=True)

    assert result.unresolved_values == 1
    assert result.deleted == ()
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "not a recognized GCS reference" in logged
    assert "not a url at all" in logged


async def test_an_unparsed_value_still_protects_an_object_by_raw_match(
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The last backstop, for a url form nobody enumerated. The object name appears
    verbatim inside the stored value, so it is protected even though the resolver
    returned nothing for that value - and the run says so, because the backstop firing
    means the resolver has a gap worth closing.

    list_eligible=True (board c414): the protection itself (protected_by_raw_match,
    deleted==()) does not depend on this flag - only whether the object name reaches
    the log does. See test_default_run_omits_object_names_and_stored_values_from_logs
    below for the default case.
    """
    setup = await make_chapter_with("member")
    name = f"posts/{setup.member.id}/unknown-form.jpg"
    await _insert_post(
        setup.chapter_id,
        setup.member.id,
        media_urls=[f"https://cdn.example.net/proxy?target={TEST_BUCKET}/{name}"],
    )
    # A second, canonical row so the abort guard (zero of N urls resolve) does not
    # fire first - this test is about the backstop, not about the guard.
    await _insert_post(
        setup.chapter_id, setup.member.id, media_urls=[_url(f"posts/{setup.member.id}/other.jpg")]
    )

    captured: dict = {}
    _install_fake_gcs(monkeypatch, [_blobs(captured, (name, NOW - timedelta(days=30)))], captured)

    with caplog.at_level(logging.WARNING, logger=media_reconcile.logger.name):
        result = await _reconcile(delete=True, list_eligible=True)

    assert result.protected_by_raw_match == 1
    assert result.deleted == ()
    assert captured.get("delete_attempts") is None
    assert "resolve_object_names() should learn this form" in "\n".join(
        r.getMessage() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# D1 (board c414): exactly one structured aggregate JSON line to stdout, every run,
# success or abort, counts only. Cloud Run Jobs parses this into jsonPayload; the two
# infra/monitoring/metrics/media-reconcile-*-runs.json log metrics filter on it.
# ---------------------------------------------------------------------------


def _aggregate_lines(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    """Every stdout line this run printed that parses as a JSON object.

    Deliberately re-parses raw stdout rather than calling a helper the production
    code exposes - a regression that stops calling json.dumps (e.g. prints a plain
    f-string instead) must be caught here, not laundered through a shared formatter.
    """
    out = capsys.readouterr().out
    lines = []
    for raw in out.splitlines():
        try:
            lines.append(json.loads(raw))
        except (json.JSONDecodeError, ValueError):
            continue
    return lines


async def test_aggregate_line_is_emitted_once_with_documented_keys_on_a_dry_run(
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup = await make_chapter_with("member")
    orphan = f"posts/{setup.member.id}/orphaned.jpg"
    captured: dict = {}
    _install_fake_gcs(
        monkeypatch, [_blobs(captured, (orphan, NOW - timedelta(days=30)))], captured
    )

    await _reconcile()

    lines = _aggregate_lines(capsys)
    assert len(lines) == 1
    line = lines[0]
    assert set(line) == set(media_reconcile.AGGREGATE_KEYS)
    assert line["schema_version"] == 1
    assert line["signal_family"] == "chirp_job"
    assert line["event"] == "media_reconcile_aggregate"
    assert line["mode"] == "dry_run"
    assert line["outcome"] == "completed"
    assert line["eligible"] == 1
    assert line["deleted"] == 0
    int_keys = set(media_reconcile.AGGREGATE_KEYS) - {
        "schema_version", "signal_family", "event", "mode", "outcome",
    }
    for key in int_keys:
        assert isinstance(line[key], int) and not isinstance(line[key], bool)
    # No object name or user id leaked into the machine-readable line.
    assert orphan not in json.dumps(line)
    assert str(setup.member.id) not in json.dumps(line)


async def test_aggregate_line_is_emitted_once_on_a_delete_run(
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup = await make_chapter_with("member")
    orphan = f"posts/{setup.member.id}/orphaned.jpg"
    captured: dict = {}
    _install_fake_gcs(
        monkeypatch, [_blobs(captured, (orphan, NOW - timedelta(days=30)))], captured
    )

    await _reconcile(delete=True)

    lines = _aggregate_lines(capsys)
    assert len(lines) == 1
    line = lines[0]
    assert set(line) == set(media_reconcile.AGGREGATE_KEYS)
    assert line["mode"] == "delete"
    assert line["outcome"] == "completed"
    assert line["eligible"] == 1
    assert line["deleted"] == 1
    assert orphan not in json.dumps(line)


async def test_aggregate_line_is_emitted_on_the_abort_path_before_it_propagates(
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T1 regression this guards against: emitting the aggregate only on success would
    lose it on exactly the path an operator most needs it - a misconfiguration."""
    setup = await make_chapter_with("member")
    await _insert_post(
        setup.chapter_id,
        setup.member.id,
        media_urls=[_url("posts/u/elsewhere.jpg", bucket=OTHER_BUCKET)],
    )
    captured: dict = {}
    _install_fake_gcs(
        monkeypatch,
        [_blobs(captured, ("posts/u/looks-orphaned.jpg", NOW - timedelta(days=30)))],
        captured,
    )

    with pytest.raises(ReconcileAborted):
        await _reconcile(delete=True)

    lines = _aggregate_lines(capsys)
    assert len(lines) == 1
    line = lines[0]
    assert set(line) == set(media_reconcile.AGGREGATE_KEYS)
    assert line["outcome"] == "aborted"
    assert line["mode"] == "delete"
    assert line["unresolved"] == 0
    assert line["scanned"] == 0


# ---------------------------------------------------------------------------
# D2 (board c414): no object name or stored value anywhere in output by default - a
# scheduled run must not write a user id to logs every day. --list-eligible restores
# them. format_cli_output is a pure function (no DB/GCS) for the CLI half; the log
# lines inside reconcile_orphaned_media are covered directly below that.
# ---------------------------------------------------------------------------


def _result(**overrides) -> media_reconcile.ReconcileResult:
    defaults: dict = dict(
        scanned=1, referenced=0, protected_by_raw_match=0, too_young=0,
        eligible=("posts/u1/orphan.jpg",), deleted=(), already_gone=(),
        unresolved_values=0,
    )
    defaults.update(overrides)
    return media_reconcile.ReconcileResult(**defaults)


def test_default_cli_output_has_no_object_name() -> None:
    output = media_reconcile.format_cli_output(_result(), delete=False, list_eligible=False)
    assert "posts/u1/orphan.jpg" not in output
    assert "eligible=1" in output


def test_list_eligible_cli_output_includes_the_object_name() -> None:
    output = media_reconcile.format_cli_output(_result(), delete=False, list_eligible=True)
    assert "posts/u1/orphan.jpg" in output
    assert "DRY RUN, would delete: posts/u1/orphan.jpg" in output


def test_list_eligible_cli_output_on_a_delete_run_says_deleted() -> None:
    output = media_reconcile.format_cli_output(
        _result(deleted=("posts/u1/orphan.jpg",)), delete=True, list_eligible=True
    )
    assert "DELETED: posts/u1/orphan.jpg" in output


def test_cli_defaults_list_eligible_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """T2 regression (b): the tests above prove logs stay silent when
    reconcile_orphaned_media itself is called with list_eligible=False, but that is a
    different place from the CLI's own default. Flipping --list-eligible's
    action="store_true" default to True would make every scheduled invocation (which
    passes no flags at all) start listing object names and user ids again without any
    of those tests going red - this one exercises main()'s own argument parsing and
    wiring instead of the underlying function's default.
    """
    captured: dict = {}

    async def _fake_run_and_report(*, delete, min_age_hours, list_eligible):
        captured["delete"] = delete
        captured["min_age_hours"] = min_age_hours
        captured["list_eligible"] = list_eligible
        return _result(eligible=())

    monkeypatch.setattr(media_reconcile, "_run_and_report", _fake_run_and_report)

    media_reconcile.main([])

    assert captured["list_eligible"] is False
    assert captured["delete"] is False


async def test_default_run_omits_object_names_and_stored_values_from_logs(
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T2 regression this guards against: logging a name or stored value
    unconditionally, ignoring list_eligible."""
    setup = await make_chapter_with("member")
    orphan = f"posts/{setup.member.id}/orphan.jpg"
    unresolved_raw = "not a url at all"
    # A canonical, resolving row alongside the unparseable one so the all-rows abort
    # guard does not fire first - this test is about default log verbosity, not the
    # guard (which has its own tests above).
    live = f"posts/{setup.member.id}/live.jpg"
    await _insert_post(setup.chapter_id, setup.member.id, media_urls=[_url(live)])
    await _insert_post(setup.chapter_id, setup.member.id, media_urls=[unresolved_raw])

    captured: dict = {}
    _install_fake_gcs(
        monkeypatch,
        [_blobs(captured, (live, NOW - timedelta(days=30)), (orphan, NOW - timedelta(days=30)))],
        captured,
    )

    with caplog.at_level(logging.INFO, logger=media_reconcile.logger.name):
        result = await _reconcile(delete=True)

    assert result.deleted == (orphan,)
    assert result.unresolved_values == 1
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert orphan not in logged
    assert unresolved_raw not in logged


async def test_list_eligible_restores_object_names_and_stored_values_in_logs(
    make_chapter_with: MakeChapterWith,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    setup = await make_chapter_with("member")
    orphan = f"posts/{setup.member.id}/orphan.jpg"
    unresolved_raw = "not a url at all"
    live = f"posts/{setup.member.id}/live.jpg"
    await _insert_post(setup.chapter_id, setup.member.id, media_urls=[_url(live)])
    await _insert_post(setup.chapter_id, setup.member.id, media_urls=[unresolved_raw])

    captured: dict = {}
    _install_fake_gcs(
        monkeypatch,
        [_blobs(captured, (live, NOW - timedelta(days=30)), (orphan, NOW - timedelta(days=30)))],
        captured,
    )

    with caplog.at_level(logging.INFO, logger=media_reconcile.logger.name):
        result = await _reconcile(delete=True, list_eligible=True)

    assert result.deleted == (orphan,)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert orphan in logged
    assert unresolved_raw in logged
