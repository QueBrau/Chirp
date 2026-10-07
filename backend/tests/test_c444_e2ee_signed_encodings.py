"""The signed byte encodings of E2EE v2, pinned with fixed-seed vectors (board card c444).

THE RUST CORE (c443) IMPLEMENTS THESE SAME BYTES. Everything the two sides must agree on
is in this file: the layout (written out a SECOND time below with no shared code, so the
vectors cannot pass merely because the encoder was compared against itself), the fixed
inputs, the hex of every encoded message and the hex of every signature. Ed25519 is
deterministic (RFC 8032), so a fixed seed and a fixed message give a fixed signature: the
Rust side reproduces each `*_SIGNATURE` hex exactly, or one of the two sides has drifted.

FIXED INPUTS (all derived from `bytes(range(n))` so they are easy to rebuild anywhere):
    NEW device Ed25519 seed        bytes 0x00..0x1f     (32 bytes)
    APPROVER device Ed25519 seed   bytes 0x20..0x3f
    NEW device Curve25519 identity bytes 0x40..0x5f     (opaque 32 bytes; not a real key)
    one-time / fallback public key bytes 0x80..0x9f
    user_id  00112233-4455-6677-8899-aabbccddeeff
    generation 7, suite "vodozemac-olm-v1"

LAYOUT: u16-BE domain length, domain (ASCII), then per field u32-BE length + bytes.
"""
from __future__ import annotations

import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core import e2ee_v2

SEED_NEW = bytes(range(0x00, 0x20))
SEED_APPROVER = bytes(range(0x20, 0x40))
CURVE_NEW = bytes(range(0x40, 0x60))
PUBLIC_KEY = bytes(range(0x80, 0xA0))
USER_ID = uuid.UUID("00112233-4455-6677-8899-aabbccddeeff")
GENERATION = 7
SUITE = "vodozemac-olm-v1"

NEW_KEY = Ed25519PrivateKey.from_private_bytes(SEED_NEW)
APPROVER_KEY = Ed25519PrivateKey.from_private_bytes(SEED_APPROVER)


