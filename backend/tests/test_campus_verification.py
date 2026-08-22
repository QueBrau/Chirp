""".edu verification: the only writer of campus_verified_at (c86).

The gate in test_campus_gate.py is worth nothing if the door beside it opens to the
wrong people, so these tests are mostly about REFUSAL: a domain that belongs to no
campus, a code that has expired, a code guessed too many times, a code replayed after
it worked. The one permissive test that matters is the ordering rule — a proved .edu
must be able to overwrite a campus inherited from an invite code, never the reverse.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.services import campus_verification, email_service, rate_limit
from tests.conftest import MakeCampus, MakeUser, set_campus

DOMAIN = "uncg.edu"


async def _set_domains(campus_id: str, domains: list[str]) -> None:
    """Attach an email-domain allowlist to a campus (no API sets this yet)."""
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE campuses SET email_domains = :domains WHERE id = :id"),
            {"domains": domains, "id": campus_id},
        )
        await session.commit()


async def _pending_row(user_id: str) -> dict:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "SELECT id, code_hash, edu_email, campus_id, attempts, expires_at, consumed_at "
                "FROM campus_verifications WHERE user_id = :u ORDER BY sent_at DESC LIMIT 1"
            ),
            {"u": user_id},
        )
        row = result.mappings().first()
        return dict(row) if row else {}


async def _expire_pending(user_id: str) -> None:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE campus_verifications SET expires_at = now() - interval '1 minute' "
                "WHERE user_id = :u AND consumed_at IS NULL"
            ),
            {"u": user_id},
        )
        await session.commit()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The send limiter is process-global; without this, tests bleed into each other."""
    rate_limit._reset_all()
    yield
    rate_limit._reset_all()


