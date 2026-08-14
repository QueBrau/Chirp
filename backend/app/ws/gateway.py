"""WebSocket gateway: authenticated per-user event stream bridged from Redis pub/sub."""
import asyncio
import contextlib
import logging
import re

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.db import get_session
from app.middleware.auth import get_user_by_uid
from app.ws.pubsub import get_redis

router = APIRouter(tags=["ws"])

_TOKEN_QS_RE = re.compile(r"([?&]token=)[^&\s]+")


class _RedactWsTokenFilter(logging.Filter):
    """Redacts `token=<...>` from uvicorn's access log (SECURITY-REVIEW finding 4).

    The WS handshake authenticates via `?token=<firebase-id-token>` in the URL (RN
    WebSocket clients can't always set headers), so uvicorn's default access log would
    otherwise write real bearer tokens to stdout/Cloud Run logs verbatim. This filter
    scrubs any log record whose args or message contain a `token=` query param, without
    touching the auth mechanism itself.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TOKEN_QS_RE.sub(r"\1[REDACTED]", value)
                if isinstance(value, str) and "token=" in value
                else value
                for value in record.args
            )
        elif isinstance(record.msg, str) and "token=" in record.msg:
            record.msg = _TOKEN_QS_RE.sub(r"\1[REDACTED]", record.msg)
        return True


def _install_ws_token_log_filter() -> None:
    """Idempotently attach the redaction filter to uvicorn's access logger."""
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _RedactWsTokenFilter) for f in access_logger.filters):
        access_logger.addFilter(_RedactWsTokenFilter())


_install_ws_token_log_filter()


def _resolve_uid(websocket: WebSocket) -> str | None:
    """Resolve a verified Firebase uid from the handshake, or None.

    Emulated mode: X-Debug-Firebase-Uid header (or ?token= fallback, since RN
    WebSocket clients cannot always set headers). Firebase mode: ?token= query
    param (or Authorization: Bearer) verified via firebase_admin.
    """
    settings = get_settings()
    if settings.auth_mode == "emulated":
        return websocket.headers.get("X-Debug-Firebase-Uid") or websocket.query_params.get("token")

    token = websocket.query_params.get("token")
    if not token:
        auth_header = websocket.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.removeprefix("Bearer ").strip()
    if not token:
        return None
    try:
        import firebase_admin
        from firebase_admin import auth as firebase_auth

        try:
            firebase_admin.get_app()
        except ValueError:
            firebase_admin.initialize_app()
        decoded = firebase_auth.verify_id_token(token)
    except Exception:  # invalid/expired token, missing SDK, or init failure
        return None
    return decoded.get("uid")


@router.websocket("/ws")
async def websocket_gateway(
    websocket: WebSocket,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Authenticate the connection, then forward user:{user_id} Redis events until disconnect."""
    uid = _resolve_uid(websocket)
    if uid is None:
        await websocket.close(code=4401)
        return
    user = await get_user_by_uid(session, uid)
    if user is None:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    channel = f"user:{user.id}"
    pubsub = get_redis().pubsub()
    await pubsub.subscribe(channel)

    async def _forward() -> None:
        async for item in pubsub.listen():
            if item.get("type") == "message":
                await websocket.send_text(item["data"])

    forward_task = asyncio.create_task(_forward())
    try:
        while True:
            # Drain client frames purely to detect disconnect; the stream is server -> client.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        forward_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await forward_task
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
