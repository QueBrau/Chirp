//! Shapes of the records that live (encrypted) in the store. Internal: none of this
//! is public API and none of it leaves the crate.

use serde::{Deserialize, Deserializer, Serialize, Serializer};
use vodozemac::olm::SessionPickle;
use zeroize::Zeroizing;

use crate::envelope::{hex, unhex};
use crate::error::{Error, Result};
use crate::types::{Direction, KeyKind, TrustState};

/// Serde helper: fixed-size byte arrays as lowercase hex strings.
pub(crate) mod hexser {
    use super::*;

    pub fn serialize<S: Serializer, const N: usize>(
        value: &[u8; N],
        serializer: S,
    ) -> std::result::Result<S::Ok, S::Error> {
        serializer.serialize_str(&hex(value))
    }

    pub fn deserialize<'de, D: Deserializer<'de>, const N: usize>(
        deserializer: D,
    ) -> std::result::Result<[u8; N], D::Error> {
        let text = String::deserialize(deserializer)?;
        unhex::<N>(&text).ok_or_else(|| serde::de::Error::custom("bad hex"))
    }
}

/// Serde helper: `Option<[u8; N]>` as an optional hex string.
pub(crate) mod hexopt {
    use super::*;

    pub fn serialize<S: Serializer, const N: usize>(
        value: &Option<[u8; N]>,
        serializer: S,
    ) -> std::result::Result<S::Ok, S::Error> {
        match value {
            Some(v) => serializer.serialize_some(&hex(v)),
            None => serializer.serialize_none(),
        }
    }

    pub fn deserialize<'de, D: Deserializer<'de>, const N: usize>(
        deserializer: D,
    ) -> std::result::Result<Option<[u8; N]>, D::Error> {
        match Option::<String>::deserialize(deserializer)? {
            Some(text) => unhex::<N>(&text)
                .map(Some)
                .ok_or_else(|| serde::de::Error::custom("bad hex")),
            None => Ok(None),
        }
    }
}

/// Serde helper: raw bytes as unpadded base64 (ciphertext legs).
pub(crate) mod b64ser {
    use super::*;

    pub fn serialize<S: Serializer>(
        value: &Vec<u8>,
        serializer: S,
    ) -> std::result::Result<S::Ok, S::Error> {
        serializer.serialize_str(&vodozemac::base64_encode(value))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> std::result::Result<Vec<u8>, D::Error> {
        let text = String::deserialize(deserializer)?;
        vodozemac::base64_decode(text).map_err(|_| serde::de::Error::custom("bad base64"))
    }
}

pub(crate) fn to_json<T: Serialize>(value: &T) -> Result<Zeroizing<Vec<u8>>> {
    serde_json::to_vec(value)
        .map(Zeroizing::new)
        .map_err(|_| Error::Internal)
}

/// Parse a stored record. A record that authenticated but does not parse means the
/// store was written by something else, so it is `StoreCorrupt`.
pub(crate) fn from_json<'a, T: Deserialize<'a>>(bytes: &'a [u8]) -> Result<T> {
    serde_json::from_slice(bytes).map_err(|_| Error::StoreCorrupt)
}

/// This device's identity and server registration state.
#[derive(Serialize, Deserialize)]
pub(crate) struct DeviceRecord {
    #[serde(with = "hexser")]
    pub user_id: [u8; 16],
    pub generation: u32,
    #[serde(with = "hexser")]
    pub curve25519: [u8; 32],
    #[serde(with = "hexser")]
    pub ed25519: [u8; 32],
    #[serde(with = "hexopt")]
    pub server_device_id: Option<[u8; 16]>,
}

#[derive(Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum KeyState {
    /// Generated and signed, not yet confirmed as stored by the server.
    Pending,
    /// The server has it; a peer may claim it at any time.
    Published,
}

