"""E2EE v2 signed-byte encodings and Ed25519 verification (board card c444).

THIS MODULE IS THE ONE PLACE THE SIGNED BYTES ARE DEFINED. The Rust core (c443) builds
the same bytes on the device and signs them with the device's Ed25519 identity; the
server rebuilds them from the request fields and verifies. A byte of drift between the two
is a registration that is refused for no visible reason, so the layout below is pinned by
tests/test_c444_e2ee_signed_encodings.py with fixed-seed vectors (hex of every encoded
message and every signature) that the Rust side can reproduce.

WIRE LAYOUT, common to every message below (E2EE-DESIGN.md section 4, as amended by the
c444 spec the Rust core implements):

    u16 big-endian  length of the domain string, in bytes
    domain          ASCII bytes
    then, for each field IN THE ORDER LISTED BELOW:
      u32 big-endian  length of the field, in bytes
      field           the raw bytes

Every field is length-prefixed and the domain string is length-prefixed and different for
each message type, so (a) no two different field tuples can encode to the same bytes, and
(b) a signature over one message type can never be replayed as another type. Fixed-width
fields are ALSO checked for their exact width before encoding: the prefix would make a
wrong-width field encode unambiguously, but a 31-byte key reaching a signer or verifier is
a bug upstream and must fail loudly here rather than produce a signature nobody can match.

THE THREE MESSAGES

1. Device binding, domain "chirp-e2ee-v1/device-binding", signed by the device's OWN
   identity_ed25519. Fields: suite (UTF-8), user_id (16 raw UUID bytes), generation
   (4 bytes, u32 big-endian), identity_curve25519 (32), identity_ed25519 (32). The device
   id is not a field: it does not exist before registration, the server assigns it.
2. One-time or fallback key, domain "chirp-e2ee-v1/otk", signed by the OWNING device's
   identity_ed25519. Fields: kind (UTF-8 "one_time" or "fallback"), key_id (8 bytes, u64
   big-endian), public_key (32), identity_curve25519 of the owning device (32). Binding the
   owner's Curve25519 identity means a key signed for one device cannot be re-published
   under another, even by a party that holds the signed bytes.
3. Device approval, domain "chirp-e2ee-v1/device-approval", signed by the APPROVER
   device's identity_ed25519. Fields describe the NEW device: user_id (16), generation
   (4, u32 big-endian), identity_curve25519 (32), identity_ed25519 (32).

WHAT A VALID SIGNATURE DOES AND DOES NOT PROVE. A valid binding proves the registrant holds
the Ed25519 private key it presented and that the Curve25519 identity travels with it - it
does NOT prove the key belongs to a person; trust comes from the first device's
trust-on-first-use pin plus the approval chain (E2EE-DESIGN.md section 8). A Firebase
token alone therefore cannot mint an approved device on an account that already has one:
the approval signature needs an approved device's private key.

Ed25519 comes from the `cryptography` package (hash-pinned in requirements.lock). Its
OpenSSL-backed verifier rejects non-canonical S values (RFC 8032 section 5.1.7), so a
signature cannot be malleated into a second valid encoding of the same statement.
"""
from __future__ import annotations

import struct
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

SUITE_VODOZEMAC_OLM_V1 = "vodozemac-olm-v1"
SUPPORTED_SUITES = frozenset({SUITE_VODOZEMAC_OLM_V1})

DOMAIN_DEVICE_BINDING = "chirp-e2ee-v1/device-binding"
DOMAIN_ONE_TIME_KEY = "chirp-e2ee-v1/otk"
DOMAIN_DEVICE_APPROVAL = "chirp-e2ee-v1/device-approval"

KEY_KIND_ONE_TIME = "one_time"
KEY_KIND_FALLBACK = "fallback"
KEY_KINDS = frozenset({KEY_KIND_ONE_TIME, KEY_KIND_FALLBACK})

CURVE25519_PUBLIC_BYTES = 32
ED25519_PUBLIC_BYTES = 32
ED25519_SIGNATURE_BYTES = 64
USER_ID_BYTES = 16

