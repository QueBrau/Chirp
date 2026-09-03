"""c284: the SES provider, and the proof its signature is actually correct.

Jose chose SES over Resend Pro for launch (c240) — cheaper by orders of magnitude at our
volume, and Resend's free tier caps at 100/DAY, not 3,000/month, which is the constraint
that actually bites. This adapter is the code half; the cutover (secret mount, env flip,
redeploy) is manager+Jose once his AWS account exists.

THE CENTRAL RISK OF THIS CARD IS THAT NOBODY CAN TEST IT AGAINST AWS. There is no AWS
account yet, so a subtly wrong SigV4 signature would not surface until the cutover — at
launch, in front of the one flow that gates every new student. Guessing and hoping was
not acceptable, so:

  test_our_signature_matches_botocores_reference_implementation signs the same request
  with botocore's SigV4Auth — AWS's own signing code — and asserts byte equality with
  what email_service produces. botocore is a DEV dependency only and is never imported
  at runtime (boto3 is synchronous and would block the single event loop this process
  serves every request from; see the _sigv4_headers docstring). If our implementation
  ever drifts from AWS's algorithm, it fails HERE instead of at a cutover nobody can
  debug under launch pressure.

Everything else in this file pins the contract that c134's semantics rest on: the route
returns 202 only because send_email raises on transport failure or any provider >= 400.
That must survive a provider swap unchanged, or "Resend accepted it" quietly becomes
"SES probably got it".

NO TEST HERE SENDS REAL MAIL. The transport is stubbed in every case and the provider
still defaults to "log" everywhere else in the suite.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import HTTPException

from app.config import Settings, get_settings
from app.services import email_service

pytestmark = pytest.mark.anyio if False else []  # keep default asyncio mode

CODE = "483920"
HTML = f"<p>Your Chirp code is {CODE}</p>"
TEXT = f"Your Chirp code is {CODE}"

REGION = "us-east-2"
ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
FIXED_NOW = datetime(2026, 9, 2, 12, 36, 0, tzinfo=timezone.utc)

_AMBIENT = (
    "EMAIL_PROVIDER",
    "RESEND_API_KEY",
    "EMAIL_FROM",
    "EMAIL_REPLY_TO",
    "AWS_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
)


@pytest.fixture
def configure(monkeypatch: pytest.MonkeyPatch):
    """Build Settings isolated from ambient config, same trap test_email_service documents:
    a developer with a real AWS_REGION in backend/.env would otherwise be testing their
    machine instead of the code."""

    def _configure(**overrides):
        for name in _AMBIENT:
            monkeypatch.delenv(name, raising=False)
        settings = Settings(_env_file=None, **overrides)  # type: ignore[arg-type,call-arg]
        monkeypatch.setattr(email_service, "get_settings", lambda: settings)
        return settings

    yield _configure
    get_settings.cache_clear()


def _ses_settings(**overrides):
    base = {
        "email_provider": "ses",
        "aws_region": REGION,
        "aws_access_key_id": ACCESS_KEY,
        "aws_secret_access_key": SECRET_KEY,
        "email_from": "Chirp <hello@josedev.app>",
    }
    base.update(overrides)
    return base


class _StubClient:
    """httpx.AsyncClient stand-in. Takes `content=` (raw bytes), not `json=`: the SES
    adapter must sign the EXACT bytes it puts on the wire, so it serialises once and
    sends the same buffer it hashed."""

    def __init__(self, response: httpx.Response | Exception, calls: list[dict]) -> None:
        self._response = response
        self._calls = calls

    async def __aenter__(self) -> "_StubClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def post(self, url: str, *, headers: dict, content: bytes) -> httpx.Response:
        self._calls.append({"url": url, "headers": headers, "content": content})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _stub(monkeypatch: pytest.MonkeyPatch, response) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(
        email_service.httpx, "AsyncClient", lambda **_k: _StubClient(response, calls)
    )
    return calls


def _ses_response(status_code: int, body: dict | str) -> httpx.Response:
    url = f"https://{email_service._ses_host(REGION)}{email_service.SES_PATH}"
    kwargs = {"json": body} if isinstance(body, dict) else {"text": body}
    return httpx.Response(status_code, request=httpx.Request("POST", url), **kwargs)


# ---------------------------------------------------------------------------
# the one that matters: is the signature actually right
# ---------------------------------------------------------------------------


def test_our_signature_matches_botocores_reference_implementation() -> None:
    """Byte equality against AWS's own signer, for a fixed request at a fixed instant.

    This is the only thing standing between a hand-rolled SigV4 and finding out it was
    wrong during the launch cutover. botocore is dev-only; nothing here ships.
    """
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    payload = json.dumps({"hello": "world", "n": 1}, separators=(",", ":")).encode()
    host = email_service._ses_host(REGION)
    url = f"https://{host}{email_service.SES_PATH}"

    # botocore stamps X-Amz-Date from its OWN clock inside add_auth and cannot be told
    # to use ours, so it signs FIRST and we adopt the instant it chose. Freezing the
    # comparison to a constant would have been the obvious move and does not work — the
    # first run of this test failed on exactly that, one UTC day apart, which is also
    # proof the comparison is real rather than self-satisfying.
    request = AWSRequest(
        method="POST",
        url=url,
        data=payload,
        headers={"Content-Type": "application/json", "Host": host},
    )
    signer = SigV4Auth(
        Credentials(ACCESS_KEY, SECRET_KEY), email_service.SES_SERVICE, REGION
    )
    signer.add_auth(request)
    signed_at = datetime.strptime(
        request.headers["X-Amz-Date"], "%Y%m%dT%H%M%SZ"
    ).replace(tzinfo=timezone.utc)

    ours = email_service._sigv4_headers(
        method="POST",
        host=host,
        path=email_service.SES_PATH,
        payload=payload,
        region=REGION,
        service=email_service.SES_SERVICE,
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        now=signed_at,
    )

    assert ours["Authorization"] == request.headers["Authorization"], (
        "our SigV4 differs from botocore's reference implementation\n"
        f"ours:     {ours['Authorization']}\n"
        f"botocore: {request.headers['Authorization']}"
    )


def test_the_signature_actually_covers_the_payload() -> None:
    """A signature that ignored the body would still look well-formed and would still
    match itself — so prove changing one byte of the payload changes the signature."""
    common = dict(
        method="POST",
        host=email_service._ses_host(REGION),
        path=email_service.SES_PATH,
        region=REGION,
        service=email_service.SES_SERVICE,
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        now=FIXED_NOW,
    )
    a = email_service._sigv4_headers(payload=b'{"a":1}', **common)
    b = email_service._sigv4_headers(payload=b'{"a":2}', **common)
    assert a["Authorization"] != b["Authorization"]


def test_region_is_bound_into_the_signature_and_the_host_together() -> None:
    """The host is region-scoped and the region is in the credential scope, so the two
    must come from one setting. A signature valid for one region against another
    region's host is an auth failure that reads like a routing bug."""
    assert email_service._ses_host("eu-west-1") == "email.eu-west-1.amazonaws.com"
    common = dict(
        method="POST",
        path=email_service.SES_PATH,
        payload=b"{}",
        service=email_service.SES_SERVICE,
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        now=FIXED_NOW,
    )
    one = email_service._sigv4_headers(
        host=email_service._ses_host("us-east-2"), region="us-east-2", **common
    )
    two = email_service._sigv4_headers(
        host=email_service._ses_host("eu-west-1"), region="eu-west-1", **common
    )
    assert "us-east-2/ses/aws4_request" in one["Authorization"]
    assert "eu-west-1/ses/aws4_request" in two["Authorization"]


