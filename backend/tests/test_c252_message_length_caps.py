"""c252: the two message-content bodies c245 left unbounded now have ceilings.

c245 capped the ten prose bodies and deliberately stopped at these two, because
neither is prose and a number chosen by analogy to the 2,000-character post cap
would have been wrong in a way that only surfaces when a real long message fails
to send.

BOTH NUMBERS DERIVE FROM ONE DECISION — the largest plaintext message we intend
to carry (MAX_MESSAGE_PLAINTEXT_LENGTH). forwarded_plaintext IS one of those
messages, decrypted for a moderator, so it inherits that ceiling directly.
Ciphertext is that same plaintext expanded twice: UTF-8 bytes, then Signal
framing, then base64. The test that matters here is
test_ciphertext_cap_accepts_a_worst_case_real_message, which builds a payload the
size a genuine maximum-length message actually produces and proves the derived
cap accommodates it. If the arithmetic in core/validation.py is ever wrong, that
test fails rather than a student's message failing in production.

Also pinned here: an oversized row written BEFORE these caps existed must still
read back. Capping the *Out shapes would 500 an entire conversation's history on
a single legacy row, which is the failure mode validate_public_url's docstring
already warns about — this file proves we did not repeat it.

libsignal is a typed stub today (app-mobile/src/crypto/signal.ts throws until the
milestone-3 spike), so the ciphertext sizes here are derived from the intended
protocol rather than measured from live output.
"""
from __future__ import annotations

import base64
import math
import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text

from app.core.validation import (
    MAX_CIPHERTEXT_B64_LENGTH,
    MAX_FORWARDED_PLAINTEXT_LENGTH,
    MAX_MESSAGE_PLAINTEXT_LENGTH,
)
from tests.conftest import MakeCampus, MakeUser, RegisterDevice, b64, share_verified_campus

# A 4-byte UTF-8 character. The worst case the byte arithmetic budgets for: a
# message that is entirely emoji costs four times what an ASCII one does, and that
# is the input the cap has to survive, not an average one.
FOUR_BYTE_CHAR = "\N{GRINNING FACE}"

# Signal framing budget from MAX_CIPHERTEXT_B64_LENGTH's derivation: a
# PreKeySignalMessage's headers plus AES-CBC padding to a 16-byte boundary.
FRAMING_OVERHEAD_BYTES = 256


def _worst_case_ciphertext_bytes() -> bytes:
    """The largest blob a legitimate maximum-length message can actually produce."""
    plaintext = FOUR_BYTE_CHAR * MAX_MESSAGE_PLAINTEXT_LENGTH
    assert len(plaintext) == MAX_MESSAGE_PLAINTEXT_LENGTH
    assert len(plaintext.encode("utf-8")) == MAX_MESSAGE_PLAINTEXT_LENGTH * 4
    return plaintext.encode("utf-8") + b"\x00" * FRAMING_OVERHEAD_BYTES


