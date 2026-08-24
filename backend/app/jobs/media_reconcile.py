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

AND THE REFERENCE SET IS BUILT CONSERVATIVELY, because the column has three eras and
only the newest one is a format guarantee. Pre-c139 rows were never validated at all;
c139..c132 rows were validated only against the bucket root, so they can name a real
object in whatever url form the client used; post-c132 rows are server-assigned and
canonical. A reference this job fails to parse is not a harmless miss — it is a live
photo that looks unreferenced and becomes eligible for deletion. So resolution accepts
every form that can name a GCS object (see resolve_object_names), an unparsed value is
logged and counted rather than dropped silently, and — as a last backstop — an object
whose name appears verbatim inside ANY stored value is protected even if no parser
recognized that value. Over-protection leaves one orphan uncollected; under-protection
destroys someone's photo, and that asymmetry decides every ambiguous case here.

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
from urllib.parse import unquote, urlsplit

from google.api_core.exceptions import NotFound
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.social import Post
from app.services import storage_service
from app.services.storage_service import PERMANENT_PREFIX

logger = logging.getLogger(__name__)

DEFAULT_MIN_AGE_HOURS = 24
# Hosts that can name an object in our bucket. The canonical form this backend emits
# is the first one, path-style — but media_urls has three eras (below) and the older
# two can hold anything a client sent, so recognizing only the emit form would leave a
# real, live photo looking unreferenced.
PUBLIC_URL_HOSTS = ("storage.googleapis.com", "storage.cloud.google.com")


def _positive_hours(value: str) -> int:
    """Parse a strictly positive age floor for the destructive CLI.

    Zero or a negative value would make every unreferenced object eligible regardless
    of age, defeating the guard against the move-before-commit race described above.
    Keep that state unrepresentable at argument parsing time rather than relying on an
    operator to notice a dangerous value in a command line.
    """
    try:
        hours = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if hours <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return hours


class ReconcileAborted(RuntimeError):
    """Refuse to run at all rather than act on a delete set we don't trust."""


@dataclass(frozen=True)
class MediaReferences:
    """What the posts table points at, in three views, because a delete job needs all
    three to be safe rather than merely correct.

    object_names — everything that RESOLVED to an object in our bucket, under any
    recognized url form. This is the protection set.

    raw_values — only the stored strings whose SHAPE was not recognized at all. The
    backstop: an object whose name appears verbatim inside one of these is protected
    even though no parser could resolve it. Values that parsed cleanly but named a
    different bucket are deliberately NOT in here — we know what those are, and
    guessing about them would protect an object we can prove is unrelated. Over-
    protection costs one uncollected orphan; under-protection costs a user their
    photo, so the asymmetry decides every case we genuinely cannot read.

    total_urls / unresolved — counts, for the abort guard and for operator visibility.
    total_urls is what separates "no post has a photo" (fine, nothing to protect) from
    "every stored url failed to resolve against this bucket" (a misconfiguration, and
    the one case where an empty reference set would otherwise authorize deleting
    everything).
    """

    object_names: frozenset[str]
    raw_values: tuple[str, ...]
    total_urls: int
    unresolved: int


@dataclass(frozen=True)
class ReconcileResult:
    scanned: int
    referenced: int
    protected_by_raw_match: int
    too_young: int
    eligible: tuple[str, ...]
    deleted: tuple[str, ...]
    already_gone: tuple[str, ...]
    unresolved_values: int


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


@dataclass(frozen=True)
class ResolvedValue:
    """What one stored media_urls value turned out to be.

    The two fields answer different questions, and conflating them is what made an
    early version of this job over-protective. `names` is "what does it name in OUR
    bucket". `understood` is "did we recognize its shape at all", independent of which
    bucket it named. A value that cleanly names a DIFFERENT bucket is understood with
    no names — we know exactly what it is, and it is not ours, so it neither protects
    anything nor needs the raw-match backstop. Only a value we could not parse at all
    goes into that backstop, which is the only place guessing is warranted.
    """

    names: frozenset[str]
    understood: bool


