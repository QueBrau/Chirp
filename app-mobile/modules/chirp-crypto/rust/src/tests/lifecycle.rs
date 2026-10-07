//! Lifecycle and store integrity: lock, wipe, wrong key, foreign and rolled-back records.

use rusqlite::{Connection, params};

use super::common::*;
use crate::store::namespace_path;
use crate::*;

fn raw(dev: &Dev) -> Connection {
    Connection::open(namespace_path(dev.dir.path(), &dev.ns)).unwrap()
}

type Row = (String, Vec<u8>, i64, Option<Vec<u8>>, Vec<u8>, Vec<u8>);

fn row(conn: &Connection, rtype: &str) -> Row {
    conn.query_row(
        "SELECT rtype, rid, gen, tag, nonce, ct FROM records WHERE rtype=?1 ORDER BY rid LIMIT 1",
        params![rtype],
        |r| {
            Ok((
                r.get(0)?,
                r.get(1)?,
                r.get(2)?,
                r.get(3)?,
                r.get(4)?,
                r.get(5)?,
            ))
        },
    )
    .unwrap()
}

fn put_row(conn: &Connection, row: &Row) {
    conn.execute(
        "INSERT OR REPLACE INTO records (rtype, rid, gen, tag, nonce, ct) VALUES (?1,?2,?3,?4,?5,?6)",
        params![row.0, row.1, row.2, row.3, row.4, row.5],
    )
    .unwrap();
}

fn lock_for_raw_access(dev: &Dev) {
    dev.core.lock();
}

#[test]
fn lock_invalidates_every_handle_and_unlock_makes_a_new_one() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    alice.send(&mut [&mut bob], 1, "before lock").unwrap();
    let stale = alice.core.clone();
    assert!(stale.is_unlocked());

    alice.core.lock();
    alice.core.lock(); // idempotent
    assert!(!stale.is_unlocked());
    assert!(!alice.core.is_unlocked());

    let me = alice.directory();
    let rec = Recipient {
        device: bob.directory(),
        claimed_key: None,
    };
    let leg = Leg {
        recipient_device_id: [0; 16],
        olm_type: 1,
        ciphertext: vec![1],
    };
    let expected = bob.expected(&alice, 1);
    let nd = alice.new_device_binding();
    for handle in [&stale, &alice.core] {
        assert_eq!(handle.namespace().err(), Some(Error::Locked));
        assert_eq!(handle.store_generation().err(), Some(Error::Locked));
        assert_eq!(handle.has_device().err(), Some(Error::Locked));
        assert_eq!(handle.public_identity().err(), Some(Error::Locked));
        assert_eq!(
            handle.create_device([0xa1; 16], 1, 1).err(),
            Some(Error::Locked)
        );
        assert_eq!(
            handle.mark_published([1; 16], &[1]).err(),
            Some(Error::Locked)
        );
        assert_eq!(handle.top_up_keys(1).err(), Some(Error::Locked));
        assert_eq!(handle.rotate_fallback_key().err(), Some(Error::Locked));
        assert_eq!(handle.pending_keys().err(), Some(Error::Locked));
        assert_eq!(handle.key_status().err(), Some(Error::Locked));
        assert_eq!(
            handle
                .encrypt(CONV, cmid(1), "x", &me, std::slice::from_ref(&rec))
                .err(),
            Some(Error::Locked)
        );
        assert_eq!(
            handle
                .decrypt(msg_id(1), &leg, &bob.directory(), &expected)
                .err(),
            Some(Error::Locked)
        );
        assert_eq!(
            handle.list_messages(CONV, None, 5).err(),
            Some(Error::Locked)
        );
        assert_eq!(
            handle
                .observe_directory(bob.user(), &[bob.directory()])
                .err(),
            Some(Error::Locked)
        );
        assert_eq!(handle.trust_state(bob.user()).err(), Some(Error::Locked));
        assert_eq!(
            handle.accept_identity_change(bob.user()).err(),
            Some(Error::Locked)
        );
        assert_eq!(handle.approve_device(&nd).err(), Some(Error::Locked));
        assert_eq!(handle.wipe().err(), Some(Error::Locked));
    }

    // A new unlock is a new handle; the committed state is all there; old handles stay dead.
    let fresh = Core::unlock(alice.dir.path(), alice.ns, &alice.key).unwrap();
    assert!(fresh.has_device().unwrap());
    assert_eq!(
        fresh.list_messages(CONV, None, 5).unwrap()[0].body,
        "before lock"
    );
    assert!(!stale.is_unlocked());
    assert_eq!(stale.has_device().err(), Some(Error::Locked));
    alice.core = fresh;
}

