//! Decrypt is idempotent for a leg that is already committed under the same server
//! message id, so a crash between commit and delivery costs the caller nothing.

use super::common::*;
use crate::*;

/// Receive `message`, then retry it in every way that must be harmless, and check that
/// nothing was written by any of them.
fn check_retries(alice: &mut Dev, bob: &mut Dev, leg: &Leg, client_id: u8, server_id: u8) {
    let first = bob.receive(alice, leg, client_id, server_id).unwrap();
    let generation = bob.core.store_generation().unwrap();
    let status = bob.core.key_status().unwrap();

    for restart in [false, true, false] {
        if restart {
            bob.reopen();
        }
        let again = bob.receive(alice, leg, client_id, server_id).unwrap();
        assert_eq!(again, first, "a retry must return the identical message");
        assert_eq!(
            bob.core.store_generation().unwrap(),
            generation,
            "retry wrote something"
        );
        assert_eq!(bob.core.key_status().unwrap(), status);
    }
    // It is still exactly one stored message.
    let stored: Vec<_> = bob
        .core
        .list_messages(CONV, None, 100)
        .unwrap()
        .into_iter()
        .filter(|m| m.client_message_id == cmid(client_id))
        .collect();
    assert_eq!(stored, vec![first]);
}

#[test]
fn a_retry_returns_the_stored_message_and_writes_nothing() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);

    // A prekey message, then normal messages in both directions.
    let legs = alice.send(&mut [&mut bob], 1, "one").unwrap();
    assert_eq!(legs[0].olm_type, 0);
    check_retries(&mut alice, &mut bob, &legs[0], 1, 1);

    let r = bob.send(&mut [&mut alice], 2, "reply").unwrap();
    check_retries(&mut bob, &mut alice, &r[0], 2, 2);

    let legs = alice.send(&mut [&mut bob], 3, "normal").unwrap();
    assert_eq!(legs[0].olm_type, 1);
    check_retries(&mut alice, &mut bob, &legs[0], 3, 3);

    // The retries touched neither the ratchet nor the replay set: the next genuine
    // message still decrypts, and so does everything before it, again.
    let next = alice.send(&mut [&mut bob], 4, "after the retries").unwrap();
    assert_eq!(
        bob.receive(&alice, &next[0], 4, 4).unwrap().body,
        "after the retries"
    );
    assert_eq!(bob.receive(&alice, &legs[0], 3, 3).unwrap().body, "normal");
    let reply = bob.send(&mut [&mut alice], 5, "and back").unwrap();
    assert_eq!(
        alice.receive(&bob, &reply[0], 5, 5).unwrap().body,
        "and back"
    );
}

#[test]
fn the_retried_message_keeps_the_trust_label_it_was_stored_with() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    // Alice is never observed by Bob, so the message is stored as UnapprovedDevice...
    let legs = alice.send(&mut [&mut bob], 1, "label me").unwrap();
    let first = bob.receive(&alice, &legs[0], 1, 1).unwrap();
    assert_eq!(first.sender_trust, TrustState::UnapprovedDevice);
    // ...and a retry after Bob has since pinned Alice returns what was committed, not a
    // freshly computed label.
    bob.core
        .observe_directory(alice.user(), &[alice.directory()])
        .unwrap();
    assert_eq!(bob.receive(&alice, &legs[0], 1, 1).unwrap(), first);
}

#[test]
fn the_same_ciphertext_under_a_different_message_id_still_fails() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    let legs = alice.send(&mut [&mut bob], 1, "once").unwrap();
    bob.receive(&alice, &legs[0], 1, 1).unwrap();
    let generation = bob.core.store_generation().unwrap();
    assert_eq!(
        bob.receive(&alice, &legs[0], 1, 2).err(),
        Some(Error::Replay)
    );
    bob.reopen();
    assert_eq!(
        bob.receive(&alice, &legs[0], 1, 2).err(),
        Some(Error::Replay)
    );
    assert_eq!(bob.core.store_generation().unwrap(), generation);
    // The refusal did not poison the genuine id.
    assert_eq!(bob.receive(&alice, &legs[0], 1, 1).unwrap().body, "once");
}

#[test]
fn a_different_ciphertext_under_a_committed_message_id_still_fails() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    let legs = alice.send(&mut [&mut bob], 1, "committed").unwrap();
    bob.receive(&alice, &legs[0], 1, 1).unwrap();
    let r = bob.send(&mut [&mut alice], 2, "r").unwrap();
    alice.receive(&bob, &r[0], 2, 2).unwrap();
    let other = alice
        .send(&mut [&mut bob], 3, "a different message")
        .unwrap();
    let generation = bob.core.store_generation().unwrap();

    // A genuinely different leg presented under message id 1.
    assert_eq!(
        bob.core
            .decrypt(
                msg_id(1),
                &other[0],
                &alice.directory(),
                &bob.expected(&alice, 3)
            )
            .err(),
        Some(Error::Replay)
    );
    // The committed leg with a damaged byte, under the same id: not the stored message.
    let mut damaged = legs[0].clone();
    damaged.ciphertext[10] ^= 1;
    assert_eq!(
        bob.receive(&alice, &damaged, 1, 1).err(),
        Some(Error::Replay)
    );
    assert_eq!(bob.core.store_generation().unwrap(), generation);
    // The other leg is unharmed under its own id.
    assert_eq!(
        bob.receive(&alice, &other[0], 3, 3).unwrap().body,
        "a different message"
    );
}

#[test]
fn a_retry_whose_routing_disagrees_with_what_was_committed_is_a_mismatch() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    let legs = alice.send(&mut [&mut bob], 1, "routed").unwrap();
    let first = bob.receive(&alice, &legs[0], 1, 1).unwrap();
    let generation = bob.core.store_generation().unwrap();
    let genuine = bob.expected(&alice, 1);

    let mut wrong = genuine;
    wrong.conversation_id = [0x77; 16];
    assert_eq!(
        bob.core
            .decrypt(msg_id(1), &legs[0], &alice.directory(), &wrong)
            .err(),
        Some(Error::EnvelopeMismatch)
    );
    let mut wrong = genuine;
    wrong.client_message_id = [0x78; 16];
    assert_eq!(
        bob.core
            .decrypt(msg_id(1), &legs[0], &alice.directory(), &wrong)
            .err(),
        Some(Error::EnvelopeMismatch)
    );
    // A different sender device presented for the same committed id.
    let mut other_sender = alice.directory();
    other_sender.device_id = [0x79; 16];
    let mut wrong = genuine;
    wrong.sender_device_id = [0x79; 16];
    assert_eq!(
        bob.core
            .decrypt(msg_id(1), &legs[0], &other_sender, &wrong)
            .err(),
        Some(Error::EnvelopeMismatch)
    );
    // The recipient must still be us, and the sender's binding must still verify.
    let mut wrong = genuine;
    wrong.recipient_device_id = [0x7a; 16];
    assert_eq!(
        bob.core
            .decrypt(msg_id(1), &legs[0], &alice.directory(), &wrong)
            .err(),
        Some(Error::EnvelopeMismatch)
    );
    let mut forged = alice.directory();
    forged.binding_signature[0] ^= 1;
    assert_eq!(
        bob.core
            .decrypt(msg_id(1), &legs[0], &forged, &genuine)
            .err(),
        Some(Error::InvalidSignature)
    );
    assert_eq!(bob.core.store_generation().unwrap(), generation);
    // None of that disturbed the committed message.
    assert_eq!(bob.receive(&alice, &legs[0], 1, 1).unwrap(), first);
}
