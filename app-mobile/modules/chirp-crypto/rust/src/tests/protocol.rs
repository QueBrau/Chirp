//! Protocol behavior: round trips, fan-out, tamper, routing mismatch, replay, idempotency.

use super::common::*;
use crate::core::ENVELOPE_HOOK;
use crate::*;

/// Install an envelope rewriter for the duration of a scope.
struct HookGuard;
impl HookGuard {
    fn set(f: impl Fn(&[u8]) -> Vec<u8> + 'static) -> HookGuard {
        ENVELOPE_HOOK.with(|h| *h.borrow_mut() = Some(Box::new(f)));
        HookGuard
    }
}
impl Drop for HookGuard {
    fn drop(&mut self) {
        ENVELOPE_HOOK.with(|h| *h.borrow_mut() = None);
    }
}

fn replace_in(plaintext: &[u8], from: &str, to: &str) -> Vec<u8> {
    let text = std::str::from_utf8(plaintext).unwrap();
    assert!(text.contains(from), "hook pattern must exist");
    text.replacen(from, to, 1).into_bytes()
}

#[test]
fn two_accounts_round_trip_prekey_then_normal_both_directions() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    assert_eq!(
        alice.core.observe_directory(bob.user(), &[bob.directory()]),
        Ok(TrustState::Trusted)
    );
    assert_eq!(
        bob.core
            .observe_directory(alice.user(), &[alice.directory()]),
        Ok(TrustState::Trusted)
    );

    // Alice -> Bob: no session yet, so a prekey message (type 0).
    let legs = alice.send(&mut [&mut bob], 1, "hello bob").unwrap();
    assert_eq!(legs.len(), 1);
    assert_eq!(legs[0].olm_type, 0);
    let got = bob.receive(&alice, &legs[0], 1, 1).unwrap();
    assert_eq!(got.body, "hello bob");
    assert_eq!(got.direction, Direction::Incoming);
    assert_eq!(got.sender_trust, TrustState::Trusted);
    assert_eq!(got.message_id, Some(msg_id(1)));
    assert_eq!(got.sender_device_id, alice.device_id);
    assert_eq!(got.kind, "text");

    // Bob -> Alice: Bob has received, so this is a normal message (type 1).
    let legs = bob.send(&mut [&mut alice], 2, "hi alice").unwrap();
    assert_eq!(legs[0].olm_type, 1);
    let got = alice.receive(&bob, &legs[0], 2, 2).unwrap();
    assert_eq!(got.body, "hi alice");

    // Alice has now received too: her next message is normal as well.
    let legs = alice.send(&mut [&mut bob], 3, "how are you").unwrap();
    assert_eq!(legs[0].olm_type, 1);
    assert_eq!(
        bob.receive(&alice, &legs[0], 3, 3).unwrap().body,
        "how are you"
    );
    let legs = bob.send(&mut [&mut alice], 4, "fine").unwrap();
    assert_eq!(legs[0].olm_type, 1);
    assert_eq!(alice.receive(&bob, &legs[0], 4, 4).unwrap().body, "fine");

    // Local history has both directions, newest first.
    let history = alice.core.list_messages(CONV, None, 50).unwrap();
    let bodies: Vec<(&str, Direction)> = history
        .iter()
        .map(|m| (m.body.as_str(), m.direction))
        .collect();
    assert_eq!(
        bodies,
        vec![
            ("fine", Direction::Incoming),
            ("how are you", Direction::Outgoing),
            ("hi alice", Direction::Incoming),
            ("hello bob", Direction::Outgoing),
        ]
    );
    assert!(history.windows(2).all(|w| w[0].seq > w[1].seq));
    // Paging: before the oldest of the first page of two.
    let page1 = alice.core.list_messages(CONV, None, 2).unwrap();
    let page2 = alice
        .core
        .list_messages(CONV, Some(page1.last().unwrap().seq), 2)
        .unwrap();
    assert_eq!(page1.len(), 2);
    assert_eq!(page2.len(), 2);
    assert_eq!(page2[1].body, "hello bob");
    assert!(
        alice
            .core
            .list_messages(CONV, Some(page2.last().unwrap().seq), 2)
            .unwrap()
            .is_empty()
    );
    assert!(
        alice
            .core
            .list_messages([9; 16], None, 10)
            .unwrap()
            .is_empty()
    );
    assert!(alice.core.list_messages(CONV, None, 0).unwrap().is_empty());
}

