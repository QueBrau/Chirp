//! The core: lifecycle, device keys, encrypt/decrypt, trust.
//!
//! # Execution model
//! A [`Core`] is a cheap, cloneable handle to ONE unlocked store. Every public call
//! takes the handle's mutex, so operations on a handle never interleave: that mutex is
//! the "serial executor". It guarantees mutual exclusion, NOT ordering, so the mobile
//! layer must also dispatch every call for an account on a single serial queue
//! (design section 2) or the order of sends/receives is up to thread scheduling.
//!
//! `lock()` and `wipe()` kill the handle for good, and every clone of it. A later
//! `unlock()` returns a brand new handle; handles from before stay dead.
//!
//! # Commit before release
//! No state lives in memory between calls. Each operation loads what it needs from the
//! store, works on those private copies (the "candidate"), stages every change in one
//! [`Tx`], commits it as ONE SQLite transaction, and only then builds its return value.
//! A failed or abandoned commit therefore leaves the old state exactly as it was, and
//! no plaintext or ciphertext from an uncommitted transition is ever returned.

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use sha2::{Digest, Sha256};
use vodozemac::olm::{Account, OlmMessage, Session, SessionConfig};
use vodozemac::{Curve25519PublicKey, KeyId};
use zeroize::Zeroizing;

use crate::encoding::{
    encode_device_approval, encode_device_binding, encode_signed_key, verify_device_binding,
    verify_signed_key,
};
use crate::envelope::{self, Envelope, MAX_LEG_B64_LEN};
use crate::error::{Error, Result};
use crate::records::{
    DeviceRecord, KeyEntry, KeyMap, KeyState, MessageRecord, OutboxRecord, SessionMeta,
    SessionRecord, StoredLeg, TrustRecord, from_json, to_json,
};
use crate::store::{
    Store, T_ACCOUNT, T_DEVICE, T_KEYS, T_MESSAGE, T_OUTBOX, T_REPLAY_CIPHER, T_REPLAY_MESSAGE,
    T_SESSION, T_TRUST, Tx, remove_namespace_files,
};
use crate::trust;
use crate::types::*;

/// Upper bounds. They exist so a hostile caller cannot make one call arbitrarily
/// expensive, and so vodozemac never silently evicts a still-published one-time key.
pub const MAX_KEYS_PER_CALL: usize = 100;
pub const MAX_STORED_ONE_TIME_KEYS: usize = 200;
pub const MAX_RECIPIENTS: usize = 64;
pub const MAX_LIST_LIMIT: u32 = 200;
/// Hard cap on a received leg's raw ciphertext (49,152 bytes is 65,536 base64 chars).
pub const MAX_LEG_BYTES: usize = 49_152;

const RID_SINGLETON: &[u8] = b"\x00";

#[cfg(test)]
thread_local! {
    /// Test hook: rewrite the plaintext envelope just before it is Olm-encrypted, so
    /// tests can produce legs with malformed or lying envelopes through the real path.
    pub(crate) static ENVELOPE_HOOK: std::cell::RefCell<Option<EnvelopeHook>> =
        const { std::cell::RefCell::new(None) };
}

#[cfg(test)]
pub(crate) type EnvelopeHook = Box<dyn Fn(&[u8]) -> Vec<u8>>;

/// Handle to one unlocked account store. See the module docs.
#[derive(Clone)]
pub struct Core {
    inner: Arc<Mutex<Option<Ready>>>,
}

impl std::fmt::Debug for Core {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Core(..)")
    }
}

struct Ready {
    store: Store,
}

impl Core {
    /// Unlock (creating the namespace's store if it does not exist). `store_key` must be
    /// exactly 32 bytes. Wrong key, foreign file or damage is `UnlockFailed`; a second
    /// open of a namespace that is already unlocked is `StoreInUse`.
    pub fn unlock(dir: &Path, namespace: Namespace, store_key: &[u8]) -> Result<Core> {
        let key: Zeroizing<[u8; 32]> =
            Zeroizing::new(store_key.try_into().map_err(|_| Error::InvalidInput)?);
        let store = Store::open(dir, namespace, &key)?;
        Ok(Core {
            inner: Arc::new(Mutex::new(Some(Ready { store }))),
        })
    }

    /// Delete a namespace's files without unlocking it (sign-out of a lost phone,
    /// "remove this phone"). The platform layer also destroys the store key.
    pub fn wipe_namespace(dir: &Path, namespace: Namespace) -> Result<()> {
        remove_namespace_files(dir, &namespace)
    }

    fn run<R>(&self, f: impl FnOnce(&mut Ready) -> Result<R>) -> Result<R> {
        let mut guard = match self.inner.lock() {
            Ok(guard) => guard,
            Err(poisoned) => {
                // A panic mid-operation: drop the store so nothing half-trusted is reused.
                *poisoned.into_inner() = None;
                return Err(Error::Internal);
            }
        };
        let ready = guard.as_mut().ok_or(Error::Locked)?;
        f(ready)
    }

    /// Invalidate this handle and every clone of it. Idempotent.
    pub fn lock(&self) {
        let mut guard = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        *guard = None;
    }

    pub fn is_unlocked(&self) -> bool {
        self.inner.lock().map(|g| g.is_some()).unwrap_or(false)
    }

