"""Durable, fail-closed post-tombstone cleanup obligations for c436."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections.abc import Awaitable, Callable
from typing import Literal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings

CATEGORIES = frozenset({"temporary_media", "identifiable_analytics"})
PUBLIC_DISCLOSURES = {
    "temporary_media": "Temporary account-owned uploads may remain while post-deletion cleanup continues.",
    "identifiable_analytics": "Identifiable analytics rows may reappear until provider ingestion drains and follow-up cleanup completes.",
}
MAX_CLAIM_LIMIT = 32
MAX_LEASE_SECONDS = 300
_HEX = re.compile(r"^[0-9a-f]{64}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")
_TABLE = re.compile(r"^run_googleapis_com_stdout_[0-9]{8}$")
_PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$")
_DATASET = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")
_PROBE = "chirps_analytics_probe_20260908"
MAX_PROVIDER_SECONDS = 120.0
MAX_DB_STATEMENT_SECONDS = 5
MAX_CLEANUP_JOURNAL_SECONDS = 5.0
MAX_RETRY_DELAY = timedelta(days=7)


def _digest(value: object) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError, RecursionError):
        return ""
    return hashlib.sha256(encoded).hexdigest()


def _canonical_uuid(value: object) -> bool:
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _valid_scope(category: str, user_id: uuid.UUID, scope: object) -> bool:
    """Validate the immutable provider boundary before any worker handoff."""
    if type(scope) is not dict:
        return False
    if category == "temporary_media":
        return (
            set(scope) == {"bucket", "tmp_prefix", "initial_snapshot_digest"}
            and type(scope.get("bucket")) is str and _BUCKET.fullmatch(scope["bucket"]) is not None
            and scope.get("tmp_prefix") == f"tmp/{user_id}/"
            and type(scope.get("initial_snapshot_digest")) is str
            and _HEX.fullmatch(scope["initial_snapshot_digest"]) is not None
        )
    if category == "identifiable_analytics":
        if set(scope) != {"project", "dataset", "tables", "table_pattern", "predicate_version", "exclusions"}:
            return False
        tables, exclusions = scope.get("tables"), scope.get("exclusions")
        if (type(scope.get("project")) is not str or _PROJECT.fullmatch(scope["project"]) is None
                or type(scope.get("dataset")) is not str or _DATASET.fullmatch(scope["dataset"]) is None
                or scope.get("predicate_version") != "user_id_analytics_v1"
                or scope.get("table_pattern") != "run_googleapis_com_stdout_YYYYMMDD"
                or type(tables) is not list or not 1 <= len(tables) <= 32
                or tables != sorted(set(tables))
                or any(type(table) is not str or _TABLE.fullmatch(table) is None for table in tables)
                or type(exclusions) is not list or len(exclusions) > 1):
            return False
        if not exclusions:
            return True
        exclusion = exclusions[0]
        return (
            type(exclusion) is dict and set(exclusion) == {"name", "schema_digest"}
            and exclusion.get("name") == _PROBE
            and type(exclusion.get("schema_digest")) is str
            and _HEX.fullmatch(exclusion["schema_digest"]) is not None
        )
    return False


@dataclass(frozen=True)
class DeferredCleanupReceipt:
    """Typed evidence that a bounded current pass succeeded, not full cleanup."""

    category: Literal["temporary_media", "identifiable_analytics"]
    provider: Literal["media_storage", "analytics"]
    subject: str
    request_id: str
    scope: dict[str, object]
    scope_sha256: str
    pass_digest: str
    readback: Literal[True]
    retained_with_reason: tuple[str, ...]
    remaining_count: int | None = None
    receipt_type: Literal["deferred_cleanup"] = "deferred_cleanup"

    def validate(self, *, expected_category: str, subject: uuid.UUID | str,
                 request_id: uuid.UUID | str | None = None) -> None:
        if (self.receipt_type != "deferred_cleanup" or self.category != expected_category
                or self.subject != str(subject) or self.readback is not True
                or (request_id is not None and self.request_id != str(request_id))
                or not _canonical_uuid(self.request_id)
                or not isinstance(self.scope, dict) or len(self.scope) > 8
                or not isinstance(self.scope_sha256, str) or not _HEX.fullmatch(self.scope_sha256)
                or _digest(self.scope) != self.scope_sha256
                or not isinstance(self.pass_digest, str) or not _HEX.fullmatch(self.pass_digest)):
            raise ValueError("deferred_cleanup_receipt_invalid")
        if self.category == "temporary_media":
            expected_prefix = f"tmp/{self.subject}/"
            if (self.provider != "media_storage" or self.scope.get("tmp_prefix") != expected_prefix
                    or not _valid_scope(self.category, uuid.UUID(self.subject), self.scope)
                    or type(self.remaining_count) is not int
                    or not 0 <= self.remaining_count <= 10_000):
                raise ValueError("deferred_cleanup_receipt_invalid")
            if (type(self.retained_with_reason) is not tuple
                    or self.retained_with_reason not in ((), ("gcs_soft_deleted_versions",))):
                raise ValueError("deferred_cleanup_receipt_invalid")
        else:
            tables = self.scope.get("tables")
            if (self.provider != "analytics" or not _valid_scope(self.category, uuid.UUID(self.subject), self.scope)
                    or self.scope.get("predicate_version") != "user_id_analytics_v1"
                    or self.scope.get("table_pattern") != "run_googleapis_com_stdout_YYYYMMDD"
                    or self.remaining_count is not None):
                raise ValueError("deferred_cleanup_receipt_invalid")
            if (type(self.retained_with_reason) is not tuple
                    or self.retained_with_reason != ("bigquery_time_travel", "bigquery_fail_safe")):
                raise ValueError("deferred_cleanup_receipt_invalid")

    def journal_value(self) -> dict[str, object]:
        """Return bounded private journal material; never contains credentials or payload."""
        return {
            "receipt_type": self.receipt_type,
            "category": self.category,
            "provider": self.provider,
            "subject": self.subject,
            "request_id": self.request_id,
            "scope": self.scope,
            "scope_sha256": self.scope_sha256,
            "pass_digest": self.pass_digest,
            "remaining_count": self.remaining_count,
            "readback": True,
            "retained_with_reason": list(self.retained_with_reason),
        }


def deferred_receipt_from_journal(value: object, *, step_key: str, subject: uuid.UUID,
                                  request_id: uuid.UUID) -> DeferredCleanupReceipt:
    if type(value) is not dict or value.get("receipt_type") != "deferred_cleanup":
        raise ValueError("deferred_cleanup_receipt_invalid")
    category = "temporary_media" if step_key == "media_storage" else "identifiable_analytics"
    expected_provider = "media_storage" if category == "temporary_media" else "analytics"
    try:
        expected_provider = "media_storage" if category == "temporary_media" else "analytics"
        if value.get("category") != category or value.get("provider") != expected_provider:
            raise ValueError("deferred_cleanup_receipt_invalid")
        receipt = DeferredCleanupReceipt(
            category=category, provider=expected_provider, subject=str(subject), request_id=value["request_id"],
            scope=value["scope"], scope_sha256=value["scope_sha256"], readback=value["readback"],
            pass_digest=value["pass_digest"], remaining_count=value.get("remaining_count"),
            retained_with_reason=tuple(value["retained_with_reason"]),
        )
    except (KeyError, TypeError):
        raise ValueError("deferred_cleanup_receipt_invalid") from None
    receipt.validate(expected_category=category, subject=subject, request_id=request_id)
    if value.get("subject") != str(subject):
        raise ValueError("deferred_cleanup_receipt_invalid")
    return receipt


def make_obligation(*, request_id: uuid.UUID, user_id: uuid.UUID,
                    receipt: DeferredCleanupReceipt) -> models.AccountPostDeletionCleanup:
    receipt.validate(expected_category=receipt.category, subject=user_id, request_id=request_id)
    return models.AccountPostDeletionCleanup(
        request_id=request_id,
        user_id=user_id,
        category=receipt.category,
        state="pending",
        scope=receipt.scope,
        scope_sha256=receipt.scope_sha256,
        last_pass_digest=receipt.pass_digest,
        remaining_count=receipt.remaining_count,
        attempt_count=0,
        next_attempt_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@dataclass(frozen=True)
class CleanupCompletionProof:
    """Provider-specific drain/readback evidence, never inferred from expiry alone."""

    category: str
    request_id: str
    subject: str
    scope_sha256: str
    pass_digest: str
    attempt_token: str
    proof_id: str
    verified: Literal[True]
    source_drained: Literal[True]
    readback: Literal[True]

    def validate(self, *, row: models.AccountPostDeletionCleanup) -> None:
        expected_proof = {
            "temporary_media": "gcs_prefix_empty_after_signed_put_drain",
            "identifiable_analytics": "bigquery_ingestion_watermark_drained",
        }.get(row.category)
        if (self.category != row.category or self.request_id != str(row.request_id)
                or self.subject != str(row.user_id) or self.scope_sha256 != row.scope_sha256
                or self.pass_digest != row.last_pass_digest or self.proof_id != expected_proof
                or row.owner_token is None or self.attempt_token != str(row.owner_token)
                or self.verified is not True or self.source_drained is not True or self.readback is not True):
            raise ValueError("cleanup_completion_proof_invalid")


@dataclass(frozen=True)
class CleanupClaim:
    id: uuid.UUID
    request_id: uuid.UUID
    user_id: uuid.UUID
    category: str
    owner_token: uuid.UUID
    scope: dict[str, object]
    scope_sha256: str


async def _valid_completed_tombstone(
    session: AsyncSession, row: models.AccountPostDeletionCleanup,
) -> bool:
    binding = (await session.execute(
        select(models.AccountDataRequest, models.User)
        .select_from(models.AccountPostDeletionCleanup)
        .join(models.AccountDataRequest,
              models.AccountDataRequest.id == models.AccountPostDeletionCleanup.request_id)
        .join(models.User, models.User.id == models.AccountPostDeletionCleanup.user_id)
        .where(models.AccountPostDeletionCleanup.id == row.id,
               models.AccountDataRequest.user_id == models.AccountPostDeletionCleanup.user_id)
    )).one_or_none()
    if binding is None:
        return False
    request, user = binding
    return bool(
        request.kind == "deletion" and request.status == "completed"
        and user.is_ghost is True
        and user.firebase_uid == f"deleted:{row.user_id}"
        and user.email == f"deleted+{row.user_id}@invalid.chirp"
    )


async def _bound_db_wait(session: AsyncSession) -> None:
    await session.execute(text("SET LOCAL lock_timeout = '2000ms'"))
    await session.execute(text(f"SET LOCAL statement_timeout = '{MAX_DB_STATEMENT_SECONDS * 1000}ms'"))


async def claim_due_cleanup(session: AsyncSession, *, limit: int = 8,
                            now: datetime | None = None,
                            lease_seconds: int = 120) -> list[CleanupClaim]:
    """Claim only known-pending obligations; expired running work is never stolen."""
    if type(limit) is not int or not 1 <= limit <= MAX_CLAIM_LIMIT:
        raise ValueError("cleanup_claim_limit_invalid")
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= MAX_LEASE_SECONDS:
        raise ValueError("cleanup_lease_invalid")
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("cleanup_clock_invalid")
    await _bound_db_wait(session)
    rows = (await session.scalars(
        select(models.AccountPostDeletionCleanup)
        .join(models.AccountDataRequest, models.AccountDataRequest.id == models.AccountPostDeletionCleanup.request_id)
        .join(models.User, models.User.id == models.AccountPostDeletionCleanup.user_id)
        .where(models.AccountPostDeletionCleanup.state == "pending",
               models.AccountPostDeletionCleanup.next_attempt_at <= now,
               models.AccountDataRequest.kind == "deletion",
               models.AccountDataRequest.user_id == models.AccountPostDeletionCleanup.user_id,
               models.AccountDataRequest.status == "completed",
               models.User.is_ghost.is_(True),
               models.User.firebase_uid == func.concat("deleted:", models.User.id),
               models.User.email == func.concat("deleted+", models.User.id, "@invalid.chirp"))
        .order_by(models.AccountPostDeletionCleanup.next_attempt_at, models.AccountPostDeletionCleanup.id)
        .limit(limit).with_for_update(of=models.AccountPostDeletionCleanup, skip_locked=True)
    )).all()
    claims = []
    for row in rows:
        if (row.category not in CATEGORIES or not _valid_scope(row.category, row.user_id, row.scope)
                or not _HEX.fullmatch(row.scope_sha256)
                or _digest(row.scope) != row.scope_sha256
                or not _HEX.fullmatch(row.last_pass_digest)
                or row.request_id is None):
            row.state = "manual_review"
            row.last_error = "cleanup_scope_invalid"
            row.updated_at = now
            continue
        token = uuid.uuid4()
        row.state = "running"
        row.owner_token = token
        row.lease_expires_at = now + timedelta(seconds=lease_seconds)
        row.attempt_count += 1
        row.updated_at = now
        claims.append(CleanupClaim(row.id, row.request_id, row.user_id, row.category,
                                   token, dict(row.scope), row.scope_sha256))
    await session.commit()
    return claims


async def settle_cleanup_claim(session: AsyncSession, claim: CleanupClaim, *,
                               state: Literal["pending", "complete", "uncertain", "manual_review"],
                               error_code: str | None = None,
                               next_attempt_at: datetime | None = None,
                               receipt: DeferredCleanupReceipt | None = None,
                               completion_proof: CleanupCompletionProof | None = None) -> bool:
    """Token-fenced transition; completion requires fresh provider-specific drain proof."""
    if type(state) is not str or state not in {"pending", "complete", "uncertain", "manual_review"}:
        raise ValueError("cleanup_state_invalid")
    await _bound_db_wait(session)
    row = (await session.scalars(select(models.AccountPostDeletionCleanup).where(
        models.AccountPostDeletionCleanup.id == claim.id,
    ).with_for_update())).one_or_none()
    if (row is None or row.state != "running" or row.owner_token != claim.owner_token
            or row.request_id != claim.request_id or row.user_id != claim.user_id
            or row.category != claim.category or row.scope_sha256 != claim.scope_sha256
            or not _valid_scope(row.category, row.user_id, row.scope)
            or row.scope != claim.scope or _digest(row.scope) != row.scope_sha256
            or _digest(claim.scope) != claim.scope_sha256
            or not await _valid_completed_tombstone(session, row)):
        await session.rollback()
        return False
    if state == "complete":
        if not isinstance(completion_proof, CleanupCompletionProof):
            raise ValueError("cleanup_completion_proof_required")
        completion_proof.validate(row=row)
    if state == "pending":
        if receipt is None:
            raise ValueError("cleanup_pass_receipt_required")
        receipt.validate(expected_category=row.category, subject=row.user_id, request_id=row.request_id)
        if receipt.scope_sha256 != row.scope_sha256:
            raise ValueError("cleanup_scope_changed")
        if row.category == "temporary_media" and receipt.remaining_count == 0:
            # An empty listing alone does not prove an unexpired or in-flight PUT
            # capability has drained. Keep pending until an explicit proof arrives.
            if next_attempt_at is None:
                raise ValueError("cleanup_next_check_required")
        if (not isinstance(next_attempt_at, datetime) or next_attempt_at.tzinfo is None
                or next_attempt_at.utcoffset() is None):
            raise ValueError("cleanup_next_check_invalid")
        if next_attempt_at <= datetime.now(timezone.utc):
            raise ValueError("cleanup_next_check_invalid")
    row.state = state
    if receipt is not None:
        row.last_pass_digest = receipt.pass_digest
        row.remaining_count = receipt.remaining_count
    # Preserve the stable provider job/pass identity when an attempt becomes
    # uncertain or manual-review. It is private evidence for operator recovery;
    # those states are never lease-reclaimed. Normal reschedule/completion release it.
    if state in {"pending", "complete"}:
        row.owner_token = None
    row.lease_expires_at = None
    row.last_error = error_code if type(error_code) is str and error_code in {
        "provider_outcome_unknown", "provider_confirmation_missing", "cleanup_source_not_drained",
    } else None
    row.next_attempt_at = next_attempt_at or row.next_attempt_at
    row.updated_at = datetime.now(timezone.utc)
    if state == "complete":
        row.completed_at = row.updated_at
    await session.commit()
    return True


async def mark_claim_uncertain(session: AsyncSession, claim: CleanupClaim) -> bool:
    """Persist ambiguity without lease-based replay; repeated failure leaves running blocker."""
    return await settle_cleanup_claim(session, claim, state="uncertain", error_code="provider_outcome_unknown")


async def reconcile_cleanup_obligation(session: AsyncSession, obligation_id: uuid.UUID,
                                       proof: CleanupCompletionProof) -> bool:
    """Resolve uncertain evidence only with a subject/scope-bound provider proof."""
    await _bound_db_wait(session)
    row = (await session.scalars(select(models.AccountPostDeletionCleanup).where(
        models.AccountPostDeletionCleanup.id == obligation_id,
    ).with_for_update())).one_or_none()
    if row is None or row.state not in {"uncertain", "manual_review"}:
        await session.rollback()
        return False
    if (not isinstance(row.scope, dict) or not _valid_scope(row.category, row.user_id, row.scope)
            or _digest(row.scope) != row.scope_sha256
            or not _HEX.fullmatch(row.last_pass_digest)):
        row.state = "manual_review"
        row.last_error = "cleanup_scope_invalid"
        row.updated_at = datetime.now(timezone.utc)
        await session.commit()
        return False
    if not await _valid_completed_tombstone(session, row):
        row.state = "manual_review"
        row.last_error = "cleanup_owner_invalid"
        row.updated_at = datetime.now(timezone.utc)
        await session.commit()
        return False
    proof.validate(row=row)
    row.state = "complete"
    row.owner_token = None
    row.lease_expires_at = None
    row.last_error = None
    row.completed_at = datetime.now(timezone.utc)
    row.updated_at = row.completed_at
    await session.commit()
    return True


def _consume_task(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass


async def _mark_uncertain_with_factory(session_factory: Callable[[], object],
                                       claim: CleanupClaim) -> None:
    async with session_factory() as session:  # type: ignore[attr-defined]
        await mark_claim_uncertain(session, claim)


async def _persist_uncertain(factory: Callable[[], object], claim: CleanupClaim) -> None:
    """Keep the short uncertainty write owned through repeated cancellation."""
    write = asyncio.create_task(_mark_uncertain_with_factory(factory, claim))
    deadline = asyncio.get_running_loop().time() + MAX_CLEANUP_JOURNAL_SECONDS
    while not write.done() and asyncio.get_running_loop().time() < deadline:
        try:
            await asyncio.wait_for(asyncio.shield(write), timeout=max(
                0.01, deadline - asyncio.get_running_loop().time(),
            ))
        except asyncio.CancelledError:
            continue
        except TimeoutError:
            break
    if not write.done():
        write.cancel()
        write.add_done_callback(_consume_task)
        return
    write.result()


async def run_post_deletion_cleanup_batch(
    session_factory: Callable[[], object],
    operation: Callable[[CleanupClaim], Awaitable[DeferredCleanupReceipt | CleanupCompletionProof]],
    *, limit: int = 8, timeout_seconds: float = MAX_PROVIDER_SECONDS,
    retry_delay: timedelta = timedelta(hours=1),
) -> dict[str, int]:
    """Run one bounded batch without holding SQL across provider I/O.

    Only pending rows are claimable. Each row is claimed immediately before its
    provider pass. A timeout, exception, or cancellation becomes permanent
    uncertainty and stops this batch until exact readback/drain evidence reconciles it.
    """
    if not get_settings().account_deletion_deferred_cleanup_enabled:
        return {"claimed": 0, "rescheduled": 0, "completed": 0, "uncertain": 0}
    if (type(limit) is not int or not 1 <= limit <= MAX_CLAIM_LIMIT
            or type(timeout_seconds) not in (int, float) or not 0.1 <= timeout_seconds <= MAX_PROVIDER_SECONDS
            or not isinstance(retry_delay, timedelta) or not timedelta(0) < retry_delay <= MAX_RETRY_DELAY):
        raise ValueError("cleanup_batch_limits_invalid")
    counts = {"claimed": 0, "rescheduled": 0, "completed": 0, "uncertain": 0}
    for _ in range(limit):
        async with session_factory() as session:  # type: ignore[attr-defined]
            claims = await claim_due_cleanup(session, limit=1)
        if not claims:
            break
        claim = claims[0]
        counts["claimed"] += 1
        task = asyncio.create_task(operation(claim))
        try:
            done, _pending = await asyncio.wait({task}, timeout=float(timeout_seconds))
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(_consume_task)
            try:
                await _persist_uncertain(session_factory, claim)
            except Exception:
                # The committed running row remains a deletion blocker.
                pass
            raise
        if not done:
            task.cancel()
            task.add_done_callback(_consume_task)
            try:
                async with session_factory() as session:  # type: ignore[attr-defined]
                    await mark_claim_uncertain(session, claim)
            except Exception:
                pass
            counts["uncertain"] += 1
            break
        try:
            result = task.result()
        except asyncio.CancelledError:
            try:
                async with session_factory() as session:  # type: ignore[attr-defined]
                    await mark_claim_uncertain(session, claim)
            except Exception:
                pass
            counts["uncertain"] += 1
            break
        except Exception:
            try:
                async with session_factory() as session:  # type: ignore[attr-defined]
                    await mark_claim_uncertain(session, claim)
            except Exception:
                pass
            counts["uncertain"] += 1
            break
        # The autonomous adapter may only record another bounded pass. Provider
        # drain proof is reconciled separately, never synthesized by an empty list.
        if not isinstance(result, DeferredCleanupReceipt):
            try:
                async with session_factory() as session:  # type: ignore[attr-defined]
                    await mark_claim_uncertain(session, claim)
            except Exception:
                pass
            counts["uncertain"] += 1
            break
        try:
            async with session_factory() as session:  # type: ignore[attr-defined]
                rescheduled = await settle_cleanup_claim(
                    session, claim, state="pending", receipt=result,
                    next_attempt_at=datetime.now(timezone.utc) + retry_delay,
                )
            if rescheduled:
                counts["rescheduled"] += 1
            else:
                counts["uncertain"] += 1
                break
        except Exception:
            # A malformed receipt or failed terminal write cannot be treated as
            # completion. Try a fresh token-fenced uncertainty write; if that also
            # fails, the committed running row remains a conservative blocker.
            try:
                async with session_factory() as session:  # type: ignore[attr-defined]
                    await mark_claim_uncertain(session, claim)
            except Exception:
                pass
            counts["uncertain"] += 1
            break
    return counts