# Generation is a u32 on the wire (it is signed as 4 bytes) and a BIGINT in storage:
# a u32 does not fit PostgreSQL's signed INTEGER, so the column cannot be INT.
MAX_GENERATION = 2**32 - 1
# Key ids are u64 on the wire (8 bytes in the signed message) but the server stores
# BIGINT, which is signed: a value >= 2**63 would overflow it, so the API refuses it
# up front rather than turning a malformed request into a database error.
MAX_KEY_ID = 2**63 - 1


def _check_width(name: str, value: bytes, width: int) -> None:
    if len(value) != width:
        raise ValueError(f"{name} must be exactly {width} bytes, got {len(value)}")


def encode_signed_message(domain: str, fields: list[bytes] | tuple[bytes, ...]) -> bytes:
    """u16-BE domain length + domain, then u32-BE length + bytes for each field."""
    domain_bytes = domain.encode("ascii")
    parts = [struct.pack(">H", len(domain_bytes)), domain_bytes]
    for field in fields:
        parts.append(struct.pack(">I", len(field)))
        parts.append(field)
    return b"".join(parts)


def _u32(name: str, value: int) -> bytes:
    if not 0 <= value <= MAX_GENERATION:
        raise ValueError(f"{name} must fit in an unsigned 32-bit integer")
    return struct.pack(">I", value)


def _u64(name: str, value: int) -> bytes:
    if not 0 <= value <= 2**64 - 1:
        raise ValueError(f"{name} must fit in an unsigned 64-bit integer")
    return struct.pack(">Q", value)


def device_binding_message(
    *,
    suite: str,
    user_id: uuid.UUID,
    generation: int,
    identity_curve25519: bytes,
    identity_ed25519: bytes,
) -> bytes:
    """Bytes the device signs (with identity_ed25519) to bind its two identities to an account."""
    _check_width("identity_curve25519", identity_curve25519, CURVE25519_PUBLIC_BYTES)
    _check_width("identity_ed25519", identity_ed25519, ED25519_PUBLIC_BYTES)
    return encode_signed_message(
        DOMAIN_DEVICE_BINDING,
        (
            suite.encode("utf-8"),
            user_id.bytes,
            _u32("generation", generation),
            identity_curve25519,
            identity_ed25519,
        ),
    )


def one_time_key_message(
    *,
    kind: str,
    key_id: int,
    public_key: bytes,
    identity_curve25519: bytes,
) -> bytes:
    """Bytes the owning device signs for one one-time or fallback Curve25519 key."""
    if kind not in KEY_KINDS:
        raise ValueError("kind must be 'one_time' or 'fallback'")
    _check_width("public_key", public_key, CURVE25519_PUBLIC_BYTES)
    _check_width("identity_curve25519", identity_curve25519, CURVE25519_PUBLIC_BYTES)
    return encode_signed_message(
        DOMAIN_ONE_TIME_KEY,
        (
            kind.encode("utf-8"),
            _u64("key_id", key_id),
            public_key,
            identity_curve25519,
        ),
    )


def device_approval_message(
    *,
    user_id: uuid.UUID,
    generation: int,
    identity_curve25519: bytes,
    identity_ed25519: bytes,
) -> bytes:
    """Bytes an APPROVER device signs to approve the new device described by the fields."""
    _check_width("identity_curve25519", identity_curve25519, CURVE25519_PUBLIC_BYTES)
    _check_width("identity_ed25519", identity_ed25519, ED25519_PUBLIC_BYTES)
    return encode_signed_message(
        DOMAIN_DEVICE_APPROVAL,
        (
            user_id.bytes,
            _u32("generation", generation),
            identity_curve25519,
            identity_ed25519,
        ),
    )


def verify_ed25519(public_key: bytes, signature: bytes, message: bytes) -> bool:
    """True only for a valid Ed25519 signature; never raises on bad key or signature bytes."""
    if len(public_key) != ED25519_PUBLIC_BYTES or len(signature) != ED25519_SIGNATURE_BYTES:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True
