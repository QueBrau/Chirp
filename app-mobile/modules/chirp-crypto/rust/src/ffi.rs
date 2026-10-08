//! The native boundary (board c448, milestone M3): the uniffi surface the iOS Expo
//! module wraps. Android gets the same surface later; nothing in here is iOS-specific.
//!
//! This layer changes no semantics. Every call forwards to [`Core`], and the rules in
//! the core's module docs still hold: one operation at a time on a handle, commit
//! before release, private keys never leave the store. What it adds is only a shape
//! the platform layers can bind to:
//!
//!  - **Ids** are canonical lowercase hyphenated UUID text (`8-4-4-4-12`). The 16 raw
//!    bytes the core uses are an internal detail. Uppercase or malformed text is
//!    `invalid_input`; the TypeScript layer lowercases before it calls.
//!  - **Binary values** (keys, signatures, ciphertext) are standard-alphabet base64,
//!    emitted WITH padding, accepted with or without. A value of the wrong length is
//!    `invalid_input`. Ciphertext is encoded exactly once, here.
//!  - **Handles.** [`CryptoCore::unlock`] returns an object, so several accounts can be
//!    open at once (production uses one at a time; tests open two). It is bound to its
//!    namespace, so callers cannot pass a different user id to a handle.
//!  - **Errors.** ONE enum, [`CryptoError`]. Its `Display` text IS the stable code
//!    string, and that text is what crosses the boundary (a flat error); a test pins
//!    every core code to it. No variant carries a payload, so no error can hold
//!    plaintext, key material or upstream error text.
//!  - **Rollback pin.** The platform layer keeps the store generation in secure storage
//!    and passes it to `unlock`; the comparison lives here so it is host-tested.
//!
//! Counters that cross to JavaScript (`key_id`, `seq`, timestamps) are `u64`; they are
//! far below 2^53 in any real store, so a JS number represents them exactly.

use std::fmt;
use std::path::PathBuf;
use std::sync::Arc;

use base64::Engine as _;
use base64::engine::general_purpose::{STANDARD, STANDARD_PAD_INDIFFERENT};
use zeroize::Zeroizing;

use crate::core::{Core, MAX_LEG_BYTES};
use crate::envelope::{uuid_from_text, uuid_to_text};
use crate::error::Error;
use crate::store::namespace_path;
use crate::types as t;

// ---------------------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------------------

/// Every failure that crosses the boundary. The first group mirrors the core's
/// [`Error`] one to one (same code strings); the last two are raised by the platform
/// layer or by this boundary and never by the core.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error, uniffi::Error)]
#[uniffi(flat_error)]
pub enum CryptoError {
    #[error("locked")]
    Locked,
    #[error("unlock_failed")]
    UnlockFailed,
    #[error("store_in_use")]
    StoreInUse,
    #[error("storage_failed")]
    Storage,
    #[error("store_corrupt")]
    StoreCorrupt,
    #[error("namespace_mismatch")]
    NamespaceMismatch,
    #[error("device_exists")]
    DeviceExists,
    #[error("no_device")]
    NoDevice,
    #[error("not_published")]
    NotPublished,
    #[error("invalid_input")]
    InvalidInput,
    #[error("body_invalid")]
    BodyInvalid,
    #[error("body_too_large")]
    BodyTooLarge,
    #[error("invalid_signature")]
    InvalidSignature,
    #[error("invalid_key")]
    InvalidKey,
    #[error("unknown_key")]
    UnknownKey,
    #[error("too_many_keys")]
    TooManyKeys,
    #[error("sender_mismatch")]
    SenderMismatch,
    #[error("missing_claimed_key")]
    MissingClaimedKey,
    #[error("trust_hard_stop")]
    TrustHardStop,
    #[error("unknown_peer")]
    UnknownPeer,
    #[error("nothing_to_accept")]
    NothingToAccept,
    #[error("client_message_id_reused")]
    ClientMessageIdReused,
    #[error("ciphertext_too_large")]
    CiphertextTooLarge,
    #[error("encrypt_failed")]
    EncryptFailed,
    #[error("decrypt_failed")]
    DecryptFailed,
    #[error("invalid_envelope")]
    InvalidEnvelope,
    #[error("envelope_mismatch")]
    EnvelopeMismatch,
    #[error("replay")]
    Replay,
    #[error("internal")]
    Internal,
    /// The store's generation is LOWER than the generation pinned in secure storage:
    /// an older copy of the store was restored. Raised by [`CryptoCore::unlock`].
    #[error("store_rolled_back")]
    StoreRolledBack,
    /// Secure storage (the iOS Keychain) failed. Only the platform layer raises it.
    #[error("keychain_failed")]
    KeychainFailed,
}

