//! The single, fixed error type for the whole crate.
//!
//! No variant carries a payload, and no `Display` text is built from upstream
//! errors, caller input, key material or plaintext. Every failure that crosses
//! the (future) native boundary is one of these codes and nothing else.

use thiserror::Error;

pub type Result<T> = std::result::Result<T, Error>;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
pub enum Error {
    /// The handle was locked or wiped; it can never be used again.
    #[error("locked")]
    Locked,
    /// Wrong store key, foreign/renamed file, or a store that fails integrity checks.
    #[error("unlock_failed")]
    UnlockFailed,
    /// Another handle already owns this namespace's store file.
    #[error("store_in_use")]
    StoreInUse,
    /// A storage operation failed; nothing was committed.
    #[error("storage_failed")]
    Storage,
    /// A stored record failed authentication or version checks after unlock.
    #[error("store_corrupt")]
    StoreCorrupt,
    /// The user id / generation given does not match the unlocked namespace.
    #[error("namespace_mismatch")]
    NamespaceMismatch,
    #[error("device_exists")]
    DeviceExists,
    #[error("no_device")]
    NoDevice,
    /// `mark_published` has not recorded this device's server id yet.
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
    /// The `sender` description does not match this device's own identity.
    #[error("sender_mismatch")]
    SenderMismatch,
    /// A recipient has no session and no claimed key was supplied.
    #[error("missing_claimed_key")]
    MissingClaimedKey,
    /// Hard stop: a peer is not in the `Trusted` state, so nothing is encrypted.
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
    /// The decrypted envelope is malformed or an unsupported version/kind.
    #[error("invalid_envelope")]
    InvalidEnvelope,
    /// The decrypted envelope is well formed but disagrees with the expected routing.
    #[error("envelope_mismatch")]
    EnvelopeMismatch,
    #[error("replay")]
    Replay,
    #[error("internal")]
    Internal,
}

impl Error {
    /// Stable machine-readable code for the binding layer.
    pub const fn code(self) -> &'static str {
        match self {
            Error::Locked => "locked",
            Error::UnlockFailed => "unlock_failed",
            Error::StoreInUse => "store_in_use",
            Error::Storage => "storage_failed",
            Error::StoreCorrupt => "store_corrupt",
            Error::NamespaceMismatch => "namespace_mismatch",
            Error::DeviceExists => "device_exists",
            Error::NoDevice => "no_device",
            Error::NotPublished => "not_published",
            Error::InvalidInput => "invalid_input",
            Error::BodyInvalid => "body_invalid",
            Error::BodyTooLarge => "body_too_large",
            Error::InvalidSignature => "invalid_signature",
            Error::InvalidKey => "invalid_key",
            Error::UnknownKey => "unknown_key",
            Error::TooManyKeys => "too_many_keys",
            Error::SenderMismatch => "sender_mismatch",
            Error::MissingClaimedKey => "missing_claimed_key",
            Error::TrustHardStop => "trust_hard_stop",
            Error::UnknownPeer => "unknown_peer",
            Error::NothingToAccept => "nothing_to_accept",
            Error::ClientMessageIdReused => "client_message_id_reused",
            Error::CiphertextTooLarge => "ciphertext_too_large",
            Error::EncryptFailed => "encrypt_failed",
            Error::DecryptFailed => "decrypt_failed",
            Error::InvalidEnvelope => "invalid_envelope",
            Error::EnvelopeMismatch => "envelope_mismatch",
            Error::Replay => "replay",
            Error::Internal => "internal",
        }
    }
}

impl From<rusqlite::Error> for Error {
    /// Upstream text is dropped on purpose; a SQLite failure is just `Storage`.
    fn from(_: rusqlite::Error) -> Self {
        Error::Storage
    }
}
