"""Transactional email: the single path for every message Chirp sends a human.

Until board c87 this repo could not send mail at all — no SMTP, no SendGrid, no SES,
no Postmark anywhere in the tree. Three separate things need it: the .edu verification
code (c86), the 30-day removal reply /privacy commits to in writing, and any future
receipt. They all call `send_email` here rather than growing three integrations that
drift apart.

PROVIDER: Resend, decided Aug 16, keyed from Secret Manager alongside the Stripe keys.
It lives behind this module specifically so swapping the sender is a config change
rather than a rewrite — which is exactly how the Aug 24 move onto our own domain went:
one env var, no code touched.

WHAT IS PROVEN, because this module used to ship with the opposite constraint and that
note outlived its truth: we DO have a verified sending domain. josedev.app was verified
in Resend on Aug 24 (board c134), and prod sends as `Chirp <hello@josedev.app>` via
EMAIL_FROM in the Cloud Run env — not via the default in config.py. The send path is
proven through the real prod flow: POST /auth/campus-verification returned 202 for an
arbitrary .edu recipient, and 202 IS proof Resend accepted the message, because the only
way out of `_send_via_resend` without a 502 is a provider response under 400. The
failure, when it comes, is still loud and logged rather than silent.

WHAT IS NOT PROVEN is the far end. No .edu mailbox the team controls has ever been
watched to RECEIVE a code, so provider acceptance is where the evidence stops (board
c71, still open by Jose's call). Acceptance is not delivery — do not let "Resend took
it" get written up as "the student got it" until someone has read a code out of a real
school inbox.

WHAT THIS MODULE MUST NEVER DO IS LOG A MESSAGE BODY OR A FULL RECIPIENT ADDRESS.
c86 puts one-time verification codes and student email addresses through here; either
one sitting in Cloud Logging is unnecessary exposure. Sends are recorded with a fixed
recipient-redaction marker, subject and provider id only. `test_email_service.py`
asserts this, and that test is there to fail if a debugging session ever adds either
value "just for now".
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException

from app.config import get_settings

logger = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"

# SES v2 SendEmail (c284). The host is region-scoped and the region is part of the
# signature's credential scope, so a mismatch between the two is an auth failure rather
# than a routing one — build both from the same setting, never from separate constants.
SES_SERVICE = "ses"
SES_PATH = "/v2/email/outbound-emails"


def _ses_host(region: str) -> str:
    return f"email.{region}.amazonaws.com"

# Resend is a dependency in the request path (c86 sends a code while the user waits),
# so a hung provider must not hold a worker open indefinitely.
REQUEST_TIMEOUT_SECONDS = 10.0


def _resend_api_key() -> str:
    """Resend key, or 503 if this deployment has not configured email.

    Mirrors stripe_service._secret_key: an unconfigured integration is a deployment
    state, not a user error, and it should read that way at the API boundary.
    """
    key = get_settings().resend_api_key
    if not key:
        raise HTTPException(status_code=503, detail="email_not_configured")
    return key


async def send_email(*, to: str, subject: str, html: str, text: str | None = None) -> str:
    """Send one transactional email; returns the provider's message id.

    Raises 503 if the deployment configured Resend without a key, and 502 if the
    provider rejected or failed the send. Callers get a message id they can log and
    correlate with the provider dashboard.
    """
    provider = get_settings().email_provider
    if provider == "log":
        return _send_via_log(to=to, subject=subject)
    if provider == "ses":
        return await _send_via_ses(to=to, subject=subject, html=html, text=text)
    return await _send_via_resend(to=to, subject=subject, html=html, text=text)


def _send_via_log(*, to: str, subject: str) -> str:
    """Development sink: record that a send happened and deliver nothing.

    This is the default so a fresh clone and the whole test suite work with no key
    and no possibility of mailing a real person.
    """
    message_id = f"log:{uuid.uuid4()}"
    logger.info(
        "email not delivered (provider=log) recipient=[redacted] subject=%s message_id=%s",
        subject,
        message_id,
    )
    return message_id


async def _send_via_resend(*, to: str, subject: str, html: str, text: str | None) -> str:
    """POST one message to Resend and return its id."""
    settings = get_settings()
    payload: dict[str, object] = {
        "from": settings.email_from,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text is not None:
        payload["text"] = text
    if settings.email_reply_to:
        payload["reply_to"] = settings.email_reply_to

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(
                RESEND_ENDPOINT,
                headers={"Authorization": f"Bearer {_resend_api_key()}"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        # Transport failure: no response at all. Log the exception type rather than
        # str(exc), which on some httpx errors carries the request URL and headers.
        logger.warning(
            "email transport failed recipient=[redacted] subject=%s error=%s",
            subject,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="email_send_failed") from exc

    if response.status_code >= 400:
        # Provider error bodies can echo request fields, including the recipient. Keep
        # the status (the useful operational signal) and omit the body entirely.
        logger.warning(
            "email rejected by provider recipient=[redacted] subject=%s status=%s",
            subject,
            response.status_code,
        )
        raise HTTPException(status_code=502, detail="email_send_failed")

    message_id = str(response.json().get("id", ""))
    logger.info(
        "email sent recipient=[redacted] subject=%s message_id=%s", subject, message_id
    )
    return message_id


def _ses_credentials() -> tuple[str, str, str]:
    """(region, access_key_id, secret_access_key), or 503 if SES is not configured.

    Same fail-closed shape as _resend_api_key and stripe_service._secret_key: a
    deployment that selected a provider without giving it credentials is a deployment
    state, and it should read that way at the API boundary rather than surfacing as a
    signing error nobody can act on.
    """
    settings = get_settings()
    region = settings.aws_region
    access_key = settings.aws_access_key_id
    secret_key = settings.aws_secret_access_key
    if not region or not access_key or not secret_key:
        raise HTTPException(status_code=503, detail="email_not_configured")
    return region, access_key, secret_key


def _sigv4_headers(
    *,
    method: str,
    host: str,
    path: str,
    payload: bytes,
    region: str,
    service: str,
    access_key: str,
    secret_key: str,
    now: datetime,
) -> dict[str, str]:
    """AWS Signature Version 4 headers for one request.

    WHY THIS IS HAND-ROLLED RATHER THAN boto3. boto3 is synchronous, and this process
    runs ONE uvicorn worker with no --workers at concurrency 80 — a single event loop
    serving every in-flight request. A blocking SDK call in the send path is the exact
    shape c211 and c223 were filed to remove, and it would have to be wrapped in
    asyncio.to_thread to be safe. Signing by hand keeps this adapter on the same async
    httpx transport the Resend path already uses, and adds no runtime dependency to an
    image where cold start is a live concern.

    WHAT MAKES THAT SAFE RATHER THAN BRAVE: the signature is not trusted because it
    looks right. test_c284_ses_provider.py signs the same request with botocore's own
    SigV4Auth — AWS's reference implementation — and asserts byte equality with what
    this function produces. botocore is a DEV dependency only; the production image
    never imports it. If this drifts from AWS's algorithm, that test fails here rather
    than at a cutover nobody can debug at launch.

    `now` is injected rather than read from the clock so the signature is deterministic
    and can be compared against a reference for a fixed instant.
    """
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload).hexdigest()

    # Header names lowercase and alphabetically ordered; the block ends with a newline,
    # so joining below produces the blank line AWS requires before signed_headers.
    canonical_headers = (
        f"content-type:application/json\nhost:{host}\nx-amz-date:{amz_date}\n"
    )
    signed_headers = "content-type;host;x-amz-date"
    canonical_request = "\n".join(
        [method, path, "", canonical_headers, signed_headers, payload_hash]
    )

    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )

    def _sign(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode(), hashlib.sha256).digest()

    signing_key = _sign(
        _sign(_sign(_sign(f"AWS4{secret_key}".encode(), datestamp), region), service),
        "aws4_request",
    )
    signature = hmac.new(
        signing_key, string_to_sign.encode(), hashlib.sha256
    ).hexdigest()

    return {
        "Content-Type": "application/json",
        "Host": host,
        "X-Amz-Date": amz_date,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


async def _send_via_ses(*, to: str, subject: str, html: str, text: str | None) -> str:
    """POST one message to SES v2 SendEmail and return its MessageId.

    Contract is IDENTICAL to _send_via_resend on purpose, because c134's semantics rest
    on it: the route returns 202 only if the provider accepted, so this must raise 502
    on a transport failure or any provider response >= 400 before the route can return.
    Swapping providers must not quietly turn "accepted" into "we tried".
    """
    settings = get_settings()
    region, access_key, secret_key = _ses_credentials()

    content: dict[str, object] = {
        "Subject": {"Data": subject, "Charset": "UTF-8"},
        "Body": {"Html": {"Data": html, "Charset": "UTF-8"}},
    }
    if text is not None:
        content["Body"]["Text"] = {"Data": text, "Charset": "UTF-8"}  # type: ignore[index]

    body: dict[str, object] = {
        "FromEmailAddress": settings.email_from,
        "Destination": {"ToAddresses": [to]},
        "Content": {"Simple": content},
    }
    if settings.email_reply_to:
        body["ReplyToAddresses"] = [settings.email_reply_to]

    # Separators without spaces: the signed payload hash must be taken over the EXACT
    # bytes that go on the wire, so the body is serialised once and reused for both.
    payload = json.dumps(body, separators=(",", ":")).encode()
    host = _ses_host(region)
    headers = _sigv4_headers(
        method="POST",
        host=host,
        path=SES_PATH,
        payload=payload,
        region=region,
        service=SES_SERVICE,
        access_key=access_key,
        secret_key=secret_key,
        now=datetime.now(timezone.utc),
    )

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"https://{host}{SES_PATH}", headers=headers, content=payload
            )
    except httpx.HTTPError as exc:
        # Same redaction rule as the Resend path: log the exception TYPE, never str(exc),
        # which on some httpx errors carries the request url and headers — and here the
        # headers carry an AWS Authorization signature.
        logger.warning(
            "email transport failed recipient=[redacted] subject=%s error=%s",
            subject,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="email_send_failed") from exc

    if response.status_code >= 400:
        # SES error bodies echo request fields including the destination address, so the
        # status is kept and the body is dropped entirely.
        logger.warning(
            "email rejected by provider recipient=[redacted] subject=%s status=%s",
            subject,
            response.status_code,
        )
        raise HTTPException(status_code=502, detail="email_send_failed")

    message_id = str(response.json().get("MessageId", ""))
    logger.info(
        "email sent recipient=[redacted] subject=%s message_id=%s", subject, message_id
    )
    return message_id
