//! Signed encodings. The backend implements the same bytes in parallel; a mismatch
//! here breaks device registration, so the format is deliberately dull.
//!
//! ```text
//! message = u16_be(len(domain)) || domain
//!           || for each field, in order:  u32_be(len(field)) || field
//! ```
//!
//! Domains and field orders (all lengths in bytes):
//!
//! | what            | domain                          | fields |
//! |-----------------|---------------------------------|--------|
//! | device binding  | `chirp-e2ee-v1/device-binding`  | suite (UTF-8 `vodozemac-olm-v1`), user_id (16), generation (4, u32 BE), identity_curve25519 (32), identity_ed25519 (32) |
//! | one-time key    | `chirp-e2ee-v1/otk`             | kind (UTF-8 `one_time` or `fallback`), key_id (8, u64 BE), public_key (32), owner identity_curve25519 (32) |
//! | device approval | `chirp-e2ee-v1/device-approval` | user_id (16), generation (4, u32 BE), identity_curve25519 (32), identity_ed25519 (32), all of the NEW device |
//!
//! A binding and a one-time key are signed by the owning device's Ed25519 identity.
//! An approval is signed by the APPROVER device's Ed25519 identity.

use vodozemac::{Ed25519PublicKey, Ed25519Signature};

use crate::error::{Error, Result};
use crate::types::{KeyKind, PublicKey32, Signature64, SignedKey, UserId};

pub const SUITE: &str = "vodozemac-olm-v1";
pub const DOMAIN_DEVICE_BINDING: &str = "chirp-e2ee-v1/device-binding";
pub const DOMAIN_ONE_TIME_KEY: &str = "chirp-e2ee-v1/otk";
pub const DOMAIN_DEVICE_APPROVAL: &str = "chirp-e2ee-v1/device-approval";

/// The generic framing. Domains are short ASCII constants, so the u16 never overflows.
fn frame(domain: &str, fields: &[&[u8]]) -> Vec<u8> {
    debug_assert!(domain.is_ascii() && domain.len() <= u16::MAX as usize);
    let mut out =
        Vec::with_capacity(2 + domain.len() + fields.iter().map(|f| 4 + f.len()).sum::<usize>());
    out.extend_from_slice(&(domain.len() as u16).to_be_bytes());
    out.extend_from_slice(domain.as_bytes());
    for field in fields {
        out.extend_from_slice(&(field.len() as u32).to_be_bytes());
        out.extend_from_slice(field);
    }
    out
}

pub fn encode_device_binding(
    user_id: &UserId,
    generation: u32,
    identity_curve25519: &PublicKey32,
    identity_ed25519: &PublicKey32,
) -> Vec<u8> {
    frame(
        DOMAIN_DEVICE_BINDING,
        &[
            SUITE.as_bytes(),
            user_id,
            &generation.to_be_bytes(),
            identity_curve25519,
            identity_ed25519,
        ],
    )
}

pub fn encode_signed_key(
    kind: KeyKind,
    key_id: u64,
    public_key: &PublicKey32,
    owner_identity_curve25519: &PublicKey32,
) -> Vec<u8> {
    frame(
        DOMAIN_ONE_TIME_KEY,
        &[
            kind.as_str().as_bytes(),
            &key_id.to_be_bytes(),
            public_key,
            owner_identity_curve25519,
        ],
    )
}

/// The fields are those of the NEW device being approved.
pub fn encode_device_approval(
    user_id: &UserId,
    generation: u32,
    identity_curve25519: &PublicKey32,
    identity_ed25519: &PublicKey32,
) -> Vec<u8> {
    frame(
        DOMAIN_DEVICE_APPROVAL,
        &[
            user_id,
            &generation.to_be_bytes(),
            identity_curve25519,
            identity_ed25519,
        ],
    )
}

/// Verify an Ed25519 signature (strict verification, as vodozemac does).
pub fn verify_signature(
    signer_ed25519: &PublicKey32,
    message: &[u8],
    signature: &Signature64,
) -> Result<()> {
    let key = Ed25519PublicKey::from_slice(signer_ed25519).map_err(|_| Error::InvalidKey)?;
    let signature = Ed25519Signature::from_slice(signature).map_err(|_| Error::InvalidSignature)?;
    key.verify(message, &signature)
        .map_err(|_| Error::InvalidSignature)
}

pub fn verify_device_binding(
    user_id: &UserId,
    generation: u32,
    identity_curve25519: &PublicKey32,
    identity_ed25519: &PublicKey32,
    signature: &Signature64,
) -> Result<()> {
    let message = encode_device_binding(user_id, generation, identity_curve25519, identity_ed25519);
    verify_signature(identity_ed25519, &message, signature)
}

/// `owner_*` are the identities of the device that owns (and signed) the key.
pub fn verify_signed_key(
    owner_identity_ed25519: &PublicKey32,
    owner_identity_curve25519: &PublicKey32,
    key: &SignedKey,
) -> Result<()> {
    let message = encode_signed_key(
        key.kind,
        key.key_id,
        &key.public_key,
        owner_identity_curve25519,
    );
    verify_signature(owner_identity_ed25519, &message, &key.signature)
}

/// Verify that `approver_ed25519` approved the new device described by the other fields.
pub fn verify_device_approval(
    approver_ed25519: &PublicKey32,
    user_id: &UserId,
    generation: u32,
    identity_curve25519: &PublicKey32,
    identity_ed25519: &PublicKey32,
    signature: &Signature64,
) -> Result<()> {
    let message =
        encode_device_approval(user_id, generation, identity_curve25519, identity_ed25519);
    verify_signature(approver_ed25519, &message, signature)
}