    /// Delete this namespace's store and lock the handle.
    pub fn wipe(&self) -> Result<()> {
        let mut guard = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        let ready = guard.take().ok_or(Error::Locked)?;
        let namespace = ready.store.namespace();
        let path = ready.store.path().to_path_buf();
        drop(ready);
        let dir = path.parent().ok_or(Error::Internal)?;
        remove_namespace_files(dir, &namespace)
    }

    pub fn namespace(&self) -> Result<Namespace> {
        self.run(|r| Ok(r.store.namespace()))
    }

    /// Monotonic commit counter. The platform layer can pin it in the Keychain/Keystore
    /// to detect a whole-file rollback, which the store alone cannot.
    pub fn store_generation(&self) -> Result<u64> {
        self.run(|r| Ok(r.store.store_generation()))
    }

    pub fn has_device(&self) -> Result<bool> {
        self.run(|r| Ok(r.load_device()?.is_some()))
    }

    /// This device's public identity, if a device exists.
    pub fn public_identity(&self) -> Result<Option<PublicIdentity>> {
        self.run(|r| {
            Ok(r.load_device()?.map(|d| PublicIdentity {
                curve25519: d.curve25519,
                ed25519: d.ed25519,
            }))
        })
    }

    /// Create this namespace's device: identity keys, binding signature, signed one-time
    /// keys and a signed fallback key. Private material stays in the store.
    pub fn create_device(
        &self,
        user_id: UserId,
        generation: u32,
        otk_count: usize,
    ) -> Result<CreatedDevice> {
        self.run(|r| r.create_device(user_id, generation, otk_count))
    }

    /// Record the server's id for this device and confirm which keys the server stored.
    /// Idempotent for keys already confirmed.
    pub fn mark_published(&self, server_device_id: DeviceId, key_ids: &[u64]) -> Result<()> {
        self.run(|r| r.mark_published(server_device_id, key_ids))
    }

    /// Generate and sign `count` more one-time keys.
    pub fn top_up_keys(&self, count: usize) -> Result<Vec<SignedKey>> {
        self.run(|r| r.top_up_keys(count))
    }

    /// Generate and sign a new fallback key (vodozemac keeps the previous one for
    /// delayed messages and forgets the one before that).
    pub fn rotate_fallback_key(&self) -> Result<SignedKey> {
        self.run(|r| r.rotate_fallback_key())
    }

    /// Signed keys that were generated but never confirmed via `mark_published`, so a
    /// crash between generating and uploading loses nothing.
    pub fn pending_keys(&self) -> Result<Vec<SignedKey>> {
        self.run(|r| r.pending_keys())
    }

    pub fn key_status(&self) -> Result<KeyStatus> {
        self.run(|r| r.key_status())
    }

    /// One ciphertext leg per recipient device. See `Ready::encrypt`.
    pub fn encrypt(
        &self,
        conversation_id: ConversationId,
        client_message_id: ClientMessageId,
        body: &str,
        sender: &DirectoryDevice,
        recipients: &[Recipient],
    ) -> Result<Vec<Leg>> {
        self.run(|r| r.encrypt(conversation_id, client_message_id, body, sender, recipients))
    }

    /// Decrypt one leg addressed to this device. See `Ready::decrypt`.
    pub fn decrypt(
        &self,
        message_id: MessageId,
        leg: &Leg,
        sender: &DirectoryDevice,
        expected: &ExpectedEnvelope,
    ) -> Result<PlaintextMessage> {
        self.run(|r| r.decrypt(message_id, leg, sender, expected))
    }

    /// Newest-first page of a conversation from the local store. `before` is exclusive
    /// (pass the smallest `seq` of the previous page); `None` starts at the newest.
    pub fn list_messages(
        &self,
        conversation_id: ConversationId,
        before: Option<u64>,
        limit: u32,
    ) -> Result<Vec<PlaintextMessage>> {
        self.run(|r| r.list_messages(conversation_id, before, limit))
    }

    /// Record a peer user's current device directory and return the resulting trust
    /// state. First contact pins; later changes are classified, never silently pinned.
    pub fn observe_directory(
        &self,
        user_id: UserId,
        devices: &[DirectoryDevice],
    ) -> Result<TrustState> {
        self.run(|r| r.observe_directory(user_id, devices))
    }

    /// The state from the last observation of this user. `UnknownPeer` if never seen.
    pub fn trust_state(&self, peer: UserId) -> Result<TrustState> {
        self.run(|r| r.trust_state(peer))
    }

    /// The user accepted the change: re-pin exactly the directory last observed.
    pub fn accept_identity_change(&self, peer: UserId) -> Result<()> {
        self.run(|r| r.accept_identity_change(peer))
    }

    /// Sign the approval of a new device of THIS account.
    pub fn approve_device(&self, new_device: &NewDeviceBinding) -> Result<DeviceApproval> {
        self.run(|r| r.approve_device(new_device))
    }

    #[cfg(test)]
    pub(crate) fn exec_sql_for_test(&self, sql: &str) {
        self.run(|r| {
            r.store.exec_for_test(sql);
            Ok(())
        })
        .unwrap();
    }

    #[cfg(test)]
    pub(crate) fn inject_fail(&self, point: crate::store::FailPoint) {
        self.run(|r| {
            r.store.fail = Some(point);
            Ok(())
        })
        .unwrap();
    }
}

