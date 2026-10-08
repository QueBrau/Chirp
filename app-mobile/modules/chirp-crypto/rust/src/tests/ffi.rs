//! The uniffi boundary (`crate::ffi`): the same two-account flow the iOS smoke test runs,
//! the rollback pin, error codes, and input hygiene. Runs on the host, so what the
//! Swift module relies on is checked before any simulator is involved.

use std::sync::Arc;

use super::common::TestDir;
use crate::envelope::uuid_to_text;
use crate::ffi::*;
use crate::{Error, MAX_LEG_BYTES};

const CONV: &str = "c0c0c0c0-c0c0-c0c0-c0c0-c0c0c0c0c0c0";

fn uuid(byte: u8) -> String {
    uuid_to_text(&[byte; 16])
}

struct Peer {
    dir: TestDir,
    key: [u8; 32],
    user: String,
    device: String,
    core: Arc<CryptoCore>,
    created: CreatedDevice,
}

impl Peer {
    fn new(user: u8, device: u8) -> Peer {
        let dir = TestDir::new();
        let key = [device; 32];
        let core = CryptoCore::unlock(
            dir.path().to_string_lossy().into_owned(),
            uuid(user),
            1,
            key.to_vec(),
            None,
        )
        .unwrap();
        let created = core.create_device(5).unwrap();
        let mut ids: Vec<u64> = created.one_time_keys.iter().map(|k| k.key_id).collect();
        ids.push(created.fallback_key.key_id);
        core.mark_published(uuid(device), ids).unwrap();
        Peer {
            dir,
            key,
            user: uuid(user),
            device: uuid(device),
            core,
            created,
        }
    }

    fn dir_text(&self) -> String {
        self.dir.path().to_string_lossy().into_owned()
    }

    fn directory(&self) -> DirectoryDevice {
        DirectoryDevice {
            user_id: self.user.clone(),
            device_id: self.device.clone(),
            generation: 1,
            identity_curve25519: self.created.identity.curve25519.clone(),
            identity_ed25519: self.created.identity.ed25519.clone(),
            binding_signature: self.created.binding_signature.clone(),
            approval: None,
        }
    }

    fn expected(&self, from: &Peer, client: u8) -> ExpectedEnvelope {
        ExpectedEnvelope {
            conversation_id: CONV.to_owned(),
            client_message_id: uuid(client),
            sender_user_id: from.user.clone(),
            sender_device_id: from.device.clone(),
            recipient_user_id: self.user.clone(),
            recipient_device_id: self.device.clone(),
        }
    }

    fn unlock_again(&self, pin: Option<u64>) -> Result<Arc<CryptoCore>, CryptoError> {
        CryptoCore::unlock(
            self.dir_text(),
            self.user.clone(),
            1,
            self.key.to_vec(),
            pin,
        )
    }
}

fn code(e: CryptoError) -> String {
    e.to_string()
}