impl From<Error> for CryptoError {
    fn from(e: Error) -> Self {
        // Exhaustive on purpose: a new core error must be mapped here or this fails to
        // compile, so a code can never silently collapse into `internal`.
        match e {
            Error::Locked => CryptoError::Locked,
            Error::UnlockFailed => CryptoError::UnlockFailed,
            Error::StoreInUse => CryptoError::StoreInUse,
            Error::Storage => CryptoError::Storage,
            Error::StoreCorrupt => CryptoError::StoreCorrupt,
            Error::NamespaceMismatch => CryptoError::NamespaceMismatch,
            Error::DeviceExists => CryptoError::DeviceExists,
            Error::NoDevice => CryptoError::NoDevice,
            Error::NotPublished => CryptoError::NotPublished,
            Error::InvalidInput => CryptoError::InvalidInput,
            Error::BodyInvalid => CryptoError::BodyInvalid,
            Error::BodyTooLarge => CryptoError::BodyTooLarge,
            Error::InvalidSignature => CryptoError::InvalidSignature,
            Error::InvalidKey => CryptoError::InvalidKey,
            Error::UnknownKey => CryptoError::UnknownKey,
            Error::TooManyKeys => CryptoError::TooManyKeys,
            Error::SenderMismatch => CryptoError::SenderMismatch,
            Error::MissingClaimedKey => CryptoError::MissingClaimedKey,
            Error::TrustHardStop => CryptoError::TrustHardStop,
            Error::UnknownPeer => CryptoError::UnknownPeer,
            Error::NothingToAccept => CryptoError::NothingToAccept,
            Error::ClientMessageIdReused => CryptoError::ClientMessageIdReused,
            Error::CiphertextTooLarge => CryptoError::CiphertextTooLarge,
            Error::EncryptFailed => CryptoError::EncryptFailed,
            Error::DecryptFailed => CryptoError::DecryptFailed,
            Error::InvalidEnvelope => CryptoError::InvalidEnvelope,
            Error::EnvelopeMismatch => CryptoError::EnvelopeMismatch,
            Error::Replay => CryptoError::Replay,
            Error::Internal => CryptoError::Internal,
        }
    }
}

type Res<T> = std::result::Result<T, CryptoError>;

// ---------------------------------------------------------------------------------------
// Value types
// ---------------------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, uniffi::Enum)]
pub enum KeyKind {
    OneTime,
    Fallback,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, uniffi::Enum)]
pub enum TrustState {
    Trusted,
    IdentityChanged,
    UnapprovedDevice,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, uniffi::Enum)]
pub enum Direction {
    Incoming,
    Outgoing,
}

#[derive(Debug, Clone, PartialEq, Eq, uniffi::Record)]
pub struct PublicIdentity {
    pub curve25519: String,
    pub ed25519: String,
}

#[derive(Debug, Clone, PartialEq, Eq, uniffi::Record)]
pub struct SignedKey {
    pub key_id: u64,
    pub kind: KeyKind,
    pub public_key: String,
    pub signature: String,
}

#[derive(Debug, Clone, PartialEq, Eq, uniffi::Record)]
pub struct CreatedDevice {
    pub identity: PublicIdentity,
    pub binding_signature: String,
    pub one_time_keys: Vec<SignedKey>,
    pub fallback_key: SignedKey,
}

