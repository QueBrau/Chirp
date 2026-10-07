//! Key lifecycle (ids, publication, caps) and the message size limits.

use std::collections::HashSet;

use super::common::*;
use crate::encoding::verify_signed_key;
use crate::envelope::{MAX_BODY_CHARS, MAX_LEG_B64_LEN};
use crate::*;

fn b64_len(bytes: usize) -> usize {
    4 * bytes.div_ceil(3)
}

#[test]
fn created_keys_are_signed_by_the_device_and_ids_are_unique_across_kinds() {
    let dev = Dev::with_otks(0xa1, 1, 5);
    let created = &dev.created;
    assert_eq!(created.one_time_keys.len(), 5);
    let all: Vec<SignedKey> = created
        .one_time_keys
        .iter()
        .copied()
        .chain([created.fallback_key])
        .collect();
    for key in &all {
        assert!(
            verify_signed_key(&created.identity.ed25519, &created.identity.curve25519, key).is_ok()
        );
    }
    assert!(
        created
            .one_time_keys
            .iter()
            .all(|k| k.kind == KeyKind::OneTime)
    );
    assert_eq!(created.fallback_key.kind, KeyKind::Fallback);
    // One id space for both kinds: vodozemac numbers them separately (both start at 0),
    // the wire does not.
    let ids: HashSet<u64> = all.iter().map(|k| k.key_id).collect();
    assert_eq!(ids.len(), 6);
    assert!(ids.iter().all(|id| *id >= 1));
    let publics: HashSet<[u8; 32]> = all.iter().map(|k| k.public_key).collect();
    assert_eq!(publics.len(), 6);
    // A key signature is bound to the key, the id, the kind and the owner.
    let mut other_owner = created.identity.curve25519;
    other_owner[0] ^= 1;
    assert!(verify_signed_key(&created.identity.ed25519, &other_owner, &all[0]).is_err());
}

#[test]
fn key_ids_are_monotonic_and_never_reused_even_after_consumption() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::with_otks(0xb1, 2, 3);
    let mut seen: HashSet<u64> = bob
        .created
        .one_time_keys
        .iter()
        .map(|k| k.key_id)
        .chain([bob.created.fallback_key.key_id])
        .collect();
    let max = *seen.iter().max().unwrap();

    // Consume one-time key #1 through a real prekey message.
    let legs = alice.send(&mut [&mut bob], 1, "uses an otk").unwrap();
    bob.receive(&alice, &legs[0], 1, 1).unwrap();
    assert_eq!(bob.core.key_status().unwrap().published_one_time, 2);

    // New keys continue the counter; the consumed id does not come back.
    let topped = bob.core.top_up_keys(3).unwrap();
    let new_ids: Vec<u64> = topped.iter().map(|k| k.key_id).collect();
    assert_eq!(new_ids, vec![max + 1, max + 2, max + 3]);
    for id in &new_ids {
        assert!(seen.insert(*id), "id {id} reused");
    }
    bob.reopen();
    let rotated = bob.core.rotate_fallback_key().unwrap();
    assert_eq!(rotated.kind, KeyKind::Fallback);
    assert_eq!(rotated.key_id, max + 4);
    assert!(seen.insert(rotated.key_id));
    bob.reopen();
    assert_eq!(bob.core.top_up_keys(1).unwrap()[0].key_id, max + 5);
}

#[test]
fn publication_state_and_pending_keys() {
    let dir = TestDir::new();
    let ns = Namespace {
        user_id: [1; 16],
        generation: 1,
    };
    let core = Core::unlock(dir.path(), ns, &store_key(1)).unwrap();
    let created = core.create_device(ns.user_id, 1, 4).unwrap();
    let status = core.key_status().unwrap();
    assert_eq!(status.pending, 5);
    assert_eq!(status.published_one_time, 0);
    assert!(status.has_fallback);

    // Crash between generating and uploading: the same signed keys come back.
    let pending = core.pending_keys().unwrap();
    assert_eq!(pending.len(), 5);
    for key in created.one_time_keys.iter().chain([&created.fallback_key]) {
        assert!(
            pending.contains(key),
            "pending keys must reproduce the original signatures"
        );
    }

    // All or nothing: one unknown id publishes nothing.
    let first = created.one_time_keys[0].key_id;
    assert_eq!(
        core.mark_published([5; 16], &[first, 9999]).err(),
        Some(Error::UnknownKey)
    );
    assert_eq!(core.key_status().unwrap().pending, 5);

    core.mark_published([5; 16], &[first, created.one_time_keys[1].key_id])
        .unwrap();
    let status = core.key_status().unwrap();
    assert_eq!((status.pending, status.published_one_time), (3, 2));
    // Idempotent for ids already confirmed.
    core.mark_published([5; 16], &[first]).unwrap();
    assert_eq!(core.key_status().unwrap().published_one_time, 2);
    // The server device id is fixed once recorded.
    assert_eq!(
        core.mark_published([6; 16], &[first]).err(),
        Some(Error::InvalidInput)
    );
    let rest: Vec<u64> = core
        .pending_keys()
        .unwrap()
        .iter()
        .map(|k| k.key_id)
        .collect();
    core.mark_published([5; 16], &rest).unwrap();
    assert_eq!(core.key_status().unwrap().pending, 0);
    assert!(core.pending_keys().unwrap().is_empty());
}

