"""The Rust core's committed signing vectors must verify against THIS server (board card c444).

app-mobile/modules/chirp-crypto/rust/test-vectors/e2ee-v1.json is written by the Rust core
(c443) from fixed Ed25519 seeds. Ed25519 is deterministic (RFC 8032), so each vector is a
byte-exact statement of what the device signs and what it must produce. This file is the
permanent, mechanical half of "the two implementations agree":

  * the server's encoders in app.core.e2ee_v2 rebuild every `encoded` message byte for byte;
  * the server's verifier accepts every Rust `signature` under the documented signer;
  * the server's signer (cryptography + the vector's seed) reproduces the same signature;
  * a single flipped bit in the message, the signature or the signer key is rejected.

The JSON is copied into this repo at the SAME path with identical bytes (git blob
f628930d), so this test runs before the c443 branch merges, and whichever branch merges
second sees no diff in that file.

A MISSING OR UNREADABLE FILE IS A FAILURE, NEVER A SKIP. These tests exist to catch drift
between the two sides; a skip would turn "the vectors vanished" into a green run.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core import e2ee_v2

# backend/tests/<this file> -> parents[2] is the repository root.
VECTORS_PATH = (
    Path(__file__).resolve().parents[2]
    / "app-mobile" / "modules" / "chirp-crypto" / "rust" / "test-vectors" / "e2ee-v1.json"
)


@pytest.fixture(scope="module")
def vectors() -> dict[str, Any]:
    assert VECTORS_PATH.is_file(), (
        f"Rust core test vectors are missing at {VECTORS_PATH}. This must FAIL, not skip: "
        "the file is copied from the c443 branch and pinned here on purpose."
    )
    data = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["suite"] == e2ee_v2.SUITE_VODOZEMAC_OLM_V1
    return data


def _public(seed_hex: str) -> bytes:
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex))
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _sign(seed_hex: str, message: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex)).sign(message)


def _flip_bit(data: bytes, index: int) -> bytes:
    return data[:index] + bytes([data[index] ^ 1]) + data[index + 1:]


def _assert_vector(
    *, encoded: bytes, signature: bytes, signer: bytes, rebuilt: bytes, signer_seed_hex: str
) -> None:
    """Shared assertions: encoders agree, signature verifies, signer reproduces it, tamper fails."""
    assert rebuilt == encoded, "server encoder disagrees with the Rust core byte for byte"
    assert e2ee_v2.verify_ed25519(signer, signature, encoded), "Rust signature rejected"
    assert _public(signer_seed_hex) == signer, "the vector's seed does not produce its signer key"
    assert _sign(signer_seed_hex, encoded) == signature, "Python signing differs from Rust signing"

    # Any single flipped bit in the signed message is rejected ...
    for index in range(len(encoded)):
        assert not e2ee_v2.verify_ed25519(signer, signature, _flip_bit(encoded, index)), (
            f"flipping a bit of message byte {index} was accepted"
        )
    # ... as is any single flipped bit of the signature ...
    for index in range(len(signature)):
        assert not e2ee_v2.verify_ed25519(signer, _flip_bit(signature, index), encoded), (
            f"flipping a bit of signature byte {index} was accepted"
        )
    # ... and the wrong signer.
    assert not e2ee_v2.verify_ed25519(_flip_bit(signer, 0), signature, encoded)
    assert not e2ee_v2.verify_ed25519(signer, signature[:-1], encoded)


def test_the_vector_file_is_present_and_has_the_documented_shape(vectors: dict[str, Any]) -> None:
    assert {"accounts", "device_binding", "device_approval", "signed_keys"} <= vectors.keys()
    assert vectors["accounts"]["device"]["identity_ed25519"] == _public(
        vectors["accounts"]["device"]["ed25519_seed"]
    ).hex()
    assert vectors["accounts"]["approver"]["identity_ed25519"] == _public(
        vectors["accounts"]["approver"]["ed25519_seed"]
    ).hex()
    assert len(vectors["signed_keys"]) == 2
    assert {k["kind"] for k in vectors["signed_keys"]} == {"one_time", "fallback"}


def test_device_binding_vector(vectors: dict[str, Any]) -> None:
    vec = vectors["device_binding"]
    device = vectors["accounts"]["device"]
    assert vec["domain"] == e2ee_v2.DOMAIN_DEVICE_BINDING
    # Signed by the device's OWN Ed25519 identity.
    assert vec["signer_ed25519"] == device["identity_ed25519"]
    assert vec["identity_curve25519"] == device["identity_curve25519"]
    assert vec["identity_ed25519"] == device["identity_ed25519"]

    rebuilt = e2ee_v2.device_binding_message(
        suite=vectors["suite"],
        user_id=uuid.UUID(hex=vec["user_id"]),
        generation=vec["generation_u32"],
        identity_curve25519=bytes.fromhex(vec["identity_curve25519"]),
        identity_ed25519=bytes.fromhex(vec["identity_ed25519"]),
    )
    _assert_vector(
        encoded=bytes.fromhex(vec["encoded"]),
        signature=bytes.fromhex(vec["signature"]),
        signer=bytes.fromhex(device["identity_ed25519"]),
        rebuilt=rebuilt,
        signer_seed_hex=device["ed25519_seed"],
    )


@pytest.mark.parametrize("index", [0, 1], ids=["first-key", "second-key"])
def test_signed_key_vectors(vectors: dict[str, Any], index: int) -> None:
    vec = vectors["signed_keys"][index]
    device = vectors["accounts"]["device"]
    key_id = int(vec["key_id_u64"])
    assert 0 <= key_id <= e2ee_v2.MAX_KEY_ID, "the API refuses key ids >= 2**63"
    # Signed by the OWNING device's Ed25519 identity, bound to its Curve25519 identity.
    assert vec["signer_ed25519"] == device["identity_ed25519"]
    assert vec["owner_identity_curve25519"] == device["identity_curve25519"]

    rebuilt = e2ee_v2.one_time_key_message(
        kind=vec["kind"],
        key_id=key_id,
        public_key=bytes.fromhex(vec["public_key"]),
        identity_curve25519=bytes.fromhex(vec["owner_identity_curve25519"]),
    )
    _assert_vector(
        encoded=bytes.fromhex(vec["encoded"]),
        signature=bytes.fromhex(vec["signature"]),
        signer=bytes.fromhex(device["identity_ed25519"]),
        rebuilt=rebuilt,
        signer_seed_hex=device["ed25519_seed"],
    )


def test_device_approval_vector(vectors: dict[str, Any]) -> None:
    vec = vectors["device_approval"]
    device = vectors["accounts"]["device"]
    approver = vectors["accounts"]["approver"]
    assert vec["domain"] == e2ee_v2.DOMAIN_DEVICE_APPROVAL
    # Signed by the APPROVER; the fields are those of the NEW device.
    assert vec["signer_ed25519"] == approver["identity_ed25519"]
    assert vec["identity_curve25519"] == device["identity_curve25519"]
    assert vec["identity_ed25519"] == device["identity_ed25519"]

    rebuilt = e2ee_v2.device_approval_message(
        user_id=uuid.UUID(hex=vec["user_id"]),
        generation=vec["generation_u32"],
        identity_curve25519=bytes.fromhex(vec["identity_curve25519"]),
        identity_ed25519=bytes.fromhex(vec["identity_ed25519"]),
    )
    _assert_vector(
        encoded=bytes.fromhex(vec["encoded"]),
        signature=bytes.fromhex(vec["signature"]),
        signer=bytes.fromhex(approver["identity_ed25519"]),
        rebuilt=rebuilt,
        signer_seed_hex=approver["ed25519_seed"],
    )
    # The new device's own key must NOT validate its approval.
    assert not e2ee_v2.verify_ed25519(
        bytes.fromhex(device["identity_ed25519"]),
        bytes.fromhex(vec["signature"]),
        bytes.fromhex(vec["encoded"]),
    )


def test_the_servers_registration_path_accepts_the_rust_binding_and_keys_end_to_end(
    vectors: dict[str, Any],
) -> None:
    """The same bytes through the server's real encoders, the way routers/keys_v2.py uses them."""
    binding = vectors["device_binding"]
    device = vectors["accounts"]["device"]
    message = e2ee_v2.device_binding_message(
        suite=vectors["suite"],
        user_id=uuid.UUID(hex=binding["user_id"]),
        generation=binding["generation_u32"],
        identity_curve25519=bytes.fromhex(device["identity_curve25519"]),
        identity_ed25519=bytes.fromhex(device["identity_ed25519"]),
    )
    assert e2ee_v2.verify_ed25519(
        bytes.fromhex(device["identity_ed25519"]), bytes.fromhex(binding["signature"]), message
    )
    # The binding is bound to the user: the same signature does not verify for another user.
    other_user = e2ee_v2.device_binding_message(
        suite=vectors["suite"],
        user_id=uuid.UUID(int=uuid.UUID(hex=binding["user_id"]).int ^ 1),
        generation=binding["generation_u32"],
        identity_curve25519=bytes.fromhex(device["identity_curve25519"]),
        identity_ed25519=bytes.fromhex(device["identity_ed25519"]),
    )
    assert not e2ee_v2.verify_ed25519(
        bytes.fromhex(device["identity_ed25519"]), bytes.fromhex(binding["signature"]), other_user
    )
