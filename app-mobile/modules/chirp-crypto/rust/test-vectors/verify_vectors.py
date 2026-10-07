#!/usr/bin/env python3
"""Independent check of e2ee-v1.json: re-derive every encoding, signature and safety
number without any Rust code. This is what the backend's own implementation must agree
with.

Encodings and safety numbers use only the standard library. Ed25519 verification (and
re-signing from the seeds, which is deterministic per RFC 8032) needs the `cryptography`
package, which the backend already depends on; without it those checks are skipped and
the script says so. Set REQUIRE_CRYPTOGRAPHY=1 (CI does) to make a missing package a
failure instead of a skip.

    backend/.venv/bin/python app-mobile/modules/chirp-crypto/rust/test-vectors/verify_vectors.py
"""
import hashlib
import json
import os
import pathlib
import struct
import sys

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
except ImportError:  # pragma: no cover
    if os.environ.get("REQUIRE_CRYPTOGRAPHY"):
        sys.exit("FAIL  REQUIRE_CRYPTOGRAPHY is set but the `cryptography` package is missing")
    Ed25519PrivateKey = Ed25519PublicKey = InvalidSignature = None

HERE = pathlib.Path(__file__).resolve().parent
V = json.loads((HERE / "e2ee-v1.json").read_text())
SUITE = V["suite"].encode()
failures = []


def check(label, ok):
    print(("ok   " if ok else "FAIL ") + label)
    if not ok:
        failures.append(label)


def frame(domain, fields):
    d = domain.encode()
    out = struct.pack(">H", len(d)) + d
    for f in fields:
        out += struct.pack(">I", len(f)) + f
    return out


def h(x):
    return bytes.fromhex(x)


def verify(public_hex, message, signature_hex):
    if Ed25519PublicKey is None:
        return None
    try:
        Ed25519PublicKey.from_public_bytes(h(public_hex)).verify(h(signature_hex), message)
        return True
    except InvalidSignature:
        return False


def resign(seed_hex, message):
    if Ed25519PrivateKey is None:
        return None
    return Ed25519PrivateKey.from_private_bytes(h(seed_hex)).sign(message)


def check_signed(label, entry, expected_encoding, signer_seed_hex):
    encoded = h(entry["encoded"])
    check(f"{label}: encoding", encoded == expected_encoding)
    result = verify(entry["signer_ed25519"], encoded, entry["signature"])
    if result is None:
        print(f"skip {label}: signature (no `cryptography` package)")
        return
    check(f"{label}: signature verifies", result)
    check(f"{label}: signature is the deterministic one", resign(signer_seed_hex, encoded) == h(entry["signature"]))
    # Any change to the message must break it.
    for i in (0, len(encoded) // 2, len(encoded) - 1):
        bad = bytearray(encoded)
        bad[i] ^= 1
        check(f"{label}: byte {i} is covered", verify(entry["signer_ed25519"], bytes(bad), entry["signature"]) is False)


acc = V["accounts"]

# Device binding.
b = V["device_binding"]
check_signed(
    "device_binding",
    b,
    frame(
        b["domain"],
        [SUITE, h(b["user_id"]), struct.pack(">I", b["generation_u32"]),
         h(b["identity_curve25519"]), h(b["identity_ed25519"])],
    ),
    acc["device"]["ed25519_seed"],
)

# One-time and fallback keys.
for k in V["signed_keys"]:
    check_signed(
        f"signed_key[{k['kind']}]",
        k,
        frame(
            "chirp-e2ee-v1/otk",
            [k["kind"].encode(), struct.pack(">Q", int(k["key_id_u64"])),
             h(k["public_key"]), h(k["owner_identity_curve25519"])],
        ),
        acc["device"]["ed25519_seed"],
    )

# Device approval (fields of the NEW device, signed by the approver).
a = V["device_approval"]
check_signed(
    "device_approval",
    a,
    frame(
        a["domain"],
        [h(a["user_id"]), struct.pack(">I", a["generation_u32"]),
         h(a["identity_curve25519"]), h(a["identity_ed25519"])],
    ),
    acc["approver"]["ed25519_seed"],
)


# Safety numbers.
def half(user_hex, identity_hexes):
    d = b"\x00" + b"".join(sorted(h(x) for x in identity_hexes)) + h(user_hex)
    for _ in range(5200):
        d = hashlib.sha512(d).digest()
    return "".join("%05d" % (int.from_bytes(d[i:i + 5], "big") % 100000) for i in range(0, 30, 5))


for n, case in enumerate(V["safety_number"]["cases"]):
    mine = (case["my_user_id"], half(case["my_user_id"], case["my_identities"]))
    peer = (case["peer_user_id"], half(case["peer_user_id"], case["peer_identities"]))
    ordered = sorted([mine, peer], key=lambda t: h(t[0]))
    check(f"safety_number[{n}]", "".join(t[1] for t in ordered) == case["digits"])

print()
if failures:
    print(f"{len(failures)} check(s) FAILED")
    sys.exit(1)
print("all checks passed")
