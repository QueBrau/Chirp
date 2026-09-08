"""WebSocket gateway: authenticated per-user event stream bridged from Redis pub/sub."""
import asyncio
import contextlib
import logging
import random
from dataclasses import dataclass

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.middleware.auth import get_user_by_uid
from app.services.identity_verification import run_verification
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
# Keep an already-open session from surviving a moderation suspension indefinitely.
# This is intentionally coarse; HTTP requests still check suspension on every request.
WS_SUSPENSION_POLL_SECONDS = 30.0
# Spread the polls out. Without this every socket on an instance polls on the same
# cadence, and they START synchronised: a Cloud Run instance dying makes every client
# reconnect at once, so their timers line up and N sockets then check out N pool
# connections in the same instant, every 30 seconds, forever. Each poll is short
# (board c205) but a synchronised burst of them is still a burst, arriving exactly
# when a cold instance is least able to absorb it.
#
# +/- 20%, so the herd is smeared across a 12-second band rather than landing on one
# tick. Deliberately jitter on EVERY iteration rather than once at connect: a single
# startup offset keeps the sockets in lockstep with each other, just at a different
# phase, and any pause that stalls all of them together re-synchronises them for good.
WS_SUSPENSION_POLL_JITTER = 0.2


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


async def _resolve_uid(websocket: WebSocket) -> str | None:
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
            project_id = settings.firebase_project_id
            firebase_admin.initialize_app(options={"projectId": project_id} if project_id else None)
        decoded = await run_verification(firebase_auth.verify_id_token, token)
    except Exception:  # invalid/expired token, missing SDK, or init failure
        return None
    return decoded.get("uid")


# c354: these bound application work; they aren't a total socket-RSS or OS
# transport-close guarantee. Keep the current 24-36s authorization reconciliation
# even when Redis fails. See WEBSOCKET-RESOURCE-LIMITS.md for the measured envelope.
WS_SUBSCRIBE_SECONDS = 2.0
WS_AUTH_SECONDS = 10.0
WS_SEND_SECONDS = 5.0
WS_CLOSE_SECONDS = 1.0
WS_CLEANUP_SECONDS = 1.0
WS_RECONCILE_SECONDS = 2.0
WS_QUEUE_MAX_AGE_SECONDS = 5.0
WS_QUEUE_MAX_FRAMES = 32
WS_QUEUE_MAX_BYTES = 512 * 1024
# Current message ciphertext input is <=64KiB, plus its small JSON envelope.
# Oversized legacy/provider events are disconnected, never truncated.
WS_FRAME_MAX_BYTES = 128 * 1024


class _EndStream(Exception):
    def __init__(self, reason: str, code: int = WS_REALTIME_UNAVAILABLE):
        self.reason = reason
        self.code = code
        super().__init__(reason)


@dataclass(frozen=True)
class _Frame:
    text: str
    size: int
    queued_at: float


class _OutboundBuffer:
    """Nonblocking admission; ownership includes the frame held by the sender."""

    def __init__(self):
        self.queue: asyncio.Queue[_Frame] = asyncio.Queue(maxsize=WS_QUEUE_MAX_FRAMES)
        self.frames = self.bytes = self.peak_frames = self.peak_bytes = 0

    def offer(self, data: str) -> None:
        if not isinstance(data, str):
            raise _EndStream("invalid_broker_frame")
        # Check characters first to avoid another huge allocation for oversized
        # input. Redis already parsed this frame; that allocation is not this cap.
        if len(data) > WS_FRAME_MAX_BYTES:
            raise _EndStream("frame_too_large")
        size = len(data.encode("utf-8"))
        if size > WS_FRAME_MAX_BYTES:
            raise _EndStream("frame_too_large")
        if self.frames >= WS_QUEUE_MAX_FRAMES or self.bytes + size > WS_QUEUE_MAX_BYTES:
            raise _EndStream("queue_full")
        self.queue.put_nowait(_Frame(data, size, asyncio.get_running_loop().time()))
        self.frames += 1
        self.bytes += size
        self.peak_frames = max(self.peak_frames, self.frames)
        self.peak_bytes = max(self.peak_bytes, self.bytes)

    def release(self, frame: _Frame) -> None:
        self.frames -= 1
        self.bytes -= frame.size

    def clear(self) -> None:
        while not self.queue.empty():
            self.release(self.queue.get_nowait())


async def _bounded_close(websocket: WebSocket, code: int) -> None:
    with contextlib.suppress(Exception):
        async with asyncio.timeout(WS_CLOSE_SECONDS):
            await websocket.close(code=code)