#[test]
fn a_namespace_cannot_be_unlocked_twice_at_once() {
    let alice = Dev::new(0xa1, 1);
    assert_eq!(
        Core::unlock(alice.dir.path(), alice.ns, &alice.key).err(),
        Some(Error::StoreInUse)
    );
    alice.core.lock();
    assert!(Core::unlock(alice.dir.path(), alice.ns, &alice.key).is_ok());
}

#[test]
fn wrong_store_key_fails_closed_with_one_fixed_error() {
    let mut alice = Dev::new(0xa1, 1);
    alice.core.lock();
    for key in [store_key(0x99), [0u8; 32]] {
        assert_eq!(
            Core::unlock(alice.dir.path(), alice.ns, &key).err(),
            Some(Error::UnlockFailed)
        );
    }
    assert_eq!(
        Core::unlock(alice.dir.path(), alice.ns, &alice.key[..31]).err(),
        Some(Error::InvalidInput)
    );
    assert_eq!(
        Core::unlock(alice.dir.path(), alice.ns, &[0u8; 33]).err(),
        Some(Error::InvalidInput)
    );
    // The wrong keys did not damage or replace anything.
    alice.core = Core::unlock(alice.dir.path(), alice.ns, &alice.key).unwrap();
    assert!(alice.core.has_device().unwrap());
    // And the error text is fixed, with nothing about key material in it.
    assert_eq!(Error::UnlockFailed.to_string(), "unlock_failed");
}

#[test]
fn a_store_file_renamed_into_another_namespace_does_not_open() {
    let a = Dev::new(0xa1, 1);
    let b = Dev::new(0xb1, 1); // same store key on purpose
    assert_eq!(a.key, b.key);
    a.core.lock();
    b.core.lock();
    // Put B's file where A's namespace expects its own.
    std::fs::copy(
        namespace_path(b.dir.path(), &b.ns),
        namespace_path(a.dir.path(), &a.ns),
    )
    .unwrap();
    assert_eq!(
        Core::unlock(a.dir.path(), a.ns, &a.key).err(),
        Some(Error::UnlockFailed)
    );
    // Same for a different generation of the same user.
    let g2 = Namespace {
        user_id: b.ns.user_id,
        generation: 2,
    };
    std::fs::copy(
        namespace_path(b.dir.path(), &b.ns),
        namespace_path(b.dir.path(), &g2),
    )
    .unwrap();
    assert_eq!(
        Core::unlock(b.dir.path(), g2, &b.key).err(),
        Some(Error::UnlockFailed)
    );
}

#[test]
fn a_record_copied_from_another_namespace_fails_to_load() {
    let a = Dev::new(0xa1, 1);
    let b = Dev::new(0xb1, 1); // identical store key: only the AAD can tell them apart
    lock_for_raw_access(&a);
    lock_for_raw_access(&b);
    let foreign = row(&raw(&b), "dev");
    put_row(&raw(&a), &foreign);
    // Structure and generation still look plausible, so the store opens...
    let core = Core::unlock(a.dir.path(), a.ns, &a.key).unwrap();
    // ...but the foreign record does not authenticate.
    assert_eq!(core.has_device().err(), Some(Error::StoreCorrupt));
}