def resolve_object_names(value: str, bucket_name: str) -> ResolvedValue:
    """Resolve one stored media_urls value against our bucket.

    Never raises: the input is arbitrary client data from before validation existed.

    MULTI-FORM ON PURPOSE. posts.media_urls has three eras, and only the newest is
    guaranteed to hold the canonical form:
      - pre-c139: no validation at all, so the value can be literally anything;
      - c139..c132: client-supplied, validated only against the BUCKET ROOT, so it can
        name a real bucket object in any url form a client happened to use;
      - post-c132: server-assigned, always https://storage.googleapis.com/{bucket}/posts/...
    Recognizing only the third era's form would let an alternate-form reference to a
    REAL, LIVE photo resolve to nothing and become eligible for deletion. That is the
    failure that matters for a job whose output is deletions, so every form that can
    name a GCS object is recognized: path-style, virtual-hosted, the JSON API's
    /b/{bucket}/o/{name} shape, both public hosts, and gs:// URIs.

    THE JSON API FORM PERCENT-ENCODES THE OBJECT NAME - posts%2Fu%2Fa.jpg, not
    posts/u/a.jpg - so a resolver that returned only the literal path segment would
    hand back a name that matches no blob and protects nothing. Both the raw and the
    decoded spelling are returned whenever they differ, for that form and any other.

    `understood` is only ever True when a BUCKET was actually identified, ours or
    someone else's. A public-host url we cannot pull a bucket and object out of stays
    understood=False so it keeps the raw-match backstop, rather than being waved
    through as "recognized, references nothing" - that distinction is the difference
    between an uncollected orphan and a deleted photo.
    """
    try:
        parts = urlsplit(value)
    except ValueError:
        return ResolvedValue(names=frozenset(), understood=False)

    host = parts.netloc.lower()
    path = parts.path.lstrip("/")
    bucket = bucket_name.lower()
    named_bucket: str | None = None
    name: str | None = None

    if parts.scheme == "gs":
        named_bucket, name = host, path
    elif host in PUBLIC_URL_HOSTS:
        if path.startswith("storage/v1/b/") or path.startswith("download/storage/v1/b/"):
            named_bucket, _, name = path.split("/b/", 1)[1].partition("/o/")
        else:
            named_bucket, _, name = path.partition("/")
    elif any(host == f"{bucket}.{public_host}" for public_host in PUBLIC_URL_HOSTS):
        named_bucket, name = bucket, path
    elif any(host.endswith(f".{public_host}") for public_host in PUBLIC_URL_HOSTS):
        # Virtual-hosted style for some OTHER bucket. Understood, and not ours.
        return ResolvedValue(names=frozenset(), understood=True)

    if not named_bucket or not name:
        return ResolvedValue(names=frozenset(), understood=False)
    if named_bucket.lower() != bucket:
        return ResolvedValue(names=frozenset(), understood=True)
    return ResolvedValue(names=frozenset({name, unquote(name)}), understood=True)


async def collect_referenced_object_names(
    session: AsyncSession, bucket_name: str
) -> MediaReferences:
    """Everything the posts table still points at, resolved conservatively.

    No deleted_at filter, on purpose — see the module docstring.

    A value that cannot be resolved is COUNTED and LOGGED, never silently dropped, and
    it never weakens protection: it is still kept in raw_values, where the substring
    backstop can protect an object this parser failed to recognize. An unresolved
    count that is not zero means either legacy rows exist (expected, harmless) or a
    url form this parser should learn (a bug worth seeing), and the log line is what
    tells those apart.
    """
    rows = (
        await session.execute(select(Post.media_urls).where(Post.media_urls.is_not(None)))
    ).scalars().all()

    names: set[str] = set()
    unparsed_values: list[str] = []
    total = 0
    for urls in rows:
        for url in urls or []:
            total += 1
            resolved = resolve_object_names(url, bucket_name)
            names |= resolved.names
            if not resolved.understood:
                unparsed_values.append(url)
                logger.warning(
                    "media_reconcile: stored media url is not a recognized GCS "
                    "reference, so it protects only by raw match: %.256s",
                    url,
                )

    return MediaReferences(
        object_names=frozenset(names),
        raw_values=tuple(unparsed_values),
        total_urls=total,
        unresolved=len(unparsed_values),
    )


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
            f"media_reconcile ABORTED (board c153): posts.media_urls holds "
            f"{references.total_urls} url(s), {references.unresolved} of them in a form "
            f"this job could not parse, and 0 resolve to bucket {bucket_name!r}. This is "
            "DIAGNOSTIC, not a crash: either media_bucket_name points at the wrong "
            "bucket, or every stored row predates c132's canonical format. Refusing to "
            "treat every object as unreferenced. Run the legacy-row SELECT in the deploy "
            "runbook to tell those two apart before re-running."
        )
        logger.error(message)
        raise ReconcileAborted(message)

    blobs = list_permanent_blobs(bucket_name)
    referenced = 0
    protected_by_raw_match = 0
    too_young = 0
    candidates = []
    for blob in blobs:
        if blob.name in references.object_names:
            referenced += 1
        elif any(blob.name in value for value in references.raw_values):
            # The backstop for a url form resolve_object_names() did not recognize.
            # It firing at all is a signal, not a normal outcome: something in the
            # column names this object in a shape the parser should learn. Protect it
            # now, count it so the run reports it, and let a human teach the parser.
            protected_by_raw_match += 1
            logger.warning(
                "media_reconcile: object=%s is referenced only by an unparsed stored "
                "value - protected, but resolve_object_names() should learn this form",
                blob.name,
            )
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
            protected_by_raw_match=protected_by_raw_match,
            too_young=too_young,
            eligible=eligible,
            deleted=(),
            already_gone=(),
            unresolved_values=references.unresolved,
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
        protected_by_raw_match=protected_by_raw_match,
        too_young=too_young,
        eligible=eligible,
        deleted=tuple(deleted),
        already_gone=tuple(already_gone),
        unresolved_values=references.unresolved,
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
        type=_positive_hours,
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
    if result.unresolved_values or result.protected_by_raw_match:
        print(
            f"  unparsed stored values={result.unresolved_values} "
            f"objects protected only by raw match={result.protected_by_raw_match} "
            "(see warnings: a url form the resolver does not recognize)"
        )
    for name in result.eligible:
        print(f"  {mode}: {name}")
    if result.already_gone:
        print(f"  already gone (counted as success): {len(result.already_gone)}")
    if not args.delete and result.eligible:
        print("  nothing was deleted; re-run with --delete to act on the list above")


if __name__ == "__main__":
    main()