#[test]
fn key_counts_are_capped() {
    let dir = TestDir::new();
    let ns = Namespace {
        user_id: [1; 16],
        generation: 1,
    };
    let core = Core::unlock(dir.path(), ns, &store_key(1)).unwrap();
    core.create_device(ns.user_id, 1, MAX_KEYS_PER_CALL)
        .unwrap();
    assert_eq!(core.top_up_keys(0).err(), Some(Error::InvalidInput));
    assert_eq!(
        core.top_up_keys(MAX_KEYS_PER_CALL + 1).err(),
        Some(Error::InvalidInput)
    );
    assert_eq!(
        core.top_up_keys(MAX_KEYS_PER_CALL).unwrap().len(),
        MAX_KEYS_PER_CALL
    );
    assert_eq!(
        MAX_STORED_ONE_TIME_KEYS,
        2 * MAX_KEYS_PER_CALL,
        "two full batches fit exactly"
    );
    assert_eq!(core.top_up_keys(1).err(), Some(Error::TooManyKeys));
    // Nothing leaked from the refused call.
    assert_eq!(
        core.key_status().unwrap().pending,
        2 * MAX_KEYS_PER_CALL + 1
    );
}

#[test]
fn only_two_fallback_keys_are_kept_and_the_previous_one_still_works() {
    let alice = Dev::new(0xa1, 1);
    let carol = Dev::new(0xc1, 3);
    let mut bob = Dev::with_otks(0xb1, 2, 1);
    // A sender claimed this fallback key, then the device rotated twice before the
    // message arrived (a delayed message).
    let old_fallback = bob.created.fallback_key;
    let first_rotation = bob.core.rotate_fallback_key().unwrap();
    // `old_fallback` is now the PREVIOUS key: still decryptable.
    bob.claimable.clear();
    let claim = old_fallback;
    let recipients = [Recipient {
        device: bob.directory(),
        claimed_key: Some(claim),
    }];
    let legs = alice
        .core
        .encrypt(CONV, cmid(1), "late", &alice.directory(), &recipients)
        .unwrap();
    assert_eq!(bob.receive(&alice, &legs[0], 1, 1).unwrap().body, "late");
    // A second rotation forgets the oldest: the very first fallback key is gone.
    let _ = bob.core.rotate_fallback_key().unwrap();
    let recipients = [Recipient {
        device: bob.directory(),
        claimed_key: Some(old_fallback),
    }];
    let legs = carol
        .core
        .encrypt(CONV, cmid(2), "too late", &carol.directory(), &recipients)
        .unwrap();
    assert_eq!(
        bob.receive(&carol, &legs[0], 2, 2).err(),
        Some(Error::DecryptFailed)
    );
    // The still-held keys keep working.
    let recipients = [Recipient {
        device: bob.directory(),
        claimed_key: Some(first_rotation),
    }];
    let dave = Dev::new(0xd1, 4);
    let legs = dave
        .core
        .encrypt(CONV, cmid(3), "in time", &dave.directory(), &recipients)
        .unwrap();
    assert_eq!(bob.receive(&dave, &legs[0], 3, 3).unwrap().body, "in time");
}