#[test]
fn a_record_moved_to_another_position_fails_to_load() {
    let dir = TestDir::new();
    let ns = Namespace {
        user_id: [1; 16],
        generation: 1,
    };
    let key = store_key(1);
    let core = Core::unlock(dir.path(), ns, &key).unwrap();
    core.create_device(ns.user_id, 1, 3).unwrap();
    core.lock();
    let conn = Connection::open(namespace_path(dir.path(), &ns)).unwrap();
    // Swap the account record's ciphertext with the device record's.
    let account = row(&conn, "acct");
    let device = row(&conn, "dev");
    assert_eq!(
        account.2, device.2,
        "same generation, so only AAD can object"
    );
    let mut moved = account.clone();
    moved.4 = device.4.clone();
    moved.5 = device.5.clone();
    put_row(&conn, &moved);
    drop(conn);
    let core = Core::unlock(dir.path(), ns, &key).unwrap();
    assert_eq!(core.top_up_keys(1).err(), Some(Error::StoreCorrupt));
}

#[test]
fn an_older_copy_of_a_mutable_record_is_refused() {
    let mut alice = Dev::new(0xa1, 1);
    alice.core.lock();
    let snapshot = alice.dir.path().join("snapshot.db");
    std::fs::copy(namespace_path(alice.dir.path(), &alice.ns), &snapshot).unwrap();
    alice.core = Core::unlock(alice.dir.path(), alice.ns, &alice.key).unwrap();
    // Progress: the account and key records are rewritten at a newer generation.
    alice.core.top_up_keys(5).unwrap();
    alice.core.lock();

    let old_conn = Connection::open(&snapshot).unwrap();
    let old_account = row(&old_conn, "acct");
    let conn = raw(&alice);
    put_row(&conn, &old_account);
    drop(conn);
    assert_eq!(
        Core::unlock(alice.dir.path(), alice.ns, &alice.key).err(),
        Some(Error::StoreCorrupt),
        "rolling one record back must be caught at unlock"
    );
}

/// Freshness cannot be forged by editing the plaintext `gen` column of an old record to
/// match the manifest: the generation is part of the AEAD associated data.
#[test]
fn an_old_record_with_a_forged_generation_column_does_not_authenticate() {
    let mut alice = Dev::new(0xa1, 1);
    alice.core.lock();
    let snapshot = alice.dir.path().join("snapshot.db");
    std::fs::copy(namespace_path(alice.dir.path(), &alice.ns), &snapshot).unwrap();
    alice.core = Core::unlock(alice.dir.path(), alice.ns, &alice.key).unwrap();
    alice.core.top_up_keys(5).unwrap();
    alice.core.lock();

    let conn = raw(&alice);
    let current = row(&conn, "acct");
    let mut forged = row(&Connection::open(&snapshot).unwrap(), "acct");
    assert!(forged.2 < current.2);
    forged.2 = current.2; // claim to be as fresh as the manifest expects
    put_row(&conn, &forged);
    drop(conn);
    // The structure now looks right, so it opens, but the old state never decrypts.
    let core = Core::unlock(alice.dir.path(), alice.ns, &alice.key).unwrap();
    assert_eq!(core.top_up_keys(1).err(), Some(Error::StoreCorrupt));
}

/// Two records of the same type and generation cannot be exchanged: the record id is part
/// of the associated data.
#[test]
fn records_of_the_same_type_cannot_be_swapped() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    let mut carol = Dev::new(0xc1, 3);
    // One commit writes the trust records of Bob, Carol and Alice herself: same type, same
    // generation.
    alice
        .send(&mut [&mut bob, &mut carol], 1, "hello both")
        .unwrap();
    alice.core.lock();
    let conn = raw(&alice);
    let mut stmt = conn
        .prepare(
            "SELECT rtype, rid, gen, tag, nonce, ct FROM records WHERE rtype='trust' ORDER BY rid",
        )
        .unwrap();
    let rows: Vec<Row> = stmt
        .query_map([], |r| {
            Ok((
                r.get(0)?,
                r.get(1)?,
                r.get(2)?,
                r.get(3)?,
                r.get(4)?,
                r.get(5)?,
            ))
        })
        .unwrap()
        .map(|r| r.unwrap())
        .collect();
    drop(stmt);
    assert_eq!(rows.len(), 3);
    assert_eq!(
        rows[0].2, rows[1].2,
        "same generation, so only the id can object"
    );
    let mut a = rows[0].clone();
    let mut b = rows[1].clone();
    std::mem::swap(&mut a.4, &mut b.4);
    std::mem::swap(&mut a.5, &mut b.5);
    put_row(&conn, &a);
    put_row(&conn, &b);
    drop(conn);
    let core = Core::unlock(alice.dir.path(), alice.ns, &alice.key).unwrap();
    let results = [bob.user(), carol.user(), alice.user()].map(|u| core.trust_state(u));
    assert_eq!(
        results
            .iter()
            .filter(|r| **r == Err(Error::StoreCorrupt))
            .count(),
        2,
        "exactly the two swapped records must fail to load: {results:?}"
    );
}

