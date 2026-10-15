"""Fence campus verification email sends against account deletion holds."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable, Callable

from fastapi import HTTPException

from app.config import get_settings
from app.services.account_external_operations import (
    ExternalOperationConflict,
    ExternalOperationHandle,
    finish_external_operation,
    register_external_operation,
)
from app.services.account_deletion_hold import DeletionHoldInvalid


async def _finish_uncertain_without_detaching(handle: ExternalOperationHandle) -> None:
    """Try to persist uncertainty despite repeated request cancellation.

    A failed journal update intentionally leaves the operation running, which also
    blocks deletion drain. This helper never converts a provider exception or
    cancellation into success and never lets a second cancel detach the journal task.
    """
    task = asyncio.create_task(finish_external_operation(handle, state="uncertain"))
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    if task.done():
        try:
            task.result()
        except BaseException:
            # A durable running row is still a blocking state.
            pass


async def send_account_verification_email(
    user_id: uuid.UUID,
    firebase_uid: str | None,
    verification_id: uuid.UUID,
    *,
    send: Callable[[], Awaitable[str]],
) -> str:
    """Run one challenge email under a durable account-operation registration.

    ``finished`` means the provider call returned successfully. It does not prove
    delivery to the recipient's inbox. No challenge content or provider receipt is
    written to the operation registry.
    """
    if not get_settings().account_deletion_http_admission_enabled:
        return await send()

    if not isinstance(user_id, uuid.UUID) or str(uuid.UUID(str(user_id))) != str(user_id):
        raise DeletionHoldInvalid("invalid_identity")
    if not isinstance(firebase_uid, str) or not firebase_uid.strip():
        raise DeletionHoldInvalid("missing_email_operation_identity")
    if not isinstance(verification_id, uuid.UUID) or str(uuid.UUID(str(verification_id))) != str(verification_id):
        raise DeletionHoldInvalid("invalid_verification_operation_id")

    effect_key = hashlib.sha256(str(verification_id).encode("ascii")).hexdigest()
    try:
        handle = await register_external_operation(
            user_id=user_id,
            firebase_uid=firebase_uid,
            kind="campus_verification_email",
            effect_key=effect_key,
        )
    except ExternalOperationConflict:
        raise HTTPException(
            status_code=409,
            detail="verification_email_operation_already_started",
        ) from None

    try:
        result = await send()
    except BaseException:
        # The provider may have accepted the message before the coroutine failed or
        # was cancelled. Never retry or claim that the remote effect did not happen.
        try:
            await _finish_uncertain_without_detaching(handle)
        except BaseException:
            # Preserve the provider failure/cancellation; a running row still blocks
            # drain if the uncertainty write could not be started.
            pass
        raise

    # A completion-write failure leaves the durable row running, which fails drain
    # closed. The send result is returned only after this transition succeeds.
    await finish_external_operation(handle, state="finished")
    return result
