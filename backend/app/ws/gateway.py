"""WebSocket gateway: authenticated per-user event stream bridged from Redis pub/sub."""
import asyncio
import contextlib
import logging

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import get_settings
from app.db import get_session
from app.middleware.auth import get_user_by_uid
from app.ws.pubsub import get_redis

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ws"])

# Application close codes live in 4000-4999. 4401 mirrors HTTP 401 and already
# means "your credentials did not resolve to a user"; this one mirrors 503 and
# means "you are authenticated, but the realtime backend is unavailable". The
# split matters to the client: 4401 should send the user to sign-in, while this
# should back off and retry, since nothing about the session is wrong.
WS_REALTIME_UNAVAILABLE = 4503
# Mirrors HTTP 403 (board c126), and is deliberately NOT 4401: the credentials
# resolved to a real user, same as middleware/auth.py's get_current_user split —
# 401 means "who are you", 403 means "I know who you are and it's a no". A client
# that ever learns to tell these apart should send 4401 to sign-in and this one to
# a "your account is suspended" screen, not another reconnect attempt.
WS_ACCOUNT_SUSPENDED = 4403


def _offered_protocol(websocket: WebSocket) -> str | None:
    """The first client-offered Sec-WebSocket-Protocol value, or None.

    (security-pass item 7, ~Aug 22): this IS the auth material now — see
    _resolve_uid. Populated by the ASGI server from the handshake request's
    Sec-WebSocket-Protocol header before accept(), so it is readable pre-accept
    the same way ?token= used to be, which is what keeps a bad token rejecting
    the handshake outright rather than accepting first and closing after.
    """
    protocols = websocket.scope.get("subprotocols") or []
    return protocols[0] if protocols else None


def _resolve_uid(websocket: WebSocket) -> str | None:
    """Resolve a verified Firebase uid from the handshake, or None.

    REWRITTEN (security-pass item 7, ~Aug 22): the handshake used to authenticate
    via `?token=<id-token>` in the URL, because RN's WebSocket constructor cannot
    set arbitrary headers. Cloud Run logs `httpRequest.requestUrl` itself — outside
    any redaction the app could install (a filter existed for uvicorn's OWN access
    log only; it never touched Cloud Run's platform-level logging, so this was
    live at the infra layer regardless of what ran in-process). The query string
    is gone entirely rather than deprecated: grepped Cloud Logging directly before
    this landed (see the security pass's report) — zero requests had ever carried
    `token=`, because nothing in the app called `chirpSocket.connect()` yet
    (board c63 is what would have started sending real traffic down this path).
    There was no live client to preserve compatibility for.

    RN's WebSocket constructor CAN set subprotocols (the second constructor arg),
    which is why that replaces the query string as the primary path rather than
    sitting next to it. Emulated mode: X-Debug-Firebase-Uid header (already
    settable by every test client in this repo), or the offered subprotocol as a
    fallback for a caller that can set one but not the header. Firebase mode: the
    offered subprotocol, or an Authorization: Bearer header for a caller that CAN
    set headers (browsers/RN cannot on a WebSocket, but this keeps the door open
    for a server-to-server or native caller that could).
    """
    settings = get_settings()
    protocol_token = _offered_protocol(websocket)

    if settings.auth_mode == "emulated":
        return websocket.headers.get("X-Debug-Firebase-Uid") or protocol_token

    token = protocol_token
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
    offered_protocol = _offered_protocol(websocket)
    uid = _resolve_uid(websocket)
    if uid is None:
        await websocket.close(code=4401)
        return
    user = await get_user_by_uid(session, uid)
    if user is None:
        await websocket.close(code=4401)
        return
    # c126: mirrors middleware/auth.py's get_current_user, the HTTP precedent —
    # same field, same "resolved but blocked" meaning. This closes NEW connection
    # attempts only, matching what the HTTP side does (checked per-request, and a
    # WS connect is this gateway's equivalent of a request). It does not reach an
    # already-open socket for someone suspended mid-session — that's a genuinely
    # different problem (periodic re-check or a suspend-triggered kill) and is
    # deliberately out of scope here rather than silently pretended-closed.
    if user.suspended_at is not None:
        await websocket.close(code=WS_ACCOUNT_SUSPENDED)
        return

    # item 7: echo the offered protocol back so a strict client (one that
    # verifies the server selected a protocol it actually offered) doesn't
    # treat a bare accept() as a mismatch. None when auth came via the
    # Authorization header instead — nothing was offered, so nothing to select.
    await websocket.accept(subprotocol=offered_protocol)
    channel = f"user:{user.id}"
    pubsub = get_redis().pubsub()

    # Subscribe is the first thing here that touches the network, and it runs
    # AFTER accept(), so an unreachable Redis used to surface as a socket that
    # opened and then died on an unhandled ConnectionError — indistinguishable
    # from a flaky client, with no server-side signal that the cause was missing
    # infrastructure (board c62). Redis was in fact never provisioned in prod
    # (c61), so this was every connection, not an edge case.
    #
    # Closing with a distinct application code lets the client tell "the realtime
    # backend is down, back off" apart from "your token is bad" (4401) and from
    # an ordinary network drop, which it would otherwise reconnect against in a
    # tight loop.
    try:
        await pubsub.subscribe(channel)
    except Exception:
        # user_id only, never ciphertext or token material (SPEC 8.1), and the
        # same warning shape messages.py already uses on the publish side.
        logger.error("ws subscribe failed, realtime unavailable user_id=%s", user.id)
        with contextlib.suppress(Exception):
            await pubsub.aclose()
        await websocket.close(code=WS_REALTIME_UNAVAILABLE)
        return

    async def _forward() -> None:
        async for item in pubsub.listen():
            if item.get("type") == "message":
                await websocket.send_text(item["data"])

    async def _drain() -> None:
        while True:
            # Drain client frames purely to detect disconnect; the stream is server -> client.
            await websocket.receive_text()

    forward_task = asyncio.create_task(_forward(), name="ws-forward")
    drain_task = asyncio.create_task(_drain(), name="ws-drain")

    try:
        # Race the two: whichever ends first ends the connection. Awaiting only
        # the client-drain (the previous shape) meant a forwarder that died —
        # Redis restarting, the VPC connector dropping — was never observed. The
        # socket stayed open delivering nothing, with no log and no close frame,
        # which is the same "looks like a flaky client" symptom c62 exists to
        # kill, just moved from connect time to steady state.
        done, _ = await asyncio.wait(
            {forward_task, drain_task}, return_when=asyncio.FIRST_COMPLETED
        )

        if forward_task in done:
            # _forward only returns if the pubsub stream ended, so reaching here
            # at all means realtime is gone for this connection.
            error = forward_task.exception()
            logger.error(
                "ws forward ended mid-connection, realtime lost user_id=%s (%s)",
                user.id,
                type(error).__name__ if error else "stream closed",
            )
            with contextlib.suppress(Exception):
                await websocket.close(code=WS_REALTIME_UNAVAILABLE)
    finally:
        for task in (forward_task, drain_task):
            task.cancel()
        for task in (forward_task, drain_task):
            # Both Exception and CancelledError, deliberately. Awaiting a task
            # re-raises whatever it stored, and CancelledError is a
            # BaseException — suppressing only one of them lets the other escape
            # the finally block and skip the teardown below it.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task

        # Teardown is best-effort: if Redis died mid-connection these raise, and
        # an exception here would mask whatever actually ended the connection.
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(channel)
        with contextlib.suppress(Exception):
            await pubsub.aclose()
