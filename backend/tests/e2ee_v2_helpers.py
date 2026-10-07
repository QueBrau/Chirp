"""Client-side stand-in for the Rust core: builds REAL signed v2 requests (board card c444).

The server verifies Ed25519 signatures, so tests cannot send random bytes. `Keyring` plays
the device: it holds a real Ed25519 identity, signs with the encodings from
app.core.e2ee_v2 (the same module the server verifies with), and produces the request
bodies the routers expect. Tests that need to prove a field is bound by a signature mutate
the BODY after signing; tests that need a valid-but-wrong signature sign different bytes.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import AsyncClient

from app.core import e2ee_v2
from tests.conftest import ApiUser, b64, share_verified_campus


def raw_public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


@dataclass
class Keyring:
    """One device's client-side key material. `id` is set once the server assigns it."""

    user: ApiUser
    generation: int
    signing_key: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)
    curve: bytes = field(default_factory=lambda: os.urandom(32))
    id: str | None = None
    next_key_id: int = 1

    @property
    def ed(self) -> bytes:
        return raw_public(self.signing_key)

    def sign_binding(self, *, user_id: str | None = None, **overrides: Any) -> bytes:
        fields: dict[str, Any] = {
            "suite": e2ee_v2.SUITE_VODOZEMAC_OLM_V1,
            "user_id": uuid.UUID(user_id or self.user.id),
            "generation": self.generation,
            "identity_curve25519": self.curve,
            "identity_ed25519": self.ed,
        }
        fields.update(overrides)
        return self.signing_key.sign(e2ee_v2.device_binding_message(**fields))

    def signed_key(self, kind: str = "one_time", *, key_id: int | None = None) -> dict[str, Any]:
        """A fresh signed key body. key_id auto-increments so ids are never reused."""
        if key_id is None:
            key_id = self.next_key_id
            self.next_key_id += 1
        public_key = os.urandom(32)
        message = e2ee_v2.one_time_key_message(
            kind=kind, key_id=key_id, public_key=public_key, identity_curve25519=self.curve
        )
        return {
            "key_id": key_id,
            "public_key_b64": b64(public_key),
            "signature_b64": b64(self.signing_key.sign(message)),
        }

    def registration_body(self, *, one_time_keys: int = 3, label: str | None = "pytest") -> dict[str, Any]:
        return {
            "device_label": label,
            "suite": e2ee_v2.SUITE_VODOZEMAC_OLM_V1,
            "generation": self.generation,
            "identity_curve25519_b64": b64(self.curve),
            "identity_ed25519_b64": b64(self.ed),
            "binding_signature_b64": b64(self.sign_binding()),
            "one_time_keys": [self.signed_key("one_time") for _ in range(one_time_keys)],
            "fallback_key": self.signed_key("fallback"),
        }

    def approval_for(self, new: Keyring) -> bytes:
        """This (approver) device's signature over `new`'s identity."""
        return self.signing_key.sign(
            e2ee_v2.device_approval_message(
                user_id=uuid.UUID(new.user.id),
                generation=new.generation,
                identity_curve25519=new.curve,
                identity_ed25519=new.ed,
            )
        )


async def register(
    client: AsyncClient, ring: Keyring, *, one_time_keys: int = 3, expect: int = 201
) -> dict[str, Any]:
    """POST /v2/devices for `ring`; records the assigned id on it."""
    response = await client.post(
        "/v2/devices",
        json=ring.registration_body(one_time_keys=one_time_keys),
        headers=ring.user.headers,
    )
    assert response.status_code == expect, response.text
    if expect == 201:
        ring.id = response.json()["id"]
    return response.json()


async def register_approved_pair(
    client: AsyncClient, user: ApiUser, *, first_generation: int = 1, one_time_keys: int = 3
) -> tuple[Keyring, Keyring]:
    """Register a root device, then a second device, and approve the second from the first."""
    root = Keyring(user, first_generation)
    await register(client, root, one_time_keys=one_time_keys)
    second = Keyring(user, first_generation + 1)
    await register(client, second, one_time_keys=one_time_keys)
    await approve(client, approver=root, new=second)
    return root, second


async def approve(
    client: AsyncClient, *, approver: Keyring, new: Keyring, expect: int = 200
) -> dict[str, Any]:
    response = await client.post(
        f"/v2/devices/{new.id}/approve",
        json={
            "approver_device_id": approver.id,
            "approval_signature_b64": b64(approver.approval_for(new)),
        },
        headers=new.user.headers,
    )
    assert response.status_code == expect, response.text
    return response.json()


async def directory(client: AsyncClient, viewer: ApiUser, target_user_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"/v2/users/{target_user_id}/devices", headers=viewer.headers)
    assert response.status_code == 200, response.text
    return response.json()["devices"]


async def open_dm(client: AsyncClient, creator: ApiUser, other: ApiUser) -> str:
    """Two campus peers and a DM between them; returns the conversation id."""
    await share_verified_campus(creator.id, other.id)
    response = await client.post(
        "/conversations", json={"kind": "dm", "member_user_ids": [other.id]}, headers=creator.headers
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


async def make_mate(
    client: AsyncClient, make_user: Any, other: ApiUser, name: str = "Viewer"
) -> ApiUser:
    """A new user who shares a current DM with `other`.

    That is the precondition (c444) for reading other's v2 directory or claiming other's
    keys: only the user themself or a current conversation mate may. Real clients always
    create the DM first, so tests that are about something else get the same starting point.
    """
    mate = await make_user(name)
    await open_dm(client, other, mate)
    return mate


def leg(ring: Keyring, *, olm_type: int = 0, body: bytes | None = None) -> dict[str, Any]:
    """A leg addressed to `ring`'s device with distinctive ciphertext bytes."""
    assert ring.id is not None
    return {
        "recipient_device_id": ring.id,
        "olm_type": olm_type,
        "ciphertext_b64": b64(body if body is not None else b"leg-for-" + ring.id.encode()),
    }


async def send(
    client: AsyncClient,
    conversation_id: str,
    sender: Keyring,
    legs: list[dict[str, Any]],
    *,
    client_message_id: str | None = None,
    user: ApiUser | None = None,
    expect: int | None = None,
) -> Any:
    """POST /v2/conversations/{id}/messages; returns the raw httpx response."""
    assert sender.id is not None
    response = await client.post(
        f"/v2/conversations/{conversation_id}/messages",
        json={
            "sender_device_id": sender.id,
            "client_message_id": client_message_id or str(uuid.uuid4()),
            "legs": legs,
        },
        headers=(user or sender.user).headers,
    )
    if expect is not None:
        assert response.status_code == expect, response.text
    return response
