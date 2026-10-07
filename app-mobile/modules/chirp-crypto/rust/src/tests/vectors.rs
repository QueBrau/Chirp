//! Signed-encoding and safety-number test vectors.
//!
//! vodozemac cannot seed an `Account`, but its pickle format can carry fixed secrets, so
//! the fixed-seed accounts here are REAL vodozemac accounts built from a hand-written
//! pickle. Their Ed25519 signatures are cross-checked against `ed25519-dalek` (a
//! test-only dev-dependency) and the whole table is written to
//! `test-vectors/e2ee-v1.json`, which the backend can load to cross-check its own
//! implementation. `UPDATE_VECTORS=1 cargo test vectors` rewrites the file; a plain run
//! fails if it differs from what the code produces.

use ed25519_dalek::{Signer, SigningKey};
use serde_json::{Value, json};
use vodozemac::olm::Account;
use vodozemac::{Curve25519PublicKey, Curve25519SecretKey};

use crate::encoding::*;
use crate::envelope::{hex, unhex};
use crate::safety::safety_number;
use crate::*;

const COMMITTED: &str = include_str!("../../test-vectors/e2ee-v1.json");

fn seq(start: u8) -> [u8; 32] {
    let mut out = [0u8; 32];
    for (i, b) in out.iter_mut().enumerate() {
        *b = start.wrapping_add(i as u8);
    }
    out
}

/// A real vodozemac account with fixed identity secrets.
fn fixed_account(ed_seed: [u8; 32], curve_secret: [u8; 32]) -> Account {
    let pickle = json!({
        "signing_key": {"Normal": ed_seed.to_vec()},
        "diffie_hellman_key": curve_secret.to_vec(),
        "one_time_keys": {"next_key_id": 0, "public_keys": {}, "private_keys": {}},
        "fallback_keys": {"key_id": 0, "fallback_key": null, "previous_fallback_key": null},
    });
    Account::from_pickle(serde_json::from_value(pickle).unwrap())
}

struct Fixed {
    ed_seed: [u8; 32],
    curve_secret: [u8; 32],
    account: Account,
    ed: [u8; 32],
    curve: [u8; 32],
}

fn fixed(ed_start: u8, curve_start: u8) -> Fixed {
    let ed_seed = seq(ed_start);
    let curve_secret = seq(curve_start);
    let account = fixed_account(ed_seed, curve_secret);
    let dalek = SigningKey::from_bytes(&ed_seed);
    let ed = dalek.verifying_key().to_bytes();
    let curve =
        Curve25519PublicKey::from(&Curve25519SecretKey::from_slice(&curve_secret)).to_bytes();
    // The account really is what we think it is.
    assert_eq!(account.ed25519_key().as_bytes(), &ed);
    assert_eq!(account.curve25519_key().to_bytes(), curve);
    Fixed {
        ed_seed,
        curve_secret,
        account,
        ed,
        curve,
    }
}

impl Fixed {
    /// Sign with dalek AND with the vodozemac account; they must agree byte for byte.
    fn sign(&self, message: &[u8]) -> [u8; 64] {
        let by_dalek = SigningKey::from_bytes(&self.ed_seed)
            .sign(message)
            .to_bytes();
        let by_account = self.account.sign(message).to_bytes();
        assert_eq!(by_dalek, by_account, "dalek and vodozemac disagree");
        by_dalek
    }
}

const USER_A: UserId = [
    0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff,
];
const USER_B: UserId = [
    0xff, 0xee, 0xdd, 0xcc, 0xbb, 0xaa, 0x99, 0x88, 0x77, 0x66, 0x55, 0x44, 0x33, 0x22, 0x11, 0x00,
];
const GENERATION: u32 = 7;
const KEY_ID: u64 = 0x0102_0304_0506_0708;

/// Independent, naive re-implementation of the framing (no shared code with `encoding`).
fn manual_frame(domain: &str, fields: &[Vec<u8>]) -> Vec<u8> {
    let mut out = Vec::new();
    out.push((domain.len() >> 8) as u8);
    out.push(domain.len() as u8);
    out.extend(domain.bytes());
    for f in fields {
        let n = f.len();
        out.extend([(n >> 24) as u8, (n >> 16) as u8, (n >> 8) as u8, n as u8]);
        out.extend(f);
    }
    out
}

