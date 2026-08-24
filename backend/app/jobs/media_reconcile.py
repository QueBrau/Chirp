"""Reclaim posts/ objects that no post row references any more (board card c153).

WHY THIS EXISTS AS A JOB AND NOT AS A DELETE IN THE PATCH ROUTE. PATCH
/chapters/{id}/posts/{id} with media_object_names=[] detaches a photo, and with a
new upload replaces one; either way the previous permanent posts/ object is left
with nothing pointing at it. The obvious fix — delete it inline — is exactly what
c132 made impossible on purpose: the API's runtime service account has NO delete
grant on posts/, IAM-conditioned to tmp/ only, so that no bug, injected object
name, or compromised route can ever destroy a published photo. That guarantee is
worth more than the kilobytes, so the orphan is accepted at write time and cleaned
up out of band here instead. Widening the runtime identity's delete condition to
cover posts/ was considered and rejected (manager decision on c153); do not
"simplify" this job away by doing that.

RUNS UNDER ITS OWN IDENTITY. This job needs storage.objects.list on the bucket and
storage.objects.delete conditioned to posts/ — a SEPARATE service account from the
API's, created as an infra step. Nothing here names or assumes an identity:
credentials come from ADC (google.auth.default() via storage.Client()), so the job
picks up whichever account it is deployed as.

TWO GUARDS AGAINST DELETING A LIVE PHOTO, because "unreferenced" is a claim about a
race-prone snapshot, not a fact:

1. AGE. An object younger than min_age_hours (default 24) is never deleted, even if
   nothing references it. finalize_media_object() moves an object to posts/ BEFORE
   the DB row naming it is committed, so there is a real window in which a perfectly
   legitimate object is genuinely unreferenced. The age floor has to be comfortably
   longer than that window, and a day is: if a create/PATCH has not committed within
   24 hours, it never will. An object whose creation time cannot be read at all is
   treated as too young rather than eligible — an unknown age is not evidence of
   being old.

2. BUCKET AGREEMENT. media_urls stores a FULL url with the bucket name baked into it
   (https://storage.googleapis.com/{bucket}/posts/...), so the reference set is only
   meaningful if the configured bucket is the one those urls actually name. Point
   this job at the wrong bucket and every stored reference stops resolving, the
   reference set comes back empty, and every object in the bucket looks like an
   orphan. That single misconfiguration is the one input that turns this job into a
   catastrophe, so if the table holds media urls and NONE of them resolve to the
   configured bucket, the job aborts instead of computing a delete set.

SOFT-DELETED POSTS STILL COUNT AS REFERENCES, deliberately. The reference query does
not filter on deleted_at: a soft-deleted post's row still names its object, the post
is still restorable, and moderation can still need to look at it. The photo becomes
collectable only once app.jobs.purge hard-deletes the row past its retention window,
at which point this job sees it unreferenced on a later pass and reclaims it. The two
jobs compose in that order without either knowing about the other.

DRY RUN IS THE DEFAULT. Nothing is deleted unless the caller passes delete=True
(`--delete` on the CLI). A plain run reports what it would remove.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from google.api_core.exceptions import NotFound
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.social import Post
from app.services import storage_service
from app.services.storage_service import PERMANENT_PREFIX

logger = logging.getLogger(__name__)

DEFAULT_MIN_AGE_HOURS = 24
PUBLIC_URL_ROOT = "https://storage.googleapis.com"


class ReconcileAborted(RuntimeError):
    """Refuse to run at all rather than act on a delete set we don't trust."""


@dataclass(frozen=True)
class MediaReferences:
    """Object names the posts table points at, plus how many urls it held in total.

    total_urls is not decoration: it is what separates "no post has a photo" (fine,
    nothing to protect) from "every stored photo url failed to resolve against this
    bucket" (a misconfiguration, and the one case where an empty reference set would
    otherwise authorize deleting the entire bucket).
    """

    object_names: frozenset[str]
    total_urls: int


@dataclass(frozen=True)
class ReconcileResult:
    scanned: int
    referenced: int
    too_young: int
    eligible: tuple[str, ...]
    deleted: tuple[str, ...]
    already_gone: tuple[str, ...]


def _bucket_name() -> str:
    """The configured bucket, or refuse to run.

    Deliberately not storage_service._bucket_name(), which raises HTTPException(503)
    — the right answer for a request handler and the wrong one for a batch job, where
    it would surface as an HTTP status in a CLI traceback.
    """
    name = get_settings().media_bucket_name
    if not name:
        raise ReconcileAborted(
            "media_bucket_name is not configured; refusing to run with no bucket"
        )
    return name


async def collect_referenced_object_names(
    session: AsyncSession, bucket_name: str
) -> MediaReferences:
    """Every posts/ object name the posts table still points at.

    No deleted_at filter, on purpose — see the module docstring. A url that does not
    resolve against this bucket is counted in total_urls but contributes no protected
    name: it cannot possibly name an object this job is about to list.
    """
    rows = (
        await session.execute(select(Post.media_urls).where(Post.media_urls.is_not(None)))
    ).scalars().all()

    prefix = f"{PUBLIC_URL_ROOT}/{bucket_name}/"
    names: set[str] = set()
    total = 0
    for urls in rows:
        for url in urls or []:
            total += 1
            if url.startswith(prefix):
                names.add(url[len(prefix):])
    return MediaReferences(object_names=frozenset(names), total_urls=total)