#[test]
fn state_survives_a_restart() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    let legs = alice.send(&mut [&mut bob], 1, "one").unwrap();
    bob.receive(&alice, &legs[0], 1, 1).unwrap();
    alice.reopen();
    bob.reopen();
    let legs = bob.send(&mut [&mut alice], 2, "two").unwrap();
    assert_eq!(alice.receive(&bob, &legs[0], 2, 2).unwrap().body, "two");
    assert_eq!(bob.core.list_messages(CONV, None, 10).unwrap().len(), 2);
}

#[test]
fn multi_device_fan_out_including_the_senders_other_device() {
    // Alice has two devices, Bob has two. Alice's first device sends to both of Bob's
    // devices and to her own second device.
    let mut a1 = Dev::new(0xa1, 1);
    let mut a2 = Dev::new(0xa1, 2).approved_by(&a1);
    let mut b1 = Dev::new(0xb1, 3);
    let mut b2 = Dev::new(0xb1, 4).approved_by(&b1);

    let legs = a1
        .send(&mut [&mut b1, &mut b2, &mut a2], 1, "to everyone")
        .unwrap();
    assert_eq!(legs.len(), 3);
    assert!(legs.iter().all(|l| l.olm_type == 0));
    let ids: Vec<DeviceId> = legs.iter().map(|l| l.recipient_device_id).collect();
    assert_eq!(ids, vec![b1.device_id, b2.device_id, a2.device_id]);
    // Each leg is its own ciphertext.
    assert_ne!(legs[0].ciphertext, legs[1].ciphertext);

    for (dev, name) in [(&mut b1, "b1"), (&mut b2, "b2"), (&mut a2, "a2")] {
        let leg = dev.leg_for(&legs).clone();
        let got = dev
            .receive(&a1, &leg, 1, 1)
            .unwrap_or_else(|e| panic!("{name}: {e}"));
        assert_eq!(got.body, "to everyone", "{name}");
        assert_eq!(got.sender_device_id, a1.device_id);
    }

    // The sender kept its own copy: it never receives a leg for itself.
    let own = a1.core.list_messages(CONV, None, 10).unwrap();
    assert_eq!(own.len(), 1);
    assert_eq!(own[0].direction, Direction::Outgoing);
    assert_eq!(own[0].body, "to everyone");
    assert_eq!(own[0].message_id, None);

    // A reply from b2 reaches both of Alice's devices.
    let legs = b2
        .send(&mut [&mut a1, &mut a2, &mut b1], 2, "reply")
        .unwrap();
    assert_eq!(legs.len(), 3);
    for dev in [&mut a1, &mut a2, &mut b1] {
        let leg = dev.leg_for(&legs).clone();
        assert_eq!(dev.receive(&b2, &leg, 2, 2).unwrap().body, "reply");
    }
}