#[derive(Debug, Clone, PartialEq, Eq, uniffi::Record)]
pub struct DeviceApproval {
    pub approver_ed25519: String,
    pub signature: String,
}

#[derive(Debug, Clone, PartialEq, Eq, uniffi::Record)]
pub struct DirectoryDevice {
    pub user_id: String,
    pub device_id: String,
    pub generation: u32,
    pub identity_curve25519: String,
    pub identity_ed25519: String,
    pub binding_signature: String,
    pub approval: Option<DeviceApproval>,
}

#[derive(Debug, Clone, PartialEq, Eq, uniffi::Record)]
pub struct NewDeviceBinding {
    pub user_id: String,
    pub generation: u32,
    pub identity_curve25519: String,
    pub identity_ed25519: String,
    pub binding_signature: String,
}

#[derive(Debug, Clone, PartialEq, Eq, uniffi::Record)]
pub struct Recipient {
    pub device: DirectoryDevice,
    pub claimed_key: Option<SignedKey>,
}

#[derive(Clone, PartialEq, Eq, uniffi::Record)]
pub struct Leg {
    pub recipient_device_id: String,
    /// 0 = prekey, 1 = normal.
    pub olm_type: u8,
    pub ciphertext: String,
}

impl fmt::Debug for Leg {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Leg")
            .field("olm_type", &self.olm_type)
            .field("ciphertext_len", &self.ciphertext.len())
            .finish_non_exhaustive()
    }
}

#[derive(Debug, Clone, PartialEq, Eq, uniffi::Record)]
pub struct ExpectedEnvelope {
    pub conversation_id: String,
    pub client_message_id: String,
    pub sender_user_id: String,
    pub sender_device_id: String,
    pub recipient_user_id: String,
    pub recipient_device_id: String,
}

#[derive(Clone, PartialEq, Eq, uniffi::Record)]
pub struct PlaintextMessage {
    pub seq: u64,
    pub direction: Direction,
    pub message_id: Option<String>,
    pub conversation_id: String,
    pub client_message_id: String,
    pub sender_user_id: String,
    pub sender_device_id: String,
    pub kind: String,
    pub body: String,
    pub sender_trust: TrustState,
    pub stored_at_ms: u64,
}

impl fmt::Debug for PlaintextMessage {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("PlaintextMessage")
            .field("seq", &self.seq)
            .field("body", &"<redacted>")
            .finish_non_exhaustive()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, uniffi::Record)]
pub struct KeyStatus {
    pub pending: u32,
    pub published_one_time: u32,
    pub has_fallback: bool,
}

// ---------------------------------------------------------------------------------------
// Text <-> core conversions
// ---------------------------------------------------------------------------------------

/// Longest accepted base64 text for one leg's ciphertext (the encoded form of the
/// core's raw cap). Checked before decoding so a hostile caller cannot make one call
/// allocate arbitrarily.
const MAX_LEG_TEXT: usize = MAX_LEG_BYTES.div_ceil(3) * 4;

fn id(text: &str) -> Res<[u8; 16]> {
    uuid_from_text(text).ok_or(CryptoError::InvalidInput)
}

fn id_text(raw: &[u8; 16]) -> String {
    uuid_to_text(raw)
}

fn b64(bytes: &[u8]) -> String {
    STANDARD.encode(bytes)
}

fn fixed<const N: usize>(text: &str) -> Res<[u8; N]> {
    // A 32-byte value is 44 characters, a 64-byte one 88; anything longer than the
    // padded form of N bytes is rejected before it is decoded.
    if text.len() > N.div_ceil(3) * 4 {
        return Err(CryptoError::InvalidInput);
    }
    let bytes = STANDARD_PAD_INDIFFERENT
        .decode(text)
        .map_err(|_| CryptoError::InvalidInput)?;
    bytes.try_into().map_err(|_| CryptoError::InvalidInput)
}

