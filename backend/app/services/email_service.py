"""Transactional email: the single path for every message Chirp sends a human.

Until board c87 this repo could not send mail at all — no SMTP, no SendGrid, no SES,
no Postmark anywhere in the tree. Three separate things need it: the .edu verification
code (c86), the 30-day removal reply /privacy commits to in writing, and any future
receipt. They all call `send_email` here rather than growing three integrations that
drift apart.

PROVIDER: Resend, decided Aug 16, keyed from Secret Manager alongside the Stripe keys.
It lives behind this module specifically so swapping the sender once c73 buys a domain
is a config change rather than a rewrite.

THE CONSTRAINT THIS SHIPS WITH, and it will look like a bug if you have not read it:
we deliberately do NOT own a sending domain yet. Resend restricts an account with no
verified domain to sending at its OWN address, so a send to a real student's inbox
will be refused by the provider, not by this code. That is expected until c73 lands.
The failure is loud and logged rather than silent, which is the point.

WHAT THIS MODULE MUST NEVER DO IS LOG A MESSAGE BODY. c86 puts one-time verification
codes through here, and a code sitting in Cloud Logging is a code that anyone with log
access can redeem — a log line is not a private place. Sends are recorded by recipient,
subject and provider id only. `test_email_service.py` asserts this, and that test is
there to fail if a debugging session ever adds the body "just for now".
"""
from __future__ import annotations

import logging
import uuid

import httpx
from fastapi import HTTPException

from app.config import get_settings

logger = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"

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
    return await _send_via_resend(to=to, subject=subject, html=html, text=text)


def _send_via_log(*, to: str, subject: str) -> str:
    """Development sink: record that a send happened and deliver nothing.

    This is the default so a fresh clone and the whole test suite work with no key
    and no possibility of mailing a real person.
    """
    message_id = f"log:{uuid.uuid4()}"
    logger.info(
        "email not delivered (provider=log) to=%s subject=%s message_id=%s",
        to,
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
        logger.warning("email transport failed to=%s subject=%s error=%s", to, subject, type(exc).__name__)
        raise HTTPException(status_code=502, detail="email_send_failed") from exc

    if response.status_code >= 400:
        # Resend's error body describes OUR request, not the message content, so it is
        # safe to log and it is the only useful clue when sends start failing. The
        # no-domain restriction described in the module docstring surfaces here.
        logger.warning(
            "email rejected by provider to=%s subject=%s status=%s body=%s",
            to,
            subject,
            response.status_code,
            response.text[:500],
        )
        raise HTTPException(status_code=502, detail="email_send_failed")

    message_id = str(response.json().get("id", ""))
    logger.info("email sent to=%s subject=%s message_id=%s", to, subject, message_id)
    return message_id