/// A genuine storage failure (the database cannot grow) rolls the whole transaction back.
#[test]
fn a_real_sqlite_failure_leaves_the_old_state() {
    let mut alice = Dev::new(0xa1, 1);
    let before = alice.core.key_status().unwrap();
    let generation = alice.core.store_generation().unwrap();
    alice.core.exec_sql_for_test("PRAGMA max_page_count = 1");
    assert_eq!(alice.core.top_up_keys(100).err(), Some(Error::Storage));
    assert_eq!(alice.core.key_status().unwrap(), before);
    assert_eq!(alice.core.store_generation().unwrap(), generation);
    alice.reopen();
    assert_eq!(alice.core.key_status().unwrap(), before);
    assert_eq!(alice.core.store_generation().unwrap(), generation);
    assert_eq!(alice.core.top_up_keys(3).unwrap().len(), 3);
}

#[cfg(unix)]
#[test]
fn the_store_file_is_owner_only() {
    use std::os::unix::fs::PermissionsExt;
    let alice = Dev::new(0xa1, 1);
    let mode = std::fs::metadata(namespace_path(alice.dir.path(), &alice.ns))
        .unwrap()
        .permissions()
        .mode();
    assert_eq!(mode & 0o777, 0o600);
    // A directory the core has to create is private too.
    let created = alice.dir.path().join("made-by-core").join("nested");
    let ns = Namespace {
        user_id: [9; 16],
        generation: 1,
    };
    Core::unlock(&created, ns, &store_key(9)).unwrap().lock();
    for path in [created.clone(), created.parent().unwrap().to_path_buf()] {
        let dir_mode = std::fs::metadata(&path).unwrap().permissions().mode();
        assert_eq!(dir_mode & 0o077, 0, "{path:?}");
    }
}

/// Documents a limit rather than a feature: restoring an older WHOLE file is
/// self-consistent, so only a counter kept outside the file can notice.
#[test]
fn whole_file_rollback_is_only_visible_through_the_generation_counter() {
    let mut alice = Dev::new(0xa1, 1);
    alice.core.lock();
    let snapshot = alice.dir.path().join("snapshot.db");
    let path = namespace_path(alice.dir.path(), &alice.ns);
    std::fs::copy(&path, &snapshot).unwrap();
    alice.core = Core::unlock(alice.dir.path(), alice.ns, &alice.key).unwrap();
    alice.core.top_up_keys(5).unwrap();
    let latest = alice.core.store_generation().unwrap();
    alice.core.lock();

    std::fs::copy(&snapshot, &path).unwrap();
    let core = Core::unlock(alice.dir.path(), alice.ns, &alice.key).unwrap();
    assert!(
        core.store_generation().unwrap() < latest,
        "the platform layer can pin the counter and refuse a lower value"
    );
    alice.core = core;
}