# ---------------------------------------------------------------------------
# the request SES actually receives
# ---------------------------------------------------------------------------


async def test_ses_posts_the_v2_sendemail_shape(
    configure, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(**_ses_settings(email_reply_to="support@josedev.app"))
    calls = _stub(monkeypatch, _ses_response(200, {"MessageId": "ses-msg-123"}))

    message_id = await email_service.send_email(
        to="student@uncg.edu", subject="Verify your .edu", html=HTML, text=TEXT
    )

    assert message_id == "ses-msg-123"
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == (
        f"https://{email_service._ses_host(REGION)}{email_service.SES_PATH}"
    )
    assert call["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=")

    body = json.loads(call["content"])
    assert body["FromEmailAddress"] == "Chirp <hello@josedev.app>"
    assert body["Destination"]["ToAddresses"] == ["student@uncg.edu"]
    simple = body["Content"]["Simple"]
    assert simple["Subject"]["Data"] == "Verify your .edu"
    assert simple["Body"]["Html"]["Data"] == HTML
    assert simple["Body"]["Text"]["Data"] == TEXT
    assert body["ReplyToAddresses"] == ["support@josedev.app"]


async def test_text_and_reply_to_are_omitted_when_unset(
    configure, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SES rejects an empty Text block, and an absent reply-to must be absent rather
    than null — the Resend adapter has the same rule for the same reason."""
    configure(**_ses_settings(email_reply_to=None))
    calls = _stub(monkeypatch, _ses_response(200, {"MessageId": "m"}))

    await email_service.send_email(to="a@b.edu", subject="s", html=HTML, text=None)

    body = json.loads(calls[0]["content"])
    assert "Text" not in body["Content"]["Simple"]["Body"]
    assert "ReplyToAddresses" not in body


async def test_the_signed_bytes_are_the_bytes_sent(
    configure, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-signing the captured body must reproduce the captured Authorization header.

    If the adapter ever serialised twice — once to hash, once to send — dict ordering or
    separator differences would produce a signature over bytes that never went on the
    wire, and SES would reject every send with a signature mismatch.
    """
    configure(**_ses_settings())
    calls = _stub(monkeypatch, _ses_response(200, {"MessageId": "m"}))

    await email_service.send_email(to="a@b.edu", subject="s", html=HTML, text=TEXT)

    call = calls[0]
    resigned = email_service._sigv4_headers(
        method="POST",
        host=email_service._ses_host(REGION),
        path=email_service.SES_PATH,
        payload=call["content"],
        region=REGION,
        service=email_service.SES_SERVICE,
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        now=datetime.strptime(call["headers"]["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        ),
    )
    assert resigned["Authorization"] == call["headers"]["Authorization"]


# ---------------------------------------------------------------------------
# the contract c134 rests on, preserved across the provider swap
# ---------------------------------------------------------------------------


async def test_transport_failure_raises_502(
    configure, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(**_ses_settings())
    _stub(monkeypatch, httpx.ConnectError("boom"))

    with pytest.raises(HTTPException) as exc:
        await email_service.send_email(to="a@b.edu", subject="s", html=HTML)

    assert exc.value.status_code == 502
    assert exc.value.detail == "email_send_failed"


@pytest.mark.parametrize("status", [400, 403, 429, 500])
async def test_a_provider_error_raises_502_so_the_route_cannot_return_202(
    configure, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """c134's acceptance semantics in one assertion: a 202 from the route IS proof the
    provider took the message, and that only holds while every >= 400 raises here."""
    configure(**_ses_settings())
    _stub(monkeypatch, _ses_response(status, {"message": "nope"}))

    with pytest.raises(HTTPException) as exc:
        await email_service.send_email(to="a@b.edu", subject="s", html=HTML)

    assert exc.value.status_code == 502
    assert exc.value.detail == "email_send_failed"


@pytest.mark.parametrize(
    "missing", ["aws_region", "aws_access_key_id", "aws_secret_access_key"]
)
async def test_incomplete_credentials_are_503_not_a_signing_error(
    configure, missing: str
) -> None:
    """Jose's AWS account does not exist yet, so this IS the current state of prod if
    the provider were flipped early. It must read as a deployment state at the boundary,
    the same way an unconfigured Resend key does."""
    configure(**_ses_settings(**{missing: None}))

    with pytest.raises(HTTPException) as exc:
        await email_service.send_email(to="a@b.edu", subject="s", html=HTML)

    assert exc.value.status_code == 503
    assert exc.value.detail == "email_not_configured"


async def test_ses_never_logs_the_recipient_or_the_body(
    configure, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The module rule that predates this provider and must survive it: a one-time code
    or a full student address sitting in Cloud Logging is unnecessary exposure."""
    configure(**_ses_settings())
    _stub(monkeypatch, _ses_response(200, {"MessageId": "m"}))

    with caplog.at_level("INFO"):
        await email_service.send_email(
            to="student@uncg.edu", subject="Verify", html=HTML, text=TEXT
        )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "student@uncg.edu" not in logged
    assert CODE not in logged
    assert "[redacted]" in logged


async def test_the_default_provider_is_still_log_and_ses_is_opt_in(
    configure, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding a provider must not change what a fresh clone or the suite does."""
    settings = configure()
    assert settings.email_provider == "log"
    calls = _stub(monkeypatch, _ses_response(200, {"MessageId": "should-not-be-used"}))

    message_id = await email_service.send_email(to="a@b.edu", subject="s", html=HTML)

    assert message_id.startswith("log:")
    assert calls == [], "the log provider must not talk to SES"