fn vectors() -> Value {
    let device = fixed(0x00, 0x20); // the device being registered
    let approver = fixed(0x40, 0x60); // an already-approved device of the same account
    let otk_public =
        Curve25519PublicKey::from(&Curve25519SecretKey::from_slice(&seq(0x80))).to_bytes();

    let binding = encode_device_binding(&USER_A, GENERATION, &device.curve, &device.ed);
    assert_eq!(
        binding,
        manual_frame(
            "chirp-e2ee-v1/device-binding",
            &[
                b"vodozemac-olm-v1".to_vec(),
                USER_A.to_vec(),
                GENERATION.to_be_bytes().to_vec(),
                device.curve.to_vec(),
                device.ed.to_vec(),
            ]
        )
    );
    let binding_sig = device.sign(&binding);
    assert!(
        verify_device_binding(&USER_A, GENERATION, &device.curve, &device.ed, &binding_sig).is_ok()
    );

    let mut keys = Vec::new();
    for kind in [KeyKind::OneTime, KeyKind::Fallback] {
        let encoded = encode_signed_key(kind, KEY_ID, &otk_public, &device.curve);
        assert_eq!(
            encoded,
            manual_frame(
                "chirp-e2ee-v1/otk",
                &[
                    kind.as_str().as_bytes().to_vec(),
                    KEY_ID.to_be_bytes().to_vec(),
                    otk_public.to_vec(),
                    device.curve.to_vec(),
                ]
            )
        );
        let signature = device.sign(&encoded);
        let key = SignedKey {
            key_id: KEY_ID,
            kind,
            public_key: otk_public,
            signature,
        };
        assert!(verify_signed_key(&device.ed, &device.curve, &key).is_ok());
        keys.push(json!({
            "kind": kind.as_str(),
            "key_id_u64": KEY_ID.to_string(),
            "public_key": hex(&otk_public),
            "owner_identity_curve25519": hex(&device.curve),
            "signer_ed25519": hex(&device.ed),
            "encoded": hex(&encoded),
            "signature": hex(&signature),
        }));
    }

    let approval = encode_device_approval(&USER_A, GENERATION, &device.curve, &device.ed);
    assert_eq!(
        approval,
        manual_frame(
            "chirp-e2ee-v1/device-approval",
            &[
                USER_A.to_vec(),
                GENERATION.to_be_bytes().to_vec(),
                device.curve.to_vec(),
                device.ed.to_vec(),
            ]
        )
    );
    let approval_sig = approver.sign(&approval);
    assert!(
        verify_device_approval(
            &approver.ed,
            &USER_A,
            GENERATION,
            &device.curve,
            &device.ed,
            &approval_sig
        )
        .is_ok()
    );

    // Safety numbers: A has two devices, B has one.
    let third = fixed(0xa0, 0xc0);
    let a_ids = [device.ed, approver.ed];
    let b_ids = [third.ed];
    let number = safety_number(&USER_A, &a_ids, &USER_B, &b_ids).unwrap();
    assert_eq!(
        number,
        safety_number(&USER_B, &b_ids, &USER_A, &a_ids).unwrap()
    );
    assert_eq!(
        number,
        safety_number(&USER_A, &[approver.ed, device.ed], &USER_B, &b_ids).unwrap(),
        "identity order must not matter"
    );
    let single = safety_number(&USER_A, &[device.ed], &USER_B, &b_ids).unwrap();

    json!({
        "version": 1,
        "description": "Test vectors for chirp-crypto-core signed encodings and safety numbers. Ed25519 signatures are deterministic (RFC 8032), so every field below is reproducible from the seeds.",
        "encoding": "u16_be(len(domain)) || domain || for each field in order: u32_be(len(field)) || field",
        "suite": "vodozemac-olm-v1",
        "accounts": {
            "device": {
                "ed25519_seed": hex(&device.ed_seed),
                "curve25519_secret": hex(&device.curve_secret),
                "identity_ed25519": hex(&device.ed),
                "identity_curve25519": hex(&device.curve),
            },
            "approver": {
                "ed25519_seed": hex(&approver.ed_seed),
                "curve25519_secret": hex(&approver.curve_secret),
                "identity_ed25519": hex(&approver.ed),
                "identity_curve25519": hex(&approver.curve),
            },
            "peer": {
                "ed25519_seed": hex(&third.ed_seed),
                "identity_ed25519": hex(&third.ed),
            },
        },
        "device_binding": {
            "domain": DOMAIN_DEVICE_BINDING,
            "user_id": hex(&USER_A),
            "generation_u32": GENERATION,
            "identity_curve25519": hex(&device.curve),
            "identity_ed25519": hex(&device.ed),
            "signer_ed25519": hex(&device.ed),
            "encoded": hex(&binding),
            "signature": hex(&binding_sig),
        },
        "signed_keys": keys,
        "device_approval": {
            "domain": DOMAIN_DEVICE_APPROVAL,
            "note": "fields are those of the NEW device; the signer is the approver",
            "user_id": hex(&USER_A),
            "generation_u32": GENERATION,
            "identity_curve25519": hex(&device.curve),
            "identity_ed25519": hex(&device.ed),
            "signer_ed25519": hex(&approver.ed),
            "encoded": hex(&approval),
            "signature": hex(&approval_sig),
        },
        "safety_number": {
            "algorithm": "per user: d = 0x00 || sorted(identity_ed25519 keys) || user_id; d = SHA-512(d) applied 5200 times; first 30 bytes as six 5-byte big-endian u40 values, each mod 100000, zero padded to 5 digits; halves concatenated with the lower user_id (bytewise) first",
            "cases": [
                {
                    "my_user_id": hex(&USER_A),
                    "my_identities": a_ids.iter().map(|k| hex(k)).collect::<Vec<_>>(),
                    "peer_user_id": hex(&USER_B),
                    "peer_identities": b_ids.iter().map(|k| hex(k)).collect::<Vec<_>>(),
                    "digits": number,
                },
                {
                    "my_user_id": hex(&USER_A),
                    "my_identities": [hex(&device.ed)],
                    "peer_user_id": hex(&USER_B),
                    "peer_identities": b_ids.iter().map(|k| hex(k)).collect::<Vec<_>>(),
                    "digits": single,
                },
            ],
        },
    })
}