def _raw(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


ED_NEW = _raw(NEW_KEY)
ED_APPROVER = _raw(APPROVER_KEY)

# ---- the vectors the Rust core must reproduce byte for byte ----

ED_NEW_PUBLIC_HEX = "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
ED_APPROVER_PUBLIC_HEX = "29acbae141bccaf0b22e1a94d34d0bc7361e526d0bfe12c89794bc9322966dd7"

BINDING_MESSAGE_HEX = (
    "001c63686972702d653265652d76312f6465766963652d62696e64696e67"
    "00000010766f646f7a656d61632d6f6c6d2d7631"
    "0000001000112233445566778899aabbccddeeff"
    "0000000400000007"
    "00000020404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f"
    "0000002003a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
)
BINDING_SIGNATURE_HEX = (
    "a492b6f211cbbb0888aa96b1adcc61e2053c0fb8b9d2bdfeebd7df101a9bccba"
    "a8eaf764ab87dc675d20de7cdcc78a9e8e77e75d844791c3e514a73ba7c8430d"
)

ONE_TIME_KEY_ID = 258
ONE_TIME_MESSAGE_HEX = (
    "001163686972702d653265652d76312f6f746b"
    "000000086f6e655f74696d65"
    "000000080000000000000102"
    "00000020808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f"
    "00000020404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f"
)
ONE_TIME_SIGNATURE_HEX = (
    "e47aad93fb3faa45d0cf653e74d8ff54daa38d5adeb6df51d26a3c95e7bc4545"
    "07570fd9d78512e481b38dcf5797f7db71eaf5efdfafbf038be23f3b67bf8d0e"
)

FALLBACK_KEY_ID = 2**63 - 1  # the largest id the API accepts
FALLBACK_MESSAGE_HEX = (
    "001163686972702d653265652d76312f6f746b"
    "0000000866616c6c6261636b"
    "000000087fffffffffffffff"
    "00000020808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f"
    "00000020404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f"
)
FALLBACK_SIGNATURE_HEX = (
    "7eb9c5e2ad5fd4a28f09f2b651c58ad2af6ad024d07c29eb6a1cc3c77101535f"
    "71a3cc3a3da64b24903119896b35c9b0e0ee0cec420ea0586e20861be0af7806"
)

APPROVAL_MESSAGE_HEX = (
    "001d63686972702d653265652d76312f6465766963652d617070726f76616c"
    "0000001000112233445566778899aabbccddeeff"
    "0000000400000007"
    "00000020404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f"
    "0000002003a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
)
APPROVAL_SIGNATURE_HEX = (
    "533b7f5c9d13da0062e1a98e64fcb9d7f7ab6eeeb5664e0df944ac9277f4712f"
    "977270191a5e2807db8e3f6c58fd8a76aee6dad72a165b30ed2318a25e470504"
)


def _spec_encode(domain: str, fields: list[bytes]) -> bytes:
    """The layout written out again, deliberately sharing no code with the module under test."""
    out = len(domain.encode("ascii")).to_bytes(2, "big") + domain.encode("ascii")
    for field in fields:
        out += len(field).to_bytes(4, "big") + field
    return out


def test_fixed_seeds_give_the_documented_public_keys() -> None:
    assert ED_NEW.hex() == ED_NEW_PUBLIC_HEX
    assert ED_APPROVER.hex() == ED_APPROVER_PUBLIC_HEX


def test_device_binding_vector() -> None:
    message = e2ee_v2.device_binding_message(
        suite=SUITE, user_id=USER_ID, generation=GENERATION,
        identity_curve25519=CURVE_NEW, identity_ed25519=ED_NEW,
    )
    assert message == _spec_encode(
        "chirp-e2ee-v1/device-binding",
        [SUITE.encode(), USER_ID.bytes, GENERATION.to_bytes(4, "big"), CURVE_NEW, ED_NEW],
    )
    assert message.hex() == BINDING_MESSAGE_HEX
    assert NEW_KEY.sign(message).hex() == BINDING_SIGNATURE_HEX
    assert e2ee_v2.verify_ed25519(ED_NEW, bytes.fromhex(BINDING_SIGNATURE_HEX), message)


def test_one_time_key_vector() -> None:
    message = e2ee_v2.one_time_key_message(
        kind="one_time", key_id=ONE_TIME_KEY_ID, public_key=PUBLIC_KEY,
        identity_curve25519=CURVE_NEW,
    )
    assert message == _spec_encode(
        "chirp-e2ee-v1/otk",
        [b"one_time", ONE_TIME_KEY_ID.to_bytes(8, "big"), PUBLIC_KEY, CURVE_NEW],
    )
    assert message.hex() == ONE_TIME_MESSAGE_HEX
    assert NEW_KEY.sign(message).hex() == ONE_TIME_SIGNATURE_HEX
    assert e2ee_v2.verify_ed25519(ED_NEW, bytes.fromhex(ONE_TIME_SIGNATURE_HEX), message)


def test_fallback_key_vector_uses_the_largest_accepted_key_id() -> None:
    message = e2ee_v2.one_time_key_message(
        kind="fallback", key_id=FALLBACK_KEY_ID, public_key=PUBLIC_KEY,
        identity_curve25519=CURVE_NEW,
    )
    assert message == _spec_encode(
        "chirp-e2ee-v1/otk",
        [b"fallback", FALLBACK_KEY_ID.to_bytes(8, "big"), PUBLIC_KEY, CURVE_NEW],
    )
    assert message.hex() == FALLBACK_MESSAGE_HEX
    assert NEW_KEY.sign(message).hex() == FALLBACK_SIGNATURE_HEX
    assert e2ee_v2.verify_ed25519(ED_NEW, bytes.fromhex(FALLBACK_SIGNATURE_HEX), message)


def test_device_approval_vector_is_signed_by_the_approver() -> None:
    message = e2ee_v2.device_approval_message(
        user_id=USER_ID, generation=GENERATION,
        identity_curve25519=CURVE_NEW, identity_ed25519=ED_NEW,
    )
    assert message == _spec_encode(
        "chirp-e2ee-v1/device-approval",
        [USER_ID.bytes, GENERATION.to_bytes(4, "big"), CURVE_NEW, ED_NEW],
    )
    assert message.hex() == APPROVAL_MESSAGE_HEX
    assert APPROVER_KEY.sign(message).hex() == APPROVAL_SIGNATURE_HEX
    # Verifies under the APPROVER's key, and NOT under the new device's own key.
    assert e2ee_v2.verify_ed25519(ED_APPROVER, bytes.fromhex(APPROVAL_SIGNATURE_HEX), message)
    assert not e2ee_v2.verify_ed25519(ED_NEW, bytes.fromhex(APPROVAL_SIGNATURE_HEX), message)


def test_domains_separate_the_three_message_types() -> None:
    """The same field bytes under another domain are a different message, so a signature
    for one type can never be replayed as another."""
    approval = e2ee_v2.device_approval_message(
        user_id=USER_ID, generation=GENERATION,
        identity_curve25519=CURVE_NEW, identity_ed25519=ED_NEW,
    )
    same_fields_other_domain = e2ee_v2.encode_signed_message(
        e2ee_v2.DOMAIN_DEVICE_BINDING,
        [USER_ID.bytes, GENERATION.to_bytes(4, "big"), CURVE_NEW, ED_NEW],
    )
    assert approval != same_fields_other_domain
    signature = APPROVER_KEY.sign(approval)
    assert not e2ee_v2.verify_ed25519(ED_APPROVER, signature, same_fields_other_domain)


def test_every_field_change_changes_the_bytes() -> None:
    base = dict(
        suite=SUITE, user_id=USER_ID, generation=GENERATION,
        identity_curve25519=CURVE_NEW, identity_ed25519=ED_NEW,
    )
    reference = e2ee_v2.device_binding_message(**base)
    mutations = {
        "suite": "vodozemac-olm-v2",
        "user_id": uuid.UUID(int=USER_ID.int ^ 1),
        "generation": GENERATION + 1,
        "identity_curve25519": CURVE_NEW[:-1] + b"\x00",
        "identity_ed25519": ED_NEW[:-1] + b"\x00",
    }
    for name, value in mutations.items():
        assert e2ee_v2.device_binding_message(**{**base, name: value}) != reference, name

    otk = dict(kind="one_time", key_id=5, public_key=PUBLIC_KEY, identity_curve25519=CURVE_NEW)
    otk_reference = e2ee_v2.one_time_key_message(**otk)
    otk_mutations = {
        "kind": "fallback",
        "key_id": 6,
        "public_key": PUBLIC_KEY[:-1] + b"\x00",
        "identity_curve25519": CURVE_NEW[:-1] + b"\x00",
    }
    for name, value in otk_mutations.items():
        assert e2ee_v2.one_time_key_message(**{**otk, name: value}) != otk_reference, name


def test_length_prefixes_make_field_boundaries_unambiguous() -> None:
    """Moving a byte from one field to the next must change the encoding (no concatenation
    ambiguity), which is what the u32 length prefix on every field buys."""
    a = e2ee_v2.encode_signed_message("d", [b"ab", b"c"])
    b = e2ee_v2.encode_signed_message("d", [b"a", b"bc"])
    assert a != b


@pytest.mark.parametrize(
    "call",
    [
        lambda: e2ee_v2.device_binding_message(
            suite=SUITE, user_id=USER_ID, generation=1,
            identity_curve25519=CURVE_NEW[:-1], identity_ed25519=ED_NEW),
        lambda: e2ee_v2.device_binding_message(
            suite=SUITE, user_id=USER_ID, generation=1,
            identity_curve25519=CURVE_NEW, identity_ed25519=ED_NEW + b"\x00"),
        lambda: e2ee_v2.device_binding_message(
            suite=SUITE, user_id=USER_ID, generation=2**32,
            identity_curve25519=CURVE_NEW, identity_ed25519=ED_NEW),
        lambda: e2ee_v2.device_binding_message(
            suite=SUITE, user_id=USER_ID, generation=-1,
            identity_curve25519=CURVE_NEW, identity_ed25519=ED_NEW),
        lambda: e2ee_v2.one_time_key_message(
            kind="other", key_id=1, public_key=PUBLIC_KEY, identity_curve25519=CURVE_NEW),
        lambda: e2ee_v2.one_time_key_message(
            kind="one_time", key_id=2**64, public_key=PUBLIC_KEY, identity_curve25519=CURVE_NEW),
        lambda: e2ee_v2.one_time_key_message(
            kind="one_time", key_id=1, public_key=PUBLIC_KEY[:-1], identity_curve25519=CURVE_NEW),
        lambda: e2ee_v2.device_approval_message(
            user_id=USER_ID, generation=1,
            identity_curve25519=CURVE_NEW[:-1], identity_ed25519=ED_NEW),
    ],
)
def test_wrong_width_or_range_fields_are_refused_not_encoded(call) -> None:
    with pytest.raises(ValueError):
        call()


def test_verify_never_raises_on_garbage() -> None:
    message = b"anything"
    signature = NEW_KEY.sign(message)
    assert e2ee_v2.verify_ed25519(ED_NEW, signature, message)
    assert not e2ee_v2.verify_ed25519(ED_NEW, signature, message + b"!")
    assert not e2ee_v2.verify_ed25519(ED_APPROVER, signature, message)
    assert not e2ee_v2.verify_ed25519(ED_NEW, signature[:-1], message)
    assert not e2ee_v2.verify_ed25519(ED_NEW[:-1], signature, message)
    assert not e2ee_v2.verify_ed25519(b"\x00" * 32, b"\x00" * 64, message)
    flipped = bytes([signature[0] ^ 1]) + signature[1:]
    assert not e2ee_v2.verify_ed25519(ED_NEW, flipped, message)


def test_verify_rejects_a_non_canonical_s_value() -> None:
    """S + L is the same point equation but a different byte string (signature
    malleability). RFC 8032 verifiers must refuse it; pin that the one in use does."""
    group_order = 2**252 + 27742317777372353535851937790883648493
    message = b"malleability"
    signature = NEW_KEY.sign(message)
    s = int.from_bytes(signature[32:], "little")
    malleated = signature[:32] + (s + group_order).to_bytes(32, "little")
    assert not e2ee_v2.verify_ed25519(ED_NEW, malleated, message)
