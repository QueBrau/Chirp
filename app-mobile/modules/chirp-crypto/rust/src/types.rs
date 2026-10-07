//! Public value types. Everything here is plain data with fixed-size arrays so
//! the binding layer can map it one-to-one; none of it holds private key material.

use std::fmt;

use serde::{Deserialize, Serialize};

/// Raw 16-byte UUIDs. The textual form only exists inside the encrypted envelope.
pub type UserId = [u8; 16];
pub type DeviceId = [u8; 16];
pub type ConversationId = [u8; 16];
pub type MessageId = [u8; 16];
pub type ClientMessageId = [u8; 16];

pub type PublicKey32 = [u8; 32];
pub type Signature64 = [u8; 64];

/// One account namespace: the store file, its AAD and every record are scoped to this.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Namespace {
    pub user_id: UserId,
    pub generation: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PublicIdentity {
    pub curve25519: PublicKey32,
    pub ed25519: PublicKey32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum KeyKind {
    OneTime,
    Fallback,
}

impl KeyKind {
    /// The exact UTF-8 string that is signed.
    pub const fn as_str(self) -> &'static str {
        match self {
            KeyKind::OneTime => "one_time",
            KeyKind::Fallback => "fallback",
        }
    }
}

/// A public one-time or fallback key with the owning device's signature over it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SignedKey {
    /// Wire id: unique within the device, never reused, shared by both kinds.
    pub key_id: u64,
    pub kind: KeyKind,
    pub public_key: PublicKey32,
    pub signature: Signature64,
}

#[derive(Debug, Clone)]
pub struct CreatedDevice {
    pub identity: PublicIdentity,
    pub binding_signature: Signature64,
    pub one_time_keys: Vec<SignedKey>,
    pub fallback_key: SignedKey,
}

/// A device that an already-approved device vouched for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DeviceApproval {
    pub approver_ed25519: PublicKey32,
    pub signature: Signature64,
}

/// One entry of a user's device directory, as read from the server and verified
/// by the core. `approval == None` means "self-approved root".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DirectoryDevice {
    pub user_id: UserId,
    pub device_id: DeviceId,
    pub generation: u32,
    pub identity_curve25519: PublicKey32,
    pub identity_ed25519: PublicKey32,
    pub binding_signature: Signature64,
    pub approval: Option<DeviceApproval>,
}

/// What `approve_device` needs: the new device's public binding.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NewDeviceBinding {
    pub user_id: UserId,
    pub generation: u32,
    pub identity_curve25519: PublicKey32,
    pub identity_ed25519: PublicKey32,
    pub binding_signature: Signature64,
}

/// A recipient device. `claimed_key` is only needed when there is no session yet.
#[derive(Debug, Clone)]
pub struct Recipient {
    pub device: DirectoryDevice,
    pub claimed_key: Option<SignedKey>,
}

/// One ciphertext leg. `olm_type` is 0 (prekey) or 1 (normal).
#[derive(Clone, PartialEq, Eq)]
pub struct Leg {
    pub recipient_device_id: DeviceId,
    pub olm_type: u8,
    pub ciphertext: Vec<u8>,
}

impl fmt::Debug for Leg {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Leg")
            .field("olm_type", &self.olm_type)
            .field("ciphertext_len", &self.ciphertext.len())
            .finish_non_exhaustive()
    }
}

/// The routing a received leg is expected to carry (from the outer, untrusted
/// message plus the authenticated directory). All six fields are compared with
/// the decrypted inner envelope.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ExpectedEnvelope {
    pub conversation_id: ConversationId,
    pub client_message_id: ClientMessageId,
    pub sender_user_id: UserId,
    pub sender_device_id: DeviceId,
    pub recipient_user_id: UserId,
    pub recipient_device_id: DeviceId,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TrustState {
    Trusted,
    IdentityChanged,
    UnapprovedDevice,
}

impl TrustState {
    pub const fn as_str(self) -> &'static str {
        match self {
            TrustState::Trusted => "trusted",
            TrustState::IdentityChanged => "identity_changed",
            TrustState::UnapprovedDevice => "unapproved_device",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Direction {
    Incoming,
    Outgoing,
}

/// A committed plaintext message from the local store. `Debug` never prints the body.
#[derive(Clone, PartialEq, Eq)]
pub struct PlaintextMessage {
    /// Local, store-assigned, strictly increasing. Cursor for `list_messages`.
    pub seq: u64,
    pub direction: Direction,
    /// Server message id. `None` for messages this device sent (the id does not exist at
    /// encryption time).
    pub message_id: Option<MessageId>,
    pub conversation_id: ConversationId,
    pub client_message_id: ClientMessageId,
    pub sender_user_id: UserId,
    pub sender_device_id: DeviceId,
    /// Always `"text"` in v1.
    pub kind: String,
    pub body: String,
    /// The sender device's standing against our pins when this was received
    /// (`Trusted` for messages we sent). Receiving is never blocked by trust; the UI
    /// uses this to flag the message.
    pub sender_trust: TrustState,
    /// Local wall clock at commit, milliseconds since the Unix epoch. Display only;
    /// not authenticated and not the server timestamp.
    pub stored_at_ms: u64,
}

impl fmt::Debug for PlaintextMessage {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("PlaintextMessage")
            .field("seq", &self.seq)
            .field("direction", &self.direction)
            .field("body", &"<redacted>")
            .finish_non_exhaustive()
    }
}

/// Counts for the client's refill logic.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct KeyStatus {
    /// Generated but not yet confirmed published via `mark_published`.
    pub pending: usize,
    /// Published and not yet consumed by a peer's prekey message.
    pub published_one_time: usize,
    pub has_fallback: bool,
}
