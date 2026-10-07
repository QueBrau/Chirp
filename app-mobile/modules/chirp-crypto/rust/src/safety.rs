//! Safety numbers: a 60-digit code two people compare in person.
//!
//! For each user: `d = 0x00 || sorted(identity_ed25519 keys, concatenated) || user_id`,
//! then `d = SHA-512(d)` applied 5200 times (plain iteration of the hash function:
//! each round hashes only the previous digest). The first 30 digest bytes become 6
//! groups of 5 digits: each 5-byte chunk is a big-endian u40, taken mod 100000 and
//! zero-padded. The two 30-digit halves are concatenated with the lower user id
//! (bytewise) first, so both sides compute the same 60 digits.

use sha2::{Digest, Sha512};
use zeroize::Zeroize;

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