fn variable(text: &str, max_text: usize) -> Res<Vec<u8>> {
    if text.len() > max_text {
        return Err(CryptoError::InvalidInput);
    }
    STANDARD_PAD_INDIFFERENT
        .decode(text)
        .map_err(|_| CryptoError::InvalidInput)
}

fn namespace(user_id: &str, generation: u32) -> Res<t::Namespace> {
    Ok(t::Namespace {
        user_id: id(user_id)?,
        generation,
    })
}

impl From<KeyKind> for t::KeyKind {
    fn from(k: KeyKind) -> Self {
        match k {
            KeyKind::OneTime => t::KeyKind::OneTime,
            KeyKind::Fallback => t::KeyKind::Fallback,
        }
    }
}

impl From<t::KeyKind> for KeyKind {
    fn from(k: t::KeyKind) -> Self {
        match k {
            t::KeyKind::OneTime => KeyKind::OneTime,
            t::KeyKind::Fallback => KeyKind::Fallback,
        }
    }
}

impl From<t::TrustState> for TrustState {
    fn from(s: t::TrustState) -> Self {
        match s {
            t::TrustState::Trusted => TrustState::Trusted,
            t::TrustState::IdentityChanged => TrustState::IdentityChanged,
            t::TrustState::UnapprovedDevice => TrustState::UnapprovedDevice,
        }
    }
}

impl From<t::Direction> for Direction {
    fn from(d: t::Direction) -> Self {
        match d {
            t::Direction::Incoming => Direction::Incoming,
            t::Direction::Outgoing => Direction::Outgoing,
        }
    }
}

impl From<t::PublicIdentity> for PublicIdentity {
    fn from(i: t::PublicIdentity) -> Self {
        PublicIdentity {
            curve25519: b64(&i.curve25519),
            ed25519: b64(&i.ed25519),
        }
    }
}

impl From<t::SignedKey> for SignedKey {
    fn from(k: t::SignedKey) -> Self {
        SignedKey {
            key_id: k.key_id,
            kind: k.kind.into(),
            public_key: b64(&k.public_key),
            signature: b64(&k.signature),
        }
    }
}

impl TryFrom<&SignedKey> for t::SignedKey {
    type Error = CryptoError;
    fn try_from(k: &SignedKey) -> Res<Self> {
        Ok(t::SignedKey {
            key_id: k.key_id,
            kind: k.kind.into(),
            public_key: fixed(&k.public_key)?,
            signature: fixed(&k.signature)?,
        })
    }
}

impl From<t::CreatedDevice> for CreatedDevice {
    fn from(d: t::CreatedDevice) -> Self {
        CreatedDevice {
            identity: d.identity.into(),
            binding_signature: b64(&d.binding_signature),
            one_time_keys: d.one_time_keys.into_iter().map(Into::into).collect(),
            fallback_key: d.fallback_key.into(),
        }
    }
}

impl From<t::DeviceApproval> for DeviceApproval {
    fn from(a: t::DeviceApproval) -> Self {
        DeviceApproval {
            approver_ed25519: b64(&a.approver_ed25519),
            signature: b64(&a.signature),
        }
    }
}

impl TryFrom<&DeviceApproval> for t::DeviceApproval {
    type Error = CryptoError;
    fn try_from(a: &DeviceApproval) -> Res<Self> {
        Ok(t::DeviceApproval {
            approver_ed25519: fixed(&a.approver_ed25519)?,
            signature: fixed(&a.signature)?,
        })
    }
}

impl TryFrom<&DirectoryDevice> for t::DirectoryDevice {
    type Error = CryptoError;
    fn try_from(d: &DirectoryDevice) -> Res<Self> {
        Ok(t::DirectoryDevice {
            user_id: id(&d.user_id)?,
            device_id: id(&d.device_id)?,
            generation: d.generation,
            identity_curve25519: fixed(&d.identity_curve25519)?,
            identity_ed25519: fixed(&d.identity_ed25519)?,
            binding_signature: fixed(&d.binding_signature)?,
            approval: d.approval.as_ref().map(TryInto::try_into).transpose()?,
        })
    }
}

