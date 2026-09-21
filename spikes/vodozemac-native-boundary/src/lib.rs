//! Isolated C++/Rust boundary experiment, not an Expo module or production API.

use serde::{Deserialize, Serialize};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use vodozemac::{
    Curve25519PublicKey, Ed25519PublicKey, Ed25519Signature,
    olm::{Account, AccountPickle, OlmMessage, Session, SessionConfig},
};

const SUITE: &str = "chirp-evaluation-only/olm-v1";
const MAX_BODY: usize = 1024;
const MAX_CIPHERTEXT: usize = 16_384;
const MAX_SNAPSHOT: usize = 1_048_576;

#[cxx::bridge(namespace = "chirp_probe")]
pub mod bridge {
    #[derive(Clone)]
    struct IdentityBinding {
        account: String,
        device: String,
        dh_key: Vec<u8>,
        signing_key: Vec<u8>,
        signature: Vec<u8>,
    }
    #[derive(Clone)]
    struct Context {
        version: u32,
        conversation: String,
        logical_id: String,
        sender_account: String,
        sender_device: String,
        recipient_account: String,
        recipient_device: String,
        kind: u8,
        epoch: u32,
    }
    #[derive(Clone)]
    struct WireMessage {
        subtype: u8,
        ciphertext: Vec<u8>,
    }
    extern "Rust" {
        type NativeAccount;
        type NativeSession;
        type InboundSession;
        fn new_account(account: &str, device: &str) -> Result<Box<NativeAccount>>;
        fn binding(self: &NativeAccount) -> Result<IdentityBinding>;
        fn next_one_time_key(self: &mut NativeAccount) -> Result<Vec<u8>>;
        fn outbound(
            self: &NativeAccount,
            peer: &IdentityBinding,
            otk: &[u8],
        ) -> Result<Box<NativeSession>>;
        fn inbound(
            self: &mut NativeAccount,
            peer: &IdentityBinding,
            message: &WireMessage,
            expected: &Context,
        ) -> Result<Box<InboundSession>>;
        fn initial_plaintext(self: &InboundSession) -> Result<Vec<u8>>;
        fn take_session(self: &mut InboundSession) -> Result<Box<NativeSession>>;
        fn encrypt(self: &mut NativeSession, context: &Context, body: &[u8])
        -> Result<WireMessage>;
        fn decrypt(
            self: &mut NativeSession,
            expected: &Context,
            message: &WireMessage,
        ) -> Result<Vec<u8>>;
        fn lock(self: &mut NativeAccount);
        fn snapshot(self: &NativeAccount, key: &[u8]) -> Result<String>;
        fn restore_account(
            account: &str,
            device: &str,
            expected_dh: &[u8],
            snapshot: &str,
            key: &[u8],
        ) -> Result<Box<NativeAccount>>;
        fn verify_binding(binding: &IdentityBinding) -> Result<()>;
    }
    unsafe extern "C++" {
        include!("probe.h");
        fn probe_case(case_id: u32) -> Result<()>;
    }
}

type ProbeResult<T> = Result<T, &'static str>;

/// All public errors are fixed literals. Neither library errors nor caller bytes
/// are formatted into C++ exceptions or test output.
pub struct NativeAccount {
    inner: Option<Account>,
    account: String,
    device: String,
    locked: Arc<AtomicBool>,
}

pub struct NativeSession {
    inner: Session,
    local_account: String,
    local_device: String,
    peer_account: String,
    peer_device: String,
    locked: Arc<AtomicBool>,
}

pub struct InboundSession {
    session: Option<Box<NativeSession>>,
    plaintext: Vec<u8>,
    locked: Arc<AtomicBool>,
}

#[derive(Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct AuthenticatedContext {
    suite: String,
    version: u32,
    conversation: String,
    logical_id: String,
    sender_account: String,
    sender_device: String,
    recipient_account: String,
    recipient_device: String,
    kind: u8,
    epoch: u32,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Payload {
    context: AuthenticatedContext,
    body: Vec<u8>,
}

