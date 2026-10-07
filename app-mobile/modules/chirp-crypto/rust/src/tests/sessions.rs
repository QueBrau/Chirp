//! Session establishment corner cases: repeated prekey messages and glare.

use super::common::*;
use crate::*;

/// Until the other side replies, every message is still a prekey message for the SAME
/// session. They must decrypt through the existing session (their one-time key is
/// already gone), in order or not.
#[test]
fn several_prekey_messages_before_a_reply_share_one_session() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    let l1 = alice.send(&mut [&mut bob], 1, "first").unwrap();
    let l2 = alice.send(&mut [&mut bob], 2, "second").unwrap();
    let l3 = alice.send(&mut [&mut bob], 3, "third").unwrap();
    assert!([&l1, &l2, &l3].iter().all(|l| l[0].olm_type == 0));

    // Delivered out of order: the LAST one arrives first and creates the session.
    assert_eq!(bob.receive(&alice, &l3[0], 3, 3).unwrap().body, "third");
    assert_eq!(bob.core.key_status().unwrap().published_one_time, 9);
    assert_eq!(bob.receive(&alice, &l1[0], 1, 1).unwrap().body, "first");
    assert_eq!(bob.receive(&alice, &l2[0], 2, 2).unwrap().body, "second");
    // Only one one-time key was ever consumed.
    assert_eq!(bob.core.key_status().unwrap().published_one_time, 9);
    // Replays of each (under another server id) are still rejected.
    assert_eq!(bob.receive(&alice, &l2[0], 2, 8).err(), Some(Error::Replay));

    let r = bob.send(&mut [&mut alice], 4, "reply").unwrap();
    assert_eq!(r[0].olm_type, 1);
    assert_eq!(alice.receive(&bob, &r[0], 4, 4).unwrap().body, "reply");
    let next = alice.send(&mut [&mut bob], 5, "now normal").unwrap();
    assert_eq!(next[0].olm_type, 1);
    assert_eq!(
        bob.receive(&alice, &next[0], 5, 5).unwrap().body,
        "now normal"
    );
}

#[test]
fn simultaneous_first_messages_converge_on_one_conversation() {
    // Both sides start a session at the same moment (glare). Each has an outbound and an
    // inbound session afterwards and must be able to read the other's later messages.
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    let a_first = alice.send(&mut [&mut bob], 1, "from alice").unwrap();
    let b_first = bob.send(&mut [&mut alice], 2, "from bob").unwrap();
    assert_eq!(a_first[0].olm_type, 0);
    assert_eq!(b_first[0].olm_type, 0);
    assert_eq!(
        bob.receive(&alice, &a_first[0], 1, 1).unwrap().body,
        "from alice"
    );
    assert_eq!(
        alice.receive(&bob, &b_first[0], 2, 2).unwrap().body,
        "from bob"
    );
    for round in 0..4u8 {
        let a = alice.send(&mut [&mut bob], 10 + round, "a").unwrap();
        assert_eq!(
            bob.receive(&alice, &a[0], 10 + round, 10 + round)
                .unwrap()
                .body,
            "a"
        );
        let b = bob.send(&mut [&mut alice], 30 + round, "b").unwrap();
        assert_eq!(
            alice
                .receive(&bob, &b[0], 30 + round, 30 + round)
                .unwrap()
                .body,
            "b"
        );
    }
}