fn sha256(parts: &[&[u8]]) -> [u8; 32] {
    let mut h = Sha256::new();
    for part in parts {
        h.update((part.len() as u32).to_be_bytes());
        h.update(part);
    }
    h.finalize().into()
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// vodozemac only exposes `KeyId::to_base64` (the big-endian counter, base64). Decode it.
fn key_id_u64(id: KeyId) -> Result<u64> {
    let bytes = vodozemac::base64_decode(id.to_base64()).map_err(|_| Error::Internal)?;
    let array: [u8; 8] = bytes.try_into().map_err(|_| Error::Internal)?;
    Ok(u64::from_be_bytes(array))
}

fn check_directory_device(device: &DirectoryDevice) -> Result<()> {
    verify_device_binding(
        &device.user_id,
        device.generation,
        &device.identity_curve25519,
        &device.identity_ed25519,
        &device.binding_signature,
    )
}

impl Ready {
    fn namespace(&self) -> Namespace {
        self.store.namespace()
    }

    // ---- loading -------------------------------------------------------------------

    fn load_device(&self) -> Result<Option<DeviceRecord>> {
        match self.store.get(T_DEVICE, RID_SINGLETON)? {
            Some(bytes) => Ok(Some(from_json(&bytes)?)),
            None => Ok(None),
        }
    }

    fn require_device(&self) -> Result<DeviceRecord> {
        self.load_device()?.ok_or(Error::NoDevice)
    }

    fn load_account(&self) -> Result<Account> {
        let bytes = self
            .store
            .get(T_ACCOUNT, RID_SINGLETON)?
            .ok_or(Error::NoDevice)?;
        Ok(Account::from_pickle(from_json(&bytes)?))
    }

    fn stage_account(&self, tx: &mut Tx, account: &Account) -> Result<()> {
        tx.put(
            T_ACCOUNT,
            RID_SINGLETON.to_vec(),
            None,
            to_json(&account.pickle())?,
        )
    }

    fn load_keymap(&self) -> Result<KeyMap> {
        let bytes = self
            .store
            .get(T_KEYS, RID_SINGLETON)?
            .ok_or(Error::NoDevice)?;
        from_json(&bytes)
    }

    fn stage_keymap(&self, tx: &mut Tx, keymap: &KeyMap) -> Result<()> {
        tx.put(T_KEYS, RID_SINGLETON.to_vec(), None, to_json(keymap)?)
    }

    fn trust_rid(&self, user: &UserId) -> Vec<u8> {
        self.store.idx("trust", user)
    }

    fn load_trust(&self, user: &UserId) -> Result<Option<TrustRecord>> {
        match self.store.get(T_TRUST, &self.trust_rid(user))? {
            Some(bytes) => Ok(Some(from_json(&bytes)?)),
            None => Ok(None),
        }
    }

    fn stage_trust(&self, tx: &mut Tx, user: &UserId, record: &TrustRecord) -> Result<()> {
        tx.put(T_TRUST, self.trust_rid(user), None, to_json(record)?)
    }

    fn session_rid(&self, session_id: &str) -> Vec<u8> {
        self.store.idx("sess", session_id.as_bytes())
    }

    fn peer_tag(&self, curve25519: &PublicKey32) -> Vec<u8> {
        self.store.idx("peer", curve25519)
    }

    /// All sessions with one peer device identity, most recently used first.
    fn sessions_for_peer(&self, curve25519: &PublicKey32) -> Result<Vec<(SessionMeta, Session)>> {
        let mut out = Vec::new();
        for (_, bytes) in self
            .store
            .get_tagged(T_SESSION, &self.peer_tag(curve25519))?
        {
            let record: SessionRecord = from_json(&bytes)?;
            out.push((record.meta, Session::from_pickle(record.pickle)));
        }
        out.sort_by_key(|(meta, _)| std::cmp::Reverse(meta.last_used_gen));
        Ok(out)
    }

    fn load_session_by_id(&self, session_id: &str) -> Result<Option<(SessionMeta, Session)>> {
        match self.store.get(T_SESSION, &self.session_rid(session_id))? {
            Some(bytes) => {
                let record: SessionRecord = from_json(&bytes)?;
                Ok(Some((record.meta, Session::from_pickle(record.pickle))))
            }
            None => Ok(None),
        }
    }

    fn stage_session(&self, tx: &mut Tx, meta: &SessionMeta, session: &Session) -> Result<()> {
        let record = SessionRecord {
            meta: meta.clone(),
            pickle: session.pickle(),
        };
        tx.put(
            T_SESSION,
            self.session_rid(&meta.session_id),
            Some(self.peer_tag(&meta.peer_curve25519)),
            to_json(&record)?,
        )
    }

    // ---- device and keys -------------------------------------------------------------

    /// Generate `otk_count` one-time keys and optionally one fallback key, record their
    /// wire ids and sign them. vodozemac's own "unpublished" set is cleared immediately
    /// so the key map is the single source of truth for publication state.
    fn generate_keys(
        account: &mut Account,
        keymap: &mut KeyMap,
        otk_count: usize,
        fallback: bool,
    ) -> Result<Vec<SignedKey>> {
        if otk_count > 0 {
            if account.stored_one_time_key_count() + otk_count > MAX_STORED_ONE_TIME_KEYS {
                return Err(Error::TooManyKeys);
            }
            let result = account.generate_one_time_keys(otk_count);
            // The cap above keeps us far below vodozemac's eviction threshold.
            if !result.removed.is_empty() || result.created.len() != otk_count {
                return Err(Error::Internal);
            }
        }
        let mut fresh: Vec<(KeyKind, u64, [u8; 32])> = Vec::new();
        let mut otks: Vec<(u64, [u8; 32])> = Vec::new();
        for (id, key) in account.one_time_keys() {
            otks.push((key_id_u64(id)?, key.to_bytes()));
        }
        otks.sort_unstable();
        if otks.len() != otk_count {
            return Err(Error::Internal);
        }
        fresh.extend(otks.into_iter().map(|(i, k)| (KeyKind::OneTime, i, k)));
        if fallback {
            account.generate_fallback_key();
            let current = account.fallback_key();
            if current.len() != 1 {
                return Err(Error::Internal);
            }
            let (id, key) = current.into_iter().next().ok_or(Error::Internal)?;
            fresh.push((KeyKind::Fallback, key_id_u64(id)?, key.to_bytes()));
        }
        account.mark_keys_as_published();

        let curve = account.curve25519_key().to_bytes();
        let mut signed = Vec::with_capacity(fresh.len());
        for (kind, vodozemac_id, public_key) in fresh {
            let wire_id = keymap.next_wire_id;
            keymap.next_wire_id = wire_id.checked_add(1).ok_or(Error::Internal)?;
            keymap.entries.push(KeyEntry {
                wire_id,
                kind,
                vodozemac_id,
                public_key,
                state: KeyState::Pending,
            });
            signed.push(Self::sign_key(account, kind, wire_id, public_key, &curve));
        }
        if fallback {
            // vodozemac holds the current and the previous fallback key only.
            let mut fallbacks: Vec<u64> = keymap
                .entries
                .iter()
                .filter(|e| e.kind == KeyKind::Fallback)
                .map(|e| e.wire_id)
                .collect();
            fallbacks.sort_unstable();
            while fallbacks.len() > 2 {
                let dropped = fallbacks.remove(0);
                keymap.entries.retain(|e| e.wire_id != dropped);
            }
        }
        Ok(signed)
    }

    fn sign_key(
        account: &Account,
        kind: KeyKind,
        wire_id: u64,
        public_key: [u8; 32],
        owner_curve: &[u8; 32],
    ) -> SignedKey {
        let message = encode_signed_key(kind, wire_id, &public_key, owner_curve);
        SignedKey {
            key_id: wire_id,
            kind,
            public_key,
            signature: account.sign(message).to_bytes(),
        }
    }

    fn create_device(
        &mut self,
        user_id: UserId,
        generation: u32,
        otk_count: usize,
    ) -> Result<CreatedDevice> {
        let ns = self.namespace();
        if user_id != ns.user_id || generation != ns.generation {
            return Err(Error::NamespaceMismatch);
        }
        if otk_count == 0 || otk_count > MAX_KEYS_PER_CALL {
            return Err(Error::InvalidInput);
        }
        if self.load_device()?.is_some() {
            return Err(Error::DeviceExists);
        }
        let mut account = Account::new();
        let identity = PublicIdentity {
            curve25519: account.curve25519_key().to_bytes(),
            ed25519: account.ed25519_key().as_bytes().to_owned(),
        };
        let binding_signature = account
            .sign(encode_device_binding(
                &user_id,
                generation,
                &identity.curve25519,
                &identity.ed25519,
            ))
            .to_bytes();
        let mut keymap = KeyMap::new();
        let mut signed = Self::generate_keys(&mut account, &mut keymap, otk_count, true)?;
        let fallback_key = signed.pop().ok_or(Error::Internal)?;

        let device = DeviceRecord {
            user_id,
            generation,
            curve25519: identity.curve25519,
            ed25519: identity.ed25519,
            server_device_id: None,
        };
        let mut tx = Tx::new();
        self.stage_account(&mut tx, &account)?;
        self.stage_keymap(&mut tx, &keymap)?;
        tx.put(T_DEVICE, RID_SINGLETON.to_vec(), None, to_json(&device)?)?;
        self.store.commit(tx)?;
        Ok(CreatedDevice {
            identity,
            binding_signature,
            one_time_keys: signed,
            fallback_key,
        })
    }

    fn mark_published(&mut self, server_device_id: DeviceId, key_ids: &[u64]) -> Result<()> {
        let mut device = self.require_device()?;
        match device.server_device_id {
            Some(existing) if existing != server_device_id => return Err(Error::InvalidInput),
            _ => {}
        }
        let mut keymap = self.load_keymap()?;
        // Validate every id before touching anything: all or nothing.
        for id in key_ids {
            if !keymap.entries.iter().any(|e| e.wire_id == *id) {
                return Err(Error::UnknownKey);
            }
        }
        for entry in &mut keymap.entries {
            if key_ids.contains(&entry.wire_id) {
                entry.state = KeyState::Published;
            }
        }
        device.server_device_id = Some(server_device_id);
        let mut tx = Tx::new();
        tx.put(T_DEVICE, RID_SINGLETON.to_vec(), None, to_json(&device)?)?;
        self.stage_keymap(&mut tx, &keymap)?;
        self.store.commit(tx)
    }

    fn top_up_keys(&mut self, count: usize) -> Result<Vec<SignedKey>> {
        self.require_device()?;
        if count == 0 || count > MAX_KEYS_PER_CALL {
            return Err(Error::InvalidInput);
        }
        let mut account = self.load_account()?;
        let mut keymap = self.load_keymap()?;
        let signed = Self::generate_keys(&mut account, &mut keymap, count, false)?;
        let mut tx = Tx::new();
        self.stage_account(&mut tx, &account)?;
        self.stage_keymap(&mut tx, &keymap)?;
        self.store.commit(tx)?;
        Ok(signed)
    }

    fn rotate_fallback_key(&mut self) -> Result<SignedKey> {
        self.require_device()?;
        let mut account = self.load_account()?;
        let mut keymap = self.load_keymap()?;
        let mut signed = Self::generate_keys(&mut account, &mut keymap, 0, true)?;
        let key = signed.pop().ok_or(Error::Internal)?;
        let mut tx = Tx::new();
        self.stage_account(&mut tx, &account)?;
        self.stage_keymap(&mut tx, &keymap)?;
        self.store.commit(tx)?;
        Ok(key)
    }

    fn pending_keys(&mut self) -> Result<Vec<SignedKey>> {
        let device = self.require_device()?;
        let account = self.load_account()?;
        let keymap = self.load_keymap()?;
        let mut out: Vec<SignedKey> = keymap
            .entries
            .iter()
            .filter(|e| e.state == KeyState::Pending)
            .map(|e| {
                Self::sign_key(
                    &account,
                    e.kind,
                    e.wire_id,
                    e.public_key,
                    &device.curve25519,
                )
            })
            .collect();
        out.sort_by_key(|k| k.key_id);
        Ok(out)
    }

    fn key_status(&mut self) -> Result<KeyStatus> {
        self.require_device()?;
        let keymap = self.load_keymap()?;
        let count = |kind: KeyKind, state: KeyState| {
            keymap
                .entries
                .iter()
                .filter(|e| e.kind == kind && e.state == state)
                .count()
        };
        Ok(KeyStatus {
            pending: keymap
                .entries
                .iter()
                .filter(|e| e.state == KeyState::Pending)
                .count(),
            published_one_time: count(KeyKind::OneTime, KeyState::Published),
            has_fallback: keymap.entries.iter().any(|e| e.kind == KeyKind::Fallback),
        })
    }

    // ---- trust --------------------------------------------------------------------

    /// Evaluate one user's directory, staging the updated trust record if it changed.
    fn evaluate_user(
        &self,
        tx: &mut Tx,
        user: &UserId,
        devices: &[DirectoryDevice],
    ) -> Result<TrustState> {
        let existing = self.load_trust(user)?;
        let old_pins = existing
            .as_ref()
            .map(|r| r.pinned.clone())
            .unwrap_or_default();
        let (state, new_pins) = trust::evaluate(&old_pins, devices)?;
        let record = if state == TrustState::Trusted {
            TrustRecord {
                pinned: new_pins,
                state,
                pending: Vec::new(),
            }
        } else {
            TrustRecord {
                pinned: old_pins,
                state,
                pending: devices.iter().map(trust::pin_of).collect(),
            }
        };
        if existing.as_ref() != Some(&record) {
            self.stage_trust(tx, user, &record)?;
        }
        Ok(state)
    }

    fn observe_directory(
        &mut self,
        user_id: UserId,
        devices: &[DirectoryDevice],
    ) -> Result<TrustState> {
        self.require_device()?;
        if devices.is_empty() || devices.len() > MAX_RECIPIENTS {
            return Err(Error::InvalidInput);
        }
        for device in devices {
            if device.user_id != user_id {
                return Err(Error::InvalidInput);
            }
            check_directory_device(device)?;
        }
        let mut tx = Tx::new();
        let state = self.evaluate_user(&mut tx, &user_id, devices)?;
        self.store.commit(tx)?;
        Ok(state)
    }

    fn trust_state(&mut self, peer: UserId) -> Result<TrustState> {
        self.require_device()?;
        Ok(self.load_trust(&peer)?.ok_or(Error::UnknownPeer)?.state)
    }

    fn accept_identity_change(&mut self, peer: UserId) -> Result<()> {
        self.require_device()?;
        let mut record = self.load_trust(&peer)?.ok_or(Error::UnknownPeer)?;
        if record.state == TrustState::Trusted || record.pending.is_empty() {
            return Err(Error::NothingToAccept);
        }
        record.pinned = std::mem::take(&mut record.pending);
        record.state = TrustState::Trusted;
        let mut tx = Tx::new();
        self.stage_trust(&mut tx, &peer, &record)?;
        self.store.commit(tx)
    }

    fn approve_device(&mut self, new_device: &NewDeviceBinding) -> Result<DeviceApproval> {
        let device = self.require_device()?;
        if new_device.user_id != device.user_id {
            return Err(Error::InvalidInput);
        }
        if new_device.identity_ed25519 == device.ed25519
            || new_device.identity_curve25519 == device.curve25519
        {
            return Err(Error::InvalidInput);
        }
        verify_device_binding(
            &new_device.user_id,
            new_device.generation,
            &new_device.identity_curve25519,
            &new_device.identity_ed25519,
            &new_device.binding_signature,
        )?;
        let account = self.load_account()?;
        let signature = account
            .sign(encode_device_approval(
                &new_device.user_id,
                new_device.generation,
                &new_device.identity_curve25519,
                &new_device.identity_ed25519,
            ))
            .to_bytes();
        Ok(DeviceApproval {
            approver_ed25519: device.ed25519,
            signature,
        })
    }

    // ---- messaging ----------------------------------------------------------------

    /// Check that `sender` describes exactly this device and return its server id.
    fn check_own_sender(
        &self,
        device: &DeviceRecord,
        sender: &DirectoryDevice,
    ) -> Result<DeviceId> {
        let server_id = device.server_device_id.ok_or(Error::NotPublished)?;
        if sender.user_id != device.user_id
            || sender.device_id != server_id
            || sender.generation != device.generation
            || sender.identity_curve25519 != device.curve25519
            || sender.identity_ed25519 != device.ed25519
        {
            return Err(Error::SenderMismatch);
        }
        check_directory_device(sender)?;
        Ok(server_id)
    }

    /// Encrypt one message for every recipient device.
    ///
    /// Order of checks: body limits, sender and recipient signatures, trust (hard stop
    /// for any non-`Trusted` user), idempotency, then crypto. Everything that changes
    /// (new sessions, advanced ratchets, outbox row, our own plaintext copy, pin updates)
    /// is committed in ONE transaction before the legs are returned. A retry with the same
    /// `client_message_id` returns the stored legs without touching any ratchet; recipients
    /// that were not in the first call (a refreshed device list) get new legs appended.
    fn encrypt(
        &mut self,
        conversation_id: ConversationId,
        client_message_id: ClientMessageId,
        body: &str,
        sender: &DirectoryDevice,
        recipients: &[Recipient],
    ) -> Result<Vec<Leg>> {
        envelope::validate_body(body)?;
        if recipients.is_empty() || recipients.len() > MAX_RECIPIENTS {
            return Err(Error::InvalidInput);
        }
        let device = self.require_device()?;
        let server_id = self.check_own_sender(&device, sender)?;
        let ns = self.namespace();

        for (i, r) in recipients.iter().enumerate() {
            check_directory_device(&r.device)?;
            if r.device.device_id == server_id
                || r.device.identity_ed25519 == device.ed25519
                || r.device.identity_curve25519 == device.curve25519
            {
                return Err(Error::InvalidInput);
            }
            if recipients[..i].iter().any(|o| {
                o.device.device_id == r.device.device_id
                    || o.device.identity_curve25519 == r.device.identity_curve25519
            }) {
                return Err(Error::InvalidInput);
            }
        }

        // Trust: group by user, with our own device joining our own account's group.
        let mut groups: BTreeMap<UserId, Vec<DirectoryDevice>> = BTreeMap::new();
        for r in recipients {
            groups
                .entry(r.device.user_id)
                .or_default()
                .push(r.device.clone());
        }
        groups.entry(ns.user_id).or_default().push(sender.clone());
        let mut tx = Tx::new();
        let mut blocked = false;
        for (user, devices) in &groups {
            if self.evaluate_user(&mut tx, user, devices)? != TrustState::Trusted {
                blocked = true;
            }
        }
        if blocked {
            // Persist what we saw (so `trust_state` and `accept_identity_change` work),
            // but encrypt nothing.
            self.store.commit(tx)?;
            return Err(Error::TrustHardStop);
        }

        // Idempotency.
        let request_hash = sha256(&[
            b"chirp-e2ee-v1/outbox-request",
            &conversation_id,
            body.as_bytes(),
        ]);
        let outbox_rid = self.store.idx("outbox", &client_message_id);
        let existing: Option<OutboxRecord> = match self.store.get(T_OUTBOX, &outbox_rid)? {
            Some(bytes) => Some(from_json(&bytes)?),
            None => None,
        };
        if let Some(ob) = &existing
            && (ob.request_hash != request_hash || ob.conversation_id != conversation_id)
        {
            return Err(Error::ClientMessageIdReused);
        }
        let mut legs: Vec<StoredLeg> = existing
            .as_ref()
            .map(|o| o.legs.clone())
            .unwrap_or_default();
        let missing: Vec<&Recipient> = recipients
            .iter()
            .filter(|r| !legs.iter().any(|l| l.device_id == r.device.device_id))
            .collect();

        if missing.is_empty() {
            self.store.commit(tx)?; // pin updates only, if any
            return Ok(order_legs(recipients, &legs));
        }

        let now_gen = self.store.store_generation() + 1;
        let account = self.load_account()?;
        for r in missing {
            let mut found = self.sessions_for_peer(&r.device.identity_curve25519)?;
            let (mut meta, mut session) = if found.is_empty() {
                let claimed = r.claimed_key.ok_or(Error::MissingClaimedKey)?;
                verify_signed_key(
                    &r.device.identity_ed25519,
                    &r.device.identity_curve25519,
                    &claimed,
                )?;
                let session = account
                    .create_outbound_session(
                        SessionConfig::version_1(),
                        Curve25519PublicKey::from_bytes(r.device.identity_curve25519),
                        Curve25519PublicKey::from_bytes(claimed.public_key),
                    )
                    .map_err(|_| Error::InvalidKey)?;
                let meta = SessionMeta {
                    session_id: session.session_id(),
                    peer_user_id: r.device.user_id,
                    peer_device_id: r.device.device_id,
                    peer_curve25519: r.device.identity_curve25519,
                    peer_ed25519: r.device.identity_ed25519,
                    created_gen: now_gen,
                    last_used_gen: now_gen,
                };
                (meta, session)
            } else {
                found.swap_remove(0)
            };

            let plaintext = envelope::encode(&Envelope {
                conversation_id,
                client_message_id,
                sender_user_id: ns.user_id,
                sender_device_id: server_id,
                recipient_user_id: r.device.user_id,
                recipient_device_id: r.device.device_id,
                body: body.to_owned(),
            })?;
            #[cfg(test)]
            let plaintext = ENVELOPE_HOOK.with(|h| match h.borrow().as_ref() {
                Some(f) => Zeroizing::new(f(&plaintext)),
                None => plaintext,
            });
            let message = session
                .encrypt(&plaintext[..])
                .map_err(|_| Error::EncryptFailed)?;
            let (olm_type, ciphertext) = message.to_parts();
            if ciphertext.len() > MAX_LEG_BYTES
                || 4 * ciphertext.len().div_ceil(3) > MAX_LEG_B64_LEN
            {
                return Err(Error::CiphertextTooLarge);
            }
            meta.last_used_gen = now_gen;
            self.stage_session(&mut tx, &meta, &session)?;
            legs.push(StoredLeg {
                device_id: r.device.device_id,
                olm_type: u8::try_from(olm_type).map_err(|_| Error::Internal)?,
                ciphertext,
            });
        }

        let outbox = OutboxRecord {
            conversation_id,
            client_message_id,
            request_hash,
            legs,
        };
        tx.put(T_OUTBOX, outbox_rid, None, to_json(&outbox)?)?;
        if existing.is_none() {
            // Our own copy of what we sent, so local history shows both directions.
            let seq = self.store.next_seq();
            let record = MessageRecord {
                seq,
                direction: Direction::Outgoing,
                message_id: None,
                conversation_id,
                client_message_id,
                sender_user_id: ns.user_id,
                sender_device_id: server_id,
                kind: envelope::KIND_TEXT.to_owned(),
                body: body.to_owned(),
                sender_trust: TrustState::Trusted,
                stored_at_ms: now_ms(),
            };
            tx.put(
                T_MESSAGE,
                seq.to_be_bytes().to_vec(),
                Some(self.store.idx("conv", &conversation_id)),
                to_json(&record)?,
            )?;
            tx.next_seq = Some(seq + 1);
        }
        self.store.commit(tx)?;
        Ok(order_legs(recipients, &outbox.legs))
    }

    /// Decrypt one leg that the server says is addressed to this device.
    ///
    /// Nothing is persisted until every check has passed: signatures, routing, replay,
    /// Olm decryption on a private copy of the session (and of the account for a prekey
    /// message), strict envelope parsing, and field-by-field comparison against
    /// `expected`. A failure at any point discards the copies, so no one-time key is
    /// consumed and no ratchet moves. On success the ratchet, the replay marks and the
    /// plaintext row are committed in ONE transaction, and only then is the plaintext
    /// returned.
    fn decrypt(
        &mut self,
        message_id: MessageId,
        leg: &Leg,
        sender: &DirectoryDevice,
        expected: &ExpectedEnvelope,
    ) -> Result<PlaintextMessage> {
        let device = self.require_device()?;
        let server_id = device.server_device_id.ok_or(Error::NotPublished)?;
        let ns = self.namespace();

        // Routing the outer message claims must agree with who we are and who is sending.
        if expected.recipient_user_id != ns.user_id || expected.recipient_device_id != server_id {
            return Err(Error::EnvelopeMismatch);
        }
        if expected.sender_user_id != sender.user_id
            || expected.sender_device_id != sender.device_id
        {
            return Err(Error::EnvelopeMismatch);
        }
        if sender.device_id == server_id
            || sender.identity_ed25519 == device.ed25519
            || sender.identity_curve25519 == device.curve25519
        {
            return Err(Error::InvalidInput);
        }
        check_directory_device(sender)?;
        if leg.olm_type > 1 || leg.ciphertext.is_empty() || leg.ciphertext.len() > MAX_LEG_BYTES {
            return Err(Error::InvalidInput);
        }

        // Replay: by ciphertext (covers fallback-key prekey replays, which would
        // otherwise start a fresh session) and by the sender's logical message id.
        let cipher_hash = sha256(&[&[leg.olm_type], &leg.ciphertext]);
        let replay_cipher_rid = self.store.idx("rplc", &cipher_hash);
        let replay_message_rid = self.store.idx(
            "rplm",
            &[&sender.device_id[..], &expected.client_message_id[..]].concat(),
        );
        if self.store.exists(T_REPLAY_CIPHER, &replay_cipher_rid)?
            || self.store.exists(T_REPLAY_MESSAGE, &replay_message_rid)?
        {
            return Err(Error::Replay);
        }

        let message = OlmMessage::from_parts(usize::from(leg.olm_type), &leg.ciphertext)
            .map_err(|_| Error::DecryptFailed)?;
        let sender_curve = Curve25519PublicKey::from_bytes(sender.identity_curve25519);
        let now_gen = self.store.store_generation() + 1;

        // Candidate state. `account_after` is Some only if a one-time key was consumed.
        let mut consumed_otk: Option<[u8; 32]> = None;
        let mut account_after: Option<Account> = None;
        let (meta, session, plaintext): (SessionMeta, Session, Zeroizing<Vec<u8>>) = match &message
        {
            OlmMessage::PreKey(prekey) => {
                if prekey.identity_key() != sender_curve {
                    return Err(Error::DecryptFailed);
                }
                if let Some((meta, mut session)) = self.load_session_by_id(&prekey.session_id())? {
                    if meta.peer_curve25519 != sender.identity_curve25519 {
                        return Err(Error::DecryptFailed);
                    }
                    let plain = session
                        .decrypt(&message)
                        .map_err(|_| Error::DecryptFailed)?;
                    (meta, session, Zeroizing::new(plain))
                } else {
                    let mut account = self.load_account()?;
                    let before = account.stored_one_time_key_count();
                    let created = account
                        .create_inbound_session(SessionConfig::version_1(), sender_curve, prekey)
                        .map_err(|_| Error::DecryptFailed)?;
                    if account.stored_one_time_key_count() < before {
                        consumed_otk = Some(prekey.one_time_key().to_bytes());
                        account_after = Some(account);
                    }
                    let meta = SessionMeta {
                        session_id: created.session.session_id(),
                        peer_user_id: sender.user_id,
                        peer_device_id: sender.device_id,
                        peer_curve25519: sender.identity_curve25519,
                        peer_ed25519: sender.identity_ed25519,
                        created_gen: now_gen,
                        last_used_gen: now_gen,
                    };
                    (meta, created.session, Zeroizing::new(created.plaintext))
                }
            }
            OlmMessage::Normal(_) => {
                let mut result = None;
                for (meta, mut session) in self.sessions_for_peer(&sender.identity_curve25519)? {
                    if let Ok(plain) = session.decrypt(&message) {
                        result = Some((meta, session, Zeroizing::new(plain)));
                        break;
                    }
                }
                result.ok_or(Error::DecryptFailed)?
            }
        };

        // The envelope is only trusted once it matches the routing field by field.
        let env = envelope::decode(&plaintext)?;
        if env.conversation_id != expected.conversation_id
            || env.client_message_id != expected.client_message_id
            || env.sender_user_id != expected.sender_user_id
            || env.sender_device_id != expected.sender_device_id
            || env.recipient_user_id != expected.recipient_user_id
            || env.recipient_device_id != expected.recipient_device_id
        {
            return Err(Error::EnvelopeMismatch);
        }

        let sender_trust = match self.load_trust(&sender.user_id)? {
            Some(record) => trust::sender_standing(&record.pinned, sender),
            None => TrustState::UnapprovedDevice,
        };

        // Stage everything, then commit once.
        let mut tx = Tx::new();
        let mut meta = meta;
        meta.last_used_gen = now_gen;
        self.stage_session(&mut tx, &meta, &session)?;
        if let Some(account) = &account_after {
            self.stage_account(&mut tx, account)?;
            let mut keymap = self.load_keymap()?;
            if let Some(used) = consumed_otk {
                keymap
                    .entries
                    .retain(|e| !(e.kind == KeyKind::OneTime && e.public_key == used));
            }
            self.stage_keymap(&mut tx, &keymap)?;
        }
        tx.put(
            T_REPLAY_CIPHER,
            replay_cipher_rid,
            None,
            Zeroizing::new(vec![1]),
        )?;
        tx.put(
            T_REPLAY_MESSAGE,
            replay_message_rid,
            None,
            Zeroizing::new(vec![1]),
        )?;
        let seq = self.store.next_seq();
        let record = MessageRecord {
            seq,
            direction: Direction::Incoming,
            message_id: Some(message_id),
            conversation_id: env.conversation_id,
            client_message_id: env.client_message_id,
            sender_user_id: env.sender_user_id,
            sender_device_id: env.sender_device_id,
            kind: envelope::KIND_TEXT.to_owned(),
            body: env.body,
            sender_trust,
            stored_at_ms: now_ms(),
        };
        tx.put(
            T_MESSAGE,
            seq.to_be_bytes().to_vec(),
            Some(self.store.idx("conv", &record.conversation_id)),
            to_json(&record)?,
        )?;
        tx.next_seq = Some(seq + 1);
        self.store.commit(tx)?;
        Ok(message_from_record(record))
    }

    fn list_messages(
        &mut self,
        conversation_id: ConversationId,
        before: Option<u64>,
        limit: u32,
    ) -> Result<Vec<PlaintextMessage>> {
        self.require_device()?;
        let limit = limit.min(MAX_LIST_LIMIT) as usize;
        if limit == 0 {
            return Ok(Vec::new());
        }
        let tag = self.store.idx("conv", &conversation_id);
        let before_rid = before.map(|b| b.to_be_bytes());
        let rows =
            self.store
                .page_desc(T_MESSAGE, &tag, before_rid.as_ref().map(|b| &b[..]), limit)?;
        let mut out = Vec::with_capacity(rows.len());
        for bytes in rows {
            let record: MessageRecord = from_json(&bytes)?;
            if record.conversation_id != conversation_id {
                return Err(Error::StoreCorrupt);
            }
            out.push(message_from_record(record));
        }
        Ok(out)
    }
}

fn order_legs(recipients: &[Recipient], legs: &[StoredLeg]) -> Vec<Leg> {
    recipients
        .iter()
        .filter_map(|r| {
            legs.iter()
                .find(|l| l.device_id == r.device.device_id)
                .map(|l| Leg {
                    recipient_device_id: l.device_id,
                    olm_type: l.olm_type,
                    ciphertext: l.ciphertext.clone(),
                })
        })
        .collect()
}

fn message_from_record(record: MessageRecord) -> PlaintextMessage {
    PlaintextMessage {
        seq: record.seq,
        direction: record.direction,
        message_id: record.message_id,
        conversation_id: record.conversation_id,
        client_message_id: record.client_message_id,
        sender_user_id: record.sender_user_id,
        sender_device_id: record.sender_device_id,
        kind: record.kind,
        body: record.body,
        sender_trust: record.sender_trust,
        stored_at_ms: record.stored_at_ms,
    }
}