#[test]
fn tampered_ciphertext_fails_and_the_genuine_message_still_decrypts() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    let legs = alice.send(&mut [&mut bob], 1, "genuine").unwrap();
    let before = bob.core.key_status().unwrap();

    // Every single-byte flip of the prekey message must be rejected.
    for i in 0..legs[0].ciphertext.len() {
        let mut leg = legs[0].clone();
        leg.ciphertext[i] ^= 0x01;
        assert!(
            bob.receive(&alice, &leg, 1, 1).is_err(),
            "flip at byte {i} accepted"
        );
    }
    assert_eq!(
        bob.core.key_status().unwrap(),
        before,
        "a rejected message consumed a key"
    );
    assert!(bob.core.list_messages(CONV, None, 10).unwrap().is_empty());
    // Truncation too.
    let mut leg = legs[0].clone();
    leg.ciphertext.truncate(leg.ciphertext.len() - 1);
    assert!(bob.receive(&alice, &leg, 1, 1).is_err());

    assert_eq!(bob.receive(&alice, &legs[0], 1, 1).unwrap().body, "genuine");

    // Same for a normal message.
    let reply = bob.send(&mut [&mut alice], 2, "r").unwrap();
    alice.receive(&bob, &reply[0], 2, 2).unwrap();
    let legs = alice.send(&mut [&mut bob], 3, "second").unwrap();
    assert_eq!(legs[0].olm_type, 1);
    for i in 0..legs[0].ciphertext.len() {
        let mut leg = legs[0].clone();
        leg.ciphertext[i] ^= 0x80;
        assert!(
            bob.receive(&alice, &leg, 3, 3).is_err(),
            "flip at byte {i} accepted"
        );
    }
    assert_eq!(bob.receive(&alice, &legs[0], 3, 3).unwrap().body, "second");
}

#[test]
fn wrong_recipient_cannot_decrypt() {
    let mut alice = Dev::new(0xa1, 1);
    let mut b1 = Dev::new(0xb1, 2);
    let mut b2 = Dev::new(0xb1, 3).approved_by(&b1);
    let legs = alice
        .send(&mut [&mut b1, &mut b2], 1, "for b1 and b2")
        .unwrap();
    let b1_leg = b1.leg_for(&legs).clone();
    let b2_leg = b2.leg_for(&legs).clone();

    // b2 is handed b1's leg, labelled as its own: the session keys are not b2's.
    assert_eq!(
        b2.receive(&alice, &b1_leg, 1, 1).err(),
        Some(Error::DecryptFailed)
    );
    // b2 is handed b1's leg with the routing still saying b1: refused before any crypto.
    let mut expected = b2.expected(&alice, 1);
    expected.recipient_device_id = b1.device_id;
    assert_eq!(
        b2.core
            .decrypt(msg_id(1), &b1_leg, &alice.directory(), &expected)
            .err(),
        Some(Error::EnvelopeMismatch)
    );
    assert!(b2.core.list_messages(CONV, None, 10).unwrap().is_empty());

    assert_eq!(
        b1.receive(&alice, &b1_leg, 1, 1).unwrap().body,
        "for b1 and b2"
    );
    assert_eq!(
        b2.receive(&alice, &b2_leg, 1, 1).unwrap().body,
        "for b1 and b2"
    );
}

#[test]
fn wrong_recipient_normal_message_has_no_session() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    let carol = Dev::new(0xc1, 3);
    let legs = alice.send(&mut [&mut bob], 1, "x").unwrap();
    bob.receive(&alice, &legs[0], 1, 1).unwrap();
    let r = bob.send(&mut [&mut alice], 2, "y").unwrap();
    alice.receive(&bob, &r[0], 2, 2).unwrap();
    let legs = alice.send(&mut [&mut bob], 3, "z").unwrap();
    assert_eq!(legs[0].olm_type, 1);
    // Carol was never part of the conversation: she holds no session for Alice.
    let expected = carol.expected(&alice, 3);
    assert_eq!(
        carol
            .core
            .decrypt(msg_id(3), &legs[0], &alice.directory(), &expected)
            .err(),
        Some(Error::DecryptFailed)
    );
}