#[test]
fn committed_vectors_match_the_code() {
    let generated = serde_json::to_string_pretty(&vectors()).unwrap() + "\n";
    if std::env::var("UPDATE_VECTORS").is_ok() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/test-vectors/e2ee-v1.json");
        std::fs::write(path, &generated).unwrap();
        return;
    }
    assert_eq!(
        generated, COMMITTED,
        "test-vectors/e2ee-v1.json is stale: run UPDATE_VECTORS=1 cargo test committed_vectors"
    );
}

/// Every field of every signed message is covered by the signature.
#[test]
fn changing_any_field_changes_the_bytes_and_fails_verification() {
    let device = fixed(0x00, 0x20);
    let approver = fixed(0x40, 0x60);
    let other = [0x5au8; 32];
    let otk = [0x77u8; 32];

    // Device binding.
    let sig = device.sign(&encode_device_binding(
        &USER_A,
        GENERATION,
        &device.curve,
        &device.ed,
    ));
    let base = encode_device_binding(&USER_A, GENERATION, &device.curve, &device.ed);
    let variants = [
        ("user_id", USER_B, GENERATION, device.curve, device.ed),
        (
            "generation",
            USER_A,
            GENERATION + 1,
            device.curve,
            device.ed,
        ),
        ("curve", USER_A, GENERATION, other, device.ed),
        ("ed25519", USER_A, GENERATION, device.curve, other),
    ];
    for (name, user, generation, curve, ed) in variants {
        assert_ne!(
            encode_device_binding(&user, generation, &curve, &ed),
            base,
            "{name}"
        );
        assert!(
            verify_device_binding(&user, generation, &curve, &ed, &sig).is_err(),
            "binding accepted a changed {name}"
        );
    }
    // A wrong signer, and a damaged signature.
    assert!(verify_device_binding(&USER_A, GENERATION, &device.curve, &approver.ed, &sig).is_err());
    let mut bad = sig;
    bad[63] ^= 1;
    assert!(verify_device_binding(&USER_A, GENERATION, &device.curve, &device.ed, &bad).is_err());

    // One-time / fallback key.
    let key = SignedKey {
        key_id: KEY_ID,
        kind: KeyKind::OneTime,
        public_key: otk,
        signature: device.sign(&encode_signed_key(
            KeyKind::OneTime,
            KEY_ID,
            &otk,
            &device.curve,
        )),
    };
    assert!(verify_signed_key(&device.ed, &device.curve, &key).is_ok());
    let mut changed = key;
    changed.kind = KeyKind::Fallback;
    assert!(verify_signed_key(&device.ed, &device.curve, &changed).is_err());
    let mut changed = key;
    changed.key_id ^= 1;
    assert!(verify_signed_key(&device.ed, &device.curve, &changed).is_err());
    let mut changed = key;
    changed.public_key[0] ^= 1;
    assert!(verify_signed_key(&device.ed, &device.curve, &changed).is_err());
    assert!(
        verify_signed_key(&device.ed, &other, &key).is_err(),
        "owner identity is signed"
    );
    assert!(verify_signed_key(&other, &device.curve, &key).is_err());
    assert_ne!(
        encode_signed_key(KeyKind::OneTime, KEY_ID, &otk, &device.curve),
        encode_signed_key(KeyKind::Fallback, KEY_ID, &otk, &device.curve)
    );
    assert_ne!(
        encode_signed_key(KeyKind::OneTime, KEY_ID, &otk, &device.curve),
        encode_signed_key(KeyKind::OneTime, KEY_ID + 1, &otk, &device.curve)
    );

    // Device approval (signed by the approver, about the new device).
    let base = encode_device_approval(&USER_A, GENERATION, &device.curve, &device.ed);
    let sig = approver.sign(&base);
    let variants = [
        ("user_id", USER_B, GENERATION, device.curve, device.ed),
        (
            "generation",
            USER_A,
            GENERATION + 1,
            device.curve,
            device.ed,
        ),
        ("curve", USER_A, GENERATION, other, device.ed),
        ("ed25519", USER_A, GENERATION, device.curve, other),
    ];
    for (name, user, generation, curve, ed) in variants {
        assert_ne!(
            encode_device_approval(&user, generation, &curve, &ed),
            base,
            "{name}"
        );
        assert!(
            verify_device_approval(&approver.ed, &user, generation, &curve, &ed, &sig).is_err(),
            "approval accepted a changed {name}"
        );
    }
    assert!(
        verify_device_approval(
            &device.ed,
            &USER_A,
            GENERATION,
            &device.curve,
            &device.ed,
            &sig
        )
        .is_err(),
        "the approver key is the signer"
    );
}