#[test]
fn body_limits_are_enforced_on_encrypt() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    assert_eq!(
        alice.send(&mut [&mut bob], 1, "").err(),
        Some(Error::BodyInvalid)
    );
    assert_eq!(
        alice
            .send(&mut [&mut bob], 2, &"a".repeat(MAX_BODY_CHARS + 1))
            .err(),
        Some(Error::BodyTooLarge)
    );
    assert_eq!(
        alice
            .send(&mut [&mut bob], 3, &"\u{1F600}".repeat(MAX_BODY_CHARS + 1))
            .err(),
        Some(Error::BodyTooLarge)
    );
    for bad in ["a\u{0}b", "bell\u{7}", "esc\u{1b}[0m", "del\u{7f}"] {
        assert_eq!(
            alice.send(&mut [&mut bob], 4, bad).err(),
            Some(Error::BodyInvalid),
            "{bad:?}"
        );
    }
    // Nothing was stored by any refusal (they fail before any state is touched).
    assert!(alice.core.list_messages(CONV, None, 10).unwrap().is_empty());
    let allowed = [
        "tab\there",
        "two\nlines\r\n",
        "\u{85}next line",
        "naive caf\u{e9}",
        "\u{202e}rtl",
    ];
    for (i, ok) in allowed.iter().enumerate() {
        let id = 20 + i as u8;
        let legs = alice.send(&mut [&mut bob], id, ok).unwrap();
        assert_eq!(bob.receive(&alice, &legs[0], id, id).unwrap().body, *ok);
    }
}

/// Worst cases for the 65,536-character leg limit. Four-byte characters are the largest
/// UTF-8 expansion; newlines and quotes are the largest JSON escape expansion that the
/// body rules still allow (two bytes). Prekey messages are the biggest legs.
#[test]
fn the_largest_accepted_bodies_stay_under_the_leg_limit() {
    let bodies = [
        ("emoji", "\u{1F600}".repeat(MAX_BODY_CHARS)),
        ("newlines", "\n".repeat(MAX_BODY_CHARS)),
        ("quotes", "\"".repeat(MAX_BODY_CHARS)),
        ("backslashes", "\\".repeat(MAX_BODY_CHARS)),
        ("ascii", "x".repeat(MAX_BODY_CHARS)),
        (
            "mixed",
            "\u{1F600}\"\\\n\u{10FFFF}"
                .chars()
                .cycle()
                .take(MAX_BODY_CHARS)
                .collect(),
        ),
    ];
    for (n, (name, body)) in bodies.iter().enumerate() {
        assert_eq!(body.chars().count(), MAX_BODY_CHARS, "{name}");
        assert!(body.len() <= 40_000, "{name}");
        let mut alice = Dev::new(0xa1, 1);
        let mut bob = Dev::new(0xb1, 2);
        let id = n as u8 + 1;
        let legs = alice.send(&mut [&mut bob], id, body).unwrap();
        assert_eq!(legs[0].olm_type, 0);
        let b64 = b64_len(legs[0].ciphertext.len());
        eprintln!(
            "{name}: {} body bytes -> {} ciphertext bytes -> {b64} base64 chars",
            body.len(),
            legs[0].ciphertext.len()
        );
        assert!(
            b64 < MAX_LEG_B64_LEN,
            "{name}: {b64} base64 chars is over the leg limit"
        );
        assert!(legs[0].ciphertext.len() <= MAX_LEG_BYTES);
        let got = bob.receive(&alice, &legs[0], id, id).unwrap();
        assert_eq!(&got.body, body, "{name}");
        // Normal messages are smaller still.
        let reply = bob.send(&mut [&mut alice], id + 50, body).unwrap();
        assert_eq!(reply[0].olm_type, 1);
        assert!(b64_len(reply[0].ciphertext.len()) < MAX_LEG_B64_LEN);
        assert_eq!(
            alice
                .receive(&bob, &reply[0], id + 50, id + 50)
                .unwrap()
                .body,
            *body
        );
    }
}

#[test]
fn a_leg_is_refused_if_it_would_exceed_the_limit() {
    // The body rules keep real messages far below the limit, so exercise the guard by
    // inflating the envelope through the test hook.
    use crate::core::ENVELOPE_HOOK;
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    ENVELOPE_HOOK.with(|h| {
        *h.borrow_mut() = Some(Box::new(|p| {
            let mut v = p.to_vec();
            v.extend_from_slice(&vec![b' '; 60_000]);
            v
        }))
    });
    let result = alice.send(&mut [&mut bob], 1, "inflated");
    ENVELOPE_HOOK.with(|h| *h.borrow_mut() = None);
    assert_eq!(result.err(), Some(Error::CiphertextTooLarge));
    assert!(alice.core.list_messages(CONV, None, 10).unwrap().is_empty());
}