fn label(value: &str) -> ProbeResult<()> {
    if value.is_empty()
        || value.len() > 64
        || !value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-')
    {
        return Err("invalid_context");
    }
    Ok(())
}

fn context(value: &bridge::Context) -> ProbeResult<AuthenticatedContext> {
    if value.version != 1 || !matches!(value.kind, 0 | 1) {
        return Err("unsupported_context");
    }
    for item in [
        &value.conversation,
        &value.logical_id,
        &value.sender_account,
        &value.sender_device,
        &value.recipient_account,
        &value.recipient_device,
    ] {
        label(item)?;
    }
    Ok(AuthenticatedContext {
        suite: SUITE.into(),
        version: value.version,
        conversation: value.conversation.clone(),
        logical_id: value.logical_id.clone(),
        sender_account: value.sender_account.clone(),
        sender_device: value.sender_device.clone(),
        recipient_account: value.recipient_account.clone(),
        recipient_device: value.recipient_device.clone(),
        kind: value.kind,
        epoch: value.epoch,
    })
}

fn key32(bytes: &[u8]) -> ProbeResult<&[u8; 32]> {
    bytes.try_into().map_err(|_| "invalid_key")
}

fn signed_bytes(binding: &bridge::IdentityBinding) -> ProbeResult<Vec<u8>> {
    label(&binding.account)?;
    label(&binding.device)?;
    key32(&binding.dh_key)?;
    key32(&binding.signing_key)?;
    // A fixed tuple has no JSON map-order ambiguity. This encoding is proposed,
    // not compatible with the current key directory or selected for production.
    serde_json::to_vec(&(
        "chirp-evaluation-only/device-binding-v1",
        &binding.account,
        &binding.device,
        &binding.dh_key,
        &binding.signing_key,
    ))
    .map_err(|_| "encode_failed")
}

pub fn verify_binding(binding: &bridge::IdentityBinding) -> ProbeResult<()> {
    let bytes = signed_bytes(binding)?;
    let key =
        Ed25519PublicKey::from_slice(key32(&binding.signing_key)?).map_err(|_| "invalid_key")?;
    let signature =
        Ed25519Signature::from_slice(&binding.signature).map_err(|_| "invalid_signature")?;
    key.verify(&bytes, &signature)
        .map_err(|_| "invalid_signature")
}

pub fn new_account(account: &str, device: &str) -> ProbeResult<Box<NativeAccount>> {
    label(account)?;
    label(device)?;
    Ok(Box::new(NativeAccount {
        inner: Some(Account::new()),
        account: account.into(),
        device: device.into(),
        locked: Arc::new(AtomicBool::new(false)),
    }))
}