impl TryFrom<&NewDeviceBinding> for t::NewDeviceBinding {
    type Error = CryptoError;
    fn try_from(b: &NewDeviceBinding) -> Res<Self> {
        Ok(t::NewDeviceBinding {
            user_id: id(&b.user_id)?,
            generation: b.generation,
            identity_curve25519: fixed(&b.identity_curve25519)?,
            identity_ed25519: fixed(&b.identity_ed25519)?,
            binding_signature: fixed(&b.binding_signature)?,
        })
    }
}

impl TryFrom<&Recipient> for t::Recipient {
    type Error = CryptoError;
    fn try_from(r: &Recipient) -> Res<Self> {
        Ok(t::Recipient {
            device: (&r.device).try_into()?,
            claimed_key: r.claimed_key.as_ref().map(TryInto::try_into).transpose()?,
        })
    }
}

impl From<t::Leg> for Leg {
    fn from(l: t::Leg) -> Self {
        Leg {
            recipient_device_id: id_text(&l.recipient_device_id),
            olm_type: l.olm_type,
            ciphertext: b64(&l.ciphertext),
        }
    }
}

impl TryFrom<&Leg> for t::Leg {
    type Error = CryptoError;
    fn try_from(l: &Leg) -> Res<Self> {
        Ok(t::Leg {
            recipient_device_id: id(&l.recipient_device_id)?,
            olm_type: l.olm_type,
            ciphertext: variable(&l.ciphertext, MAX_LEG_TEXT)?,
        })
    }
}

impl TryFrom<&ExpectedEnvelope> for t::ExpectedEnvelope {
    type Error = CryptoError;
    fn try_from(e: &ExpectedEnvelope) -> Res<Self> {
        Ok(t::ExpectedEnvelope {
            conversation_id: id(&e.conversation_id)?,
            client_message_id: id(&e.client_message_id)?,
            sender_user_id: id(&e.sender_user_id)?,
            sender_device_id: id(&e.sender_device_id)?,
            recipient_user_id: id(&e.recipient_user_id)?,
            recipient_device_id: id(&e.recipient_device_id)?,
        })
    }
}

impl From<t::PlaintextMessage> for PlaintextMessage {
    fn from(m: t::PlaintextMessage) -> Self {
        PlaintextMessage {
            seq: m.seq,
            direction: m.direction.into(),
            message_id: m.message_id.as_ref().map(id_text),
            conversation_id: id_text(&m.conversation_id),
            client_message_id: id_text(&m.client_message_id),
            sender_user_id: id_text(&m.sender_user_id),
            sender_device_id: id_text(&m.sender_device_id),
            kind: m.kind,
            body: m.body,
            sender_trust: m.sender_trust.into(),
            stored_at_ms: m.stored_at_ms,
        }
    }
}

impl From<t::KeyStatus> for KeyStatus {
    fn from(s: t::KeyStatus) -> Self {
        let count = |n: usize| u32::try_from(n).unwrap_or(u32::MAX);
        KeyStatus {
            pending: count(s.pending),
            published_one_time: count(s.published_one_time),
            has_fallback: s.has_fallback,
        }
    }
}

// ---------------------------------------------------------------------------------------
// Free functions
// ---------------------------------------------------------------------------------------

/// The stable text for a namespace: the 32 hex digits of the user id and the device
/// generation, as in the store's file name stem. Platform layers use it as the
/// secure-storage account name, and validating the UUID here means that name can never
/// be built from unvalidated caller text.
#[uniffi::export]
pub fn namespace_label(user_id: String, generation: u32) -> Res<String> {
    let ns = namespace(&user_id, generation)?;
    Ok(format!(
        "{}-g{}",
        crate::envelope::hex(&ns.user_id),
        ns.generation
    ))
}