#[test]
fn two_accounts_round_trip_through_the_boundary() {
    let a = Peer::new(0xa1, 0x01);
    let b = Peer::new(0xb1, 0x02);

    assert_eq!(
        a.core.observe_directory(b.user.clone(), vec![b.directory()]),
        Ok(TrustState::Trusted)
    );
    assert_eq!(
        b.core.observe_directory(a.user.clone(), vec![a.directory()]),
        Ok(TrustState::Trusted)
    );

    // A -> B: first message, so B's published one-time key is claimed.
    let legs = a
        .core
        .encrypt(
            CONV.to_owned(),
            uuid(0x11),
            "hello from a".to_owned(),
            a.directory(),
            vec![Recipient {
                device: b.directory(),
                claimed_key: Some(b.created.one_time_keys[0].clone()),
            }],
        )
        .unwrap();
    assert_eq!(legs.len(), 1);
    assert_eq!(legs[0].recipient_device_id, b.device);
    assert_eq!(legs[0].olm_type, 0, "first message is a prekey message");
    assert!(legs[0].ciphertext.len() % 4 == 0, "padded base64 out");

    let got = b
        .core
        .decrypt(
            uuid(0x31),
            legs[0].clone(),
            a.directory(),
            b.expected(&a, 0x11),
        )
        .unwrap();
    assert_eq!(got.body, "hello from a");
    assert_eq!(got.direction, Direction::Incoming);
    assert_eq!(got.kind, "text");
    assert_eq!(got.sender_trust, TrustState::Trusted);
    assert_eq!(got.message_id.as_deref(), Some(uuid(0x31).as_str()));
    assert_eq!(got.conversation_id, CONV);
    assert_eq!(got.client_message_id, uuid(0x11));
    assert_eq!(got.sender_user_id, a.user);
    assert_eq!(got.sender_device_id, a.device);

    // B -> A on the normal path: B now has a session, so no claimed key is needed.
    let reply = b
        .core
        .encrypt(
            CONV.to_owned(),
            uuid(0x12),
            "hello back".to_owned(),
            b.directory(),
            vec![Recipient {
                device: a.directory(),
                claimed_key: None,
            }],
        )
        .unwrap();
    let back = a
        .core
        .decrypt(
            uuid(0x32),
            reply[0].clone(),
            b.directory(),
            a.expected(&b, 0x12),
        )
        .unwrap();
    assert_eq!(back.body, "hello back");
    assert_eq!(back.sender_trust, TrustState::Trusted);

    // History on both sides, newest first, both directions.
    let on_a = a.core.list_messages(CONV.to_owned(), None, 10).unwrap();
    assert_eq!(on_a.len(), 2);
    assert_eq!(on_a[0].body, "hello back");
    assert_eq!(on_a[0].direction, Direction::Incoming);
    assert_eq!(on_a[1].body, "hello from a");
    assert_eq!(on_a[1].direction, Direction::Outgoing);
    assert_eq!(on_a[1].message_id, None);
    let on_b = b.core.list_messages(CONV.to_owned(), None, 10).unwrap();
    assert_eq!(on_b.len(), 2);
    assert_eq!(on_b[0].body, "hello back");
    assert_eq!(on_b[0].direction, Direction::Outgoing);
    assert_eq!(on_b[1].body, "hello from a");
    assert_eq!(on_b[1].direction, Direction::Incoming);

    // Idempotent retry of the same server message id returns the stored message.
    let again = b
        .core
        .decrypt(
            uuid(0x31),
            legs[0].clone(),
            a.directory(),
            b.expected(&a, 0x11),
        )
        .unwrap();
    assert_eq!(again, got);
    assert_eq!(b.core.list_messages(CONV.to_owned(), None, 10).unwrap().len(), 2);

    // Safety numbers agree from both sides.
    let ed = |p: &Peer| vec![p.created.identity.ed25519.clone()];
    let from_a = safety_number(a.user.clone(), ed(&a), b.user.clone(), ed(&b)).unwrap();
    let from_b = safety_number(b.user.clone(), ed(&b), a.user.clone(), ed(&a)).unwrap();
    assert_eq!(from_a, from_b);
    assert_eq!(from_a.len(), 60);
    assert!(from_a.chars().all(|c| c.is_ascii_digit()));
}

#[test]
fn a_changed_identity_is_a_hard_stop_across_the_boundary() {
    let a = Peer::new(0xa1, 0x01);
    let b = Peer::new(0xb1, 0x02);
    assert_eq!(
        a.core.observe_directory(b.user.clone(), vec![b.directory()]),
        Ok(TrustState::Trusted)
    );
    // Same user, same device id, different keys: the identity changed.
    let imposter = {
        let dir = TestDir::new();
        let core = CryptoCore::unlock(
            dir.path().to_string_lossy().into_owned(),
            b.user.clone(),
            1,
            vec![9; 32],
            None,
        )
        .unwrap();
        let created = core.create_device(1).unwrap();
        core.mark_published(b.device.clone(), vec![created.fallback_key.key_id])
            .unwrap();
        (dir, core.clone(), created)
    };
    let swapped = DirectoryDevice {
        identity_curve25519: imposter.2.identity.curve25519.clone(),
        identity_ed25519: imposter.2.identity.ed25519.clone(),
        binding_signature: imposter.2.binding_signature.clone(),
        ..b.directory()
    };
    assert_eq!(
        a.core.observe_directory(b.user.clone(), vec![swapped.clone()]),
        Ok(TrustState::IdentityChanged)
    );
    assert_eq!(
        a.core.trust_state(b.user.clone()),
        Ok(TrustState::IdentityChanged)
    );
    let blocked = a.core.encrypt(
        CONV.to_owned(),
        uuid(0x11),
        "nope".to_owned(),
        a.directory(),
        vec![Recipient {
            device: swapped,
            claimed_key: Some(imposter.2.fallback_key.clone()),
        }],
    );
    assert_eq!(blocked, Err(CryptoError::TrustHardStop));
    assert_eq!(a.core.accept_identity_change(b.user.clone()), Ok(()));
    assert_eq!(a.core.trust_state(b.user.clone()), Ok(TrustState::Trusted));
}