impl NativeAccount {
    fn account(&self) -> ProbeResult<&Account> {
        if self.locked.load(Ordering::Acquire) {
            return Err("account_locked");
        }
        self.inner.as_ref().ok_or("account_locked")
    }
    pub fn binding(&self) -> ProbeResult<bridge::IdentityBinding> {
        let account = self.account()?;
        let mut result = bridge::IdentityBinding {
            account: self.account.clone(),
            device: self.device.clone(),
            dh_key: account.curve25519_key().as_bytes().to_vec(),
            signing_key: account.ed25519_key().as_bytes().to_vec(),
            signature: vec![],
        };
        result.signature = account.sign(signed_bytes(&result)?).to_bytes().to_vec();
        Ok(result)
    }
    pub fn next_one_time_key(&mut self) -> ProbeResult<Vec<u8>> {
        self.account()?;
        let account = self.inner.as_mut().ok_or("account_locked")?;
        account.generate_one_time_keys(1);
        let key = account
            .one_time_keys()
            .values()
            .next()
            .copied()
            .ok_or("prekey_unavailable")?;
        account.mark_keys_as_published();
        Ok(key.as_bytes().to_vec())
    }
    fn session(&self, peer: &bridge::IdentityBinding, inner: Session) -> Box<NativeSession> {
        Box::new(NativeSession {
            inner,
            local_account: self.account.clone(),
            local_device: self.device.clone(),
            peer_account: peer.account.clone(),
            peer_device: peer.device.clone(),
            locked: self.locked.clone(),
        })
    }
    pub fn outbound(
        &self,
        peer: &bridge::IdentityBinding,
        otk: &[u8],
    ) -> ProbeResult<Box<NativeSession>> {
        verify_binding(peer)?;
        let inner = self
            .account()?
            .create_outbound_session(
                SessionConfig::version_1(),
                Curve25519PublicKey::from_bytes(*key32(&peer.dh_key)?),
                Curve25519PublicKey::from_bytes(*key32(otk)?),
            )
            .map_err(|_| "session_failed")?;
        Ok(self.session(peer, inner))
    }
    pub fn inbound(
        &mut self,
        peer: &bridge::IdentityBinding,
        message: &bridge::WireMessage,
        expected: &bridge::Context,
    ) -> ProbeResult<Box<InboundSession>> {
        self.account()?;
        verify_binding(peer)?;
        let expected = context(expected)?;
        if expected.sender_account != peer.account
            || expected.sender_device != peer.device
            || expected.recipient_account != self.account
            || expected.recipient_device != self.device
        {
            return Err("context_mismatch");
        }
        let native = decode(message)?;
        let OlmMessage::PreKey(prekey) = native else {
            return Err("prekey_required");
        };
        // Trial on a private copy: failed context/crypto checks must not consume
        // the live account's OTK. This is memory-only, NOT a durable transaction.
        let mut candidate = Account::from_pickle(self.account()?.pickle());
        let result = candidate
            .create_inbound_session(
                SessionConfig::version_1(),
                Curve25519PublicKey::from_bytes(*key32(&peer.dh_key)?),
                &prekey,
            )
            .map_err(|_| "decrypt_failed")?;
        let plaintext = check_plaintext(&result.plaintext, &expected)?;
        let session = self.session(peer, result.session);
        self.inner = Some(candidate);
        Ok(Box::new(InboundSession {
            session: Some(session),
            plaintext,
            locked: self.locked.clone(),
        }))
    }
    pub fn lock(&mut self) {
        self.locked.store(true, Ordering::Release);
        self.inner = None;
    }
    pub fn snapshot(&self, key: &[u8]) -> ProbeResult<String> {
        Ok(self.account()?.pickle().encrypt(key32(key)?))
    }
}

pub fn restore_account(
    account: &str,
    device: &str,
    expected_dh: &[u8],
    snapshot: &str,
    key: &[u8],
) -> ProbeResult<Box<NativeAccount>> {
    label(account)?;
    label(device)?;
    if snapshot.len() > MAX_SNAPSHOT {
        return Err("snapshot_too_large");
    }
    let pickle =
        AccountPickle::from_encrypted(snapshot, key32(key)?).map_err(|_| "restore_failed")?;
    let inner = Account::from_pickle(pickle);
    if inner.curve25519_key().as_bytes() != key32(expected_dh)? {
        return Err("identity_mismatch");
    }
    // Caller supplies trusted scope + expected identity. The opaque pickle does
    // not authenticate account/device labels; proposed storage contract does.
    Ok(Box::new(NativeAccount {
        inner: Some(inner),
        account: account.into(),
        device: device.into(),
        locked: Arc::new(AtomicBool::new(false)),
    }))
}

impl InboundSession {
    fn check(&self) -> ProbeResult<()> {
        if self.locked.load(Ordering::Acquire) {
            return Err("account_locked");
        }
        Ok(())
    }
    pub fn initial_plaintext(&self) -> ProbeResult<Vec<u8>> {
        self.check()?;
        Ok(self.plaintext.clone())
    }
    pub fn take_session(&mut self) -> ProbeResult<Box<NativeSession>> {
        self.check()?;
        self.session.take().ok_or("session_taken")
    }
}