/// Whether a store file exists for this namespace in `dir`. The platform layer uses it
/// to tell a fresh namespace from an existing one before it touches secure storage.
#[uniffi::export]
pub fn store_exists(dir: String, user_id: String, generation: u32) -> Res<bool> {
    let ns = namespace(&user_id, generation)?;
    Ok(namespace_path(&PathBuf::from(dir), &ns).exists())
}

/// Delete a namespace's store files without unlocking it. The platform layer locks
/// any open handle for the namespace first and destroys the store key and the pin
/// itself (the key's destruction is the real erasure).
#[uniffi::export]
pub fn wipe_namespace(dir: String, user_id: String, generation: u32) -> Res<()> {
    let ns = namespace(&user_id, generation)?;
    Core::wipe_namespace(&PathBuf::from(dir), ns)?;
    Ok(())
}

/// The 60-digit safety number for this user and a peer, from each side's approved
/// device Ed25519 identities (base64). Identical on both sides.
#[uniffi::export]
pub fn safety_number(
    my_user_id: String,
    my_identities: Vec<String>,
    peer_user_id: String,
    peer_identities: Vec<String>,
) -> Res<String> {
    let mine: Vec<[u8; 32]> = my_identities
        .iter()
        .map(|k| fixed(k))
        .collect::<Res<_>>()?;
    let theirs: Vec<[u8; 32]> = peer_identities
        .iter()
        .map(|k| fixed(k))
        .collect::<Res<_>>()?;
    Ok(crate::safety::safety_number(
        &id(&my_user_id)?,
        &mine,
        &id(&peer_user_id)?,
        &theirs,
    )?)
}

// ---------------------------------------------------------------------------------------
// The handle
// ---------------------------------------------------------------------------------------

/// One unlocked account store. Calls on a handle never interleave (the core's mutex),
/// but the platform layer must still dispatch them from a single serial queue so the
/// ORDER of sends and receives is deterministic.
#[derive(uniffi::Object)]
pub struct CryptoCore {
    core: Core,
    namespace: t::Namespace,
}

impl fmt::Debug for CryptoCore {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("CryptoCore(..)")
    }
}

#[uniffi::export]
impl CryptoCore {
    /// Open the namespace's store, creating it if it does not exist. `store_key` is
    /// exactly 32 bytes and is zeroized after use here (the platform layer's own copy
    /// is its responsibility). `pinned_generation` is the generation last pinned in
    /// secure storage, or `None` when there is no pin. A store whose generation is
    /// LOWER than the pin is a restored older copy: it is closed again and refused
    /// with `store_rolled_back`. A pin that is equal or lower is fine, because the
    /// pin is written after a commit and a crash in between leaves it behind.
    #[uniffi::constructor]
    pub fn unlock(
        dir: String,
        user_id: String,
        generation: u32,
        store_key: Vec<u8>,
        pinned_generation: Option<u64>,
    ) -> Res<Arc<CryptoCore>> {
        let store_key = Zeroizing::new(store_key);
        let ns = namespace(&user_id, generation)?;
        let core = Core::unlock(&PathBuf::from(dir), ns, &store_key)?;
        if let Some(pinned) = pinned_generation {
            let current = match core.store_generation() {
                Ok(current) => current,
                Err(e) => {
                    core.lock();
                    return Err(e.into());
                }
            };
            if current < pinned {
                core.lock();
                return Err(CryptoError::StoreRolledBack);
            }
        }
        Ok(Arc::new(CryptoCore {
            core,
            namespace: ns,
        }))
    }

    /// Invalidate this handle for good. Idempotent.
    pub fn lock(&self) {
        self.core.lock();
    }

    pub fn is_unlocked(&self) -> bool {
        self.core.is_unlocked()
    }

    /// Monotonic commit counter, for the platform layer's rollback pin.
    pub fn store_generation(&self) -> Res<u64> {
        Ok(self.core.store_generation()?)
    }

