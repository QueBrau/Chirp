"""Track actual media workers before deletion can inspect their external effects.

This is distinct from the request's SQL write guard. A completed storage worker
does not authorize a domain write, attest that a signed upload has expired, or
prove that all of an account's external producers have drained.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Callable

from fastapi import HTTPException

from app.config import get_settings
from app.services.account_deletion_hold import DeletionHoldInvalid
from app.services.account_external_operations import (
    ExternalOperationConflict,
    ExternalOperationHandle,
    finish_external_operation,
    register_external_operation,
)
from app.services.storage_service import (
    AVATAR_PREFIX,
    PERMANENT_PREFIX,
    validate_media_object_names,
)

logger = logging.getLogger(__name__)


async def _run_registered_worker(
    handle: ExternalOperationHandle, finalize: Callable[..., str],
    user_id: uuid.UUID, object_name: str, destination_prefix: str,
) -> str:
    try:
        if destination_prefix == PERMANENT_PREFIX:
            result = await asyncio.to_thread(finalize, str(user_id), object_name)
        else:
            result = await asyncio.to_thread(
                finalize, str(user_id), object_name,
                destination_prefix=destination_prefix,
            )
    except BaseException:
        # SDK errors can occur after a remote write. Even a stopped Python thread
        # does not prove that a timed-out remote request had no effect. Do not
        # retry it, or declare it drained, based on elapsed time.
        try:
            await finish_external_operation(handle, state="uncertain")
        except Exception:
            # The durable running row also blocks drain. Neither the provider's
            # exception nor the internal owner token belongs in this log.
            logger.warning("media operation could not record uncertain completion")
        raise
    await finish_external_operation(handle, state="finished")
    return result


async def finalize_account_media(
    user_id: uuid.UUID, firebase_uid: str | None, object_name: str, *,
    finalize: Callable[..., str], destination_prefix: str = PERMANENT_PREFIX,
) -> str:
    """Run one finalizer without a database connection across storage I/O.

    With admission disabled, retain the original threading and schema behavior.
    With it enabled, registration commits before the worker starts. Repeated
    cancellation waits for the actual worker and its durable completion attempt;
    cancellation cannot erase the pending operation or turn it into success.
    """
    if not get_settings().account_deletion_http_admission_enabled:
        if destination_prefix == PERMANENT_PREFIX:
            return await asyncio.to_thread(finalize, str(user_id), object_name)
        return await asyncio.to_thread(
            finalize, str(user_id), object_name,
            destination_prefix=destination_prefix,
        )

    if destination_prefix not in (PERMANENT_PREFIX, AVATAR_PREFIX):
        raise DeletionHoldInvalid("invalid_media_operation_kind")
    if not isinstance(firebase_uid, str) or not firebase_uid:
        raise DeletionHoldInvalid("missing_media_operation_identity")
    validate_media_object_names(str(user_id), [object_name])
    kind = "media_finalize_avatar" if destination_prefix == AVATAR_PREFIX else "media_finalize_post"
    # Hash the validated, caller-owned source. No raw filename, signed URL,
    # provider credential or image content is persisted in this control record.
    effect_key = hashlib.sha256(object_name.encode("utf-8")).hexdigest()
    try:
        handle = await register_external_operation(
            user_id=user_id, firebase_uid=firebase_uid, kind=kind,
            effect_key=effect_key,
        )
    except ExternalOperationConflict:
        raise HTTPException(status_code=409, detail="media_operation_already_started") from None

    worker = asyncio.create_task(
        _run_registered_worker(handle, finalize, user_id, object_name, destination_prefix)
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Shield alone leaves a detached worker. Drain it here, including when
        # shutdown or the caller cancels this request more than once. A process
        # crash can still leave running work, which remains blocking for review.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if worker.done():
            try:
                worker.result()
            except BaseException:
                pass
        raise