fn decode(message: &bridge::WireMessage) -> ProbeResult<OlmMessage> {
    if message.ciphertext.is_empty() || message.ciphertext.len() > MAX_CIPHERTEXT {
        return Err("invalid_ciphertext_size");
    }
    OlmMessage::from_parts(message.subtype as usize, &message.ciphertext)
        .map_err(|_| "invalid_message")
}

fn check_plaintext(bytes: &[u8], expected: &AuthenticatedContext) -> ProbeResult<Vec<u8>> {
    let parsed: Payload = serde_json::from_slice(bytes).map_err(|_| "invalid_payload")?;
    if &parsed.context != expected {
        return Err("context_mismatch");
    }
    if parsed.body.len() > MAX_BODY {
        return Err("body_too_large");
    }
    Ok(parsed.body)
}

impl NativeSession {
    fn check(&self, value: &bridge::Context, sending: bool) -> ProbeResult<AuthenticatedContext> {
        if self.locked.load(Ordering::Acquire) {
            return Err("account_locked");
        }
        let value = context(value)?;
        let (local_account, local_device, peer_account, peer_device) = if sending {
            (
                &value.sender_account,
                &value.sender_device,
                &value.recipient_account,
                &value.recipient_device,
            )
        } else {
            (
                &value.recipient_account,
                &value.recipient_device,
                &value.sender_account,
                &value.sender_device,
            )
        };
        if local_account != &self.local_account
            || local_device != &self.local_device
            || peer_account != &self.peer_account
            || peer_device != &self.peer_device
        {
            return Err("context_mismatch");
        }
        Ok(value)
    }
    pub fn encrypt(
        &mut self,
        value: &bridge::Context,
        body: &[u8],
    ) -> ProbeResult<bridge::WireMessage> {
        let context = self.check(value, true)?;
        if body.len() > MAX_BODY {
            return Err("body_too_large");
        }
        let bytes = serde_json::to_vec(&Payload {
            context,
            body: body.to_vec(),
        })
        .map_err(|_| "encode_failed")?;
        let mut candidate = Session::from_pickle(self.inner.pickle());
        let (subtype, ciphertext) = candidate
            .encrypt(bytes)
            .map_err(|_| "encrypt_failed")?
            .to_parts();
        if ciphertext.len() > MAX_CIPHERTEXT {
            return Err("invalid_ciphertext_size");
        }
        self.inner = candidate;
        Ok(bridge::WireMessage {
            subtype: subtype as u8,
            ciphertext,
        })
    }
    pub fn decrypt(
        &mut self,
        value: &bridge::Context,
        message: &bridge::WireMessage,
    ) -> ProbeResult<Vec<u8>> {
        let expected = self.check(value, false)?;
        let native = decode(message)?;
        let mut candidate = Session::from_pickle(self.inner.pickle());
        let bytes = candidate.decrypt(&native).map_err(|_| "decrypt_failed")?;
        let body = check_plaintext(&bytes, &expected)?;
        self.inner = candidate;
        Ok(body)
    }
}

pub fn run_probe(case: u32) -> Result<(), cxx::Exception> {
    bridge::probe_case(case)
}

#[cfg(test)]
mod tests {
    macro_rules! probe {
        ($name:ident, $id:literal) => {
            #[test]
            fn $name() {
                super::run_probe($id).unwrap();
            }
        };
    }
    probe!(cpp_real_prekey_reply_and_normal_roundtrip, 0);
    probe!(cpp_signed_identity_binding_rejects_changes, 1);
    probe!(cpp_wrong_device_cannot_decrypt, 2);
    probe!(cpp_tamper_failure_preserves_valid_message, 3);
    probe!(cpp_context_failure_preserves_ratchet_and_prekey, 4);
    probe!(cpp_unsupported_version_type_and_bounds_fail_closed, 5);
    probe!(cpp_account_lock_invalidates_existing_sessions, 6);
    probe!(
        cpp_encrypted_account_restore_rejects_wrong_key_and_identity,
        7
    );
    probe!(cpp_replay_rejected_and_opaque_session_moves_once, 8);
}