    pub fn has_device(&self) -> Res<bool> {
        Ok(self.core.has_device()?)
    }

    pub fn public_identity(&self) -> Res<Option<PublicIdentity>> {
        Ok(self.core.public_identity()?.map(Into::into))
    }

    pub fn create_device(&self, otk_count: u32) -> Res<CreatedDevice> {
        Ok(self
            .core
            .create_device(
                self.namespace.user_id,
                self.namespace.generation,
                otk_count as usize,
            )?
            .into())
    }

    pub fn mark_published(&self, server_device_id: String, key_ids: Vec<u64>) -> Res<()> {
        Ok(self.core.mark_published(id(&server_device_id)?, &key_ids)?)
    }

    pub fn top_up_keys(&self, count: u32) -> Res<Vec<SignedKey>> {
        Ok(self
            .core
            .top_up_keys(count as usize)?
            .into_iter()
            .map(Into::into)
            .collect())
    }

    pub fn rotate_fallback_key(&self) -> Res<SignedKey> {
        Ok(self.core.rotate_fallback_key()?.into())
    }

    pub fn pending_keys(&self) -> Res<Vec<SignedKey>> {
        Ok(self
            .core
            .pending_keys()?
            .into_iter()
            .map(Into::into)
            .collect())
    }

    pub fn key_status(&self) -> Res<KeyStatus> {
        Ok(self.core.key_status()?.into())
    }

    pub fn encrypt(
        &self,
        conversation_id: String,
        client_message_id: String,
        body: String,
        sender: DirectoryDevice,
        recipients: Vec<Recipient>,
    ) -> Res<Vec<Leg>> {
        // The body is plaintext: our copy is wiped when this call returns.
        let body = Zeroizing::new(body);
        let sender = t::DirectoryDevice::try_from(&sender)?;
        let recipients: Vec<t::Recipient> = recipients
            .iter()
            .map(t::Recipient::try_from)
            .collect::<Res<_>>()?;
        let legs = self.core.encrypt(
            id(&conversation_id)?,
            id(&client_message_id)?,
            body.as_str(),
            &sender,
            &recipients,
        )?;
        Ok(legs.into_iter().map(Into::into).collect())
    }

    pub fn decrypt(
        &self,
        message_id: String,
        leg: Leg,
        sender: DirectoryDevice,
        expected: ExpectedEnvelope,
    ) -> Res<PlaintextMessage> {
        let message = self.core.decrypt(
            id(&message_id)?,
            &t::Leg::try_from(&leg)?,
            &t::DirectoryDevice::try_from(&sender)?,
            &t::ExpectedEnvelope::try_from(&expected)?,
        )?;
        Ok(message.into())
    }

    pub fn list_messages(
        &self,
        conversation_id: String,
        before: Option<u64>,
        limit: u32,
    ) -> Res<Vec<PlaintextMessage>> {
        Ok(self
            .core
            .list_messages(id(&conversation_id)?, before, limit)?
            .into_iter()
            .map(Into::into)
            .collect())
    }

    pub fn observe_directory(
        &self,
        user_id: String,
        devices: Vec<DirectoryDevice>,
    ) -> Res<TrustState> {
        let devices: Vec<t::DirectoryDevice> = devices
            .iter()
            .map(t::DirectoryDevice::try_from)
            .collect::<Res<_>>()?;
        Ok(self.core.observe_directory(id(&user_id)?, &devices)?.into())
    }

    pub fn trust_state(&self, peer_user_id: String) -> Res<TrustState> {
        Ok(self.core.trust_state(id(&peer_user_id)?)?.into())
    }

    pub fn accept_identity_change(&self, peer_user_id: String) -> Res<()> {
        Ok(self.core.accept_identity_change(id(&peer_user_id)?)?)
    }

    pub fn approve_device(&self, new_device: NewDeviceBinding) -> Res<DeviceApproval> {
        Ok(self
            .core
            .approve_device(&t::NewDeviceBinding::try_from(&new_device)?)?
            .into())
    }
}
