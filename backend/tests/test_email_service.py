"""app.services.email_service: the one send path (c87), and the code it must not leak.

These are deliberately DB-free — the mailer has no schema and should stay runnable
when Postgres is not. The load-bearing test here is the last one: c86 puts one-time
verification codes through send_email, so a body reaching the log is a code anyone
with log access can redeem. If that test fails, a debugging change leaked a secret.
"""
from __future__ import annotations

import logging

import httpx
import pytest
from fastapi import HTTPException

from app.config import Settings, get_settings
from app.services import email_service

CODE = "483920"
HTML = f"<p>Your Chirp code is {CODE}</p>"


# Every field this suite asserts on. Cleared from BOTH sources before a Settings is
# built, so a test states what the code does rather than what this machine happens to
# be configured for.
_AMBIENT_EMAIL_VARS = (
    "EMAIL_PROVIDER",
    "RESEND_API_KEY",
    "EMAIL_FROM",
    "EMAIL_REPLY_TO",
)


@pytest.fixture
def configure(monkeypatch: pytest.MonkeyPatch):
    """Override settings for one test, isolated from ambient config.

    `_env_file=None` AND the env-var deletions are both required, and neither is
    belt-and-braces. Settings reads backend/.env, so a developer with a real
    EMAIL_PROVIDER=resend in that file made the "the default is log" test assert
    something about their machine instead of about the code — it passed in CI, which
    builds a clean checkout with no .env, and failed for anyone who had one. Reported
    by the c84 session hitting it on a full-suite run before merging.

    The general trap, worth remembering beyond this file: ANY test that constructs
    Settings() directly inherits backend/.env, so the suite silently tests whatever
    configuration the developer happens to have. That is the same shape as the shared
    test database and the destroyed venv — a result that depends on machine state
    rather than on the code, which is the most expensive kind of green.
    """

    def _configure(**overrides: object) -> Settings:
        for name in _AMBIENT_EMAIL_VARS:
            monkeypatch.delenv(name, raising=False)
        settings = Settings(_env_file=None, **overrides)  # type: ignore[arg-type,call-arg]
        monkeypatch.setattr(email_service, "get_settings", lambda: settings)
        return settings

    yield _configure
    get_settings.cache_clear()


class _StubClient:
    """Stands in for httpx.AsyncClient, capturing the one request the adapter makes."""

    def __init__(self, response: httpx.Response | Exception, calls: list[dict]) -> None:
        self._response = response
        self._calls = calls

    async def __aenter__(self) -> "_StubClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def post(self, url: str, *, headers: dict, json: dict) -> httpx.Response:
        self._calls.append({"url": url, "headers": headers, "json": json})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _stub_httpx(monkeypatch: pytest.MonkeyPatch, response: httpx.Response | Exception) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(
        email_service.httpx,
        "AsyncClient",
        lambda **_kwargs: _StubClient(response, calls),
    )
    return calls


def _resend_response(status_code: int, body: dict | str) -> httpx.Response:
    kwargs = {"json": body} if isinstance(body, dict) else {"text": body}
    return httpx.Response(status_code, request=httpx.Request("POST", email_service.RESEND_ENDPOINT), **kwargs)


