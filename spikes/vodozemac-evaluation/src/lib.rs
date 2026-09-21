//! Disposable protocol experiments. Not an application crypto implementation.
//! No network, storage, production accounts, secret-key output, or native binding.

use base64::{Engine, engine::general_purpose::STANDARD};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use vodozemac::olm::{Account, OlmMessage, SessionConfig};
#[cfg(test)]
use vodozemac::{
    megolm::{GroupSession, InboundGroupSession, SessionConfig as GroupConfig},
    olm::Session,
};

const ALGORITHM: &str = "chirp-evaluation-only/olm-v1";
#[cfg(test)]
const GROUP_ALGORITHM: &str = "chirp-evaluation-only/megolm-v1";
const CONVERSATION: &str = "00000000-0000-4000-8000-000000000001";
const SENDER: &str = "00000000-0000-4000-8000-000000000002";
const RECIPIENT: &str = "00000000-0000-4000-8000-000000000003";

/// All fields are inside the encrypted plaintext. This is an experimental
/// application framing rule, not AAD supplied by the library or a proposed API.
#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct Context {
    algorithm: String,
    version: u8,
    conversation: String,
    sender_device: String,
    recipient_device: String,
    kind: String,
    epoch: u32,
}

impl Context {
    fn pairwise() -> Self {
        Self {
            algorithm: ALGORITHM.into(),
            version: 1,
            conversation: CONVERSATION.into(),
            sender_device: SENDER.into(),
            recipient_device: RECIPIENT.into(),
            kind: "message".into(),
            epoch: 0,
        }
    }
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Payload {
    context: Context,
    body: String,
}

fn payload(context: &Context, body: &str) -> Vec<u8> {
    serde_json::to_vec(&Payload {
        context: context.clone(),
        body: body.into(),
    })
    .unwrap()
}

#[cfg(test)]
fn checked_body(plaintext: &[u8], expected: &Context) -> Result<String, &'static str> {
    let parsed: Payload = serde_json::from_slice(plaintext).map_err(|_| "invalid_payload")?;
    if &parsed.context != expected {
        return Err("context_mismatch");
    }
    Ok(parsed.body)
}

/// The JSON is itself standard padded base64 for Chirp's opaque-byte transport.
/// Olm's subtype must be carried; Chirp's outer "signal" literal cannot supply it.
fn wire(message: &OlmMessage, recipient: &str) -> Value {
    let (subtype, body) = message.to_parts();
    json!({"evaluation_version": 1, "algorithm": ALGORITHM,
           "recipient_device": recipient, "olm_type": subtype,
           "body": base64::engine::general_purpose::STANDARD_NO_PAD.encode(body)})
}

fn transport(value: &Value) -> String {
    STANDARD.encode(serde_json::to_vec(value).unwrap())
}

#[cfg(test)]
fn start(alice: &Account, bob: &mut Account, plaintext: &[u8]) -> (Session, Session) {
    bob.generate_one_time_keys(1);
    let key = *bob.one_time_keys().values().next().unwrap();
    let mut outbound = alice
        .create_outbound_session(SessionConfig::version_1(), bob.curve25519_key(), key)
        .unwrap();
    bob.mark_keys_as_published();
    let first = outbound.encrypt(plaintext).unwrap();
    let OlmMessage::PreKey(first) = first else {
        panic!("initial message must be prekey")
    };
    let inbound = bob
        .create_inbound_session(SessionConfig::version_1(), alice.curve25519_key(), &first)
        .unwrap();
    assert_eq!(inbound.plaintext, plaintext);
    assert_eq!(outbound.session_id(), inbound.session.session_id());
    let mut inbound = inbound.session;
    let reply = inbound.encrypt(b"acknowledgement").unwrap();
    assert_eq!(outbound.decrypt(&reply).unwrap(), b"acknowledgement");
    (outbound, inbound)
}