@router.websocket("/ws")
async def websocket_gateway(websocket: WebSocket) -> None:
    """Short SQL scopes, bounded outbound ownership, and explicit stream failure.

    No yield dependency: a websocket must never pin its authentication SQL
    connection for its lifetime (c205). A Redis subscription belongs to this
    socket until teardown; sharing subscriptions is a measured follow-up.
    """
    try:
        async with asyncio.timeout(WS_AUTH_SECONDS):
            uid = await _resolve_uid(websocket)
    except TimeoutError:
        await _bounded_close(websocket, WS_REALTIME_UNAVAILABLE)
        return
    if uid is None:
        await _bounded_close(websocket, 4401)
        return

    try:
        async with asyncio.timeout(WS_RECONCILE_SECONDS):
            async with get_session_factory()() as session:
                user = await get_user_by_uid(session, uid)
                user_id = user.id if user is not None else None
                suspended_at = user.suspended_at if user is not None else None
    except Exception:
        await _bounded_close(websocket, WS_REALTIME_UNAVAILABLE)
        return
    # Always release SQL before any transport write, including rejection.
    if user_id is None:
        await _bounded_close(websocket, 4401)
        return
    if suspended_at is not None:
        await _bounded_close(websocket, WS_ACCOUNT_SUSPENDED)
        return

    channel = f"user:{user_id}"
    buffer = _OutboundBuffer()
    pubsub = None
    tasks: list[asyncio.Task] = []
    reason, close_code = "client_disconnect", None

    async def read_broker() -> None:
        # redis-py subscribe() sends the command but does NOT read its ACK.
        # A ready event before this ACK recreates the history/live delivery gap.
        async with asyncio.timeout(WS_SUBSCRIBE_SECONDS):
            await pubsub.subscribe(channel)
            stream = pubsub.listen()
            while True:
                item = await anext(stream)
                if item.get("type") == "subscribe" and item.get("channel") == channel:
                    break
                if item.get("type") == "message":
                    raise _EndStream("message_before_subscription_ack")
        buffer.offer('{"type":"ready"}')
        async for item in stream:
            kind = item.get("type")
            if kind == "message":
                buffer.offer(item["data"])
            elif kind == "subscribe":
                # redis-py can reconnect/resubscribe transparently. Events may
                # have been lost; force a client reconnect and durable catch-up.
                raise _EndStream("broker_resubscribed")
        raise _EndStream("broker_stream_ended")

    async def send_frames() -> None:
        while True:
            frame = await buffer.queue.get()
            try:
                remaining = WS_QUEUE_MAX_AGE_SECONDS - (asyncio.get_running_loop().time() - frame.queued_at)
                if remaining <= 0:
                    raise _EndStream("frame_expired")
                async with asyncio.timeout(min(WS_SEND_SECONDS, remaining)):
                    await websocket.send_text(frame.text)
            finally:
                buffer.release(frame)

    async def drain_client() -> None:
        while True:
            # Application traffic is server -> client. The container's ASGI
            # settings bound incoming message payload bytes before this handler;
            # they do not bound fragment count (see the open runbook issue).
            await websocket.receive_text()

    async def reconcile_user() -> None:
        while True:
            jitter = 1 + random.uniform(-WS_SUSPENSION_POLL_JITTER, WS_SUSPENSION_POLL_JITTER)
            await asyncio.sleep(WS_SUSPENSION_POLL_SECONDS * jitter)
            async with asyncio.timeout(WS_RECONCILE_SECONDS):
                async with get_session_factory()() as poll_session:
                    result = await poll_session.execute(
                        select(models.User.suspended_at).where(models.User.id == user_id)
                    )
                    row = result.one_or_none()
            # scalar_one_or_none cannot distinguish a missing user from the
            # SQL NULL that means an existing user is not suspended.
            if row is None:
                raise _EndStream("account_missing", 4401)
            if row[0] is not None:
                raise _EndStream("account_suspended", WS_ACCOUNT_SUSPENDED)

    try:
        async with asyncio.timeout(WS_SUBSCRIBE_SECONDS):
            await websocket.accept(subprotocol=_offered_protocol(websocket))
        pubsub = get_redis().pubsub()
        # All tasks only report a reason. The owner stops every producer and
        # sender BEFORE writing close, including suspension and queue overflow.
        reader = asyncio.create_task(read_broker(), name="ws-subscribe-forward")
        sender = asyncio.create_task(send_frames(), name="ws-send")
        drain = asyncio.create_task(drain_client(), name="ws-drain")
        watcher = asyncio.create_task(reconcile_user(), name="ws-suspension-watch")
        tasks = [watcher, reader, sender, drain]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # Authorization takes precedence if several tasks finish in one turn.
        for task in tasks:
            if task in done:
                await task
    except _EndStream as exc:
        reason, close_code = exc.reason, exc.code
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        # Exception type is safe; provider URLs, token material, and payloads
        # are not. Timeouts/failures are realtime loss, not credential failure.
        reason, close_code = type(exc).__name__, WS_REALTIME_UNAVAILABLE
    finally:
        # ASGI/AnyIO can cancel at every checkpoint, not only once. Protect
        # owned teardown from that outer scope while retaining each local
        # timeout; the original cancellation still propagates after cleanup.
        with anyio.CancelScope(shield=True):
            for task in tasks:
                task.cancel()
            with contextlib.suppress(Exception):
                async with asyncio.timeout(WS_CLEANUP_SECONDS):
                    await asyncio.gather(*tasks, return_exceptions=True)
            buffer.clear()
            if close_code is not None:
                logger.warning(
                    "ws stream closed reason=%s user_id=%s peak_frames=%s peak_bytes=%s",
                    reason, user_id, buffer.peak_frames, buffer.peak_bytes,
                    extra={"ws_reason": reason, "ws_peak_frames": buffer.peak_frames, "ws_peak_bytes": buffer.peak_bytes},
                )
                await _bounded_close(websocket, close_code)
            if pubsub is not None:
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(WS_CLEANUP_SECONDS):
                        # aclose disconnects and releases the dedicated subscription
                        # connection. No extra UNSUBSCRIBE round trip is necessary.
                        await pubsub.aclose()