@pytest.fixture
def sent_codes(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture outbound mail so a test can read the code the user would have received."""
    captured: list[dict] = []

    async def _capture(*, to: str, subject: str, html: str, text: str | None = None) -> str:
        captured.append({"to": to, "subject": subject, "html": html, "text": text})
        return "msg_test"

    monkeypatch.setattr(email_service, "send_email", _capture)
    monkeypatch.setattr(campus_verification.email_service, "send_email", _capture)
    return captured


def _code_from(mail: dict) -> str:
    """Pull the six-digit code out of a captured message."""
    import re

    match = re.search(r"\b(\d{6})\b", mail["html"])
    assert match, f"no code in {mail['html']!r}"
    return match.group(1)


@pytest.fixture
async def campus_user(make_user: MakeUser, make_campus: MakeCampus):
    user = await make_user()
    campus_id = await make_campus()
    await _set_domains(campus_id, [DOMAIN])
    return user, campus_id


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


async def test_a_domain_no_campus_claims_is_refused(
    client: AsyncClient, campus_user, sent_codes: list[dict]
) -> None:
    """Fails CLOSED. An address nobody has claimed proves nothing, and no mail goes out."""
    user, _ = campus_user

    response = await client.post(
        "/auth/campus-verification",
        json={"edu_email": "someone@not-a-campus.edu"},
        headers=user.headers,
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "unrecognized_edu_domain"
    assert sent_codes == []


async def test_a_subdomain_does_not_inherit_the_parent_domain(
    client: AsyncClient, campus_user, sent_codes: list[dict]
) -> None:
    """Exact match, deliberately: a uncg.edu rule does not admit students.uncg.edu.
    Widening it must be a visible data change, not an accident of string matching."""
    user, _ = campus_user

    response = await client.post(
        "/auth/campus-verification",
        json={"edu_email": f"someone@students.{DOMAIN}"},
        headers=user.headers,
    )

    assert response.status_code == 400, response.text
    assert sent_codes == []


async def test_sending_stores_a_hash_and_never_the_code(
    client: AsyncClient, campus_user, sent_codes: list[dict]
) -> None:
    """The plaintext code exists only in the delivered message."""
    user, campus_id = campus_user

    response = await client.post(
        "/auth/campus-verification",
        json={"edu_email": f"student@{DOMAIN}"},
        headers=user.headers,
    )

    assert response.status_code == 202, response.text
    assert len(sent_codes) == 1
    code = _code_from(sent_codes[0])

    row = await _pending_row(user.id)
    assert row["code_hash"] != code
    assert code not in row["code_hash"]
    assert row["edu_email"] == f"student@{DOMAIN}"
    assert str(row["campus_id"]) == campus_id


async def test_the_address_is_normalized_before_it_is_matched(
    client: AsyncClient, campus_user, sent_codes: list[dict]
) -> None:
    """Mixed case and stray whitespace are a student typing, not a different domain."""
    user, _ = campus_user

    response = await client.post(
        "/auth/campus-verification",
        json={"edu_email": f"  Student@{DOMAIN.upper()}  "},
        headers=user.headers,
    )

    assert response.status_code == 202, response.text
    row = await _pending_row(user.id)
    assert row["edu_email"] == f"student@{DOMAIN}"


async def test_requesting_codes_in_a_loop_is_rate_limited(
    client: AsyncClient, campus_user, sent_codes: list[dict]
) -> None:
    """A student who mistypes may retry; a script may not mail an inbox repeatedly."""
    user, _ = campus_user
    body = {"edu_email": f"student@{DOMAIN}"}

    for _ in range(campus_verification.SEND_MAX_PER_WINDOW):
        assert (
            await client.post("/auth/campus-verification", json=body, headers=user.headers)
        ).status_code == 202

    response = await client.post("/auth/campus-verification", json=body, headers=user.headers)

    assert response.status_code == 429, response.text
    assert response.json()["detail"] == "verification_rate_limited"


async def test_resending_retires_the_previous_code(
    client: AsyncClient, campus_user, sent_codes: list[dict]
) -> None:
    """Two live codes would double an attacker's guessing budget, so the old one dies."""
    user, _ = campus_user
    body = {"edu_email": f"student@{DOMAIN}"}

    await client.post("/auth/campus-verification", json=body, headers=user.headers)
    first_code = _code_from(sent_codes[0])
    await client.post("/auth/campus-verification", json=body, headers=user.headers)

    response = await client.post(
        "/auth/campus-verification/redeem",
        json={"code": first_code},
        headers=user.headers,
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "verification_code_invalid"


# ---------------------------------------------------------------------------
# Redeeming
# ---------------------------------------------------------------------------


async def test_the_right_code_verifies_and_opens_the_campus(
    client: AsyncClient, campus_user, sent_codes: list[dict]
) -> None:
    """The whole point: after this, the campus feed answers 200."""
    user, campus_id = campus_user
    await client.post(
        "/auth/campus-verification",
        json={"edu_email": f"student@{DOMAIN}"},
        headers=user.headers,
    )

    response = await client.post(
        "/auth/campus-verification/redeem",
        json={"code": _code_from(sent_codes[0])},
        headers=user.headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["verified"] is True
    assert response.json()["campus_id"] == campus_id

    feed = await client.get(f"/campuses/{campus_id}/feed", headers=user.headers)
    assert feed.status_code == 200, feed.text


async def test_verification_overwrites_a_chapter_derived_campus(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus, sent_codes: list[dict]
) -> None:
    """THE ORDERING RULE. c96's invite path only ever FILLS a null so this can win: a
    proved .edu is strictly stronger than a forwarded invite code. If this ever
    reverses, an invite silently outranks real evidence."""
    user = await make_user()
    invite_campus = await make_campus()
    edu_campus = await make_campus()
    await _set_domains(edu_campus, [DOMAIN])
    # The c96 shape: a campus arrived from a chapter, with no verification behind it.
    await set_campus(user.id, invite_campus, verified=False)

    await client.post(
        "/auth/campus-verification",
        json={"edu_email": f"student@{DOMAIN}"},
        headers=user.headers,
    )
    response = await client.post(
        "/auth/campus-verification/redeem",
        json={"code": _code_from(sent_codes[0])},
        headers=user.headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["campus_id"] == edu_campus
    assert response.json()["campus_id"] != invite_campus


async def test_a_wrong_code_is_refused_and_costs_an_attempt(
    client: AsyncClient, campus_user, sent_codes: list[dict]
) -> None:
    """The attempt must PERSIST — if the rollback refunded it the cap would never bind."""
    user, _ = campus_user
    await client.post(
        "/auth/campus-verification",
        json={"edu_email": f"student@{DOMAIN}"},
        headers=user.headers,
    )

    response = await client.post(
        "/auth/campus-verification/redeem", json={"code": "000000"}, headers=user.headers
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "verification_code_invalid"
    assert (await _pending_row(user.id))["attempts"] == 1


async def test_guessing_runs_out_of_attempts(
    client: AsyncClient, campus_user, sent_codes: list[dict]
) -> None:
    """A six-digit code has a million values; the cap is what makes that irrelevant."""
    user, _ = campus_user
    await client.post(
        "/auth/campus-verification",
        json={"edu_email": f"student@{DOMAIN}"},
        headers=user.headers,
    )

    for _ in range(campus_verification.MAX_ATTEMPTS):
        await client.post(
            "/auth/campus-verification/redeem",
            json={"code": "000000"},
            headers=user.headers,
        )

    response = await client.post(
        "/auth/campus-verification/redeem", json={"code": "000000"}, headers=user.headers
    )

    assert response.status_code == 429, response.text
    assert response.json()["detail"] == "verification_attempts_exhausted"


async def test_attempts_never_creeps_past_the_cap(
    client: AsyncClient, campus_user, sent_codes: list[dict]
) -> None:
    """Board card c138: the atomic guard (UPDATE ... WHERE attempts < MAX_ATTEMPTS ...)
    must reject a guess ONCE the cap is reached, not merely refuse to authenticate it —
    a rejected guess must never increment the stored attempts value. Sequential coverage
    of the guard's own WHERE clause; the genuinely concurrent case (25 real OS threads,
    25 independent connections, hammering one row at once) is proven separately with a
    standalone script outside pytest/ASGI-TestClient, documented in the c138 PR - this
    codebase already established (test_moderation_resolve.py, test_spend_approval_decide.py)
    that asyncio.gather over the ASGI transport never interleaves inside the critical
    section, so a gather-based test here would pass regardless of whether the guard exists.
    """
    user, _ = campus_user
    await client.post(
        "/auth/campus-verification",
        json={"edu_email": f"student@{DOMAIN}"},
        headers=user.headers,
    )

    for _ in range(campus_verification.MAX_ATTEMPTS):
        await client.post(
            "/auth/campus-verification/redeem",
            json={"code": "000000"},
            headers=user.headers,
        )
    assert (await _pending_row(user.id))["attempts"] == campus_verification.MAX_ATTEMPTS

    for _ in range(3):
        response = await client.post(
            "/auth/campus-verification/redeem", json={"code": "000000"}, headers=user.headers
        )
        assert response.status_code == 429, response.text
        assert (await _pending_row(user.id))["attempts"] == campus_verification.MAX_ATTEMPTS, (
            "a rejected over-cap guess must not increment attempts further"
        )


async def test_an_expired_code_is_refused_even_when_correct(
    client: AsyncClient, campus_user, sent_codes: list[dict]
) -> None:
    """Expiry is checked before the code, so a stale-but-correct code cannot pass."""
    user, _ = campus_user
    await client.post(
        "/auth/campus-verification",
        json={"edu_email": f"student@{DOMAIN}"},
        headers=user.headers,
    )
    code = _code_from(sent_codes[0])
    await _expire_pending(user.id)

    response = await client.post(
        "/auth/campus-verification/redeem", json={"code": code}, headers=user.headers
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "verification_expired"


async def test_a_code_cannot_be_redeemed_twice(
    client: AsyncClient, campus_user, sent_codes: list[dict]
) -> None:
    """Single use. A replayed code must not re-verify a user whose access was revoked."""
    user, _ = campus_user
    await client.post(
        "/auth/campus-verification",
        json={"edu_email": f"student@{DOMAIN}"},
        headers=user.headers,
    )
    code = _code_from(sent_codes[0])

    first = await client.post(
        "/auth/campus-verification/redeem", json={"code": code}, headers=user.headers
    )
    second = await client.post(
        "/auth/campus-verification/redeem", json={"code": code}, headers=user.headers
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 404, second.text
    assert second.json()["detail"] == "no_pending_verification"


async def test_redeeming_with_nothing_pending_is_404(
    client: AsyncClient, campus_user
) -> None:
    """Distinct from a wrong code, because c90 shows a different screen for each."""
    user, _ = campus_user

    response = await client.post(
        "/auth/campus-verification/redeem", json={"code": "123456"}, headers=user.headers
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "no_pending_verification"


# ---------------------------------------------------------------------------
# Status, and the yearly re-check
# ---------------------------------------------------------------------------


async def test_status_reports_a_lapsed_verification_as_unverified(
    client: AsyncClient, campus_user
) -> None:
    """Jose's yearly re-check. verified goes false while verified_at stays populated, so
    c90 can say 'your verification expired' rather than 'verify your .edu' — a returning
    student should not be told they have never been here."""
    user, campus_id = campus_user
    stale = datetime.now(timezone.utc) - campus_verification.CODE_TTL - timedelta(days=400)
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text(
                "UPDATE users SET campus_id = :c, campus_verified_at = :t WHERE id = :id"
            ),
            {"c": campus_id, "t": stale, "id": user.id},
        )
        await session.commit()

    status = await client.get("/auth/campus-verification", headers=user.headers)
    feed = await client.get(f"/campuses/{campus_id}/feed", headers=user.headers)

    assert status.status_code == 200, status.text
    assert status.json()["verified"] is False
    assert status.json()["verified_at"] is not None
    assert feed.status_code == 403, feed.text
    assert feed.json()["detail"] == "campus_unverified"


async def test_the_code_never_reaches_the_log(
    client: AsyncClient, campus_user, sent_codes: list[dict], caplog: pytest.LogCaptureFixture
) -> None:
    """Same invariant test_email_service.py asserts on the mailer, enforced end to end
    through the real endpoint: a code in Cloud Logging is a redeemable secret.

    Asserted against the ACTUAL code the user received, not a proxy for it — capturing
    the outbound message is the only way to know the real value to search the log for.
    """
    user, _ = campus_user

    with caplog.at_level(logging.DEBUG):
        response = await client.post(
            "/auth/campus-verification",
            json={"edu_email": f"student@{DOMAIN}"},
            headers=user.headers,
        )

    assert response.status_code == 202, response.text
    code = _code_from(sent_codes[0])
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert code not in logged
    assert (await _pending_row(user.id))["code_hash"] not in logged
