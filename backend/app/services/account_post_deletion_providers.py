"""Explicit provider operations for the dormant post-deletion cleanup runner.

Every successful pass remains pending. Neither a current empty listing nor an
empty analytics query proves that an already issued upload or delayed sink event
cannot arrive later. Credentials and reviewed storage policy are caller supplied;
importing this module does not discover credentials or register a background job.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from app.services.account_post_deletion_cleanup import (
    CleanupClaim, DeferredCleanupReceipt, _digest,
)
from app.services import account_provider_gcs as gcs
from app.services import account_provider_analytics as bq


def _receipt(claim: CleanupClaim, *, pass_digest: str, remaining_count=None,
             retention=()) -> DeferredCleanupReceipt:
    result = DeferredCleanupReceipt(
        category=claim.category,
        provider="media_storage" if claim.category == "temporary_media" else "analytics",
        subject=str(claim.user_id), request_id=str(claim.request_id),
        scope=claim.scope, scope_sha256=claim.scope_sha256,
        pass_digest=pass_digest, remaining_count=remaining_count,
        readback=True, retained_with_reason=tuple(retention),
    )
    result.validate(expected_category=claim.category, subject=claim.user_id,
                    request_id=claim.request_id)
    return result


async def cleanup_temporary_media(
    claim: CleanupClaim, *, reader: gcs.MediaReader,
    retention: gcs.MediaRetentionReview, timeout_seconds: float = 30,
) -> DeferredCleanupReceipt:
    """Remove only inventoried tmp generations and keep late arrivals pending."""
    if claim.category != "temporary_media":
        raise ValueError("cleanup_category_invalid")
    # Validate the entire immutable scope before even a provider read.
    _receipt(claim, pass_digest="0" * 64, remaining_count=0)
    if (reader.bucket_name != claim.scope["bucket"]
            or type(timeout_seconds) not in (int, float)
            or not 1 <= timeout_seconds <= 120):
        raise ValueError("cleanup_media_binding_invalid")
    codes = retention.receipt_codes(reader.bucket_name)
    deadline = time.monotonic() + timeout_seconds
    prefix = claim.scope["tmp_prefix"]

    async def inventory():
        rows = await gcs._list_bounded(reader, prefix, deadline=deadline, limit=gcs.MAX_OBJECTS)
        result = {}
        for raw in rows:
            item = gcs._metadata(raw)
            if (not item.name.startswith(prefix) or not gcs._owned_name(item.name, claim.user_id)
                    or item.name in result):
                raise gcs.MediaProviderUnavailable("media_followup_scope_invalid")
            result[item.name] = item.generation
        return result

    before = await inventory()
    for name, generation in sorted(before.items()):
        observed = await gcs._reader_call(reader, "read", name, deadline=deadline)
        if observed is None:
            continue
        observed = gcs._metadata(observed)
        if observed.name != name:
            raise gcs.MediaProviderUnavailable("media_followup_read_binding_invalid")
        if observed.generation != generation:
            # A replacement was not in this pass's inventory. A later pass may
            # inventory it; never delete a generation merely by name.
            continue
        await gcs._reader_call(reader, "delete", name, generation, deadline=deadline)
        after = await gcs._reader_call(reader, "read", name, deadline=deadline)
        if after is not None:
            after = gcs._metadata(after)
            if after.name != name or after.generation == generation:
                raise gcs.MediaProviderUnavailable("media_followup_readback_failed")
    remaining = await inventory()
    if any(remaining.get(name) == generation for name, generation in before.items()):
        raise gcs.MediaProviderUnavailable("media_followup_readback_failed")
    return _receipt(
        claim, remaining_count=len(remaining), retention=codes,
        pass_digest=_digest({"attempt": str(claim.owner_token),
                             "before": sorted(before.items()), "after": sorted(remaining.items())}),
    )


async def _analytics_tables(rest: bq._Rest, exclusions: tuple[bq.AnalyticsExclusion, ...]):
    """Discover only the approved dated-table family, within the existing cap."""
    approved_exclusions = {item.name for item in exclusions}
    names, tokens, page = set(), set(), None
    for _ in range(bq.MAX_PAGES):
        params = {"maxResults": bq.MAX_TABLES}
        if page is not None:
            params["pageToken"] = page
        listing = await rest.request("GET", rest.data + "/tables", params=params)
        rows = listing.get("tables", [])
        if type(rows) is not list:
            raise bq._fail("inventory_invalid")
        for row in rows:
            ref = row.get("tableReference") if type(row) is dict else None
            if (type(ref) is not dict or row.get("type") != "TABLE"
                    or ref.get("projectId") != rest.project or ref.get("datasetId") != rest.dataset):
                raise bq._fail("inventory_invalid")
            name = ref.get("tableId")
            if (type(name) is not str or name in names
                    or not (bq.STDOUT.fullmatch(name) or name in approved_exclusions)):
                raise bq._fail("inventory_drift")
            names.add(name)
            if len(names) > bq.MAX_TABLES:
                raise bq._fail("inventory_invalid")
        page = listing.get("nextPageToken")
        if page is None:
            break
        if type(page) is not str or not 1 <= len(page) <= 2048 or page in tokens:
            raise bq._fail("inventory_invalid")
        tokens.add(page)
    else:
        raise bq._fail("inventory_page_limit")
    tables = tuple(sorted(names - approved_exclusions))
    if not tables or not approved_exclusions.issubset(names):
        raise bq._fail("inventory_drift")
    return tables


async def cleanup_identifiable_analytics(
    claim: CleanupClaim, *, token: str, transport=None,
    timeout_seconds: float = 20,
) -> DeferredCleanupReceipt:
    """Delete current matching rows, including new dated sink tables, then reschedule.

    The claim's owner token identifies one durable pass. The existing adapter's
    job binding, byte budget, parameterized user predicate, schema checks and
    exact readback remain in force. An uncertain pass is never automatically
    reissued by the runner.
    """
    if claim.category != "identifiable_analytics":
        raise ValueError("cleanup_category_invalid")
    retention = ("bigquery_time_travel", "bigquery_fail_safe")
    _receipt(claim, pass_digest="0" * 64, retention=retention)
    scope = claim.scope
    exclusions = tuple(bq.AnalyticsExclusion(
        name=item["name"], schema_digest=item["schema_digest"], reviewed_non_account_probe=True,
    ) for item in scope["exclusions"])
    rest = bq._Rest(token, scope["project"], scope["dataset"], transport, timeout_seconds)
    async with asyncio.timeout(bq.MAX_SECONDS):
        async with rest.client:
            tables = await _analytics_tables(rest, exclusions)
        # A completed account tombstone prevents new authenticated activity, but
        # not delayed ingestion. The latter remains explicitly unproven.
        freeze = bq.AnalyticsFreezeReceipt(
            user_id=str(claim.user_id), freeze_id="post_deletion_" + claim.owner_token.hex,
            expires_at=time.time() + bq.MAX_SECONDS + 30, verified=True, ingestion_drained=False,
        )
        snapshot = await bq.BigQueryAnalyticsProvider.capture_preview_snapshot(
            token=token, project=scope["project"], dataset=scope["dataset"],
            request_id=str(claim.owner_token), user_id=str(claim.user_id), freeze=freeze,
            reviewed_tables=tables, exclusions=exclusions,
            query_byte_budget=bq.MAX_BYTES // bq.MAX_TABLES,
            transport=transport, timeout_seconds=timeout_seconds, deferred_cleanup=True,
        )
        provider = bq.BigQueryAnalyticsProvider(
            token=token, project=scope["project"], dataset=scope["dataset"], snapshot=snapshot,
            freeze=freeze, reviewed_snapshot_digest=snapshot.snapshot_digest,
            transport=transport, timeout_seconds=timeout_seconds, deferred_cleanup=True,
        )
        result = await provider.delete_account_data(
            user=SimpleNamespace(id=claim.user_id),
            idempotency_key=f"account-deletion:{claim.owner_token}:analytics",
        )
    if not isinstance(result, DeferredCleanupReceipt):
        raise bq._fail("followup_receipt_invalid")
    result.validate(expected_category=claim.category, subject=claim.user_id,
                    request_id=claim.owner_token)
    # New table inventory is evidence of this pass, not an expansion to a new
    # project/dataset/predicate. Preserve the original obligation's scope digest.
    return _receipt(claim, pass_digest=result.pass_digest, retention=retention)


class PostDeletionProviders:
    """Concrete callback for run_post_deletion_cleanup_batch, with explicit access."""

    def __init__(self, *, media_reader=None, media_retention=None,
                 analytics_token: str | None = None, analytics_transport=None):
        self.media_reader = media_reader
        self.media_retention = media_retention
        self.analytics_token = analytics_token
        self.analytics_transport = analytics_transport

    async def __call__(self, claim: CleanupClaim) -> DeferredCleanupReceipt:
        if claim.category == "temporary_media":
            if self.media_reader is None or self.media_retention is None:
                raise ValueError("cleanup_media_provider_unconfigured")
            return await cleanup_temporary_media(
                claim, reader=self.media_reader, retention=self.media_retention,
            )
        if claim.category == "identifiable_analytics":
            if self.analytics_token is None:
                raise ValueError("cleanup_analytics_provider_unconfigured")
            return await cleanup_identifiable_analytics(
                claim, token=self.analytics_token, transport=self.analytics_transport,
            )
        raise ValueError("cleanup_category_invalid")