#[test]
fn deleted_or_edited_rows_are_detected() {
    // A deleted mutable record.
    let alice = Dev::new(0xa1, 1);
    lock_for_raw_access(&alice);
    raw(&alice)
        .execute("DELETE FROM records WHERE rtype='acct'", [])
        .unwrap();
    assert_eq!(
        Core::unlock(alice.dir.path(), alice.ns, &alice.key).err(),
        Some(Error::StoreCorrupt)
    );

    // A deleted append-only record (a message): the header's count no longer matches.
    let mut a = Dev::new(0xa2, 3);
    let mut b = Dev::new(0xb2, 4);
    a.send(&mut [&mut b], 1, "x").unwrap();
    lock_for_raw_access(&a);
    raw(&a)
        .execute("DELETE FROM records WHERE rtype='msg'", [])
        .unwrap();
    assert_eq!(
        Core::unlock(a.dir.path(), a.ns, &a.key).err(),
        Some(Error::StoreCorrupt)
    );

    // A record of an unknown type.
    let c = Dev::new(0xa3, 5);
    lock_for_raw_access(&c);
    raw(&c)
        .execute(
            "INSERT INTO records (rtype, rid, gen, tag, nonce, ct) VALUES ('evil', x'00', 1, NULL, x'00', x'00')",
            [],
        )
        .unwrap();
    assert_eq!(
        Core::unlock(c.dir.path(), c.ns, &c.key).err(),
        Some(Error::StoreCorrupt)
    );

    // A flipped ciphertext bit: header damage fails unlock, record damage fails use.
    let d = Dev::new(0xa4, 6);
    lock_for_raw_access(&d);
    let conn = raw(&d);
    let mut dev_row = row(&conn, "dev");
    let last = dev_row.5.len() - 1;
    dev_row.5[last] ^= 1;
    put_row(&conn, &dev_row);
    drop(conn);
    let core = Core::unlock(d.dir.path(), d.ns, &d.key).unwrap();
    assert_eq!(core.has_device().err(), Some(Error::StoreCorrupt));
    core.lock();
    let conn = raw(&d);
    let mut header = row(&conn, "hdr");
    header.5[0] ^= 1;
    put_row(&conn, &header);
    drop(conn);
    assert_eq!(
        Core::unlock(d.dir.path(), d.ns, &d.key).err(),
        Some(Error::UnlockFailed)
    );
}

#[test]
fn stored_rows_do_not_contain_plaintext_or_identifiers() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    alice
        .send(&mut [&mut bob], 1, "SECRET-BODY-MARKER-12345")
        .unwrap();
    alice.core.lock();
    let bytes = std::fs::read(namespace_path(alice.dir.path(), &alice.ns)).unwrap();
    let contains = |needle: &[u8]| bytes.windows(needle.len()).any(|w| w == needle);
    assert!(!contains(b"SECRET-BODY-MARKER-12345"));
    assert!(!contains(&CONV), "conversation id must not appear in clear");
    assert!(
        !contains(&bob.user()),
        "peer user id must not appear in clear"
    );
    assert!(!contains(&bob.identity().curve25519));
    assert!(!contains(&alice.identity().ed25519));
}

#[test]
fn wipe_deletes_the_namespace_and_locks_the_handle() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    alice.send(&mut [&mut bob], 1, "gone soon").unwrap();
    let stale = alice.core.clone();
    let path = namespace_path(alice.dir.path(), &alice.ns);
    assert!(path.exists());
    alice.core.wipe().unwrap();
    assert!(!path.exists());
    assert_eq!(stale.has_device().err(), Some(Error::Locked));
    assert_eq!(alice.core.wipe().err(), Some(Error::Locked));
    // A new unlock starts an empty namespace.
    let fresh = Core::unlock(alice.dir.path(), alice.ns, &alice.key).unwrap();
    assert!(!fresh.has_device().unwrap());
    assert_eq!(
        fresh.list_messages(CONV, None, 10).err(),
        Some(Error::NoDevice)
    );
    alice.core = fresh;
}

#[test]
fn wipe_namespace_needs_no_key_and_tolerates_missing_files() {
    let alice = Dev::new(0xa1, 1);
    alice.core.lock();
    let path = namespace_path(alice.dir.path(), &alice.ns);
    assert!(path.exists());
    Core::wipe_namespace(alice.dir.path(), alice.ns).unwrap();
    assert!(!path.exists());
    Core::wipe_namespace(alice.dir.path(), alice.ns).unwrap();
}