/// Mutate one routing field at a time. The outer message lies about the routing, or the
/// directory lies about the sender; either way nothing may be consumed or advanced.
#[test]
fn each_expected_field_mismatch_is_rejected_without_consuming_anything() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    let legs = alice.send(&mut [&mut bob], 1, "mismatch me").unwrap();
    assert_eq!(legs[0].olm_type, 0);
    let before = bob.core.key_status().unwrap();

    let genuine = bob.expected(&alice, 1);
    let mut cases: Vec<(&str, ExpectedEnvelope, DirectoryDevice, Error)> = Vec::new();
    let sender = alice.directory();

    let mut e = genuine;
    e.conversation_id = [0x77; 16];
    cases.push((
        "conversation_id",
        e,
        sender.clone(),
        Error::EnvelopeMismatch,
    ));
    let mut e = genuine;
    e.client_message_id = [0x78; 16];
    cases.push((
        "client_message_id",
        e,
        sender.clone(),
        Error::EnvelopeMismatch,
    ));
    // The inner envelope names Alice's real device; the directory entry claims another id.
    let mut e = genuine;
    e.sender_device_id = [0x79; 16];
    let mut other_device = sender.clone();
    other_device.device_id = [0x79; 16];
    cases.push((
        "sender_device_id (inner)",
        e,
        other_device,
        Error::EnvelopeMismatch,
    ));
    // Outer routing disagrees with the directory entry.
    let mut e = genuine;
    e.sender_device_id = [0x7a; 16];
    cases.push((
        "sender_device_id (outer)",
        e,
        sender.clone(),
        Error::EnvelopeMismatch,
    ));
    let mut e = genuine;
    e.sender_user_id = [0x7b; 16];
    cases.push((
        "sender_user_id (outer)",
        e,
        sender.clone(),
        Error::EnvelopeMismatch,
    ));
    let mut e = genuine;
    e.recipient_device_id = [0x7c; 16];
    cases.push((
        "recipient_device_id",
        e,
        sender.clone(),
        Error::EnvelopeMismatch,
    ));
    let mut e = genuine;
    e.recipient_user_id = [0x7d; 16];
    cases.push((
        "recipient_user_id",
        e,
        sender.clone(),
        Error::EnvelopeMismatch,
    ));

    for (name, expected, directory, error) in cases {
        assert_eq!(
            bob.core
                .decrypt(msg_id(1), &legs[0], &directory, &expected)
                .err(),
            Some(error),
            "{name}"
        );
        assert_eq!(
            bob.core.key_status().unwrap(),
            before,
            "{name} consumed a one-time key"
        );
        assert!(
            bob.core.list_messages(CONV, None, 10).unwrap().is_empty(),
            "{name} stored a message"
        );
    }

    // The genuine message still decrypts, and consumes exactly one key now.
    assert_eq!(
        bob.receive(&alice, &legs[0], 1, 1).unwrap().body,
        "mismatch me"
    );
    assert_eq!(
        bob.core.key_status().unwrap().published_one_time,
        before.published_one_time - 1
    );

    // Normal-message ratchet: a failed attempt must not advance it.
    let r = bob.send(&mut [&mut alice], 2, "r").unwrap();
    alice.receive(&bob, &r[0], 2, 2).unwrap();
    let legs = alice.send(&mut [&mut bob], 3, "ratchet").unwrap();
    assert_eq!(legs[0].olm_type, 1);
    let mut e = bob.expected(&alice, 3);
    e.conversation_id = [0x55; 16];
    for _ in 0..3 {
        assert_eq!(
            bob.core
                .decrypt(msg_id(3), &legs[0], &alice.directory(), &e)
                .err(),
            Some(Error::EnvelopeMismatch)
        );
    }
    assert_eq!(bob.receive(&alice, &legs[0], 3, 3).unwrap().body, "ratchet");
}