#[test]
fn every_core_error_keeps_its_code_through_the_boundary() {
    // If the core gains a variant, `From<Error>` stops compiling; add it here too.
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
        let mapped = CryptoError::from(e);
        assert_eq!(mapped.to_string(), e.code());
        assert!(seen.insert(mapped.to_string()), "codes are unique");
    }
    assert_eq!(code(CryptoError::StoreRolledBack), "store_rolled_back");
    assert_eq!(code(CryptoError::KeychainFailed), "keychain_failed");
    assert!(!seen.contains("store_rolled_back") && !seen.contains("keychain_failed"));
}

#[test]
fn the_rollback_pin_refuses_only_a_lower_store_generation() {
    let a = Peer::new(0xa1, 0x01);
    let generation = a.core.store_generation().unwrap();
    assert!(generation >= 2, "create_device and mark_published committed");
    a.core.lock();

    // No pin, an equal pin, and a pin that lags behind (a crash between commit and pin).
    for pin in [None, Some(0), Some(generation - 1), Some(generation)] {
        let core = a.unlock_again(pin).unwrap();
        assert_eq!(core.store_generation().unwrap(), generation);
        core.lock();
    }

    // A pin ahead of the store means an older copy was restored.
    assert_eq!(
        a.unlock_again(Some(generation + 1)).err(),
        Some(CryptoError::StoreRolledBack)
    );
    // The refusal closed the store again, so the next honest unlock is not store_in_use.
    let ok = a.unlock_again(Some(generation)).unwrap();
    assert!(ok.is_unlocked());
}

#[test]
fn several_handles_may_be_open_but_not_the_same_namespace_twice() {
    let a = Peer::new(0xa1, 0x01);
    let b = Peer::new(0xb1, 0x02);
    assert!(a.core.is_unlocked() && b.core.is_unlocked());
    assert_eq!(a.unlock_again(None).err(), Some(CryptoError::StoreInUse));

    a.core.lock();
    a.core.lock(); // idempotent
    assert!(!a.core.is_unlocked());
    assert!(b.core.is_unlocked(), "locking one handle leaves the other alone");
    assert_eq!(a.core.has_device().err(), Some(CryptoError::Locked));
    assert_eq!(a.core.key_status().err(), Some(CryptoError::Locked));

    let reopened = a.unlock_again(None).unwrap();
    assert!(reopened.has_device().unwrap());
    assert_eq!(
        reopened.public_identity().unwrap(),
        Some(a.created.identity.clone())
    );
}

#[test]
fn a_wrong_or_short_store_key_cannot_open_the_store() {
    let a = Peer::new(0xa1, 0x01);
    a.core.lock();
    assert_eq!(
        CryptoCore::unlock(a.dir_text(), a.user.clone(), 1, vec![0xee; 32], None).err(),
        Some(CryptoError::UnlockFailed)
    );
    assert_eq!(
        CryptoCore::unlock(a.dir_text(), a.user.clone(), 1, vec![0xee; 31], None).err(),
        Some(CryptoError::InvalidInput)
    );
}