def list_permanent_blobs(bucket_name: str) -> list:
    """Every object under posts/, walked page by page.

    The pages are walked EXPLICITLY rather than by iterating the iterator directly.
    Both auto-paginate — client.list_blobs returns an HTTPIterator, verified against
    google/cloud/storage/client.py's own body, not its docstring — but a bucket with
    more objects than one page is precisely the case where a subtly wrong listing
    turns into a wrong delete set, so the paging is written where it can be read and
    tested rather than left implicit in an iterator's behaviour.
    """
    client = storage_service._storage_client()
    iterator = client.list_blobs(bucket_name, prefix=f"{PERMANENT_PREFIX}/")
    blobs: list = []
    for page in iterator.pages:
        blobs.extend(page)
    return blobs


def _is_old_enough(blob, cutoff: datetime) -> bool:
    """True only if the object's creation time is known AND older than the cutoff.

    blob.time_created reads the timeCreated property off the listing response and
    returns None when it was never loaded (checked in blob.py, not assumed). An
    unreadable age is treated as too young: this job would rather skip an object
    forever than delete one whose age it cannot establish.
    """
    created = blob.time_created
    return created is not None and created < cutoff


async def reconcile_orphaned_media(
    session: AsyncSession,
    *,
    delete: bool = False,
    min_age_hours: int = DEFAULT_MIN_AGE_HOURS,
    now: datetime | None = None,
) -> ReconcileResult:
    """Diff posts/ against posts.media_urls; report, and delete only if asked.

    `now` is injectable so the age boundary is deterministic in tests, the same
    reason app.jobs.purge.purge_expired_soft_deletes takes it.

    Deletes nothing unless delete=True. A delete that 404s counts as success: the
    object was already gone, which is the state this job wanted. blob.delete() sends
    the generation it was listed with (it forwards generation=self.generation to
    bucket.delete_blob, read off blob.py), so an object replaced between the listing
    and the delete 404s rather than having its replacement removed.
    """
    bucket_name = _bucket_name()
    resolved_now = now if now is not None else datetime.now(timezone.utc)
    cutoff = resolved_now - timedelta(hours=min_age_hours)

    references = await collect_referenced_object_names(session, bucket_name)
    if references.total_urls and not references.object_names:
        message = (
            f"media_reconcile ABORTED: posts.media_urls holds {references.total_urls} "
            f"url(s) and 0 of them resolve to bucket {bucket_name!r}. That is a bucket "
            "misconfiguration, not an orphaned bucket - refusing to treat every object "
            "as unreferenced. Check media_bucket_name against what the stored urls "
            "actually name."
        )
        logger.error(message)
        raise ReconcileAborted(message)

    blobs = list_permanent_blobs(bucket_name)
    referenced = 0
    too_young = 0
    candidates = []
    for blob in blobs:
        if blob.name in references.object_names:
            referenced += 1
        elif not _is_old_enough(blob, cutoff):
            too_young += 1
        else:
            candidates.append(blob)

    eligible = tuple(blob.name for blob in candidates)
    if not delete:
        for name in eligible:
            logger.info("media_reconcile dry-run: would delete unreferenced object=%s", name)
        return ReconcileResult(
            scanned=len(blobs),
            referenced=referenced,
            too_young=too_young,
            eligible=eligible,
            deleted=(),
            already_gone=(),
        )

    deleted: list[str] = []
    already_gone: list[str] = []
    for blob in candidates:
        try:
            blob.delete()
        except NotFound:
            already_gone.append(blob.name)
            logger.info("media_reconcile: object=%s was already gone", blob.name)
        else:
            deleted.append(blob.name)
            logger.info("media_reconcile: deleted unreferenced object=%s", blob.name)

    return ReconcileResult(
        scanned=len(blobs),
        referenced=referenced,
        too_young=too_young,
        eligible=eligible,
        deleted=tuple(deleted),
        already_gone=tuple(already_gone),
    )


async def _run_and_report(*, delete: bool, min_age_hours: int) -> ReconcileResult:
    """Open one read-only session against the configured DB and reconcile.

    No commit: this job only ever SELECTs from the database. Everything it changes
    lives in the bucket.
    """
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        return await reconcile_orphaned_media(
            session, delete=delete, min_age_hours=min_age_hours
        )


def main() -> None:
    """CLI entry point: `python -m app.jobs.media_reconcile [--delete]`."""
    parser = argparse.ArgumentParser(
        description=(
            "Delete posts/ objects no post row references any more. Reports without "
            "deleting unless --delete is passed."
        )
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="actually delete the eligible objects (default: report only)",
    )
    parser.add_argument(
        "--min-age-hours",
        type=int,
        default=DEFAULT_MIN_AGE_HOURS,
        help=(
            "never delete an object younger than this, even if unreferenced "
            f"(default: {DEFAULT_MIN_AGE_HOURS})"
        ),
    )
    args = parser.parse_args()

    result = asyncio.run(
        _run_and_report(delete=args.delete, min_age_hours=args.min_age_hours)
    )

    mode = "DELETED" if args.delete else "DRY RUN, would delete"
    print(
        f"media_reconcile: scanned={result.scanned} referenced={result.referenced} "
        f"too_young={result.too_young} eligible={len(result.eligible)}"
    )
    for name in result.eligible:
        print(f"  {mode}: {name}")
    if result.already_gone:
        print(f"  already gone (counted as success): {len(result.already_gone)}")
    if not args.delete and result.eligible:
        print("  nothing was deleted; re-run with --delete to act on the list above")


if __name__ == "__main__":
    main()