/// The three messages can never be confused with one another, even with equal fields.
#[test]
fn domains_separate_the_three_signed_messages() {
    let curve = [1u8; 32];
    let ed = [2u8; 32];
    let a = encode_device_binding(&USER_A, 1, &curve, &ed);
    let c = encode_device_approval(&USER_A, 1, &curve, &ed);
    assert_ne!(a, c);
    assert!(a.starts_with(&[0, 28]));
    assert!(a[2..30].eq(b"chirp-e2ee-v1/device-binding"));
    assert!(c[2..31].eq(b"chirp-e2ee-v1/device-approval"));
    let k = encode_signed_key(KeyKind::OneTime, 1, &curve, &ed);
    assert!(k[2..19].eq(b"chirp-e2ee-v1/otk"));
    // A signature for one message does not verify as another.
    let device = fixed(0x00, 0x20);
    let binding_sig = device.sign(&encode_device_binding(
        &USER_A,
        1,
        &device.curve,
        &device.ed,
    ));
    assert!(
        verify_device_approval(
            &device.ed,
            &USER_A,
            1,
            &device.curve,
            &device.ed,
            &binding_sig
        )
        .is_err()
    );
}

#[test]
fn signature_inputs_are_validated() {
    let device = fixed(0x00, 0x20);
    let sig = device.sign(b"m");
    assert!(verify_signature(&device.ed, b"m", &sig).is_ok());
    assert!(verify_signature(&device.ed, b"n", &sig).is_err());
    // An all-zero signer is not a usable key, and an all-zero signature never verifies.
    assert!(verify_signature(&[0u8; 32], b"m", &sig).is_err());
    assert!(verify_signature(&device.ed, b"m", &[0u8; 64]).is_err());
    let mut high = sig;
    high[63] = 0xff; // non-canonical scalar: strict verification must refuse it
    assert!(verify_signature(&device.ed, b"m", &high).is_err());
}