/// A lying or malformed envelope is produced through the real encrypt path with the
/// test hook, so the receiver sees genuine Olm ciphertext carrying bad content.
#[test]
fn malformed_and_lying_envelopes_are_rejected_without_state_change() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    let before = bob.core.key_status().unwrap();

    type Rewrite = Box<dyn Fn(&[u8]) -> Vec<u8>>;
    let cases: Vec<(&str, Rewrite, Error)> = vec![
        (
            "version",
            Box::new(|p| replace_in(p, "\"v\":1", "\"v\":2")),
            Error::InvalidEnvelope,
        ),
        (
            "kind",
            Box::new(|p| replace_in(p, "\"kind\":\"text\"", "\"kind\":\"image\"")),
            Error::InvalidEnvelope,
        ),
        (
            "unknown field",
            Box::new(|p| replace_in(p, "\"kind\"", "\"extra\":true,\"kind\"")),
            Error::InvalidEnvelope,
        ),
        (
            "control character in body",
            Box::new(|p| replace_in(p, "\"body\":\"msg", "\"body\":\"\\u0000msg")),
            Error::InvalidEnvelope,
        ),
        (
            "not json",
            Box::new(|_| b"not json".to_vec()),
            Error::InvalidEnvelope,
        ),
        (
            "conversation",
            Box::new(|p| replace_in(p, "c0c0c0c0", "c0c0c0c1")),
            Error::EnvelopeMismatch,
        ),
        (
            "recipient device",
            Box::new(|p| {
                // Last occurrence is the recipient's device id (02020202...).
                let text = std::str::from_utf8(p).unwrap();
                let at = text.rfind("02020202").unwrap();
                let mut out = text.to_owned();
                out.replace_range(at..at + 8, "02020203");
                out.into_bytes()
            }),
            Error::EnvelopeMismatch,
        ),
        (
            "sender device",
            Box::new(|p| replace_in(p, "01010101-0101", "01010101-0102")),
            Error::EnvelopeMismatch,
        ),
    ];

    for (n, (name, rewrite, error)) in cases.into_iter().enumerate() {
        let n = n as u8 + 1;
        let legs = {
            let _hook = HookGuard::set(move |p| rewrite(p));
            alice.send(&mut [&mut bob], n, &format!("msg {n}")).unwrap()
        };
        assert_eq!(
            bob.receive(&alice, &legs[0], n, n).err(),
            Some(error),
            "{name}"
        );
        assert_eq!(
            bob.core.key_status().unwrap(),
            before,
            "{name} consumed a key"
        );
        assert!(
            bob.core.list_messages(CONV, None, 10).unwrap().is_empty(),
            "{name}"
        );
    }
    // Bob is entirely unchanged, and a clean message still works.
    let legs = alice.send(&mut [&mut bob], 50, "clean").unwrap();
    assert_eq!(bob.receive(&alice, &legs[0], 50, 50).unwrap().body, "clean");
}

#[test]
fn replay_is_rejected_by_ciphertext_and_by_logical_message_id() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    let legs = alice.send(&mut [&mut bob], 1, "once").unwrap();
    assert_eq!(bob.receive(&alice, &legs[0], 1, 1).unwrap().body, "once");
    // The same leg under a DIFFERENT server id is a replay. (Under the SAME server id it
    // is an idempotent retry: see `idempotent.rs`.)
    assert_eq!(
        bob.receive(&alice, &legs[0], 1, 9).err(),
        Some(Error::Replay)
    );
    bob.reopen();
    assert_eq!(
        bob.receive(&alice, &legs[0], 1, 9).err(),
        Some(Error::Replay)
    );

    // A different ciphertext reusing the same (sender device, client_message_id) is also
    // a replay of the logical message: build one with a second sender state.
    let r = bob.send(&mut [&mut alice], 2, "r").unwrap();
    alice.receive(&bob, &r[0], 2, 2).unwrap();
    // Alice encrypts a different body under a NEW client id, then we present it as the
    // old client id: the inner envelope disagrees, so it is a mismatch, not accepted.
    let legs2 = alice.send(&mut [&mut bob], 3, "different").unwrap();
    assert_eq!(
        bob.core
            .decrypt(
                msg_id(5),
                &legs2[0],
                &alice.directory(),
                &bob.expected(&alice, 1)
            )
            .err(),
        Some(Error::Replay),
        "the logical id of message 1 is already used"
    );
    assert_eq!(
        bob.receive(&alice, &legs2[0], 3, 3).unwrap().body,
        "different"
    );
}