/// Only public keys, signatures and synthetic ciphertext leave this process.
/// The "shape_only" registration is deliberately NOT a semantic key mapping.
pub fn public_specimen() -> Value {
    let alice = Account::new();
    let mut bob = Account::new();
    bob.generate_one_time_keys(1);
    bob.generate_fallback_key();
    let otk = *bob.one_time_keys().values().next().unwrap();
    let fallback = *bob.fallback_key().values().next().unwrap();
    let signature = bob.sign(fallback.as_bytes());
    let mut outbound = alice
        .create_outbound_session(SessionConfig::version_1(), bob.curve25519_key(), otk)
        .unwrap();
    let first = outbound
        .encrypt(payload(&Context::pairwise(), "synthetic probe"))
        .unwrap();
    let first_wire = transport(&wire(&first, RECIPIENT));
    let long_body = "\u{1f642}".repeat(10_000);
    let long_message = outbound
        .encrypt(payload(&Context::pairwise(), &long_body))
        .unwrap();
    let long_wire = wire(&long_message, RECIPIENT);
    let single = transport(&long_wire);
    let mut other = Account::new();
    other.generate_one_time_keys(1);
    let mut other_session = alice
        .create_outbound_session(
            SessionConfig::version_1(),
            other.curve25519_key(),
            *other.one_time_keys().values().next().unwrap(),
        )
        .unwrap();
    let other_message = other_session
        .encrypt(payload(&Context::pairwise(), &long_body))
        .unwrap();
    let two = transport(
        &json!({"evaluation_version":1,"envelopes":[long_wire,wire(&other_message, "other-device")]}),
    );
    let raw = STANDARD.encode(long_message.to_parts().1);
    json!({
        "candidate": "vodozemac", "version": "0.11.0", "host_only": true,
        "curve_identity_b64": STANDARD.encode(bob.curve25519_key().as_bytes()),
        "signing_identity_b64": STANDARD.encode(bob.ed25519_key().as_bytes()),
        "native_unpadded_curve_b64": bob.curve25519_key().to_base64(),
        "shape_only_registration": {
            "registration_id": 1,
            "identity_key_b64": STANDARD.encode(bob.curve25519_key().as_bytes()),
            "signed_prekey": {"key_id": 1,
                "public_key_b64": STANDARD.encode(fallback.as_bytes()),
                "signature_b64": STANDARD.encode(signature.to_bytes())},
            "one_time_prekeys": [{"key_id": 2, "public_key_b64": STANDARD.encode(otk.as_bytes())}]
        },
        "message": {"sender_device_id": SENDER, "message_type": "signal", "ciphertext_b64": first_wire},
        "max_text_single_device_b64": single,
        "max_text_two_devices_b64": two,
        "lengths": {"text_unicode_scalars":10000,"text_utf8_bytes":long_body.len(),
            "raw_single_olm_transport_chars":raw.len(),
            "single_device_transport_chars":single.len(), "two_device_transport_chars":two.len()},
        "no_private_keys_or_plaintext_in_output": true
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use vodozemac::{Curve25519PublicKey, Ed25519PublicKey, megolm::MegolmMessage};

    #[test]
    fn real_olm_prekey_then_normal_roundtrip() {
        let alice = Account::new();
        let (mut a, mut b) = start(&alice, &mut Account::new(), b"initial");
        let message = a.encrypt(b"next").unwrap();
        assert!(matches!(message, OlmMessage::Normal(_)));
        assert_eq!(b.decrypt(&message).unwrap(), b"next");
    }

    #[test]
    fn olm_subtype_is_required_and_unknown_or_signal_subtypes_are_rejected() {
        let alice = Account::new();
        let (mut a, mut b) = start(&alice, &mut Account::new(), b"initial");
        let message = a.encrypt(b"next").unwrap();
        let (kind, bytes) = message.to_parts();
        assert_eq!(kind, 1);
        for wrong in [2, 3, 255] {
            assert!(OlmMessage::from_parts(wrong, &bytes).is_err());
        }
        let decoded = OlmMessage::from_parts(kind, &bytes).unwrap();
        assert_eq!(b.decrypt(&decoded).unwrap(), b"next");
    }

    #[test]
    fn experimental_transport_serialization_roundtrips_to_real_decryption() {
        let alice = Account::new();
        let (mut a, mut b) = start(&alice, &mut Account::new(), b"initial");
        let context = Context::pairwise();
        let message = a.encrypt(payload(&context, "framed body")).unwrap();
        let encoded = transport(&wire(&message, RECIPIENT));
        let decoded: Value = serde_json::from_slice(&STANDARD.decode(encoded).unwrap()).unwrap();
        assert_eq!(decoded["algorithm"], ALGORITHM);
        assert_eq!(decoded["evaluation_version"], 1);
        assert_eq!(decoded["recipient_device"], RECIPIENT);
        let ciphertext = base64::engine::general_purpose::STANDARD_NO_PAD
            .decode(decoded["body"].as_str().unwrap())
            .unwrap();
        let native =
            OlmMessage::from_parts(decoded["olm_type"].as_u64().unwrap() as usize, &ciphertext)
                .unwrap();
        let plaintext = b.decrypt(&native).unwrap();
        assert_eq!(checked_body(&plaintext, &context).unwrap(), "framed body");
    }

    #[test]
    fn separate_device_sessions_do_not_cross_decrypt() {
        let alice = Account::new();
        let (mut a1, mut b1) = start(&alice, &mut Account::new(), b"device one");
        let (mut a2, mut b2) = start(&alice, &mut Account::new(), b"device two");
        assert_ne!(a1.session_id(), a2.session_id());
        let m1 = a1.encrypt(b"one").unwrap();
        let m2 = a2.encrypt(b"two").unwrap();
        assert!(b2.decrypt(&m1).is_err());
        assert!(b1.decrypt(&m2).is_err());
        assert_eq!(b1.decrypt(&m1).unwrap(), b"one");
        assert_eq!(b2.decrypt(&m2).unwrap(), b"two");
    }

    #[test]
    fn signatures_need_the_separate_ed25519_identity() {
        let account = Account::new();
        let bytes = b"synthetic published key and device binding";
        let signature = account.sign(bytes);
        assert!(account.ed25519_key().verify(bytes, &signature).is_ok());
        assert!(
            Account::new()
                .ed25519_key()
                .verify(bytes, &signature)
                .is_err()
        );
        assert!(
            account
                .ed25519_key()
                .verify(b"changed binding", &signature)
                .is_err()
        );
        assert_ne!(
            account.curve25519_key().as_bytes(),
            account.ed25519_key().as_bytes()
        );
        // Equal byte lengths do not make the existing DH identity a signing key.
        if let Ok(wrong_key) = Ed25519PublicKey::from_slice(account.curve25519_key().as_bytes()) {
            assert!(wrong_key.verify(bytes, &signature).is_err());
        }
    }

    #[test]
    fn server_claim_is_not_local_secret_deletion_and_second_inbound_is_rejected() {
        let alice = Account::new();
        let mut bob = Account::new();
        bob.generate_one_time_keys(1);
        let key = *bob.one_time_keys().values().next().unwrap();
        bob.mark_keys_as_published();
        assert!(bob.one_time_keys().is_empty()); // unpublished list only
        let mut a = alice
            .create_outbound_session(SessionConfig::version_1(), bob.curve25519_key(), key)
            .unwrap();
        let OlmMessage::PreKey(message) = a.encrypt(b"delayed initial").unwrap() else {
            panic!()
        };
        assert!(
            bob.create_inbound_session(
                SessionConfig::version_1(),
                alice.curve25519_key(),
                &message
            )
            .is_ok()
        );
        assert!(
            bob.create_inbound_session(
                SessionConfig::version_1(),
                alice.curve25519_key(),
                &message
            )
            .is_err()
        );
    }

    #[test]
    fn fallback_is_real_distinct_protocol_material_not_a_signed_prekey_assumption() {
        let alice = Account::new();
        let mut bob = Account::new();
        assert!(bob.one_time_keys().is_empty());
        bob.generate_fallback_key();
        let fallback = *bob.fallback_key().values().next().unwrap();
        for _ in 0..2 {
            let mut a = alice
                .create_outbound_session(SessionConfig::version_1(), bob.curve25519_key(), fallback)
                .unwrap();
            let OlmMessage::PreKey(message) = a.encrypt(b"fallback").unwrap() else {
                panic!()
            };
            assert!(
                bob.create_inbound_session(
                    SessionConfig::version_1(),
                    alice.curve25519_key(),
                    &message
                )
                .is_ok()
            );
        }
    }

    #[test]
    fn altered_ciphertext_fails_without_poisoning_next_message() {
        let alice = Account::new();
        let (mut a, mut b) = start(&alice, &mut Account::new(), b"initial");
        let valid = a.encrypt(b"intact").unwrap();
        let (kind, mut bytes) = valid.to_parts();
        let last = bytes.len() - 1;
        bytes[last] ^= 1;
        let changed = OlmMessage::from_parts(kind, &bytes).unwrap();
        assert!(b.decrypt(&changed).is_err());
        assert_eq!(b.decrypt(&valid).unwrap(), b"intact");
        assert!(b.decrypt(&valid).is_err()); // Olm replay rejected
    }

    #[test]
    fn all_application_context_fields_are_checked_after_authenticated_decryption() {
        let expected = Context::pairwise();
        for field in 0..7 {
            let alice = Account::new();
            let (mut a, mut b) = start(&alice, &mut Account::new(), b"initial");
            let message = a.encrypt(payload(&expected, "private body")).unwrap();
            let decrypted = b.decrypt(&message).unwrap();
            let mut changed = expected.clone();
            match field {
                0 => changed.algorithm = "unknown".into(),
                1 => changed.version = 0,
                2 => changed.conversation = "other".into(),
                3 => changed.sender_device = "other".into(),
                4 => changed.recipient_device = "other".into(),
                5 => changed.kind = "distribution".into(),
                _ => changed.epoch = 1,
            }
            assert!(checked_body(&decrypted, &changed).is_err());
            assert_eq!(checked_body(&decrypted, &expected).unwrap(), "private body");
        }
    }

    #[test]
    fn wrong_prekey_sender_identity_is_rejected() {
        let alice = Account::new();
        let mut bob = Account::new();
        bob.generate_one_time_keys(1);
        let key = *bob.one_time_keys().values().next().unwrap();
        let mut a = alice
            .create_outbound_session(SessionConfig::version_1(), bob.curve25519_key(), key)
            .unwrap();
        let OlmMessage::PreKey(message) = a.encrypt(b"initial").unwrap() else {
            panic!()
        };
        assert!(
            bob.create_inbound_session(
                SessionConfig::version_1(),
                Account::new().curve25519_key(),
                &message
            )
            .is_err()
        );
        assert!(
            bob.create_inbound_session(
                SessionConfig::version_1(),
                alice.curve25519_key(),
                &message
            )
            .is_ok()
        );
    }

    #[test]
    fn state_pickle_restore_is_real_but_not_a_secure_storage_proof() {
        let alice = Account::new();
        let (mut a, b) = start(&alice, &mut Account::new(), b"initial");
        let pickle = b.pickle().encrypt(&[7; 32]);
        assert!(vodozemac::olm::SessionPickle::from_encrypted(&pickle, &[8; 32]).is_err());
        let mut restored = Session::from_pickle(
            vodozemac::olm::SessionPickle::from_encrypted(&pickle, &[7; 32]).unwrap(),
        );
        assert_eq!(
            restored
                .decrypt(&a.encrypt(b"after restore").unwrap())
                .unwrap(),
            b"after restore"
        );
        let encoded = alice.pickle().encrypt(&[9; 32]);
        let restored_account = Account::from_pickle(
            vodozemac::olm::AccountPickle::from_encrypted(&encoded, &[9; 32]).unwrap(),
        );
        assert_eq!(restored_account.curve25519_key(), alice.curve25519_key());
    }

    #[test]
    fn group_key_is_distributed_pairwise_then_rotation_excludes_old_key() {
        let alice = Account::new();
        let mut group = GroupSession::new(GroupConfig::version_1());
        let mut context = Context::pairwise();
        context.kind = "sender_key_distribution".into();
        context.epoch = 1;
        let mut receivers = Vec::new();
        for recipient in ["bob-phone", "bob-tablet", "carol-phone"] {
            context.recipient_device = recipient.into();
            let (mut a, mut b) = start(&alice, &mut Account::new(), b"initial");
            let distribution = a
                .encrypt(payload(&context, &group.session_key().to_base64()))
                .unwrap();
            let plaintext = b.decrypt(&distribution).unwrap();
            let key = checked_body(&plaintext, &context).unwrap();
            let session_key = vodozemac::megolm::SessionKey::from_base64(&key).unwrap();
            receivers.push(InboundGroupSession::new(
                &session_key,
                GroupConfig::version_1(),
            ));
            let mut wrong = context.clone();
            wrong.conversation = "other".into();
            assert!(checked_body(&plaintext, &wrong).is_err());
        }
        let before = group.encrypt(b"before leave");
        for receiver in &mut receivers {
            assert_eq!(
                receiver.decrypt(&before).unwrap().plaintext,
                b"before leave"
            );
        }
        let mut rotated = GroupSession::new(GroupConfig::version_1());
        let mut remaining =
            InboundGroupSession::new(&rotated.session_key(), GroupConfig::version_1());
        let after = rotated.encrypt(b"after leave");
        assert_eq!(remaining.decrypt(&after).unwrap().plaintext, b"after leave");
        for old in &mut receivers {
            assert!(old.decrypt(&after).is_err());
        }
        assert_ne!(group.session_id(), rotated.session_id());
        // Manual rotation only: no production leave event or epoch store exists here.
    }

    #[test]
    fn megolm_replay_requires_application_tracking_and_supports_out_of_order() {
        let mut group = GroupSession::new(GroupConfig::version_1());
        let mut receiver = InboundGroupSession::new(&group.session_key(), GroupConfig::version_1());
        let first = group.encrypt(b"zero");
        let second = group.encrypt(b"one");
        assert_eq!(receiver.decrypt(&second).unwrap().message_index, 1);
        assert_eq!(receiver.decrypt(&first).unwrap().message_index, 0);
        // Library deliberately permits repeat decrypt: the app needs persistent
        // (sender, conversation, session_id, message_index) duplicate detection.
        assert_eq!(receiver.decrypt(&first).unwrap().message_index, 0);
        let mut seen = std::collections::HashSet::new();
        assert!(seen.insert((receiver.session_id(), 0)));
        assert!(!seen.insert((receiver.session_id(), 0)));
    }

    #[test]
    fn group_tamper_and_cross_context_are_rejected() {
        let mut group = GroupSession::new(GroupConfig::version_1());
        let mut receiver = InboundGroupSession::new(&group.session_key(), GroupConfig::version_1());
        let mut context = Context::pairwise();
        context.algorithm = GROUP_ALGORITHM.into();
        context.recipient_device = "group".into();
        context.epoch = 1;
        let message = group.encrypt(payload(&context, "group body"));
        let mut bytes = base64::engine::general_purpose::STANDARD_NO_PAD
            .decode(message.to_base64())
            .unwrap();
        let last = bytes.len() - 1;
        bytes[last] ^= 1;
        let changed = MegolmMessage::from_base64(
            &base64::engine::general_purpose::STANDARD_NO_PAD.encode(bytes),
        )
        .unwrap();
        assert!(receiver.decrypt(&changed).is_err());
        let decrypted = receiver.decrypt(&message).unwrap();
        let mut wrong = context.clone();
        wrong.epoch = 0;
        assert!(checked_body(&decrypted.plaintext, &wrong).is_err());
        assert_eq!(
            checked_body(&decrypted.plaintext, &context).unwrap(),
            "group body"
        );
    }

    #[test]
    fn raw_curve_and_signing_keys_are_both_32_bytes_not_interchangeable() {
        let account = Account::new();
        assert_eq!(account.curve25519_key().as_bytes().len(), 32);
        assert_eq!(account.ed25519_key().as_bytes().len(), 32);
        assert_eq!(account.sign(b"probe").to_bytes().len(), 64);
        assert!(Curve25519PublicKey::from_base64(&account.curve25519_key().to_base64()).is_ok());
        assert_eq!(account.curve25519_key().to_base64().len(), 43);
    }
}