#[test]
fn safety_numbers_have_the_documented_shape_and_properties() {
    let a = [[1u8; 32], [2u8; 32]];
    let b = [[9u8; 32]];
    let n = safety_number(&USER_A, &a, &USER_B, &b).unwrap();
    assert_eq!(n.len(), 60);
    assert!(n.chars().all(|c| c.is_ascii_digit()));
    // Both parties compute the same number.
    assert_eq!(n, safety_number(&USER_B, &b, &USER_A, &a).unwrap());
    // The lower user id's half comes first.
    let low = safety_number(&USER_A, &a, &USER_B, &b).unwrap();
    let a_only = safety_number(&USER_A, &a, &USER_B, &[[7u8; 32]]).unwrap();
    assert_eq!(&low[..30], &a_only[..30], "user A (lower id) leads");
    assert_ne!(&low[30..], &a_only[30..]);
    // Any identity change, addition, removal or user swap changes it.
    for (label, other) in [
        (
            "changed key",
            safety_number(&USER_A, &[[1u8; 32], [3u8; 32]], &USER_B, &b),
        ),
        (
            "added key",
            safety_number(&USER_A, &[[1u8; 32], [2u8; 32], [3u8; 32]], &USER_B, &b),
        ),
        (
            "removed key",
            safety_number(&USER_A, &[[1u8; 32]], &USER_B, &b),
        ),
        (
            "peer key",
            safety_number(&USER_A, &a, &USER_B, &[[8u8; 32]]),
        ),
        ("different user", safety_number(&USER_A, &a, &[5u8; 16], &b)),
    ] {
        assert_ne!(other.unwrap(), n, "{label}");
    }
    // Input validation.
    assert_eq!(
        safety_number(&USER_A, &[], &USER_B, &b).err(),
        Some(Error::InvalidInput)
    );
    assert_eq!(
        safety_number(&USER_A, &a, &USER_B, &[]).err(),
        Some(Error::InvalidInput)
    );
    assert_eq!(
        safety_number(&USER_A, &[[1u8; 32], [1u8; 32]], &USER_B, &b).err(),
        Some(Error::InvalidInput)
    );
    assert_eq!(
        safety_number(&USER_A, &a, &USER_A, &b).err(),
        Some(Error::InvalidInput)
    );
    // Round-trip of the helper used by the vectors file.
    assert_eq!(unhex::<4>("01020304"), Some([1, 2, 3, 4]));
}

/// The digit extraction is pinned against a value computed independently in Python.
#[test]
fn safety_number_digit_grouping_is_big_endian_mod_100000() {
    let digest: Vec<u8> = (0u8..64).collect();
    // int.from_bytes(chunk, "big") % 100000 per 5-byte chunk:
    // 0x0001020304 = 16_909_060 -> 09060, 0x0506070809 = 21_575_960_585 -> 60585, ...
    assert_eq!(
        crate::safety::digits(&digest),
        "090606058512110636351516066685"
    );
    // Bytes past the first 30 are ignored.
    let mut other = digest.clone();
    other[40] ^= 0xff;
    assert_eq!(
        crate::safety::digits(&other),
        crate::safety::digits(&digest)
    );
}