/// A fallback key is not consumed, so a replayed prekey message would start a brand
/// new session and be accepted by Olm itself. The replay set is what stops it.
#[test]
fn fallback_key_prekey_replay_is_rejected() {
    let mut bob = Dev::with_otks(0xb1, 2, 1);
    let mut alice = Dev::new(0xa1, 1);
    let mut carol = Dev::new(0xc1, 3);
    let mut dave = Dev::new(0xd1, 4);

    // Alice takes the only one-time key; Carol and Dave are handed the fallback key.
    let legs = alice.send(&mut [&mut bob], 1, "otk").unwrap();
    bob.receive(&alice, &legs[0], 1, 1).unwrap();
    assert_eq!(bob.core.key_status().unwrap().published_one_time, 0);

    let legs = carol.send(&mut [&mut bob], 2, "via fallback").unwrap();
    assert_eq!(legs[0].olm_type, 0);
    assert_eq!(
        bob.receive(&carol, &legs[0], 2, 2).unwrap().body,
        "via fallback"
    );
    // Under another server id the same prekey message is a replay, even though Olm itself
    // would happily start a new session from it.
    assert_eq!(
        bob.receive(&carol, &legs[0], 2, 7).err(),
        Some(Error::Replay)
    );
    bob.reopen();
    assert_eq!(
        bob.receive(&carol, &legs[0], 2, 3).err(),
        Some(Error::Replay)
    );

    // The fallback key is still usable by someone else.
    let legs = dave.send(&mut [&mut bob], 4, "also fallback").unwrap();
    assert_eq!(
        bob.receive(&dave, &legs[0], 4, 4).unwrap().body,
        "also fallback"
    );
    assert!(bob.core.key_status().unwrap().has_fallback);
}

#[test]
fn outbox_retry_returns_stored_legs_without_encrypting_again() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    let first = alice.send(&mut [&mut bob], 1, "idempotent").unwrap();
    let generation = alice.core.store_generation().unwrap();

    let again = alice.send(&mut [&mut bob], 1, "idempotent").unwrap();
    assert_eq!(first, again, "a retry must return byte-identical legs");
    assert_eq!(
        alice.core.store_generation().unwrap(),
        generation,
        "a retry must not write anything"
    );
    // Also across a restart.
    alice.reopen();
    let after_restart = alice.send(&mut [&mut bob], 1, "idempotent").unwrap();
    assert_eq!(first, after_restart);

    // The stored legs decrypt, and the ratchet was not disturbed by the retries.
    assert_eq!(
        bob.receive(&alice, &first[0], 1, 1).unwrap().body,
        "idempotent"
    );
    let next = alice.send(&mut [&mut bob], 2, "next").unwrap();
    assert_eq!(bob.receive(&alice, &next[0], 2, 2).unwrap().body, "next");

    // Our own history has the message once.
    let own = alice.core.list_messages(CONV, None, 10).unwrap();
    assert_eq!(own.iter().filter(|m| m.body == "idempotent").count(), 1);
}

#[test]
fn reusing_a_client_message_id_for_different_content_is_refused() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    alice.send(&mut [&mut bob], 1, "original").unwrap();
    assert_eq!(
        alice.send(&mut [&mut bob], 1, "changed").err(),
        Some(Error::ClientMessageIdReused)
    );
    // Same body in a different conversation is also a different request.
    let recipients = vec![Recipient {
        device: bob.directory(),
        claimed_key: None,
    }];
    assert_eq!(
        alice
            .core
            .encrypt(
                [0x99; 16],
                cmid(1),
                "original",
                &alice.directory(),
                &recipients
            )
            .err(),
        Some(Error::ClientMessageIdReused)
    );
}