#[test]
fn create_device_checks_the_namespace_and_only_runs_once() {
    let dir = TestDir::new();
    let ns = Namespace {
        user_id: [7; 16],
        generation: 3,
    };
    let core = Core::unlock(dir.path(), ns, &store_key(7)).unwrap();
    assert_eq!(core.namespace().unwrap(), ns);
    assert!(!core.has_device().unwrap());
    assert_eq!(core.public_identity().unwrap(), None);
    assert_eq!(
        core.create_device([8; 16], 3, 5).err(),
        Some(Error::NamespaceMismatch)
    );
    assert_eq!(
        core.create_device([7; 16], 4, 5).err(),
        Some(Error::NamespaceMismatch)
    );
    assert_eq!(
        core.create_device([7; 16], 3, 0).err(),
        Some(Error::InvalidInput)
    );
    assert_eq!(
        core.create_device([7; 16], 3, MAX_KEYS_PER_CALL + 1).err(),
        Some(Error::InvalidInput)
    );
    let created = core.create_device([7; 16], 3, 5).unwrap();
    assert_eq!(core.public_identity().unwrap(), Some(created.identity));
    assert_eq!(
        core.create_device([7; 16], 3, 5).err(),
        Some(Error::DeviceExists)
    );
    // Curve and Ed25519 identities are different keys.
    assert_ne!(created.identity.curve25519, created.identity.ed25519);
}

#[test]
fn operations_are_serialized_across_threads() {
    let alice = Dev::new(0xa1, 1);
    let core = alice.core.clone();
    let mut handles = Vec::new();
    for _ in 0..8 {
        let core = core.clone();
        handles.push(std::thread::spawn(move || {
            let mut ids = Vec::new();
            for _ in 0..5 {
                ids.extend(core.top_up_keys(2).unwrap().into_iter().map(|k| k.key_id));
            }
            ids
        }));
    }
    let mut all: Vec<u64> = handles
        .into_iter()
        .flat_map(|h| h.join().unwrap())
        .collect();
    assert_eq!(all.len(), 80);
    all.sort_unstable();
    let before = all.len();
    all.dedup();
    assert_eq!(
        all.len(),
        before,
        "two concurrent operations handed out the same key id"
    );
    assert_eq!(core.key_status().unwrap().pending, 80);
}

#[test]
fn every_error_has_a_fixed_code_equal_to_its_text() {
    let all = [
        Error::Locked,
        Error::UnlockFailed,
        Error::StoreInUse,
        Error::Storage,
        Error::StoreCorrupt,
        Error::NamespaceMismatch,
        Error::DeviceExists,
        Error::NoDevice,
        Error::NotPublished,
        Error::InvalidInput,
        Error::BodyInvalid,
        Error::BodyTooLarge,
        Error::InvalidSignature,
        Error::InvalidKey,
        Error::UnknownKey,
        Error::TooManyKeys,
        Error::SenderMismatch,
        Error::MissingClaimedKey,
        Error::TrustHardStop,
        Error::UnknownPeer,
        Error::NothingToAccept,
        Error::ClientMessageIdReused,
        Error::CiphertextTooLarge,
        Error::EncryptFailed,
        Error::DecryptFailed,
        Error::InvalidEnvelope,
        Error::EnvelopeMismatch,
        Error::Replay,
        Error::Internal,
    ];
    let mut seen = std::collections::HashSet::new();
    for e in all {
        assert_eq!(e.to_string(), e.code());
        assert!(seen.insert(e.code()), "duplicate code {}", e.code());
        assert!(e.code().chars().all(|c| c.is_ascii_lowercase() || c == '_'));
    }
}

#[test]
fn debug_output_never_prints_plaintext_or_secrets() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    let legs = alice
        .send(&mut [&mut bob], 1, "DEBUG-MARKER-PLAINTEXT")
        .unwrap();
    let msg = bob.receive(&alice, &legs[0], 1, 1).unwrap();
    let printed = format!("{msg:?} {:?} {:?} {:?}", legs[0], alice.core, legs);
    assert!(!printed.contains("DEBUG-MARKER-PLAINTEXT"));
    assert!(printed.contains("redacted"));
}
