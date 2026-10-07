//! Simulated crashes at every commit boundary. After a failure injected before, in the
//! middle of, or after the commit, the state must be either fully the old one or fully
//! the new one, and nothing (plaintext or ciphertext) may have been released.
//!
//! Each case runs twice: on the same handle, and after dropping the handle and
//! unlocking the file again, which is what a process restart does.

use super::common::*;
use crate::store::FailPoint;
use crate::*;

const OLD_STATE: [FailPoint; 2] = [FailPoint::MidTransaction, FailPoint::BeforeCommit];

fn crash_decrypt(point: FailPoint, restart: bool, normal: bool) {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    let mut next = 1u8;
    if normal {
        let legs = alice.send(&mut [&mut bob], next, "setup").unwrap();
        bob.receive(&alice, &legs[0], next, next).unwrap();
        next += 1;
        let r = bob.send(&mut [&mut alice], next, "setup reply").unwrap();
        alice.receive(&bob, &r[0], next, next).unwrap();
        next += 1;
    }
    let cm = next;
    let legs = alice.send(&mut [&mut bob], cm, "crash me").unwrap();
    assert_eq!(legs[0].olm_type, if normal { 1 } else { 0 });
    let status_before = bob.core.key_status().unwrap();
    let messages_before = bob.core.list_messages(CONV, None, 50).unwrap().len();

    bob.core.inject_fail(point);
    let failed = bob.receive(&alice, &legs[0], cm, cm);
    assert_eq!(
        failed.err(),
        Some(Error::Storage),
        "no plaintext may be returned"
    );
    if restart {
        bob.reopen();
    }

    if OLD_STATE.contains(&point) {
        // Fully old: nothing stored, nothing consumed, the message still decrypts.
        assert_eq!(bob.core.key_status().unwrap(), status_before);
        assert_eq!(
            bob.core.list_messages(CONV, None, 50).unwrap().len(),
            messages_before
        );
        assert_eq!(
            bob.receive(&alice, &legs[0], cm, cm).unwrap().body,
            "crash me"
        );
    } else {
        // Fully new: the message is committed and the key consumed, but the caller never
        // saw the plaintext. The retry is idempotent: it returns the stored message.
        let history = bob.core.list_messages(CONV, None, 50).unwrap();
        assert_eq!(history.len(), messages_before + 1);
        assert_eq!(history[0].body, "crash me");
        if !normal {
            assert_eq!(
                bob.core.key_status().unwrap().published_one_time,
                status_before.published_one_time - 1
            );
        }
        let generation = bob.core.store_generation().unwrap();
        let retried = bob.receive(&alice, &legs[0], cm, cm).unwrap();
        assert_eq!(
            retried, history[0],
            "the retry must return the identical message"
        );
        assert_eq!(
            bob.core.store_generation().unwrap(),
            generation,
            "the idempotent path must not write anything"
        );
        assert_eq!(
            bob.core.list_messages(CONV, None, 50).unwrap().len(),
            messages_before + 1
        );
        // The committed ratchet is coherent: the next message decrypts.
        let next_legs = alice.send(&mut [&mut bob], cm + 1, "after").unwrap();
        assert_eq!(
            bob.receive(&alice, &next_legs[0], cm + 1, cm + 1)
                .unwrap()
                .body,
            "after"
        );
    }
}

#[test]
fn decrypt_crash_at_each_commit_boundary() {
    for point in [
        FailPoint::MidTransaction,
        FailPoint::BeforeCommit,
        FailPoint::AfterCommit,
    ] {
        for restart in [false, true] {
            for normal in [false, true] {
                crash_decrypt(point, restart, normal);
            }
        }
    }
}

fn crash_encrypt(point: FailPoint, restart: bool) {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    let claim = bob.claim();
    let with_claim = Recipient {
        device: bob.directory(),
        claimed_key: Some(claim),
    };
    let without_claim = Recipient {
        device: bob.directory(),
        claimed_key: None,
    };
    let encrypt = |alice: &Dev, recipient: &Recipient| {
        alice.core.encrypt(
            CONV,
            cmid(1),
            "crash me",
            &alice.directory(),
            std::slice::from_ref(recipient),
        )
    };

    alice.core.inject_fail(point);
    assert_eq!(
        encrypt(&alice, &with_claim).err(),
        Some(Error::Storage),
        "no ciphertext may be returned"
    );
    if restart {
        alice.reopen();
    }

    if OLD_STATE.contains(&point) {
        // Fully old: no outbox row, no session, no local copy. The retry starts over.
        assert!(alice.core.list_messages(CONV, None, 10).unwrap().is_empty());
        assert_eq!(
            encrypt(&alice, &without_claim).err(),
            Some(Error::MissingClaimedKey),
            "no session survived the failed commit"
        );
        let legs = encrypt(&alice, &with_claim).unwrap();
        assert_eq!(
            bob.receive(&alice, &legs[0], 1, 1).unwrap().body,
            "crash me"
        );
        assert_eq!(encrypt(&alice, &without_claim).unwrap(), legs);
    } else {
        // Fully new: the outbox row exists, so the retry returns the stored legs.
        let own = alice.core.list_messages(CONV, None, 10).unwrap();
        assert_eq!(own.len(), 1);
        assert_eq!(own[0].body, "crash me");
        let legs = encrypt(&alice, &without_claim).unwrap();
        assert_eq!(legs.len(), 1);
        assert_eq!(
            bob.receive(&alice, &legs[0], 1, 1).unwrap().body,
            "crash me"
        );
        assert_eq!(alice.core.list_messages(CONV, None, 10).unwrap().len(), 1);
        assert_eq!(encrypt(&alice, &with_claim).unwrap(), legs);
    }
}