#[test]
fn a_refreshed_device_list_adds_only_the_missing_legs() {
    let mut alice = Dev::new(0xa1, 1);
    let mut b1 = Dev::new(0xb1, 2);
    let mut b2 = Dev::new(0xb1, 3).approved_by(&b1);
    let first = alice.send(&mut [&mut b1], 1, "grew").unwrap();
    // The server answers device_list_mismatch; the client retries with both devices.
    let both = alice.send(&mut [&mut b1, &mut b2], 1, "grew").unwrap();
    assert_eq!(both.len(), 2);
    assert_eq!(
        both[0], first[0],
        "the existing leg must be reused byte for byte"
    );
    assert_eq!(b2.receive(&alice, &both[1], 1, 1).unwrap().body, "grew");
    assert_eq!(b1.receive(&alice, &both[0], 1, 1).unwrap().body, "grew");
    // Still one local copy of the message.
    assert_eq!(alice.core.list_messages(CONV, None, 10).unwrap().len(), 1);
    // And a request for only b2 returns just b2's stored leg.
    let only = alice.send(&mut [&mut b2], 1, "grew").unwrap();
    assert_eq!(only, vec![both[1].clone()]);
}

#[test]
fn claimed_keys_are_verified_before_a_session_exists() {
    let alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    let mut bob2 = Dev::new(0xb1, 5).approved_by(&bob);
    let carol = Dev::new(0xc1, 6);
    let mut mallory = Dev::new(0xee, 9);
    let good = bob.claim();
    let send = |device: &Dev, claimed: Option<SignedKey>| {
        alice.core.encrypt(
            CONV,
            cmid(1),
            "hi",
            &alice.directory(),
            &[Recipient {
                device: device.directory(),
                claimed_key: claimed,
            }],
        )
    };
    assert_eq!(send(&bob, None).err(), Some(Error::MissingClaimedKey));

    let mut bad = good;
    bad.signature[0] ^= 1;
    assert_eq!(send(&bob, Some(bad)).err(), Some(Error::InvalidSignature));
    let mut bad = good;
    bad.key_id += 1;
    assert_eq!(send(&bob, Some(bad)).err(), Some(Error::InvalidSignature));
    let mut bad = good;
    bad.kind = KeyKind::Fallback;
    assert_eq!(send(&bob, Some(bad)).err(), Some(Error::InvalidSignature));
    let mut bad = good;
    bad.public_key[0] ^= 1;
    assert_eq!(send(&bob, Some(bad)).err(), Some(Error::InvalidSignature));
    // A key signed by someone else, presented for Bob's device.
    let foreign = mallory.claim();
    assert_eq!(
        send(&bob, Some(foreign)).err(),
        Some(Error::InvalidSignature)
    );
    // A key of Bob's OTHER device, presented for this one.
    let sibling = bob2.claim();
    assert_eq!(
        send(&bob, Some(sibling)).err(),
        Some(Error::InvalidSignature)
    );
    // None of the failures left a session behind: the first valid claim is accepted.
    assert!(send(&bob, Some(good)).is_ok());
    // A fallback key is a valid claim too.
    assert!(send(&carol, Some(carol.created.fallback_key)).is_ok());
}

#[test]
fn decrypt_input_validation() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    let legs = alice.send(&mut [&mut bob], 1, "x").unwrap();
    let good = legs[0].clone();

    let mut leg = good.clone();
    leg.olm_type = 2;
    assert_eq!(
        bob.receive(&alice, &leg, 1, 1).err(),
        Some(Error::InvalidInput)
    );
    let mut leg = good.clone();
    leg.ciphertext.clear();
    assert_eq!(
        bob.receive(&alice, &leg, 1, 1).err(),
        Some(Error::InvalidInput)
    );
    let mut leg = good.clone();
    leg.ciphertext = vec![7; MAX_LEG_BYTES + 1];
    assert_eq!(
        bob.receive(&alice, &leg, 1, 1).err(),
        Some(Error::InvalidInput)
    );
    let mut leg = good.clone();
    leg.ciphertext = vec![7; 200];
    assert_eq!(
        bob.receive(&alice, &leg, 1, 1).err(),
        Some(Error::DecryptFailed)
    );
    leg.olm_type = 1;
    assert_eq!(
        bob.receive(&alice, &leg, 1, 1).err(),
        Some(Error::DecryptFailed)
    );

    // Sender binding that does not verify.
    let mut forged = alice.directory();
    forged.binding_signature[3] ^= 1;
    assert_eq!(
        bob.core
            .decrypt(msg_id(1), &good, &forged, &bob.expected(&alice, 1))
            .err(),
        Some(Error::InvalidSignature)
    );
    // A leg that claims to come from ourselves.
    let me = bob.directory();
    let mut expected = bob.expected(&alice, 1);
    expected.sender_user_id = me.user_id;
    expected.sender_device_id = me.device_id;
    assert_eq!(
        bob.core.decrypt(msg_id(1), &good, &me, &expected).err(),
        Some(Error::InvalidInput)
    );
    assert_eq!(bob.receive(&alice, &good, 1, 1).unwrap().body, "x");
}