async def test_log_provider_is_the_default_and_delivers_nothing(
    configure, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh clone sends no mail: default provider is 'log', so no key is needed and
    no test can reach a real inbox."""
    settings = configure()
    assert settings.email_provider == "log"
    calls = _stub_httpx(monkeypatch, _resend_response(200, {"id": "should-not-be-used"}))

    message_id = await email_service.send_email(to="a@b.edu", subject="Verify", html=HTML)

    assert message_id.startswith("log:")
    assert calls == [], "the log provider must not talk to Resend"


async def test_resend_without_a_key_is_503_not_a_crash(configure) -> None:
    """An unconfigured integration is a deployment state, and reads as one at the
    boundary — same shape stripe_service uses."""
    configure(email_provider="resend", resend_api_key=None)

    with pytest.raises(HTTPException) as exc:
        await email_service.send_email(to="a@b.edu", subject="Verify", html=HTML)

    assert exc.value.status_code == 503
    assert exc.value.detail == "email_not_configured"


async def test_resend_posts_the_expected_request(configure, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bearer auth, the configured sender, the recipient as a list, and reply_to only
    when set — the payload shape Resend actually requires."""
    configure(
        email_provider="resend",
        resend_api_key="re_test_key",
        email_from="Chirp <onboarding@resend.dev>",
        email_reply_to="support@example.com",
    )
    calls = _stub_httpx(monkeypatch, _resend_response(200, {"id": "msg_abc123"}))

    message_id = await email_service.send_email(
        to="student@uncg.edu", subject="Verify your .edu", html=HTML, text=f"Code {CODE}"
    )

    assert message_id == "msg_abc123"
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == email_service.RESEND_ENDPOINT
    assert call["headers"]["Authorization"] == "Bearer re_test_key"
    assert call["json"]["from"] == "Chirp <onboarding@resend.dev>"
    assert call["json"]["to"] == ["student@uncg.edu"]
    assert call["json"]["reply_to"] == "support@example.com"


async def test_reply_to_is_omitted_when_unset(configure, monkeypatch: pytest.MonkeyPatch) -> None:
    """Until c74's support mailbox exists there is no reply address, and sending a null
    one is not the same as sending no key."""
    configure(email_provider="resend", resend_api_key="re_test_key", email_reply_to=None)
    calls = _stub_httpx(monkeypatch, _resend_response(200, {"id": "msg_abc123"}))

    await email_service.send_email(to="a@b.edu", subject="Verify", html=HTML)

    assert "reply_to" not in calls[0]["json"]


async def test_provider_rejection_is_502(configure, monkeypatch: pytest.MonkeyPatch) -> None:
    """This is the path the no-domain restriction takes: Resend refuses the send, and
    it must fail loudly rather than look delivered."""
    configure(email_provider="resend", resend_api_key="re_test_key")
    _stub_httpx(
        monkeypatch,
        _resend_response(403, {"message": "You can only send testing emails to your own address"}),
    )

    with pytest.raises(HTTPException) as exc:
        await email_service.send_email(to="student@uncg.edu", subject="Verify", html=HTML)

    assert exc.value.status_code == 502
    assert exc.value.detail == "email_send_failed"


async def test_transport_failure_is_502(configure, monkeypatch: pytest.MonkeyPatch) -> None:
    """No response at all (timeout, DNS, connection refused) is still a failed send."""
    configure(email_provider="resend", resend_api_key="re_test_key")
    _stub_httpx(monkeypatch, httpx.ConnectTimeout("timed out"))

    with pytest.raises(HTTPException) as exc:
        await email_service.send_email(to="a@b.edu", subject="Verify", html=HTML)

    assert exc.value.status_code == 502


@pytest.mark.parametrize("provider", ["log", "resend"])
async def test_the_message_body_never_reaches_the_log(
    provider: str, configure, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """THE POINT OF THIS FILE. c86 sends one-time codes through send_email, so a body in
    the log is a redeemable secret sitting in Cloud Logging. Full recipient addresses
    are also unnecessary PII; subject and provider id are sufficient for correlation.
    Neither body nor recipient may appear on the success path or the failure one.

    If you are here because this test failed, do not relax it — remove whatever started
    logging the body.
    """
    configure(email_provider=provider, resend_api_key="re_test_key")
    _stub_httpx(monkeypatch, _resend_response(200, {"id": "msg_abc123"}))

    with caplog.at_level(logging.DEBUG, logger=email_service.logger.name):
        await email_service.send_email(
            to="student@uncg.edu", subject="Verify your .edu", html=HTML, text=f"Code {CODE}"
        )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert logged, "a send must leave an audit trail even though it omits the body"
    assert CODE not in logged
    assert HTML not in logged
    assert "student@uncg.edu" not in logged
    assert "recipient=[redacted]" in logged


async def test_provider_failure_does_not_log_recipient_or_echoed_body(
    configure, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A provider error body is untrusted and may echo the address we sent."""
    configure(email_provider="resend", resend_api_key="re_test_key")
    _stub_httpx(
        monkeypatch,
        _resend_response(403, {"message": "student@uncg.edu is not allowed"}),
    )

    with caplog.at_level(logging.WARNING, logger=email_service.logger.name):
        with pytest.raises(HTTPException):
            await email_service.send_email(
                to="student@uncg.edu", subject="Verify your .edu", html=HTML
            )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "student@uncg.edu" not in logged
    assert "not allowed" not in logged
    assert "recipient=[redacted]" in logged
    assert "status=403" in logged


async def test_transport_failure_does_not_log_recipient(
    configure, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    configure(email_provider="resend", resend_api_key="re_test_key")
    _stub_httpx(monkeypatch, httpx.ConnectTimeout("student@uncg.edu timed out"))

    with caplog.at_level(logging.WARNING, logger=email_service.logger.name):
        with pytest.raises(HTTPException):
            await email_service.send_email(
                to="student@uncg.edu", subject="Verify your .edu", html=HTML
            )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "student@uncg.edu" not in logged
    assert "recipient=[redacted]" in logged
    assert "ConnectTimeout" in logged
