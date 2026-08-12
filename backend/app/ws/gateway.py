"""WebSocket gateway: authenticated per-user event stream bridged from Redis pub/sub."""
import asyncio
import contextlib

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.db import get_session
from app.ws.pubsub import get_redis

router = APIRouter(tags=["ws"])


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
    result = await session.execute(select(models.User).where(models.User.firebase_uid == uid))
    user = result.scalar_one_or_none()
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