#[test]
fn encrypt_input_validation() {
    let alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    let claim = bob.claim();
    let rec = |claimed: Option<SignedKey>| Recipient {
        device: bob.directory(),
        claimed_key: claimed,
    };
    let enc = |sender: &DirectoryDevice, recipients: &[Recipient]| {
        alice.core.encrypt(CONV, cmid(1), "hi", sender, recipients)
    };
    assert_eq!(
        enc(&alice.directory(), &[]).err(),
        Some(Error::InvalidInput)
    );
    // Duplicate recipient.
    assert_eq!(
        enc(&alice.directory(), &[rec(Some(claim)), rec(Some(claim))]).err(),
        Some(Error::InvalidInput)
    );
    // Sending to ourselves.
    assert_eq!(
        enc(
            &alice.directory(),
            &[Recipient {
                device: alice.directory(),
                claimed_key: Some(claim)
            }]
        )
        .err(),
        Some(Error::InvalidInput)
    );
    // A sender description that is not us.
    let mut other = alice.directory();
    other.device_id = [0x42; 16];
    assert_eq!(
        enc(&other, &[rec(Some(claim))]).err(),
        Some(Error::SenderMismatch)
    );
    let mut other = alice.directory();
    other.identity_ed25519 = bob.identity().ed25519;
    assert_eq!(
        enc(&other, &[rec(Some(claim))]).err(),
        Some(Error::SenderMismatch)
    );
    // Recipient binding that does not verify.
    let mut forged = rec(Some(claim));
    forged.device.binding_signature[0] ^= 1;
    assert_eq!(
        enc(&alice.directory(), &[forged]).err(),
        Some(Error::InvalidSignature)
    );
    // Too many recipients.
    let many: Vec<Recipient> = (0..=MAX_RECIPIENTS).map(|_| rec(None)).collect();
    assert_eq!(
        enc(&alice.directory(), &many).err(),
        Some(Error::InvalidInput)
    );
}

#[test]
fn encrypting_needs_the_device_to_be_published() {
    let dir = TestDir::new();
    let ns = Namespace {
        user_id: [1; 16],
        generation: 1,
    };
    let core = Core::unlock(dir.path(), ns, &store_key(1)).unwrap();
    assert_eq!(core.key_status().err(), Some(Error::NoDevice));
    let created = core.create_device(ns.user_id, 1, 3).unwrap();
    let me = DirectoryDevice {
        user_id: ns.user_id,
        device_id: [1; 16],
        generation: 1,
        identity_curve25519: created.identity.curve25519,
        identity_ed25519: created.identity.ed25519,
        binding_signature: created.binding_signature,
        approval: None,
    };
    let peer = Dev::new(0xb1, 2);
    let recipients = [Recipient {
        device: peer.directory(),
        claimed_key: Some(peer.created.one_time_keys[0]),
    }];
    assert_eq!(
        core.encrypt(CONV, cmid(1), "x", &me, &recipients).err(),
        Some(Error::NotPublished)
    );
}