#[test]
fn encrypt_crash_at_each_commit_boundary() {
    for point in [
        FailPoint::MidTransaction,
        FailPoint::BeforeCommit,
        FailPoint::AfterCommit,
    ] {
        for restart in [false, true] {
            crash_encrypt(point, restart);
        }
    }
}

#[test]
fn create_device_crash_at_each_commit_boundary() {
    for point in [
        FailPoint::MidTransaction,
        FailPoint::BeforeCommit,
        FailPoint::AfterCommit,
    ] {
        for restart in [false, true] {
            let dir = TestDir::new();
            let ns = Namespace {
                user_id: [1; 16],
                generation: 1,
            };
            let key = store_key(1);
            let mut core = Core::unlock(dir.path(), ns, &key).unwrap();
            core.inject_fail(point);
            assert_eq!(
                core.create_device(ns.user_id, 1, 4).err(),
                Some(Error::Storage)
            );
            if restart {
                core.lock();
                core = Core::unlock(dir.path(), ns, &key).unwrap();
            }
            if OLD_STATE.contains(&point) {
                assert!(!core.has_device().unwrap(), "half a device survived");
                assert!(core.create_device(ns.user_id, 1, 4).is_ok());
            } else {
                assert!(core.has_device().unwrap());
                assert_eq!(
                    core.create_device(ns.user_id, 1, 4).err(),
                    Some(Error::DeviceExists)
                );
                assert_eq!(core.key_status().unwrap().pending, 5);
            }
        }
    }
}

#[test]
fn mark_published_and_top_up_crash_at_each_commit_boundary() {
    for point in [
        FailPoint::MidTransaction,
        FailPoint::BeforeCommit,
        FailPoint::AfterCommit,
    ] {
        for restart in [false, true] {
            let dir = TestDir::new();
            let ns = Namespace {
                user_id: [1; 16],
                generation: 1,
            };
            let key = store_key(1);
            let mut core = Core::unlock(dir.path(), ns, &key).unwrap();
            let created = core.create_device(ns.user_id, 1, 4).unwrap();
            let mut ids: Vec<u64> = created.one_time_keys.iter().map(|k| k.key_id).collect();
            ids.push(created.fallback_key.key_id);

            // mark_published
            core.inject_fail(point);
            assert_eq!(
                core.mark_published([9; 16], &ids).err(),
                Some(Error::Storage)
            );
            if restart {
                core.lock();
                core = Core::unlock(dir.path(), ns, &key).unwrap();
            }
            let pending = core.key_status().unwrap().pending;
            if OLD_STATE.contains(&point) {
                assert_eq!(pending, 5, "partial publication survived");
            } else {
                assert_eq!(pending, 0);
            }
            core.mark_published([9; 16], &ids).unwrap(); // retry is fine either way
            assert_eq!(core.key_status().unwrap().pending, 0);

            // top_up_keys: a failed call must not burn or reuse ids.
            let max_before = *ids.iter().max().unwrap();
            core.inject_fail(point);
            assert_eq!(core.top_up_keys(3).err(), Some(Error::Storage));
            if restart {
                core.lock();
                core = Core::unlock(dir.path(), ns, &key).unwrap();
            }
            let status = core.key_status().unwrap();
            let retry = core.top_up_keys(3).unwrap();
            let retry_ids: Vec<u64> = retry.iter().map(|k| k.key_id).collect();
            if OLD_STATE.contains(&point) {
                assert_eq!(status.pending, 0);
                assert_eq!(
                    retry_ids,
                    vec![max_before + 1, max_before + 2, max_before + 3]
                );
            } else {
                assert_eq!(status.pending, 3);
                assert_eq!(
                    retry_ids,
                    vec![max_before + 4, max_before + 5, max_before + 6]
                );
            }
        }
    }
}
