//! Safety numbers: a 60-digit code two people compare in person.
//!
//! For each user: `d = 0x00 || sorted(identity_ed25519 keys, concatenated) || user_id`,
//! then `d = SHA-512(d)` applied 5200 times (plain iteration of the hash function:
//! each round hashes only the previous digest). The first 30 digest bytes become 6
//! groups of 5 digits: each 5-byte chunk is a big-endian u40, taken mod 100000 and
//! zero-padded. The two 30-digit halves are concatenated with the lower user id
//! (bytewise) first, so both sides compute the same 60 digits.

use sha2::{Digest, Sha256, Sha512};
use zeroize::Zeroize;

use crate::encoding::{DOMAIN_DEVICE_CODE, frame};
use crate::error::{Error, Result};
use crate::types::{PublicKey32, UserId};

pub const SAFETY_NUMBER_VERSION: u8 = 0;
pub const SAFETY_NUMBER_ITERATIONS: usize = 5200;

fn half(user_id: &UserId, identities: &[PublicKey32]) -> Result<String> {
    if identities.is_empty() {
        return Err(Error::InvalidInput);
    }
    let mut sorted = identities.to_vec();
    sorted.sort_unstable();
    if sorted.windows(2).any(|w| w[0] == w[1]) {
        return Err(Error::InvalidInput);
    }
    let mut digest: Vec<u8> = Vec::with_capacity(1 + 32 * sorted.len() + 16);
    digest.push(SAFETY_NUMBER_VERSION);
    for key in &sorted {
        digest.extend_from_slice(key);
    }
    digest.extend_from_slice(user_id);
    for _ in 0..SAFETY_NUMBER_ITERATIONS {
        let next = Sha512::digest(&digest);
        digest.zeroize();
        digest = next.to_vec();
    }
    let out = digits(&digest);
    digest.zeroize();
    Ok(out)
}

/// First 30 digest bytes as six 5-digit groups (5-byte big-endian u40, mod 100000).
pub(crate) fn digits(digest: &[u8]) -> String {
    let mut out = String::with_capacity(30);
    for chunk in digest[..30].chunks_exact(5) {
        let mut value: u64 = 0;
        for byte in chunk {
            value = (value << 8) | u64::from(*byte);
        }
        out.push_str(&format!("{:05}", value % 100_000));
    }
    out
}

/// 60 decimal digits, identical on both sides. Identities are each user's approved
/// device Ed25519 keys; order within each list does not matter.
pub fn safety_number(
    my_user: &UserId,
    my_identities: &[PublicKey32],
    peer_user: &UserId,
    peer_identities: &[PublicKey32],
) -> Result<String> {
    if my_user == peer_user {
        return Err(Error::InvalidInput);
    }
    let mine = half(my_user, my_identities)?;
    let theirs = half(peer_user, peer_identities)?;
    Ok(if my_user < peer_user {
        mine + &theirs
    } else {
        theirs + &mine
    })
}

/// Digits in a device comparison code.
pub const DEVICE_CODE_DIGITS: usize = 9;

/// A short code for ONE device, shown on both phones while a new phone is approved so the
/// person can compare them side by side (design section 8, "New device approval"). It is
/// derived from the new device's Ed25519 identity alone, so the new phone (which knows its
/// own key) and the approving phone (which reads the key from the server's pending list)
/// get the same code without sharing anything else.
///
/// `d = SHA-256(frame("chirp-e2ee-v1/device-code", [identity_ed25519]))`, then the first 8
/// digest bytes as a big-endian u64, mod 10^9, zero-padded to 9 digits (about 29.9 bits).
/// The UI groups it as `123 456 789`.
///
/// What it is for: telling two phones apart and catching an honest mix-up (an unknown pending
/// phone, or the wrong one of two). What it is NOT: an authenticator. 30 bits can be ground
/// by an attacker who can already register devices on the account, so the approving phone
/// still shows it for a human decision and nothing is approved silently. The 60-digit
/// safety number is the strong comparison.
pub fn device_comparison_code(identity_ed25519: &PublicKey32) -> String {
    let digest = Sha256::digest(frame(DOMAIN_DEVICE_CODE, &[identity_ed25519]));
    let mut head = [0u8; 8];
    head.copy_from_slice(&digest[..8]);
    format!("{:09}", u64::from_be_bytes(head) % 1_000_000_000)
}