#[test]
fn text_that_is_not_canonical_is_invalid_input() {
    let a = Peer::new(0xa1, 0x01);
    let b = Peer::new(0xb1, 0x02);
    let upper = uuid(0xb1).to_uppercase();
    for bad in [
        upper.as_str(),
        "not-a-uuid",
        "",
        "b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1",
    ] {
        assert_eq!(
            a.core.trust_state(bad.to_owned()),
            Err(CryptoError::InvalidInput),
            "{bad:?}"
        );
        assert_eq!(namespace_label(bad.to_owned(), 1), Err(CryptoError::InvalidInput));
    }

    let mut dev = b.directory();
    dev.identity_ed25519 = "AAAA".to_owned(); // 3 bytes, not 32
    assert_eq!(
        a.core.observe_directory(b.user.clone(), vec![dev]),
        Err(CryptoError::InvalidInput)
    );
    let mut dev = b.directory();
    dev.binding_signature = "!!!!".repeat(22);
    assert_eq!(
        a.core.observe_directory(b.user.clone(), vec![dev]),
        Err(CryptoError::InvalidInput)
    );

    let huge = "A".repeat(MAX_LEG_BYTES.div_ceil(3) * 4 + 4);
    let leg = Leg {
        recipient_device_id: b.device.clone(),
        olm_type: 0,
        ciphertext: huge,
    };
    assert_eq!(
        a.core
            .decrypt(uuid(0x31), leg, b.directory(), a.expected(&b, 0x11))
            .err(),
        Some(CryptoError::InvalidInput)
    );
}

#[test]
fn base64_is_emitted_padded_and_accepted_either_way() {
    let a = Peer::new(0xa1, 0x01);
    let b = Peer::new(0xb1, 0x02);
    let ed = &a.created.identity.ed25519;
    assert_eq!(ed.len(), 44);
    assert!(ed.ends_with('='), "32 bytes encode with one pad character");
    let unpadded = ed.trim_end_matches('=').to_owned();
    assert_eq!(unpadded.len(), 43);

    let with_pad = safety_number(a.user.clone(), vec![ed.clone()], b.user.clone(), vec![b.created.identity.ed25519.clone()]);
    let without = safety_number(a.user.clone(), vec![unpadded], b.user.clone(), vec![b.created.identity.ed25519.clone()]);
    assert_eq!(with_pad, without);
    assert!(with_pad.is_ok());
    assert_eq!(
        safety_number(a.user.clone(), vec![ed.clone()], a.user.clone(), vec![ed.clone()]),
        Err(CryptoError::InvalidInput),
        "a user's number with themselves is refused by the core"
    );
}

#[test]
fn labels_existence_and_wipe() {
    let a = Peer::new(0xa1, 0x01);
    assert_eq!(
        namespace_label(a.user.clone(), 7).unwrap(),
        format!("{}-g7", "a1".repeat(16))
    );
    assert_eq!(store_exists(a.dir_text(), a.user.clone(), 1), Ok(true));
    assert_eq!(store_exists(a.dir_text(), uuid(0xb1), 1), Ok(false));
    assert_eq!(store_exists(a.dir_text(), a.user.clone(), 2), Ok(false));

    a.core.lock();
    wipe_namespace(a.dir_text(), a.user.clone(), 1).unwrap();
    wipe_namespace(a.dir_text(), a.user.clone(), 1).unwrap(); // idempotent
    assert_eq!(store_exists(a.dir_text(), a.user.clone(), 1), Ok(false));
    // A fresh unlock creates an empty store: no device, generation restarts.
    let fresh = a.unlock_again(None).unwrap();
    assert_eq!(fresh.has_device(), Ok(false));
    assert_eq!(fresh.public_identity(), Ok(None));
}

#[test]
fn debug_output_never_shows_plaintext_or_ciphertext() {
    let msg = PlaintextMessage {
        seq: 1,
        direction: Direction::Incoming,
        message_id: None,
        conversation_id: CONV.to_owned(),
        client_message_id: uuid(1),
        sender_user_id: uuid(2),
        sender_device_id: uuid(3),
        kind: "text".to_owned(),
        body: "secret words".to_owned(),
        sender_trust: TrustState::Trusted,
        stored_at_ms: 0,
    };
    let leg = Leg {
        recipient_device_id: uuid(4),
        olm_type: 1,
        ciphertext: "c2VjcmV0".to_owned(),
    };
    let shown = format!("{msg:?} {leg:?}");
    assert!(!shown.contains("secret words"));
    assert!(!shown.contains("c2VjcmV0"));
}