/// One live (unconsumed) key: the wire id <-> vodozemac id mapping lives here.
#[derive(Clone, Serialize, Deserialize)]
pub(crate) struct KeyEntry {
    /// The id on the wire and in the signature; unique per device, never reused.
    pub wire_id: u64,
    pub kind: KeyKind,
    /// vodozemac's own `KeyId` counter value (OTKs and fallback keys count separately
    /// inside vodozemac, so these can collide across kinds; `wire_id` cannot).
    pub vodozemac_id: u64,
    #[serde(with = "hexser")]
    pub public_key: [u8; 32],
    pub state: KeyState,
}

#[derive(Serialize, Deserialize)]
pub(crate) struct KeyMap {
    /// Next wire id to hand out. Only ever increases, so ids are never reused even
    /// after the entries for consumed keys are dropped.
    pub next_wire_id: u64,
    pub entries: Vec<KeyEntry>,
}

impl KeyMap {
    pub(crate) fn new() -> Self {
        KeyMap {
            next_wire_id: 1,
            entries: Vec::new(),
        }
    }
}

/// Metadata for one Olm session with one peer device.
#[derive(Clone, Serialize, Deserialize)]
pub(crate) struct SessionMeta {
    pub session_id: String,
    #[serde(with = "hexser")]
    pub peer_user_id: [u8; 16],
    #[serde(with = "hexser")]
    pub peer_device_id: [u8; 16],
    #[serde(with = "hexser")]
    pub peer_curve25519: [u8; 32],
    #[serde(with = "hexser")]
    pub peer_ed25519: [u8; 32],
    /// Store generation at creation and at last use; orders sessions per peer.
    pub created_gen: u64,
    pub last_used_gen: u64,
}

#[derive(Serialize, Deserialize)]
pub(crate) struct SessionRecord {
    #[serde(flatten)]
    pub meta: SessionMeta,
    pub pickle: SessionPickle,
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct PinnedDevice {
    #[serde(with = "hexser")]
    pub device_id: [u8; 16],
    #[serde(with = "hexser")]
    pub ed25519: [u8; 32],
    #[serde(with = "hexser")]
    pub curve25519: [u8; 32],
    pub generation: u32,
    /// Ed25519 identity of the approving device; `None` for the self-approved root.
    #[serde(with = "hexopt")]
    pub approver: Option<[u8; 32]>,
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct TrustRecord {
    /// Devices whose identities are pinned (the trusted set).
    pub pinned: Vec<PinnedDevice>,
    /// Result of the last evaluation.
    pub state: TrustState,
    /// The directory as last observed when the state is not `Trusted`;
    /// `accept_identity_change` re-pins exactly this.
    pub pending: Vec<PinnedDevice>,
}

#[derive(Clone, Serialize, Deserialize)]
pub(crate) struct StoredLeg {
    #[serde(with = "hexser")]
    pub device_id: [u8; 16],
    pub olm_type: u8,
    #[serde(with = "b64ser")]
    pub ciphertext: Vec<u8>,
}

#[derive(Serialize, Deserialize)]
pub(crate) struct OutboxRecord {
    #[serde(with = "hexser")]
    pub conversation_id: [u8; 16],
    #[serde(with = "hexser")]
    pub client_message_id: [u8; 16],
    /// Binds the idempotency key to what was encrypted, without storing the body twice.
    #[serde(with = "hexser")]
    pub request_hash: [u8; 32],
    pub legs: Vec<StoredLeg>,
}

#[derive(Serialize, Deserialize)]
pub(crate) struct MessageRecord {
    pub seq: u64,
    pub direction: Direction,
    #[serde(with = "hexopt")]
    pub message_id: Option<[u8; 16]>,
    #[serde(with = "hexser")]
    pub conversation_id: [u8; 16],
    #[serde(with = "hexser")]
    pub client_message_id: [u8; 16],
    #[serde(with = "hexser")]
    pub sender_user_id: [u8; 16],
    #[serde(with = "hexser")]
    pub sender_device_id: [u8; 16],
    pub kind: String,
    pub body: String,
    pub sender_trust: TrustState,
    pub stored_at_ms: u64,
}