async def _dm_with_device(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> tuple[Any, str, str]:
    """A DM between two reachable campus peers, plus the sender's device id."""
    sender = await make_user("Sender")
    recipient = await make_user("Recipient")
    await share_verified_campus(sender.id, recipient.id)
    device = await register_device(sender, one_time_prekey_count=1)

    conversation = await client.post(
        "/conversations",
        json={"kind": "dm", "member_user_ids": [recipient.id]},
        headers=sender.headers,
    )
    assert conversation.status_code == 201, conversation.text
    return sender, conversation.json()["id"], device["id"]


# ---------------------------------------------------------------------------
# ciphertext
# ---------------------------------------------------------------------------


def test_the_derivation_leaves_real_headroom() -> None:
    """The arithmetic in core/validation.py, checked rather than trusted.

    No database, no route — this is the claim the comment makes, executed. If
    someone lowers MAX_CIPHERTEXT_B64_LENGTH or raises the plaintext ceiling
    without redoing the expansion, this fails immediately and points at why.
    """
    worst_case_bytes = MAX_MESSAGE_PLAINTEXT_LENGTH * 4 + FRAMING_OVERHEAD_BYTES
    b64_chars = math.ceil(worst_case_bytes / 3) * 4

    assert worst_case_bytes == 40_256
    assert b64_chars == 53_676
    assert b64_chars < MAX_CIPHERTEXT_B64_LENGTH, (
        "the cap no longer fits a worst-case maximum-length message: "
        f"needs {b64_chars}, cap is {MAX_CIPHERTEXT_B64_LENGTH}"
    )
    # Enough slack for a protocol revision that frames slightly larger, without
    # being so loose that the cap stops meaning anything.
    headroom = (MAX_CIPHERTEXT_B64_LENGTH - b64_chars) / b64_chars
    assert 0.15 < headroom < 0.5, f"headroom drifted to {headroom:.1%}"


async def test_ciphertext_cap_accepts_a_worst_case_real_message(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """THE TEST THAT MATTERS: a genuine maximum-length message still sends.

    Not `"a" * n` — a full-length all-emoji plaintext, expanded through UTF-8 and
    Signal framing and base64 exactly the way a real one would be. A cap a student
    can hit is a bug report, and this is the input that would produce it.
    """
    sender, conversation_id, device_id = await _dm_with_device(
        client, make_user, register_device
    )
    payload = base64.b64encode(_worst_case_ciphertext_bytes()).decode("ascii")
    assert len(payload) <= MAX_CIPHERTEXT_B64_LENGTH

    response = await client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "sender_device_id": device_id,
            "ciphertext_b64": payload,
            "message_type": "signal",
        },
        headers=sender.headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["ciphertext_b64"] == payload


async def test_ciphertext_cap_rejects_an_oversized_but_VALID_payload(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """Over the cap and still well-formed base64 — the cap must be what refuses it.

    The obvious version of this test, `"A" * (cap + 1)`, is a trap: that length is
    not a multiple of four, so `_b64_to_bytes` rejects it with its own 422 and the
    test passes identically whether or not a cap exists. It was written that way
    first and only the falsification run caught it. So the payload here is real
    b64encode output, four characters over the ceiling (the nearest valid length
    above it), and the assertion checks the 422 did NOT come from the decoder.
    """
    sender, conversation_id, device_id = await _dm_with_device(
        client, make_user, register_device
    )
    # ceil(49155 / 3) * 4 == 65,540 characters: valid base64, 4 over the cap.
    oversized = base64.b64encode(b"\x00" * 49_155).decode("ascii")
    assert len(oversized) == MAX_CIPHERTEXT_B64_LENGTH + 4
    assert len(oversized) % 4 == 0

    response = await client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "sender_device_id": device_id,
            "ciphertext_b64": oversized,
            "message_type": "signal",
        },
        headers=sender.headers,
    )
    assert response.status_code == 422, response.text
    assert response.json().get("detail") != "invalid_base64", (
        "refused by the base64 decoder rather than by the length cap — "
        "this test would pass with no cap at all"
    )


async def test_history_still_reads_back_a_row_larger_than_the_cap(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """A message stored before this cap existed must still be readable.

    MessageOut is deliberately uncapped. If the ceiling were put on the response
    shape too, one oversized legacy row would 500 the entire conversation history
    for both participants — and prod has been accepting unbounded ciphertext since
    messaging shipped, so such rows are a live possibility, not a hypothetical.
    """
    sender, conversation_id, device_id = await _dm_with_device(
        client, make_user, register_device
    )
    oversized = b"\x01" * (MAX_CIPHERTEXT_B64_LENGTH * 2)

    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text(
                "INSERT INTO messages (id, conversation_id, sender_device_id, "
                "ciphertext, message_type) VALUES (:id, :conv, :dev, :ct, 'signal')"
            ),
            {
                "id": str(uuid.uuid4()),
                "conv": conversation_id,
                "dev": device_id,
                "ct": oversized,
            },
        )
        await session.commit()

    history = await client.get(
        f"/conversations/{conversation_id}/messages", headers=sender.headers
    )
    assert history.status_code == 200, history.text
    assert base64.b64decode(history.json()[0]["ciphertext_b64"]) == oversized


# ---------------------------------------------------------------------------
# forwarded plaintext
# ---------------------------------------------------------------------------


async def test_forwarded_plaintext_accepts_a_full_length_message(
    client: AsyncClient, make_campus: MakeCampus, make_user: MakeUser
) -> None:
    """A moderator must be able to receive the WHOLE reported message.

    The ceiling is the message plaintext ceiling for exactly this reason: a report
    that silently could not carry the last paragraph of what was said is evidence
    with the worst part potentially missing.
    """
    reporter = await make_user("Reporter")
    response = await client.post(
        "/moderation/reports",
        json={
            "target_type": "message_forward",
            "forwarded_plaintext": "x" * MAX_FORWARDED_PLAINTEXT_LENGTH,
            "reason": "forwarding the whole thread for review",
        },
        headers=reporter.headers,
    )
    assert response.status_code == 201, response.text


async def test_forwarded_plaintext_rejects_one_character_over(
    client: AsyncClient, make_user: MakeUser
) -> None:
    reporter = await make_user("Reporter")
    response = await client.post(
        "/moderation/reports",
        json={
            "target_type": "message_forward",
            "forwarded_plaintext": "x" * (MAX_FORWARDED_PLAINTEXT_LENGTH + 1),
            "reason": "too long to forward",
        },
        headers=reporter.headers,
    )
    assert response.status_code == 422, response.text


def test_forwarded_plaintext_tracks_the_message_ceiling() -> None:
    """These two must not drift apart: a message becomes unreportable the moment
    the forward ceiling sits below the ceiling on messages themselves."""
    assert MAX_FORWARDED_PLAINTEXT_LENGTH == MAX_MESSAGE_PLAINTEXT_LENGTH
